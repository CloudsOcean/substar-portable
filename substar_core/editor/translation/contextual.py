from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from substar_core.artifacts import atomic_write_json
from substar_core.ai_block_cache import fingerprint, load_ai_block_cache, save_ai_block_cache
from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    DisplayCue,
    EntityState,
    TranslationTrack,
    stable_id,
)
from substar_core.editor.translation.grouping import translation_groups
from substar_core.glossary import active_glossary, glossary_prompt
from substar_core.prompt_registry import (
    opposite_language,
    render_prompt,
    translation_variant,
)
from substar_core.policy import SubtitlePolicy
from substar_core.semantic_execution import validate_presentation_plan
from substar_core.model_gateway import ModelGatewayRequestError, call_translation_model
from substar_core.model_routing import resolve_stage_request
from substar_core.storage import ProjectStore
from substar_core.task_info import load_task_info


def clean_groups(document: Any, settings: dict[str, Any]) -> list[dict[str, Any]]:
    groups, _, _ = translation_groups(document, settings)
    cleaned: list[dict[str, Any]] = []
    for group in groups:
        cleaned.append({
            "group_id": group["group_id"],
            "semantic_group_ids": list(group.get("semantic_group_ids", [])),
            "cues": [
                {
                    "cue_id": cue["cue_id"],
                    "source_text": cue["source_text"],
                    "start": cue.get("start"),
                    "end": cue.get("end"),
                    "hard_limit": cue.get("hard_limit"),
                    "count_rule": cue.get("count_rule"),
                }
                for cue in group["cues"]
            ],
        })
    mapping_mode = str(settings.get("translation_mapping_mode") or "many_to_many")
    if mapping_mode == "many_to_many":
        return cleaned
    if mapping_mode != "one_to_one":
        raise RuntimeError(f"不支持的翻译分配模式：{mapping_mode}")
    return [
        {
            "group_id": f"line:{cue['cue_id']}",
            "semantic_group_ids": list(group.get("semantic_group_ids", [])),
            "cues": [dict(cue)],
        }
        for group in cleaned
        for cue in group["cues"]
    ]


