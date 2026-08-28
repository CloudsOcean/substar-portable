from __future__ import annotations

import concurrent.futures
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from substar_core.artifacts import atomic_write_json
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
from substar_core.stage2 import call_translation_model
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
    return cleaned


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
             groups: list[dict[str, Any]], stage_name: str = "translation") -> tuple[dict[str, Any], dict[str, Any]]:
    last_error: Exception | None = None
    prompt_groups = [
        {key: value for key, value in group.items() if not str(key).startswith("_")}
        for group in groups
    ]
    for outer_attempt in range(1, 5):
        try:
            return call_translation_model(
                base_url=str(settings["translation_api_base_url"]),
                api_key=str(settings.get("translation_api_key") or ""),
                auth_mode=str(settings.get("translation_api_auth_mode", "bearer")),
                model=str(settings.get(f"stage_{stage_name}_model") or settings.get("translation_api_model") or "deepseek-v4-flash"),
                system_prompt=system_prompt,
                groups=prompt_groups,
                timeout=int(settings.get("translation_api_timeout_seconds", 300)),
                thinking_mode=str(settings.get(f"stage_{stage_name}_thinking_mode", "enabled")),
                reasoning_effort=str(settings.get(f"stage_{stage_name}_reasoning_effort", "low")),
                request_attempts=int(settings.get("http_retry_attempts", 2)),
                max_tokens=int(settings.get(f"stage_{stage_name}_max_tokens", 131072)),
                temperature=float(settings.get(f"stage_{stage_name}_temperature", 0.0)),
            )
        except Exception as exc:
            last_error = exc
            if outer_attempt == 4:
                raise
            time.sleep(outer_attempt * 3)
    raise RuntimeError(f"API 请求未完成：{last_error}")


