from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from pathlib import Path
import time
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
from substar_core.cue_script import (
    compile_translation_units,
    finalize_translation,
    output_contract,
    render_translation_request,
)
from substar_core.model_gateway import (
    ModelGatewayRequestError,
    call_text_model,
)
from substar_core.model_routing import resolve_stage_request
from substar_core.storage import ProjectStore
from substar_core.task_info import load_task_info


def call_translation_model(*, groups: list[dict[str, Any]] | None = None,
                           mapping_mode: str | None = None, **kwargs: Any):
    """Compatibility injection seam; production transport is raw text."""
    return call_text_model(**kwargs)


def clean_groups(document: Any, settings: dict[str, Any]) -> list[dict[str, Any]]:
    groups, _, _ = translation_groups(document, settings)
    cleaned: list[dict[str, Any]] = []
    for group in groups:
        cleaned.append({
            "group_id": group["group_id"],
            "execution_block_id": str(group.get("execution_block_id") or "manual"),
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
            "execution_block_id": str(group.get("execution_block_id") or "manual"),
            "cues": [dict(cue)],
        }
        for group in cleaned
        for cue in group["cues"]
    ]


def execution_block_batches(
    document: Any, groups: list[dict[str, Any]], *, mapping_mode: str = "many_to_many",
) -> list[dict[str, Any]]:
    if mapping_mode not in {"many_to_many", "one_to_one"}:
        raise RuntimeError(f"不支持的翻译分配模式：{mapping_mode}")
    ordered_ids = list(dict.fromkeys(
        str(group.get("execution_block_id") or "manual") for group in groups
    ))
    batches: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        block_id = str(group.get("execution_block_id") or "manual")
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
    wire_text, wire_ledger = render_translation_request(
        prompt_groups, mapping_mode=mapping_mode
    )
    wire_system_prompt = system_prompt + "\n\n" + output_contract("TRANSLATE")
    cache_key = fingerprint({
        "scope": cache_scope,
        "stage": stage_name,
        "model": str(route["model"]),
        "system_prompt": wire_system_prompt,
        "user_text": wire_text,
        "mapping_mode": mapping_mode,
        "thinking_mode": str(route["thinking_mode"]),
        "reasoning_effort": str(route["reasoning_effort"]),
        "max_tokens": int(route["max_tokens"]),
        "temperature": float(route["temperature"]),
        "schema": "translation-map.v5-raw-cache",
    })
    cached = load_ai_block_cache(cache_directory, cache_key) if cache_directory else None
    if cached is not None:
        cached_raw = cached.get("_raw_model_response")
        if isinstance(cached_raw, str):
            cached_response = finalize_translation(
                cached_raw, prompt_groups, wire_ledger, mapping_mode=mapping_mode
            )
            if (
                cache_validator(cached_response) if cache_validator is not None
                else _response_contract_valid(
                    cached_response, prompt_groups, mapping_mode
                )
            ):
                return cached_response, {
                    "cache_hit": True, "cache_key": cache_key,
                    "wire_protocol": "substar-translation-map.v5",
                    "raw_model_response": cached_raw,
                    "finalized_response": cached_response,
                }
        elif (
            cache_validator(cached) if cache_validator is not None
            else _response_contract_valid(cached, prompt_groups, mapping_mode)
        ):
            # Read compatibility for v4 canonical cache entries. They are not
            # written again because their embedded project IDs are not portable.
            return cached, {"cache_hit": True, "cache_key": cache_key}
    model_output, telemetry = call_translation_model(
        base_url=str(route["base_url"]),
        api_key=str(route["api_key"]),
        auth_mode=str(route["auth_mode"]),
        model=str(route["model"]),
        system_prompt=wire_system_prompt,
        user_text=wire_text,
        groups=prompt_groups,
        mapping_mode=mapping_mode,
        timeout=int(settings.get("translation_api_timeout_seconds", 300)),
        thinking_mode=str(route["thinking_mode"]),
        reasoning_effort=str(route["reasoning_effort"]),
        request_attempts=int(settings.get("http_retry_attempts", 2)),
        max_tokens=int(route["max_tokens"]),
        temperature=float(route["temperature"]),
    )
    # Older integrations may inject the former JSON seam. Keeping this branch
    # costs production nothing and makes the wire-protocol migration reversible.
    if isinstance(model_output, Mapping):
        response = dict(model_output)
        if cache_directory is not None and (
            cache_validator(response) if cache_validator is not None
            else _response_contract_valid(response, prompt_groups, mapping_mode)
        ):
            save_ai_block_cache(cache_directory, cache_key, response)
        return response, {**telemetry, "cache_hit": False, "cache_key": cache_key}
    raw_response = str(model_output)
    exchange_path = None
    if cache_directory is not None:
        exchange_path = cache_directory / "exchanges" / f"{time.time_ns()}_{cache_key}.json"
        atomic_write_json(exchange_path, {
            "schema_version": "substar.model-exchange.v1",
            "stage": stage_name,
            "wire_protocol": "substar-translation-map.v5",
            "system_prompt": wire_system_prompt,
            "system_prompt_sha256": fingerprint({"text": wire_system_prompt}),
            "request_text": wire_text,
            "raw_model_response": raw_response,
            "transport_telemetry": telemetry,
        })
    try:
        response = finalize_translation(
            raw_response, prompt_groups, wire_ledger, mapping_mode=mapping_mode
        )
    except (TypeError, ValueError, KeyError) as exc:
        if exchange_path is not None:
            atomic_write_json(exchange_path, {
                "schema_version": "substar.model-exchange.v1",
                "stage": stage_name,
                "wire_protocol": "substar-translation-map.v5",
                "system_prompt": wire_system_prompt,
                "system_prompt_sha256": fingerprint({"text": wire_system_prompt}),
                "request_text": wire_text,
                "raw_model_response": raw_response,
                "transport_telemetry": telemetry,
                "finalizer_error": str(exc),
            })
        raise RuntimeError(f"翻译 Cue Script finalizer 拒绝模型输出：{exc}") from exc
    telemetry = {
        **telemetry,
        "wire_protocol": "substar-translation-map.v5",
        "wire_input_characters": len(wire_text),
        "wire_output_characters": len(raw_response),
        "raw_model_response": raw_response,
        "finalized_response": response,
    }
    if exchange_path is not None:
        atomic_write_json(exchange_path, {
            "schema_version": "substar.model-exchange.v1",
            "stage": stage_name,
            "wire_protocol": "substar-translation-map.v5",
            "system_prompt": wire_system_prompt,
            "system_prompt_sha256": fingerprint({"text": wire_system_prompt}),
            "request_text": wire_text,
            "raw_model_response": raw_response,
            "finalized_response": response,
            "transport_telemetry": {
                key: value for key, value in telemetry.items()
                if key not in {"raw_model_response", "finalized_response"}
            },
        })
    if cache_directory is not None and (
        cache_validator(response) if cache_validator is not None
        else _response_contract_valid(response, prompt_groups, mapping_mode)
    ):
        # Cache the provider-visible raw response, not a canonical response
        # containing project-specific IDs. A hit is finalized again against
        # the current alias ledger, so identical source blocks can be reused
        # safely across projects and revisions.
        save_ai_block_cache(
            cache_directory, cache_key, {"_raw_model_response": raw_response}
        )
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
                progress_callback(len(results), len(batches))
    rows = [
        row for batch in batches
        for row in results[str(batch["block_id"])]["response"].get("group_results", [])
        if isinstance(row, dict)
    ]
    wire_units = [
        dict(unit)
        for batch in batches
        for unit in results[str(batch["block_id"])]["response"].get("_wire_units", [])
        if isinstance(unit, Mapping)
    ]
    issues = [
        {**dict(issue), "block_id": str(batch["block_id"])}
        for batch in batches
        for issue in results[str(batch["block_id"])]["response"].get("_cue_script_issues", [])
        if isinstance(issue, Mapping)
    ]
    return {
        "group_results": rows,
        "_wire_units": wire_units,
        "_cue_script_issues": issues,
    }, {
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
    allowed = {
        "group_results", "_wire_units", "_covered_cue_ids",
        "_cue_script_issues", "_cue_script_warnings",
    }
    if "group_results" not in response or not set(response) <= allowed:
        return False
    if response.get("_cue_script_issues"):
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


def _target_length(value: str, count_rule: str) -> int:
    if count_rule == "characters_excluding_spaces":
        return sum(not char.isspace() for char in value)
    return len(value)


def _plan_limit_issues(
    plan: Mapping[str, Any], cues_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    assigned_by_unit: dict[str, list[str]] = {}
    for assignment in plan.get("cue_assignments", []):
        if isinstance(assignment, Mapping):
            assigned_by_unit.setdefault(
                str(assignment.get("meaning_unit_id") or ""), []
            ).append(str(assignment.get("cue_id") or ""))
    issues: list[dict[str, Any]] = []
    for unit in plan.get("meaning_units", []):
        if not isinstance(unit, Mapping):
            continue
        unit_id = str(unit.get("meaning_unit_id") or "")
        cue_ids = [cue_id for cue_id in assigned_by_unit.get(unit_id, []) if cue_id in cues_by_id]
        if not cue_ids:
            continue
        text = str(unit.get("target_text") or "")
        limits = [int(cues_by_id[cue_id].get("hard_limit") or 0) for cue_id in cue_ids]
        limits = [value for value in limits if value > 0]
        limit = min(limits) if limits else 0
        rule = str(cues_by_id[cue_ids[0]].get("count_rule") or "all_characters_including_spaces")
        count = _target_length(text, rule)
        if limit and count > limit:
            issues.append({
                "code": "target_over_limit",
                "meaning_unit_id": unit_id,
                "cue_ids": cue_ids,
                "target_text": text,
                "count": count,
                "limit": limit,
                "count_rule": rule,
            })
    return issues


def _plans_as_wire_units(plans: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    covered: set[str] = set()
    for plan in plans:
        assignments: dict[str, list[str]] = {}
        for row in plan.get("cue_assignments", []):
            if isinstance(row, Mapping):
                assignments.setdefault(str(row.get("meaning_unit_id") or ""), []).append(
                    str(row.get("cue_id") or "")
                )
        for unit in plan.get("meaning_units", []):
            if not isinstance(unit, Mapping):
                continue
            cue_ids = [
                cue_id for cue_id in assignments.get(
                    str(unit.get("meaning_unit_id") or ""), []
                ) if cue_id and cue_id not in covered
            ]
            target = str(unit.get("target_text") or "").strip()
            if cue_ids and target:
                units.append({"cue_ids": cue_ids, "target_text": target})
                covered.update(cue_ids)
    return units


def _response_wire_units(
    response: Mapping[str, Any], groups: list[dict[str, Any]], mapping_mode: str,
) -> list[dict[str, Any]]:
    raw_units = response.get("_wire_units")
    if isinstance(raw_units, list):
        return [dict(row) for row in raw_units if isinstance(row, Mapping)]
    rows = _result_rows(dict(response))
    plans = [
        plan for group in groups
        if (plan := _presentation_plan(
            group, rows.get(str(group["group_id"])), mapping_mode
        )) is not None
    ]
    return _plans_as_wire_units(plans)


def _compile_translation_plans(
    groups: list[dict[str, Any]], units: list[dict[str, Any]], mapping_mode: str,
) -> list[dict[str, Any]]:
    rows = _result_rows(
        compile_translation_units(groups, units, mapping_mode=mapping_mode)
    )
    return [
        plan for group in groups
        if (plan := _presentation_plan(
            group, rows.get(str(group["group_id"])), mapping_mode
        )) is not None
    ]


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
    """Finalize translation with at most one structural repair per source block.

    Valid alias bindings are immutable. A repair sees the complete original
    block, receives every program error together, and owns only unresolved
    aliases. The deterministic compiler then merges that patch with the frozen
    primary units and rebuilds the unchanged delivery contract.
    """
    non_repairable = set(non_repairable_group_ids or set())
    block_ids = {
        str(group["group_id"]): str(
            (group_block_ids or {}).get(
                str(group["group_id"]),
                group.get("execution_block_id") or group["group_id"],
            )
        )
        for group in groups
    }
    groups_by_block: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        groups_by_block.setdefault(block_ids[str(group["group_id"])], []).append(group)

    primary_units = _response_wire_units(response, groups, mapping_mode)
    current_units = [dict(row) for row in primary_units]
    plans = _compile_translation_plans(groups, current_units, mapping_mode)
    # Compatibility with injected legacy JSON responses that do not expose
    # wire units but already passed the canonical validator.
    if not current_units:
        rows = _result_rows(response)
        plans = [
            plan for group in groups
            if (plan := _presentation_plan(
                group, rows.get(str(group["group_id"])), mapping_mode
            )) is not None
        ]
        current_units = _plans_as_wire_units(plans)

    accepted_ids = {str(plan["group_id"]) for plan in plans}
    invalid_ids = {
        str(group["group_id"]) for group in groups
        if str(group["group_id"]) not in accepted_ids
    }
    repair_blocks = {
        block_id: block_groups
        for block_id, block_groups in groups_by_block.items()
        if any(
            str(group["group_id"]) in invalid_ids
            and str(group["group_id"]) not in non_repairable
            for group in block_groups
        )
    }
    repair_record: dict[str, Any] = {
        "attempted_group_ids": sorted(invalid_ids - non_repairable),
        "repair_phase_entered": bool(repair_blocks),
        "groups": [],
    }
    if progress_callback is not None and repair_blocks:
        progress_callback(0, len(repair_blocks), 0)

    primary_issues = [
        dict(row) for row in response.get("_cue_script_issues", [])
        if isinstance(row, Mapping)
    ]

    def repair_one(
        item: tuple[str, list[dict[str, Any]]],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        block_id, block_groups = item
        affected = [
            group for group in block_groups
            if str(group["group_id"]) in invalid_ids
            and str(group["group_id"]) not in non_repairable
        ]
        affected_cues = {
            str(cue["cue_id"])
            for group in affected for cue in group.get("cues", [])
        }
        block_cues = {
            str(cue["cue_id"])
            for group in block_groups for cue in group.get("cues", [])
        }
        frozen_cues = {
            str(cue_id)
            for unit in current_units for cue_id in unit.get("cue_ids", [])
            if str(cue_id) in block_cues
        }
        editable_cues = affected_cues - frozen_cues
        if not editable_cues:
            # A legacy/non-wire invalid row has no safely frozen ownership.
            editable_cues = set(affected_cues)
        validation_issues = [
            issue for issue in primary_issues
            if str(issue.get("cue_id") or "") in affected_cues
            or str(issue.get("block_id") or "") == block_id
        ]
        rows = _result_rows(response)
        for group in affected:
            group_id = str(group["group_id"])
            if not any(str(issue.get("group_id") or "") == group_id for issue in validation_issues):
                validation_issues.extend(
                    _translation_validation_issues(
                        group, rows.get(group_id), mapping_mode
                    )
                )
        payloads: list[dict[str, Any]] = []
        for group_index, group in enumerate(block_groups):
            payloads.append({
                **group,
                "cues": [
                    {**cue, "editable": str(cue["cue_id"]) in editable_cues}
                    for cue in group.get("cues", [])
                ],
                "program_validation_errors": validation_issues if group_index == 0 else [],
                "repair_attempt": 1,
            })
        if failure_injector is not None:
            failure_injector(block_id, 1)
        try:
            patch_response, telemetry = api_call(
                settings=settings, system_prompt=repair_prompt,
                groups=payloads, stage_name="translation_repair",
                cache_directory=cache_directory, cache_scope=cache_scope,
                mapping_mode=mapping_mode,
                cache_validator=lambda value: (
                    set(str(value_id) for value_id in value.get("_covered_cue_ids", []))
                    == editable_cues
                    and not value.get("_cue_script_issues")
                ),
            )
            patch_units = _response_wire_units(patch_response, payloads, mapping_mode)
            covered = {
                str(cue_id) for unit in patch_units for cue_id in unit.get("cue_ids", [])
            }
            if covered != editable_cues:
                raise RuntimeError(
                    "修复响应没有精确覆盖 OWN Cue："
                    f"missing={sorted(editable_cues - covered)} "
                    f"extra={sorted(covered - editable_cues)}"
                )
            return block_id, patch_units, {
                "attempt": 1, "block_id": f"{block_id}:repair",
                "source_block_id": block_id, "response": patch_response,
                "telemetry": telemetry, "validation_errors": validation_issues,
                "editable_cue_ids": sorted(editable_cues),
                "frozen_cue_ids": sorted(frozen_cues), "valid": True,
            }
        except Exception as exc:
            return block_id, [], {
                "attempt": 1, "block_id": f"{block_id}:repair",
                "source_block_id": block_id, "error": str(exc),
                "validation_errors": validation_issues,
                "editable_cue_ids": sorted(editable_cues),
                "frozen_cue_ids": sorted(frozen_cues), "valid": False,
            }

    repair_results: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    if repair_blocks:
        workers = min(
            len(repair_blocks), max(1, int(settings.get("translation_workers", 8)))
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(repair_one, item): item[0]
                for item in repair_blocks.items()
            }
            for future in concurrent.futures.as_completed(futures):
                block_id, patch_units, audit = future.result()
                repair_results[block_id] = (patch_units, audit)
                if progress_callback is not None:
                    progress_callback(
                        len(repair_results), len(repair_blocks),
                        sum(bool(row[1].get("valid")) for row in repair_results.values()),
                    )

    for block_id, block_groups in repair_blocks.items():
        patch_units, audit = repair_results[block_id]
        affected_ids = [
            str(group["group_id"]) for group in block_groups
            if str(group["group_id"]) in invalid_ids
        ]
        repair_record["groups"].append({
            "group_id": block_id,
            "affected_group_ids": affected_ids,
            "source_block_id": block_id,
            "attempts": [audit],
            "accepted": bool(audit.get("valid")),
            "primary_request_count": 1,
            "repair_attempted": True,
            "repair_request_count": 1,
        })
        if audit.get("valid"):
            current_units.extend(patch_units)

    plans = _compile_translation_plans(groups, current_units, mapping_mode)
    accepted_ids = {str(plan["group_id"]) for plan in plans}
    invalid = [
        group for group in groups if str(group["group_id"]) not in accepted_ids
    ]

    # Length violations use the same block-wide repair invariant. The helper
    # receives complete plans, groups all issues by their source block and
    # freezes every target unit outside the explicit over-limit scope.
    plans, limit_rows = _repair_over_limit_plans_blockwise(
        plans=plans, groups=groups, settings=settings,
        repair_prompt=repair_prompt, mapping_mode=mapping_mode,
        cache_directory=cache_directory, cache_scope=cache_scope,
        group_block_ids=block_ids, progress_callback=progress_callback,
        completed_repairs=len(repair_record["groups"]),
    )
    if limit_rows:
        repair_record["groups"].extend(limit_rows)
        repair_record["repair_phase_entered"] = True

    candidate_targets_by_cue: dict[str, str] = {}
    for unit in current_units:
        target = str(unit.get("target_text") or "").strip()
        for cue_id in unit.get("cue_ids", []):
            if target:
                candidate_targets_by_cue.setdefault(str(cue_id), target)
    # Legacy JSON/provider seams may carry useful text without a safe wire
    # binding. Preserve it only as an editable candidate for unresolved Cues;
    # it never becomes accepted output automatically.
    response_rows = _result_rows(response)
    for group in invalid:
        for cue_id, target in _candidate_targets(
            group, response_rows.get(str(group["group_id"]))
        ).items():
            candidate_targets_by_cue.setdefault(cue_id, target)
    return plans, {
        "model_repair": repair_record,
        "invalid_group_ids": [str(group["group_id"]) for group in invalid],
        "candidate_targets_by_cue": candidate_targets_by_cue,
    }


def _repair_over_limit_plans_blockwise(
    *, plans: list[dict[str, Any]], groups: list[dict[str, Any]],
    settings: dict[str, Any], repair_prompt: str, mapping_mode: str,
    cache_directory: Path | None, cache_scope: str,
    group_block_ids: Mapping[str, str],
    progress_callback: Callable[[int, int, int], None] | None,
    completed_repairs: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cues_by_id = {
        str(cue["cue_id"]): cue
        for group in groups for cue in group.get("cues", [])
    }
    groups_by_block: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        block_id = str(group_block_ids.get(str(group["group_id"]), "manual"))
        groups_by_block.setdefault(block_id, []).append(group)
    issues_by_block: dict[str, list[dict[str, Any]]] = {}
    for plan in plans:
        block_id = str(group_block_ids.get(str(plan["group_id"]), "manual"))
        issues_by_block.setdefault(block_id, []).extend(
            _plan_limit_issues(plan, cues_by_id)
        )
    issues_by_block = {
        block_id: issues for block_id, issues in issues_by_block.items() if issues
    }
    if not issues_by_block:
        return plans, []

    base_units = _plans_as_wire_units(plans)
    total = completed_repairs + len(issues_by_block)
    if progress_callback is not None:
        progress_callback(completed_repairs, total, completed_repairs)

    def repair_block(
        item: tuple[str, list[dict[str, Any]]],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        block_id, issues = item
        editable = {
            str(cue_id) for issue in issues for cue_id in issue.get("cue_ids", [])
        }
        payloads: list[dict[str, Any]] = []
        for index, group in enumerate(groups_by_block[block_id]):
            payloads.append({
                **group,
                "cues": [
                    {**cue, "editable": str(cue["cue_id"]) in editable}
                    for cue in group.get("cues", [])
                ],
                "program_validation_errors": issues if index == 0 else [],
                "repair_attempt": 1,
            })

        def patch_valid(value: dict[str, Any]) -> bool:
            units = _response_wire_units(value, payloads, mapping_mode)
            covered = {
                str(cue_id) for unit in units for cue_id in unit.get("cue_ids", [])
            }
            if covered != editable or value.get("_cue_script_issues"):
                return False
            for unit in units:
                cue_ids = [str(value) for value in unit.get("cue_ids", [])]
                limits = [
                    int(cues_by_id[cue_id].get("hard_limit") or 0)
                    for cue_id in cue_ids if cue_id in cues_by_id
                ]
                limits = [value for value in limits if value > 0]
                if not limits:
                    continue
                rule = str(cues_by_id[cue_ids[0]].get(
                    "count_rule", "all_characters_including_spaces"
                ))
                if _target_length(str(unit.get("target_text") or ""), rule) > min(limits):
                    return False
            return True

        try:
            response, telemetry = api_call(
                settings=settings, system_prompt=repair_prompt,
                groups=payloads, stage_name="translation_repair",
                cache_directory=cache_directory, cache_scope=cache_scope,
                mapping_mode=mapping_mode, cache_validator=patch_valid,
            )
            patch_units = _response_wire_units(response, payloads, mapping_mode)
            if not patch_valid(response):
                raise RuntimeError("长度修复响应没有精确覆盖 OWN Cue 或仍然超限")
            return block_id, patch_units, {
                "attempt": 1, "source_block_id": block_id,
                "response": response, "telemetry": telemetry,
                "validation_errors": issues,
                "editable_cue_ids": sorted(editable), "valid": True,
            }
        except Exception as exc:
            return block_id, [], {
                "attempt": 1, "source_block_id": block_id,
                "error": str(exc), "validation_errors": issues,
                "editable_cue_ids": sorted(editable), "valid": False,
            }

    results: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    workers = min(
        len(issues_by_block), max(1, int(settings.get("translation_workers", 8)))
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(repair_block, item): item[0]
            for item in issues_by_block.items()
        }
        for future in concurrent.futures.as_completed(futures):
            block_id, units, audit = future.result()
            results[block_id] = (units, audit)
            if progress_callback is not None:
                progress_callback(
                    completed_repairs + len(results), total,
                    completed_repairs + sum(
                        bool(value[1].get("valid")) for value in results.values()
                    ),
                )

    merged_units = [dict(row) for row in base_units]
    audit_rows: list[dict[str, Any]] = []
    for block_id, issues in issues_by_block.items():
        patch_units, audit = results[block_id]
        editable = set(audit.get("editable_cue_ids", []))
        if audit.get("valid"):
            merged_units = [
                unit for unit in merged_units
                if not editable.intersection(str(value) for value in unit.get("cue_ids", []))
            ]
            merged_units.extend(patch_units)
        audit_rows.append({
            "group_id": block_id,
            "repair_kind": "target_over_limit",
            "source_block_id": block_id,
            "validation_errors": issues,
            "attempts": [audit],
            "accepted": bool(audit.get("valid")),
            "primary_request_count": 1,
            "repair_attempted": True,
            "repair_request_count": 1,
        })
    return _compile_translation_plans(groups, merged_units, mapping_mode), audit_rows


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


def _translation_system_prompt(
    base_prompt: str,
    direction: str,
    glossary_entries: list[dict[str, Any]],
) -> str:
    """Compose one stage prompt without emitting an empty glossary section."""
    sections = [str(base_prompt).strip(), str(direction).strip()]
    if glossary_entries:
        sections.append(glossary_prompt(glossary_entries))
    return "\n\n".join(section for section in sections if section)


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
        "# TASK CONFIGURATION\n"
        f"源语言：{language_names.get(source_language, source_language)}\n"
        f"目标语言：{language_names.get(target_language, target_language)}\n"
        f"目标字幕 hard_limit：{int(settings.get({'en': 'english_hard_limit', 'zh-CN': 'chinese_hard_limit', 'ja': 'japanese_hard_limit', 'ko': 'korean_hard_limit'}.get(target_language, 'english_hard_limit'), 55))}。"
    )
    task_info = load_task_info(work, work.name)
    glossary_entries = active_glossary(str(task_info.get("glossary_id") or ""))
    system_prompt = _translation_system_prompt(
        prompt.text, direction, glossary_entries
    )
    repair_system_prompt = _translation_system_prompt(
        repair_prompt.text, direction, glossary_entries
    )
    batches = execution_block_batches(
        revision.document, groups, mapping_mode=mapping_mode
    )
    if progress_callback is not None:
        progress_callback("executing", completed=0, planned=len(batches))
    response, telemetry = call_block_batches(
        settings=settings,
        system_prompt=system_prompt,
        batches=batches,
        cache_directory=work / "translation" / "block_cache",
        cache_scope="translation-map-v5-block-patch",
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
    group_block_ids = {
        str(group["group_id"]): str(batch["block_id"])
        for batch in batches for group in batch["groups"]
    }
    cue_block_ids = {
        str(cue["cue_id"]): str(batch["block_id"])
        for batch in batches for group in batch["groups"] for cue in group["cues"]
    }
    plans, repair = complete_results(
        settings=settings,
        repair_prompt=repair_system_prompt,
        groups=groups,
        response=response,
        mapping_mode=mapping_mode,
        non_repairable_group_ids=non_repairable_group_ids,
        cache_directory=work / "translation" / "block_cache",
        cache_scope="translation-map-v5-block-patch",
        group_block_ids=group_block_ids,
        progress_callback=(
            (lambda done, total, accepted: progress_callback(
                "repair", completed=done, planned=len(batches),
                repair_planned=total, repair_accepted=accepted,
            )) if progress_callback is not None else None
        ),
    )
    repair_attempt_rows = [
        attempt
        for group in repair["model_repair"].get("groups", [])
        for attempt in group.get("attempts", [])
        if isinstance(attempt, Mapping)
    ]
    attempted_repair_block_ids = {
        str(row["source_block_id"])
        for row in repair_attempt_rows if str(row.get("source_block_id") or "")
    }
    unresolved_repair_block_ids = {
        str(attempt.get("source_block_id"))
        for group in repair["model_repair"].get("groups", [])
        if not bool(group.get("accepted"))
        for attempt in group.get("attempts", [])
        if str(attempt.get("source_block_id") or "")
    }
    accepted_repair_block_count = len(
        attempted_repair_block_ids - unresolved_repair_block_ids
    )
    if progress_callback is not None:
        progress_callback(
            "validating", completed=len(batches), planned=len(batches),
            repair_planned=len(attempted_repair_block_ids),
            repair_accepted=accepted_repair_block_count,
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
            "materializing", completed=len(batches), planned=len(batches),
            repair_planned=len(attempted_repair_block_ids),
            repair_accepted=accepted_repair_block_count,
        )
    revision_id = _save_translation(
        work=work, plans=plans, settings=settings,
        metadata=metadata, target_language=target_language,
        expected_revision_id=revision.revision_id,
        candidate_targets_by_cue=repair.get("candidate_targets_by_cue", {}),
    )
    return {
        "revision_id": revision_id,
        "repair": repair,
        "cue_block_ids": cue_block_ids,
        **metadata,
    }