def execution_block_batches(document: Any, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    editor_groups = {group.group_id: group for group in document.groups}
    cue_blocks = {
        cue.cue_id: list(editor_groups.get(cue.group_id).execution_block_ids)
        if cue.group_id in editor_groups else []
        for cue in document.cues
        if cue.state is EntityState.ACTIVE
    }
    ordered_ids = list(dict.fromkeys(
        block_id
        for group in document.groups
        for block_id in group.execution_block_ids
    ))
    batches: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        candidates = [
            block_id for cue in group["cues"]
            for block_id in cue_blocks.get(str(cue["cue_id"]), [])
        ]
        block_id = next((item for item in ordered_ids if item in candidates), None)
        block_id = block_id or (candidates[0] if candidates else "manual")
        batches.setdefault(block_id, []).append(group)
    rank = {block_id: index for index, block_id in enumerate(ordered_ids)}
    return [
        {"block_id": block_id, "groups": batches[block_id]}
        for block_id in sorted(batches, key=lambda value: (rank.get(value, len(rank)), value))
    ]


def api_call(*, settings: dict[str, Any], system_prompt: str,
             groups: list[dict[str, Any]], stage_name: str = "translation",
             cache_directory: Path | None = None,
             cache_scope: str = "", mapping_mode: str = "many_to_many",
             cache_validator: Callable[[dict[str, Any]], bool] | None = None,
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    route = resolve_stage_request(settings, stage_name)
    prompt_groups = [
        {key: value for key, value in group.items() if not str(key).startswith("_")}
        for group in groups
    ]
    cache_key = fingerprint({
        "scope": cache_scope,
        "stage": stage_name,
        "model": str(route["model"]),
        "system_prompt": system_prompt,
        "mapping_mode": mapping_mode,
        "groups": prompt_groups,
        "schema": "translation-result.v3",
    })
    cached = load_ai_block_cache(cache_directory, cache_key) if cache_directory else None
    if cached is not None and (cache_validator is None or cache_validator(cached)):
        return cached, {"cache_hit": True, "cache_key": cache_key}
    response, telemetry = call_translation_model(
        base_url=str(route["base_url"]),
        api_key=str(route["api_key"]),
        auth_mode=str(route["auth_mode"]),
        model=str(route["model"]),
        system_prompt=system_prompt,
        groups=prompt_groups,
        mapping_mode=mapping_mode,
        timeout=int(settings.get("translation_api_timeout_seconds", 300)),
        thinking_mode=str(route["thinking_mode"]),
        reasoning_effort=str(route["reasoning_effort"]),
        request_attempts=int(settings.get("http_retry_attempts", 2)),
        max_tokens=int(route["max_tokens"]),
        temperature=float(route["temperature"]),
    )
    if cache_directory is not None and (
        cache_validator is None or cache_validator(response)
    ):
        save_ai_block_cache(cache_directory, cache_key, response)
    return response, {**telemetry, "cache_hit": False, "cache_key": cache_key}


def call_block_batches(*, settings: dict[str, Any], system_prompt: str,
                       batches: list[dict[str, Any]],
                       cache_directory: Path | None = None,
                       cache_scope: str = "",
                       mapping_mode: str = "many_to_many",
                       progress_callback: Callable[[int, int], None] | None = None,
                       ) -> tuple[dict[str, Any], dict[str, Any]]:
    if not batches:
        return {"group_results": []}, {"execution_blocks": []}
    results: dict[str, dict[str, Any]] = {}

    def run(batch: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        block_id = str(batch["block_id"])
        batch_groups = list(batch["groups"])

        def valid_cache(value: dict[str, Any]) -> bool:
            return _response_contract_valid(value, batch_groups, mapping_mode)

        try:
            response, telemetry = api_call(
                settings=settings, system_prompt=system_prompt, groups=batch_groups,
                cache_directory=cache_directory, cache_scope=cache_scope,
                mapping_mode=mapping_mode, cache_validator=valid_cache,
            )
            return block_id, {"response": response, "telemetry": telemetry}
        except ModelGatewayRequestError as exc:
            if not exc.retryable:
                raise
            return block_id, {
                "response": {}, "error": str(exc), "non_repairable": True,
            }
        except Exception as exc:
            return block_id, {"response": {}, "error": str(exc)}

    workers = min(len(batches), max(1, int(settings.get("translation_workers", 8))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, batch) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            block_id, value = future.result()
            results[block_id] = value
            if progress_callback is not None:
                finished_group_count = sum(
                    len(batch["groups"])
                    for batch in batches
                    if str(batch["block_id"]) in results
                )
                progress_callback(finished_group_count, sum(len(row["groups"]) for row in batches))
    rows = [
        row for batch in batches
        for row in results[str(batch["block_id"])]["response"].get("group_results", [])
        if isinstance(row, dict)
    ]
    return {"group_results": rows}, {
        "execution_blocks": [
            {"block_id": batch["block_id"], **results[str(batch["block_id"])]}
            for batch in batches
        ]
    }


def _result_rows(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = response.get("group_results")
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            group_id = str(row.get("group_id") or "").strip()
            if group_id and group_id not in result:
                result[group_id] = row
    return result


def _presentation_plan(
    group: dict[str, Any], row: dict[str, Any] | None,
    mapping_mode: str = "many_to_many",
) -> dict[str, Any] | None:
    if mapping_mode == "one_to_one":
        if not isinstance(row, dict) or len(group.get("cues", [])) != 1:
            return None
        cue_id = str(group["cues"][0]["cue_id"])
        if set(row) != {"group_id", "cue_id", "target_text"}:
            return None
        target_text = str(row.get("target_text") or "")
        if str(row.get("cue_id") or "") != cue_id or not target_text.strip():
            return None
        return {
            "group_id": str(group["group_id"]),
            "source_cue_ids": [cue_id],
            "meaning_units": [{
                "meaning_unit_id": "unit_1",
                "target_text": target_text,
                "source_evidence_cue_ids": [cue_id],
            }],
            "cue_assignments": [{
                "cue_id": cue_id, "meaning_unit_id": "unit_1",
            }],
        }
    return validate_presentation_plan(group, row)


def _translation_validation_issues(
    group: dict[str, Any], row: dict[str, Any] | None,
    mapping_mode: str = "many_to_many",
) -> list[dict[str, Any]]:
    group_id = str(group["group_id"])
    if row is None:
        return [{
            "code": "missing_group_result",
            "group_id": group_id,
            "detail": "响应遗漏了该翻译组。",
        }]
    if mapping_mode == "one_to_one":
        expected_cue_id = str(group["cues"][0]["cue_id"])
        issues: list[dict[str, Any]] = []
        if set(row) != {"group_id", "cue_id", "target_text"}:
            issues.append({
                "code": "one_to_one_schema_mismatch", "group_id": group_id,
                "detail": "逐条模式只能返回 group_id、cue_id 和 target_text。",
            })
        if str(row.get("cue_id") or "") != expected_cue_id:
            issues.append({
                "code": "foreign_cue_reference", "group_id": group_id,
                "expected_cue_id": expected_cue_id,
                "actual_cue_id": str(row.get("cue_id") or ""),
                "detail": "逐条译文绑定了其他 Cue。",
            })
        if not str(row.get("target_text") or "").strip():
            issues.append({
                "code": "empty_target", "group_id": group_id,
                "detail": "逐条译文为空。",
            })
        return issues or [{
            "code": "invalid_one_to_one_result", "group_id": group_id,
            "detail": "逐条翻译结果未通过验收。",
        }]

    expected = [str(cue["cue_id"]) for cue in group.get("cues", [])]
    expected_set = set(expected)
    units = row.get("meaning_units")
    assignments = row.get("cue_assignments")
    issues = []
    if not isinstance(units, list) or not units:
        issues.append({"code": "missing_meaning_units", "group_id": group_id})
        units = []
    if not isinstance(assignments, list):
        issues.append({"code": "missing_cue_assignments", "group_id": group_id})
        assignments = []
    unit_ids = [
        str(item.get("meaning_unit_id") or "")
        for item in units if isinstance(item, dict)
    ]
    if any(not str(item.get("target_text") or "").strip() for item in units if isinstance(item, dict)):
        issues.append({"code": "empty_target", "group_id": group_id})
    evidence = [
        str(cue_id)
        for item in units if isinstance(item, dict)
        for cue_id in (
            item.get("source_evidence_cue_ids", [])
            if isinstance(item.get("source_evidence_cue_ids"), list) else []
        )
    ]
    if any(cue_id not in expected_set for cue_id in evidence):
        issues.append({"code": "evidence_outside_group", "group_id": group_id})
    if set(evidence) != expected_set:
        issues.append({"code": "incomplete_source_evidence", "group_id": group_id})
    assigned_cues = [
        str(item.get("cue_id") or "")
        for item in assignments if isinstance(item, dict)
    ]
    if assigned_cues != expected:
        issues.append({
            "code": "cue_assignment_mismatch", "group_id": group_id,
            "expected_cue_ids": expected, "actual_cue_ids": assigned_cues,
        })
    assigned_units = [
        str(item.get("meaning_unit_id") or "")
        for item in assignments if isinstance(item, dict)
    ]
    if any(unit_id not in unit_ids for unit_id in assigned_units):
        issues.append({"code": "unknown_meaning_unit", "group_id": group_id})
    if set(assigned_units) != set(unit_ids):
        issues.append({"code": "unused_meaning_unit", "group_id": group_id})
    return issues or [{
        "code": "invalid_presentation_plan", "group_id": group_id,
        "detail": "意义单元或 Cue 分配未通过验收。",
    }]


def _candidate_targets(
    group: dict[str, Any], row: dict[str, Any] | None,
) -> dict[str, str]:
    """Preserve non-empty model text without treating it as accepted output."""
    if not isinstance(row, dict):
        return {}
    expected = {str(cue["cue_id"]) for cue in group.get("cues", [])}
    direct = str(row.get("target_text") or "").strip()
    cue_id = str(row.get("cue_id") or "")
    if direct and cue_id in expected:
        return {cue_id: direct}
    # A one-Cue group has an unambiguous ownership boundary.  A wrong/missing
    # cue_id still makes the model row invalid, but it must not erase useful
    # target-language text: retain it as a manual-review candidate for the
    # group's sole Cue instead of treating it as accepted output.
    if direct and len(expected) == 1:
        return {next(iter(expected)): direct}
    units = {
        str(item.get("meaning_unit_id") or ""): str(item.get("target_text") or "").strip()
        for item in row.get("meaning_units", [])
        if isinstance(item, dict) and str(item.get("target_text") or "").strip()
    }
    if len(expected) == 1 and len(units) == 1:
        return {next(iter(expected)): next(iter(units.values()))}
    result: dict[str, str] = {}
    for item in row.get("cue_assignments", []):
        if not isinstance(item, dict):
            continue
        assigned_cue = str(item.get("cue_id") or "")
        target = units.get(str(item.get("meaning_unit_id") or ""), "")
        if assigned_cue in expected and target:
            result.setdefault(assigned_cue, target)
    return result


def _reject_provider_wide_translation_failure(
    batches: list[dict[str, Any]], execution_by_block: Mapping[str, Mapping[str, Any]],
) -> None:
    """Do not misreport a total transport outage as a delivered translation."""
    if not batches:
        return
    rows = [execution_by_block.get(str(batch["block_id"]), {}) for batch in batches]
    if not all(row.get("error") and row.get("non_repairable") for row in rows):
        return
    errors = list(dict.fromkeys(
        str(row.get("error") or "").strip() for row in rows
        if str(row.get("error") or "").strip()
    ))
    detail = errors[0] if errors else "模型服务未返回可用结果"
    raise RuntimeError(f"字幕翻译所有执行块均请求失败：{detail}")


def _result_row_occurrences(
    response: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    rows = response.get("group_results")
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        group_id = str(row.get("group_id") or "").strip()
        if group_id:
            result.setdefault(group_id, []).append(row)
    return result


def _response_contract_valid(
    response: Mapping[str, Any], groups: list[dict[str, Any]], mapping_mode: str,
) -> bool:
    if set(response) != {"group_results"}:
        return False
    occurrences = _result_row_occurrences(response)
    expected = {str(group["group_id"]) for group in groups}
    if set(occurrences) != expected or any(len(rows) != 1 for rows in occurrences.values()):
        return False
    return all(
        _presentation_plan(group, occurrences[str(group["group_id"])][0], mapping_mode)
        is not None
        for group in groups
    )


def _repair_group(*, settings: dict[str, Any], repair_prompt: str,
                  group: dict[str, Any], cache_directory: Path | None = None,
                  cache_scope: str = "", mapping_mode: str = "many_to_many",
                  ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    try:
        response, telemetry = api_call(
            settings=settings, system_prompt=repair_prompt,
            groups=[group], stage_name="translation_repair",
            cache_directory=cache_directory, cache_scope=cache_scope,
            mapping_mode=mapping_mode,
            cache_validator=lambda value: _presentation_plan(
                group, _result_rows(value).get(str(group["group_id"])), mapping_mode
            ) is not None,
        )
        plan = _presentation_plan(
            group, _result_rows(response).get(group["group_id"]), mapping_mode
        )
        records.append({"attempt": 1, "response": response, "telemetry": telemetry, "valid": bool(plan)})
        return plan, records
    except Exception as exc:
        records.append({"attempt": 1, "error": str(exc), "valid": False})
        return None, records


def complete_results(*, settings: dict[str, Any], repair_prompt: str,
                      groups: list[dict[str, Any]], response: dict[str, Any],
                      mapping_mode: str = "many_to_many",
                      non_repairable_group_ids: set[str] | None = None,
                      progress_callback: Callable[[int, int, int], None] | None = None,
                     cache_directory: Path | None = None,
                     cache_scope: str = "",
                     group_block_ids: dict[str, str] | None = None,
                     failure_injector: Callable[[str, int], None] | None = None,
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    occurrences = _result_row_occurrences(response)
    rows = {group_id: values[0] for group_id, values in occurrences.items() if values}
    plans: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    initial_issues: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        group_id = str(group["group_id"])
        group_rows = occurrences.get(group_id, [])
        if len(group_rows) > 1:
            initial_issues[group_id] = [{
                "code": "duplicate_group_result", "group_id": group_id,
                "count": len(group_rows),
                "detail": "响应重复返回了同一翻译组。",
            }]
            plan = None
        else:
            plan = _presentation_plan(group, rows.get(group_id), mapping_mode)
            if plan is None:
                initial_issues[group_id] = _translation_validation_issues(
                    group, rows.get(group_id), mapping_mode
                )
        if plan is None:
            invalid.append(group)
        else:
            plans.append(plan)
    non_repairable = set(non_repairable_group_ids or set())
    repairable_invalid = [
        group for group in invalid if str(group["group_id"]) not in non_repairable
    ]
    repair_record: dict[str, Any] = {
        "attempted_group_ids": [str(group["group_id"]) for group in repairable_invalid],
        "groups": [],
    }
    repair_enabled = True
    repair_record["repair_phase_entered"] = bool(repairable_invalid)
    if progress_callback is not None and repairable_invalid:
        progress_callback(0, len(repairable_invalid), 0)
    if repairable_invalid and repair_enabled:
        repair_results: dict[str, tuple[dict[str, Any] | None, list[dict[str, Any]]]] = {}

        if group_block_ids:
            repair_blocks: dict[str, list[dict[str, Any]]] = {}
            for group in repairable_invalid:
                group_id = str(group["group_id"])
                repair_blocks.setdefault(group_block_ids.get(group_id, group_id), []).append(group)

            def repair_block(block: tuple[str, list[dict[str, Any]]]) -> dict[str, tuple[dict[str, Any] | None, list[dict[str, Any]]]]:
                block_id, block_groups = block
                payloads = []
                issues_by_group: dict[str, list[dict[str, Any]]] = {}
                for group in block_groups:
                    group_id = str(group["group_id"])
                    rejected_output = rows.get(group_id)
                    issues = initial_issues.get(group_id) or _translation_validation_issues(
                        group, rejected_output, mapping_mode
                    )
                    issues_by_group[group_id] = issues
                    payloads.append({
                        **group,
                        "rejected_output": rejected_output,
                        "program_validation_errors": issues,
                        "frozen_accepted_output": {},
                        "repair_attempt": 1,
                    })
                response_value, telemetry = api_call(
                    settings=settings, system_prompt=repair_prompt,
                    groups=payloads, stage_name="translation_repair",
                    cache_directory=cache_directory, cache_scope=cache_scope,
                    mapping_mode=mapping_mode,
                    cache_validator=lambda value: _response_contract_valid(
                        value, block_groups, mapping_mode
                    ),
                )
                repaired_rows = _result_rows(response_value)
                repaired_occurrences = _result_row_occurrences(response_value)
                return {
                    str(group["group_id"]): (
                        _presentation_plan(
                            group,
                            repaired_rows.get(str(group["group_id"]))
                            if len(repaired_occurrences.get(str(group["group_id"]), [])) == 1
                            else None,
                            mapping_mode,
                        ),
                        [{
                            "attempt": 1, "block_id": block_id,
                            "response": response_value, "telemetry": telemetry,
                            "validation_errors": issues_by_group[str(group["group_id"])],
                            "rejected_output": rows.get(str(group["group_id"])),
                            "valid": bool(_presentation_plan(
                                group,
                                repaired_rows.get(str(group["group_id"]))
                                if len(repaired_occurrences.get(str(group["group_id"]), [])) == 1
                                else None,
                                mapping_mode,
                            )),
                        }],
                    )
                    for group in block_groups
                }

            workers = min(len(repair_blocks), max(1, int(settings.get("translation_workers", 8))))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(repair_block, item) for item in repair_blocks.items()]
                for future in concurrent.futures.as_completed(futures):
                    repair_results.update(future.result())
                    if progress_callback is not None:
                        progress_callback(
                            len(repair_results), len(repairable_invalid),
                            sum(bool(row[0]) for row in repair_results.values()),
                        )

        def repair(group: dict[str, Any]) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
            if failure_injector is not None:
                failure_injector(str(group["group_id"]), 1)
            rejected_output = rows.get(str(group["group_id"]))
            validation_issues = _translation_validation_issues(
                group, rejected_output, mapping_mode
            ) if str(group["group_id"]) not in initial_issues else initial_issues[str(group["group_id"])]
            repair_group = {
                **group,
                "rejected_output": rejected_output,
                "program_validation_errors": validation_issues,
                "frozen_accepted_output": {},
                "repair_attempt": 1,
            }
            repair_kwargs: dict[str, Any] = {
                "settings": settings,
                "repair_prompt": repair_prompt,
                "group": repair_group,
                "mapping_mode": mapping_mode,
            }
            if cache_directory is not None:
                repair_kwargs.update(
                    cache_directory=cache_directory, cache_scope=cache_scope
                )
            plan, records = _repair_group(**repair_kwargs)
            for record in records:
                record["validation_errors"] = validation_issues
                record["rejected_output"] = rejected_output
            return str(group["group_id"]), plan, records

        if not group_block_ids:
            workers = min(
                len(repairable_invalid), max(1, int(settings.get("translation_workers", 8)))
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(repair, group) for group in repairable_invalid]
                for future in concurrent.futures.as_completed(futures):
                    group_id, plan, records = future.result()
                    repair_results[group_id] = (plan, records)
                    if progress_callback is not None:
                        progress_callback(
                            len(repair_results), len(repairable_invalid),
                            sum(bool(row[0]) for row in repair_results.values()),
                        )

        remaining: list[dict[str, Any]] = [
            group for group in invalid if str(group["group_id"]) in non_repairable
        ]
        for group in repairable_invalid:
            plan, records = repair_results[str(group["group_id"])]
            repair_record["groups"].append({
                "group_id": str(group["group_id"]),
                "attempts": records,
                "accepted": bool(plan),
                "primary_request_count": 1,
                "repair_attempted": True,
                "repair_request_count": 1,
            })
            if plan is None:
                remaining.append(group)
            else:
                plans.append(plan)
        invalid = remaining
    accepted_group_ids = {str(plan["group_id"]) for plan in plans}
    candidate_targets_by_cue: dict[str, str] = {}
    for group in groups:
        group_id = str(group["group_id"])
        if group_id in accepted_group_ids:
            continue
        repair_entry = next(
            (
                item for item in repair_record["groups"]
                if str(item.get("group_id")) == group_id
            ),
            None,
        )
        repaired_row = None
        if repair_entry and repair_entry.get("attempts"):
            repaired_response = repair_entry["attempts"][-1].get("response", {})
            repaired_row = _result_rows(repaired_response).get(group_id)
        for cue_id, target in {
            **_candidate_targets(group, rows.get(group_id)),
            **_candidate_targets(group, repaired_row),
        }.items():
            candidate_targets_by_cue.setdefault(cue_id, target)
    return plans, {
        "model_repair": repair_record,
        "invalid_group_ids": [str(group["group_id"]) for group in invalid],
        "candidate_targets_by_cue": candidate_targets_by_cue,
    }


def warning_report(translations: dict[str, str], settings: dict[str, Any]) -> list[dict[str, Any]]:
    warnings = []
    policy = SubtitlePolicy.from_settings(settings)
    for cue_id, value in translations.items():
        count = policy.line_length(value)
        limit = policy.hard_limit(value)
        if count > limit:
            warnings.append({"code": "target_character_limit_warning", "cue_id": cue_id, "count": count, "limit": limit})
    return warnings


def materialize_presentation(
    document: Any, plans: list[dict[str, Any]], target_language: str,
    candidate_targets_by_cue: Mapping[str, str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Register the model-authored display grid without changing its content."""

    cue_by_id = {
        cue.cue_id: cue
        for cue in document.cues
        if cue.state is EntityState.ACTIVE
    }
    covered: set[str] = set()
    presentation: list[DisplayCue] = []
    report_rows: list[dict[str, Any]] = []
    provenance = ChangeProvenance(
        kind=ChangeKind.AI,
        operation="contextual_translation_model_presentation",
        actor="substar-production",
        metadata={"target_language": target_language},
    )
    for plan in plans:
        unit_by_id = {
            row["meaning_unit_id"]: row
            for row in plan["meaning_units"]
        }
        for local_index, row in enumerate(plan["cue_assignments"], start=1):
            source_cue = cue_by_id[row["cue_id"]]
            unit = unit_by_id[row["meaning_unit_id"]]
            target_text = unit["target_text"]
            target_provenance = replace(
                provenance,
                metadata={
                    **dict(provenance.metadata),
                    "meaning_unit_id": row["meaning_unit_id"],
                    "source_evidence_cue_ids": unit["source_evidence_cue_ids"],
                },
            )
            mapping = {
                "schema_version": "substar.presentation-mapping.v1",
                "mapping_type": "1:1",
                "group_mapping_type": "model-authored-meaning-units",
                "source_cue_ids": [source_cue.cue_id],
                "source_evidence_cue_ids": unit["source_evidence_cue_ids"],
                "meaning_unit_id": row["meaning_unit_id"],
            }
            presentation.append(DisplayCue(
                cue_id=stable_id("cue", {
                    "translation_group": plan["group_id"],
                    "position": local_index,
                    "source_cue_id": source_cue.cue_id,
                    "meaning_unit_id": row["meaning_unit_id"],
                    "target_text": target_text,
                }),
                index=len(presentation),
                display_token_ids=source_cue.display_token_ids,
                start=source_cue.start,
                end=source_cue.end,
                target=TranslationTrack(
                    target_text=target_text,
                    original_text=target_text,
                    language=target_language,
                    provenance=target_provenance,
                ),
                speaker=source_cue.speaker,
                state=EntityState.ACTIVE,
                group_id=source_cue.group_id,
                mapping=mapping,
            ))
            covered.add(source_cue.cue_id)
            report_rows.append({
                **mapping,
                "cue_id": presentation[-1].cue_id,
                "start": source_cue.start,
                "end": source_cue.end,
                "target_text": target_text,
            })
    expected = set(cue_by_id)
    unresolved = expected - covered
    candidate_targets = {
        str(cue_id): str(text).strip()
        for cue_id, text in dict(candidate_targets_by_cue or {}).items()
        if str(text).strip()
    }
    unresolved_source_cue_ids: list[str] = []
    unresolved_provenance = ChangeProvenance(
        kind=ChangeKind.AI,
        operation="contextual_translation_unresolved",
        actor="substar-production",
        metadata={
            "target_language": target_language,
            "translation_unresolved": True,
            "requires_manual_translation": True,
        },
    )
    for source_cue in document.cues:
        if source_cue.state is not EntityState.ACTIVE or source_cue.cue_id not in unresolved:
            continue
        source_mapping = source_cue.mapping if isinstance(source_cue.mapping, Mapping) else {}
        previous_target = (
            source_cue.target.target_text.strip()
            if source_cue.target is not None
            and source_cue.target.target_text.strip()
            and source_mapping.get("translation_unresolved") is not True
            else ""
        )
        candidate_target = candidate_targets.get(source_cue.cue_id, "")
        target_text = previous_target or candidate_target
        needs_review = not bool(previous_target)
        if needs_review:
            unresolved_source_cue_ids.append(source_cue.cue_id)
        unresolved_mapping = {
            "schema_version": "substar.presentation-mapping.v1",
            "mapping_type": "1:1",
            "group_mapping_type": (
                "reused-previous-translation" if previous_target else
                "manual-required-candidate" if candidate_target else
                "manual-required-empty-target"
            ),
            "source_cue_ids": [source_cue.cue_id],
            "translation_unresolved": needs_review,
            "requires_manual_translation": needs_review,
            "translation_status": "manual_required" if needs_review else "translated",
            "issue_code": "translation_unresolved" if needs_review else None,
            "editable": True,
            "candidate_preserved": bool(candidate_target and not previous_target),
        }
        presentation.append(replace(
            source_cue,
            index=len(presentation),
            target=TranslationTrack(
                target_text=target_text,
                original_text=target_text,
                language=target_language,
                provenance=unresolved_provenance,
                translation_status="manual_required" if needs_review else "translated",
                issue_code="translation_unresolved" if needs_review else None,
                editable=True,
            ),
            mapping=unresolved_mapping,
        ))
        report_rows.append({
            **unresolved_mapping,
            "cue_id": source_cue.cue_id,
            "start": source_cue.start,
            "end": source_cue.end,
            "target_text": target_text,
            "translation_status": "manual_required" if needs_review else "translated",
            "issue_code": "translation_unresolved" if needs_review else None,
            "editable": True,
        })
    presentation.sort(key=lambda cue: (cue.start, cue.end, cue.index, cue.cue_id))
    presentation = [replace(cue, index=index) for index, cue in enumerate(presentation)]
    return replace(document, cues=tuple(presentation)), {
        "schema_version": "substar.presentation-plan.v1",
        "groups": [
            {
                "group_id": plan["group_id"],
                "meaning_units": [dict(row) for row in plan["meaning_units"]],
                "meaning_unit_sequence": [
                    row["meaning_unit_id"]
                    for row in plan["cue_assignments"]
                ],
            }
            for plan in plans
        ],
        "cues": report_rows,
        "unresolved_source_cue_ids": sorted(unresolved_source_cue_ids),
        "preserved_candidate_cue_ids": sorted(candidate_targets),
    }


def _save_translation(*, work: Path, plans: list[dict[str, Any]], settings: dict[str, Any],
                      metadata: dict[str, Any], target_language: str,
                      expected_revision_id: str,
                      candidate_targets_by_cue: Mapping[str, str] | None = None,
                      ) -> str:
    store = ProjectStore.open(work / "project")
    revision = store.load_latest()
    if revision is None:
        raise RuntimeError("项目缺少可翻译版本")
    if revision.revision_id != expected_revision_id:
        raise RuntimeError("翻译期间编辑版本已变化；结果未写入")
    candidate, presentation_report = materialize_presentation(
        revision.document, plans, target_language, candidate_targets_by_cue
    )
    translations = {
        cue.cue_id: cue.target.target_text for cue in candidate.cues if cue.target is not None
    }
    warnings = warning_report(translations, settings)
    report = {"validation": {"warnings": warnings}, "presentation": presentation_report}
    report["contextual_translation"] = metadata
    problem_reasons: dict[str, list[str]] = {
        str(item["cue_id"]): ["translation_over_limit"] for item in warnings
    }
    for cue_id in presentation_report.get("unresolved_source_cue_ids", []):
        problem_reasons.setdefault(str(cue_id), []).append("translation_unresolved")
    provenance = ChangeProvenance(
        kind=ChangeKind.AI,
        operation="contextual_translation",
        actor="substar-production",
        metadata={
            **metadata,
            "translation_problem_cue_ids": sorted(problem_reasons),
            "translation_problem_reasons": problem_reasons,
        },
    )
    candidate = replace(candidate, changes=(*candidate.changes, provenance))
    saved = store.save(candidate, provenance=provenance, expected_revision_id=revision.revision_id)
    atomic_write_json(work / "translation_report.json", report)
    return saved.revision_id


def run_contextual_translation(
    work: Path,
    settings: dict[str, Any],
    *,
    artifact_dir: Path | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> dict[str, Any]:
    audit_dir = artifact_dir or work
    audit_dir.mkdir(parents=True, exist_ok=True)
    revision = ProjectStore.open(work / "project").load_latest()
    if revision is None:
        raise RuntimeError("项目缺少可翻译版本")
    groups = clean_groups(revision.document, settings)
    source_language = str(settings.get("translation_source_language") or "")
    if source_language not in {"mixed", "zh-CN", "en", "ja", "ko"}:
        raise RuntimeError("翻译任务缺少已确认的原始语言")
    configured_target = str(settings.get("target_language_mode") or "auto_opposite")
    target_language = (
        opposite_language(source_language)
        if configured_target == "auto_opposite" else configured_target
    )
    if source_language == target_language:
        raise RuntimeError(
            f"目标语言与已确认的原始语言相同（{target_language}），请选择其他目标语言"
        )
    variant = translation_variant(source_language, target_language)
    mapping_mode = str(settings.get("translation_mapping_mode") or "many_to_many")
    prompt = render_prompt(
        "contextual_translation", variant=variant, mode=mapping_mode
    )
    repair_prompt = render_prompt(
        "contextual_translation_repair", variant=variant, mode=mapping_mode
    )
    language_names = {"zh-CN": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文"}
    direction = (
        "# ACTIVE_LANGUAGE_DIRECTION\n"
        f"源语言：{language_names.get(source_language, source_language)}\n"
        f"目标语言：{language_names.get(target_language, target_language)}\n"
        "所有 target_text 必须使用目标语言。\n"
        f"目标语言 hard_limit：{int(settings.get({'en': 'english_hard_limit', 'zh-CN': 'chinese_hard_limit', 'ja': 'japanese_hard_limit', 'ko': 'korean_hard_limit'}.get(target_language, 'english_hard_limit'), 55))}；"
        f"当前 mapping_mode：{mapping_mode}。必须严格遵守对应模式契约。"
    )
    if mapping_mode == "many_to_many":
        direction += "每个 meaning_units.target_text 必须是完整、唯一的目标语意义单元。"
    else:
        direction += "每个 group_results.target_text 必须是该单一 Cue 的完整非空译文。"
    task_info = load_task_info(work, work.name)
    glossary = glossary_prompt(active_glossary(str(task_info.get("glossary_id") or "")))
    if source_language == target_language:
        direction += " 本任务是同语种字幕校订：保留原意与语言，只做自然表达和 Cue 分配。"
    batches = execution_block_batches(revision.document, groups)
    if progress_callback is not None:
        progress_callback("executing", completed=0, planned=len(groups))
    response, telemetry = call_block_batches(
        settings=settings,
        system_prompt=f"{prompt.text}\n\n{direction}\n\n{glossary}",
        batches=batches,
        cache_directory=work / "translation" / "block_cache",
        cache_scope="translation-contract-v3",
        mapping_mode=mapping_mode,
        progress_callback=(
            (lambda done, total: progress_callback(
                "executing", completed=done, planned=total
            )) if progress_callback is not None else None
        ),
    )
    execution_by_block = {
        str(row.get("block_id")): row
        for row in telemetry.get("execution_blocks", [])
        if isinstance(row, dict)
    }
    _reject_provider_wide_translation_failure(batches, execution_by_block)
    non_repairable_group_ids = {
        str(group["group_id"])
        for batch in batches
        if execution_by_block.get(str(batch["block_id"]), {}).get("non_repairable")
        for group in batch["groups"]
    }
    plans, repair = complete_results(
        settings=settings,
        repair_prompt=f"{repair_prompt.text}\n\n{direction}\n\n{glossary}",
        groups=groups,
        response=response,
        mapping_mode=mapping_mode,
        non_repairable_group_ids=non_repairable_group_ids,
        cache_directory=work / "translation" / "block_cache",
        cache_scope="translation-contract-v3",
        group_block_ids={
            str(group["group_id"]): str(batch["block_id"])
            for batch in batches for group in batch["groups"]
        },
        progress_callback=(
            (lambda done, total, accepted: progress_callback(
                "repair", completed=done, planned=len(groups),
                repair_planned=total, repair_accepted=accepted,
            )) if progress_callback is not None else None
        ),
    )
    if progress_callback is not None:
        progress_callback(
            "validating", completed=len(groups), planned=len(groups),
            repair_planned=len(repair["model_repair"]["attempted_group_ids"]),
            repair_accepted=(
                len(repair["model_repair"]["attempted_group_ids"])
                - len(repair["invalid_group_ids"])
            ),
        )
    atomic_write_json(audit_dir / "contextual_translation_response.json", response)
    atomic_write_json(audit_dir / "contextual_translation_telemetry.json", telemetry)
    atomic_write_json(audit_dir / "contextual_translation_repair.json", repair)
    metadata = {
        "source_language": source_language,
        "target_language": target_language,
        "mapping_mode": mapping_mode,
        "prompt": prompt.metadata(),
        "repair_prompt": repair_prompt.metadata(),
        "execution_block_ids": [item["block_id"] for item in batches],
        "problem_group_ids": list(repair["invalid_group_ids"]),
    }
    if progress_callback is not None:
        progress_callback(
            "materializing", completed=len(groups), planned=len(groups),
            repair_planned=len(repair["model_repair"]["attempted_group_ids"]),
            repair_accepted=(
                len(repair["model_repair"]["attempted_group_ids"])
                - len(repair["invalid_group_ids"])
            ),
        )
    revision_id = _save_translation(
        work=work, plans=plans, settings=settings,
        metadata=metadata, target_language=target_language,
        expected_revision_id=revision.revision_id,
        candidate_targets_by_cue=repair.get("candidate_targets_by_cue", {}),
    )
    return {"revision_id": revision_id, "repair": repair, **metadata}