def call_block_batches(*, settings: dict[str, Any], system_prompt: str,
                       batches: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not batches:
        return {"group_results": []}, {"execution_blocks": []}
    results: dict[str, dict[str, Any]] = {}

    def run(batch: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        block_id = str(batch["block_id"])
        try:
            response, telemetry = api_call(
                settings=settings, system_prompt=system_prompt, groups=batch["groups"]
            )
            return block_id, {"response": response, "telemetry": telemetry}
        except Exception as exc:
            return block_id, {"response": {}, "error": str(exc)}

    workers = min(len(batches), max(1, int(settings.get("translation_workers", 8))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, batch) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            block_id, value = future.result()
            results[block_id] = value
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
    group: dict[str, Any], row: dict[str, Any] | None
) -> dict[str, Any] | None:
    return validate_presentation_plan(group, row)


def _repair_group(*, settings: dict[str, Any], repair_prompt: str,
                  group: dict[str, Any], attempts: int = 2) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        try:
            response, telemetry = api_call(
                settings=settings, system_prompt=repair_prompt,
                groups=[group], stage_name="translation_repair",
            )
            plan = _presentation_plan(group, _result_rows(response).get(group["group_id"]))
            records.append({"attempt": attempt, "response": response, "telemetry": telemetry, "valid": bool(plan)})
            if plan:
                return plan, records
        except Exception as exc:
            records.append({"attempt": attempt, "error": str(exc), "valid": False})
    return None, records


def complete_results(*, settings: dict[str, Any], repair_prompt: str,
                     groups: list[dict[str, Any]], response: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _result_rows(response)
    plans: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for group in groups:
        group_id = str(group["group_id"])
        plan = _presentation_plan(group, rows.get(group_id))
        if plan is None:
            invalid.append(group)
        else:
            plans.append(plan)
    repair_record: dict[str, Any] = {
        "attempted_group_ids": [str(group["group_id"]) for group in invalid],
        "groups": [],
    }
    attempts = max(0, int(settings.get("translation_repair_attempts", 1)))
    if invalid and attempts:
        repair_results: dict[str, tuple[dict[str, Any] | None, list[dict[str, Any]]]] = {}

        def repair(group: dict[str, Any]) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
            plan, records = _repair_group(
                settings=settings, repair_prompt=repair_prompt,
                group=group, attempts=attempts,
            )
            return str(group["group_id"]), plan, records

        workers = min(
            len(invalid), max(1, int(settings.get("translation_workers", 8)))
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(repair, group) for group in invalid]
            for future in concurrent.futures.as_completed(futures):
                group_id, plan, records = future.result()
                repair_results[group_id] = (plan, records)

        remaining: list[dict[str, Any]] = []
        for group in invalid:
            plan, records = repair_results[str(group["group_id"])]
            repair_record["groups"].append({
                "group_id": str(group["group_id"]),
                "attempts": records,
                "accepted": bool(plan),
            })
            if plan is None:
                remaining.append(group)
            else:
                plans.append(plan)
        invalid = remaining
    return plans, {
        "model_repair": repair_record,
        "invalid_group_ids": [str(group["group_id"]) for group in invalid],
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
    document: Any, plans: list[dict[str, Any]], target_language: str
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
    for source_cue in document.cues:
        if source_cue.state is not EntityState.ACTIVE or source_cue.cue_id not in unresolved:
            continue
        presentation.append(replace(source_cue, index=len(presentation)))
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
        "unresolved_source_cue_ids": sorted(unresolved),
    }


def _save_translation(*, work: Path, plans: list[dict[str, Any]], settings: dict[str, Any],
                      metadata: dict[str, Any], target_language: str) -> str:
    store = ProjectStore.open(work / "project")
    revision = store.load_latest()
    if revision is None:
        raise RuntimeError("项目缺少可翻译版本")
    candidate, presentation_report = materialize_presentation(
        revision.document, plans, target_language
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
    prompt = render_prompt("contextual_translation", variant=variant)
    repair_prompt = render_prompt("contextual_translation_repair", variant=variant)
    language_names = {"zh-CN": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文"}
    direction = (
        "# ACTIVE_LANGUAGE_DIRECTION\n"
        f"源语言：{language_names.get(source_language, source_language)}\n"
        f"目标语言：{language_names.get(target_language, target_language)}\n"
        "所有 target_text 必须使用目标语言。\n"
        f"目标语言 hard_limit：{int(settings.get({'en': 'english_hard_limit', 'zh-CN': 'chinese_hard_limit', 'ja': 'japanese_hard_limit', 'ko': 'korean_hard_limit'}.get(target_language, 'english_hard_limit'), 55))}；"
        "每个 meaning_units.target_text 必须是完整、唯一的目标语意义单元；"
        "你必须亲自决定 cue_assignments，程序只按引用原样显示，不生成或拆分文本。"
    )
    task_info = load_task_info(work, work.name)
    glossary = glossary_prompt(active_glossary(str(task_info.get("glossary_id") or "")))
    if source_language == target_language:
        direction += " 本任务是同语种字幕校订：保留原意与语言，只做自然表达和 Cue 分配。"
    batches = execution_block_batches(revision.document, groups)
    response, telemetry = call_block_batches(
        settings=settings,
        system_prompt=f"{prompt.text}\n\n{direction}\n\n{glossary}",
        batches=batches,
    )
    plans, repair = complete_results(
        settings=settings,
        repair_prompt=f"{repair_prompt.text}\n\n{direction}\n\n{glossary}",
        groups=groups,
        response=response,
    )
    atomic_write_json(audit_dir / "contextual_translation_response.json", response)
    atomic_write_json(audit_dir / "contextual_translation_telemetry.json", telemetry)
    atomic_write_json(audit_dir / "contextual_translation_repair.json", repair)
    metadata = {
        "source_language": source_language,
        "target_language": target_language,
        "prompt": prompt.metadata(),
        "repair_prompt": repair_prompt.metadata(),
        "execution_block_ids": [item["block_id"] for item in batches],
        "problem_group_ids": list(repair["invalid_group_ids"]),
    }
    revision_id = _save_translation(
        work=work, plans=plans, settings=settings,
        metadata=metadata, target_language=target_language,
    )
    return {"revision_id": revision_id, "repair": repair, **metadata}
