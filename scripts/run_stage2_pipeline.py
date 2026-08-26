from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.config import load_settings  # noqa: E402
from substar_core.checkpoint import (  # noqa: E402
    read_checkpoint,
    stable_fingerprint,
    write_checkpoint,
)
from substar_core.glossary import active_glossary, glossary_prompt  # noqa: E402
from substar_core.policy import SubtitlePolicy  # noqa: E402
from substar_core.stage1 import display_normalize, han_count  # noqa: E402
from substar_core.stage2 import (  # noqa: E402
    Cue,
    Stage2Error,
    build_cues,
    call_translation_model,
    classify_source_language,
    render_srt,
    subtitle_visual_width,
    validate_final,
    validate_translation,
    target_language_for,
)
from substar_core.stage_progress import StageProgress  # noqa: E402
from substar_core.stage_settings import overlay_relay_profile  # noqa: E402
from substar_core.translation_atoms import (  # noqa: E402
    T1_ATOM_SCHEMA,
    T2_ASSEMBLY_SCHEMA,
    stage2_result_from_assembly,
    validate_t1_atoms,
)
from substar_core.translation_input_v2 import (  # noqa: E402
    build_translation_input_v2,
    persist_translation_revision_v2,
)
from substar_core.storage import ProjectStore, ProjectStoreError  # noqa: E402


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Stage2Error(f"{path.name} 顶层必须是 JSON 对象")
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def inject_manual_unaligned_cues(
    cues: list[Cue],
    groups: list[dict[str, Any]],
    manual_path: Path,
) -> None:
    if not manual_path.is_file():
        return
    rows = read_json(manual_path).get("cues", [])
    if not rows:
        return
    group_by_id = {str(group["group_id"]): group for group in groups}
    next_id = max((int(cue.cue_id) for cue in cues), default=0) + 1
    for row in rows:
        text = str(row.get("text", "")).strip()
        if not text:
            raise Stage2Error("人工未对齐字幕仍为空，请先在编辑器补录文字")
        start = float(row["start"])
        end = float(row["end"])
        if end <= start:
            raise Stage2Error("人工未对齐字幕时间范围无效")
        nearest = min(
            cues,
            key=lambda cue: min(abs(cue.end - start), abs(cue.start - end)),
        )
        group_id = str(nearest.group_id)
        source_language = classify_source_language(text)
        cue = Cue(
            cue_id=next_id,
            group_id=group_id,
            source_raw=text,
            source=text,
            alignment_start=int(nearest.alignment_end),
            alignment_end=int(nearest.alignment_end),
            start=start,
            end=end,
        )
        cues.append(cue)
        group_by_id[group_id]["cues"].append(
            {
                "cue_id": next_id,
                "source": text,
                "source_language": source_language,
                "target_language": target_language_for(source_language),
                "time_status": "manual_unaligned",
            }
        )
        next_id += 1
    cues.sort(key=lambda cue: (cue.start, cue.end))
    remap: dict[int, int] = {}
    for number, cue in enumerate(cues, start=1):
        remap[int(cue.cue_id)] = number
        cue.cue_id = number
    for group in groups:
        for item in group["cues"]:
            item["cue_id"] = remap[int(item["cue_id"])]
        group["cues"].sort(key=lambda item: int(item["cue_id"]))


def aggregate_by_meaning_groups(
    cues: list[Any],
    display_groups: list[dict[str, Any]],
    meaning_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Rebuild T1 units from P2 groups without splitting any final display cue."""
    meaning_groups = (
        meaning_plan.get("groups", [])
        if isinstance(meaning_plan, dict)
        else []
    )
    if not meaning_groups:
        return display_groups

    cue_ranges = {
        int(cue.cue_id): (
            int(cue.alignment_start),
            int(cue.alignment_end),
        )
        for cue in cues
    }
    display_rows: list[dict[str, Any]] = []
    for group in display_groups:
        cue_ids = [int(item["cue_id"]) for item in group["cues"]]
        if not cue_ids:
            continue
        display_rows.append(
            {
                "group": group,
                "alignment_start": min(cue_ranges[item][0] for item in cue_ids),
                "alignment_end": max(cue_ranges[item][1] for item in cue_ids),
            }
        )

    # A final cue can cross a P2 boundary after Pro editing. Such a boundary
    # cannot remain a translation boundary because the cue is indivisible.
    removable_boundaries = {
        int(meaning_groups[index]["alignment_end"]): index
        for index in range(len(meaning_groups) - 1)
    }
    crossed_boundaries: set[int] = set()
    for row in display_rows:
        crossed_boundaries.update(
            index
            for boundary, index in removable_boundaries.items()
            if int(row["alignment_start"]) <= boundary < int(row["alignment_end"])
        )

    components: list[tuple[int, int]] = []
    component_start = 0
    for index in range(len(meaning_groups) - 1):
        if index not in crossed_boundaries:
            components.append((component_start, index))
            component_start = index + 1
    components.append((component_start, len(meaning_groups) - 1))

    result: list[dict[str, Any]] = []
    claimed: set[int] = set()
    for left, right in components:
        alignment_start = int(meaning_groups[left]["alignment_start"])
        alignment_end = int(meaning_groups[right]["alignment_end"])
        member_rows = [
            row
            for row in display_rows
            if not (
                int(row["alignment_end"]) < alignment_start
                or int(row["alignment_start"]) > alignment_end
            )
        ]
        model_cues: list[dict[str, Any]] = []
        for row in member_rows:
            for cue in row["group"]["cues"]:
                cue_id = int(cue["cue_id"])
                if cue_id in claimed:
                    continue
                claimed.add(cue_id)
                model_cues.append(cue)
        if not model_cues:
            continue
        first_id = str(meaning_groups[left]["group_id"])
        last_id = str(meaning_groups[right]["group_id"])
        result.append(
            {
                "group_id": (
                    first_id if first_id == last_id else f"{first_id}__{last_id}"
                ),
                "alignment_start": alignment_start,
                "alignment_end": alignment_end,
                "meaning_group_ids": [
                    str(meaning_groups[index]["group_id"])
                    for index in range(left, right + 1)
                ],
                "cues": model_cues,
            }
        )
    expected = {
        int(item["cue_id"])
        for group in display_groups
        for item in group["cues"]
    }
    if claimed != expected:
        missing = sorted(expected - claimed)
        raise Stage2Error(
            f"T1 意义组重建未完整覆盖显示 cue：{missing[:20]}"
        )
    return result


def chunk_translation_groups_by_execution_blocks(
    groups: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Reuse P1's canonical block ownership for every translation stage."""
    if not blocks:
        return []
    chunks: list[list[dict[str, Any]]] = []
    claimed: set[str] = set()
    for block in blocks:
        start = int(block["alignment_start"])
        end = int(block["alignment_end"])
        members = [
            group for group in groups
            if start <= int(group.get("alignment_start", -1))
            and int(group.get("alignment_end", -1)) <= end
        ]
        for group in members:
            claimed.add(str(group["group_id"]))
        if members:
            chunks.append(members)
    expected = {str(group["group_id"]) for group in groups}
    if claimed != expected:
        missing = sorted(expected - claimed)
        raise Stage2Error(f"翻译意义组越过统一执行块边界：{missing[:20]}")
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description="Substar 03B 组级翻译与双语 SRT 导出")
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--stage1-dir", required=True, type=Path)
    parser.add_argument("--project-store-dir", required=True, type=Path)
    parser.add_argument("--expected-revision-id")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model")
    parser.add_argument("--translation-model")
    parser.add_argument("--mapping-model")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--thinking-mode", choices=["enabled", "disabled"])
    parser.add_argument(
        "--translation-thinking-mode",
        choices=["enabled", "disabled"],
        default="disabled",
    )
    parser.add_argument(
        "--mapping-thinking-mode",
        choices=["enabled", "disabled"],
        default="disabled",
    )
    parser.add_argument("--translation-reasoning-effort", choices=["low", "medium", "high", "max", "xhigh"], default="high")
    parser.add_argument("--mapping-reasoning-effort", choices=["low", "medium", "high", "max", "xhigh"], default="high")
    parser.add_argument("--translation-max-tokens", type=int, default=65536)
    parser.add_argument("--mapping-max-tokens", type=int, default=131072)
    parser.add_argument("--translation-temperature", type=float, default=1.3)
    parser.add_argument("--mapping-temperature", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=64, choices=range(1, 257))
    parser.add_argument("--repair-attempts", type=int, choices=range(0, 3))
    parser.add_argument(
        "--whole-program",
        action="store_true",
        help="把完整节目作为一个翻译上下文提交，不进行语义分块",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--progress-file", type=Path)
    args = parser.parse_args()
    progress = StageProgress(args.progress_file)
    try:
        settings = overlay_relay_profile(load_settings(include_secret=True), args.stage1_dir)
        api_key = str(settings.get("translation_api_key", ""))
        api_available = bool(api_key) and not args.offline
        args.output_dir.mkdir(parents=True, exist_ok=True)
        project_store = ProjectStore.open(args.project_store_dir)
        base_revision = project_store.load_latest()
        if base_revision is None:
            raise Stage2Error("项目没有可翻译的 EditorDocument V2 修订")
        if (
            args.expected_revision_id
            and base_revision.revision_id != args.expected_revision_id
        ):
            raise Stage2Error("翻译所基于的 EditorDocument V2 修订已变化")
        cues, groups, execution_blocks, domain_cue_ids = build_translation_input_v2(
            base_revision.document
        )
        top_baseline = str(settings.get("top_baseline_punctuation", "preserve"))
        top_raised = str(settings.get("top_raised_punctuation", "preserve"))
        bottom_baseline = str(settings.get("bottom_baseline_punctuation", "normalize"))
        bottom_raised = str(settings.get("bottom_raised_punctuation", "preserve"))
        display_order = (
            "source_target"
            if base_revision.document.presentation.display_order.value
            == "source_above_target"
            else "target_source"
        )
        policy = SubtitlePolicy.from_settings(settings)
        write_json(
            args.output_dir / "timing_snap_report.json",
            {
                "schema_version": "substar.timing-snap-report.v2",
                "source_revision_id": base_revision.revision_id,
                "decisions": [],
                "note": "时间由当前 EditorDocument 修订直接提供，翻译阶段不再吸附或重建。",
            },
        )
        target_mode = str(settings.get("target_language_mode", "auto_opposite"))
        if target_mode in {"zh-CN", "en", "ja", "ko"}:
            for group in groups:
                for cue in group["cues"]:
                    cue["target_language"] = target_mode
        chunks = (
            [groups]
            if args.whole_program
            else chunk_translation_groups_by_execution_blocks(groups, execution_blocks)
        )
        translation_block_ids = [
            f"t{number:04d}" for number in range(1, len(chunks) + 1)
        ]
        progress.plan("T1", len(chunks), block_ids=translation_block_ids)
        progress.plan("T2", len(chunks), block_ids=translation_block_ids)
        write_json(
            args.output_dir / "translation_chunk_plan.json",
            {
                "schema_version": "substar.stage2.chunk-plan.v1",
                "strategy": (
                    "whole_program" if args.whole_program
                    else "editor_document_v2_lineage"
                ),
                "source_revision_id": base_revision.revision_id,
                "meaning_group_count": len(groups),
                "chunk_count": len(chunks),
                "chunks": [
                    {
                        "chunk": number,
                        "group_ids": [
                            str(group["group_id"]) for group in chunk
                        ],
                        "alignment_start": int(
                            chunk[0].get("alignment_start", 0)
                        ),
                        "alignment_end": int(
                            chunk[-1].get("alignment_end", 0)
                        ),
                        "alignment_units": sum(
                            int(group.get("alignment_end", 0))
                            - int(group.get("alignment_start", 0))
                            + 1
                            for group in chunk
                        ),
                    }
                    for number, chunk in enumerate(chunks, start=1)
                ],
            },
        )
        base_prompt = (PROJECT_ROOT / "prompts" / "03B_组级翻译与映射_JSON.md").read_text(
            encoding="utf-8"
        )
        company_translation_examples = (
            PROJECT_ROOT / "prompts" / "references" / "构造翻译案例库.md"
        ).read_text(encoding="utf-8")
        translation_model = str(
            args.translation_model
            or args.model
            or settings["translation_api_model"]
        )
        mapping_model = str(
            args.mapping_model
            or args.model
            or settings["translation_api_model"]
        )
        context_path = args.job_dir / "stage03T_translation_context.json"
        if context_path.exists():
            context = read_json(context_path)
            active_terms = [
                item
                for item in context.get("matched_glossary", [])
                if isinstance(item, dict)
            ]
        else:
            active_terms = active_glossary(str(settings.get("project_name", "")))
        dynamic_translation_context = "\n\n".join(
            [
                "# ACTIVE_OUTPUT_PROFILE",
                f"翻译风格：{settings['translation_style']}",
                f"目标语言模式：{target_mode}",
                f"文件级显示顺序：{display_order}",
                "本阶段保留规范标点；显示层标点由编辑器按上下行规则投影。",
                f"英文硬上限：{settings['english_hard_limit']}",
                f"中文硬上限：{settings['chinese_hard_limit']}",
                f"中英混合硬上限：{settings.get('mixed_hard_limit', 25)}",
                f"日文硬上限：{settings['japanese_hard_limit']}",
                f"韩文硬上限：{settings['korean_hard_limit']}",
                f"目标视觉宽度硬上限：{settings['target_visual_width_limit']}",
                glossary_prompt(active_terms),
            ]
        )
        prompt = "\n\n".join(
            [
                base_prompt,
                company_translation_examples,
                dynamic_translation_context,
            ]
        )
        translation_atoms_contract = (
            PROJECT_ROOT / "prompts" / "T1_TRANSLATION_ATOMS_V2.md"
        ).read_text(encoding="utf-8")
        translation_only_prompt = "\n\n".join(
            [
                translation_atoms_contract,
                company_translation_examples,
                "# ACTIVE_OUTPUT_PROFILE",
                f"翻译风格：{settings['translation_style']}",
                f"目标语言模式：{target_mode}",
                glossary_prompt(active_terms),
            ]
        )
        assembly_contract = (
            PROJECT_ROOT / "prompts" / "T2_CUE_ASSEMBLY_V2.md"
        ).read_text(encoding="utf-8")
        mapping_prompt = "\n\n".join(
            [
                assembly_contract,
                company_translation_examples,
                dynamic_translation_context,
            ]
        )
        translation_thinking = args.thinking_mode or args.translation_thinking_mode
        mapping_thinking = args.thinking_mode or args.mapping_thinking_mode
        telemetry: list[dict[str, Any]] = []
        relation_groups: list[dict[str, Any]] = []
        terminology: list[dict[str, Any]] = []
        all_targets: dict[int, str] = {}
        checkpoint_dir = args.output_dir / "translation_chunks"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        translation_only_dir = args.output_dir / "group_translation_chunks"
        translation_only_dir.mkdir(parents=True, exist_ok=True)
        repair_budget = (
            int(args.repair_attempts)
            if args.repair_attempts is not None
            else int(settings.get("translation_repair_attempts", 1))
        )
        english_hard = int(settings.get("english_hard_limit", 55))
        chinese_hard = int(settings.get("chinese_hard_limit", 24))
        mixed_hard = int(settings.get("mixed_hard_limit", 25))
        japanese_hard = int(settings.get("japanese_hard_limit", 25))
        korean_hard = int(settings.get("korean_hard_limit", 32))
        visual_hard = int(settings.get("target_visual_width_limit", 48))

        def delivery_fallback(
            source_groups: list[dict[str, Any]],
            reason: str,
        ) -> dict[str, Any]:
            fallback_groups: list[dict[str, Any]] = []
            for group in source_groups:
                targets = []
                for cue in group["cues"]:
                    target_language = str(cue.get("target_language", "zh-CN"))
                    text = (
                        "Translation failed review required"
                        if target_language == "en"
                        else "【翻译失败 待人工复核】"
                    )
                    targets.append({"cue_id": int(cue["cue_id"]), "text": text})
                    targets[-1]["status"] = "failed"
                    targets[-1]["review_reason"] = reason
                fallback_groups.append(
                    {
                        "group_id": group["group_id"],
                        "relation": "N:N",
                        "strategy": "delivery_fallback",
                        "targets": targets,
                    }
                )
            return {
                "schema_version": "substar.stage2.translation.v1",
                "groups": fallback_groups,
                "terminology": [],
                "fallback_reason": reason,
            }

        def valid_groups_and_invalid_sources(
            result: dict[str, Any],
            source_groups: list[dict[str, Any]],
        ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
            actual_groups = result.get("groups")
            if not isinstance(actual_groups, list):
                return {}, list(source_groups)
            actual_by_id = {
                str(item.get("group_id")): item
                for item in actual_groups
                if isinstance(item, dict) and item.get("group_id") is not None
            }
            valid: dict[str, dict[str, Any]] = {}
            invalid: list[dict[str, Any]] = []
            for source in source_groups:
                group_id = str(source["group_id"])
                actual = actual_by_id.get(group_id)
                if actual is None:
                    invalid.append(source)
                    continue
                try:
                    validate_translation(
                        {
                            "schema_version": "substar.stage2.translation.v1",
                            "groups": [actual],
                        },
                        [source],
                        target_baseline_punctuation=bottom_baseline,
                        target_raised_punctuation=bottom_raised,
                        top_baseline_punctuation=top_baseline,
                        top_raised_punctuation=top_raised,
                        display_order=display_order,
                    )
                except Stage2Error:
                    invalid.append(source)
                else:
                    valid[group_id] = actual
            return valid, invalid

        def groups_over_hard_limits(
            result: dict[str, Any],
            source_groups: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            translated_by_id = {
                str(group["group_id"]): group for group in result["groups"]
            }
            invalid: list[dict[str, Any]] = []
            for source_group in source_groups:
                translated = translated_by_id[str(source_group["group_id"])]
                for target in translated.get("targets", []):
                    cue_id = int(target.get("cue_id", -1))
                    source_cue = next(
                        item
                        for item in source_group["cues"]
                        if int(item["cue_id"]) == cue_id
                    )
                    target_language = str(source_cue.get("target_language", "zh-CN"))
                    text = display_normalize(
                        str(target.get("text", "")),
                        baseline_punctuation=bottom_baseline,
                        raised_punctuation=bottom_raised,
                    )
                    if (
                        policy.line_length(text) > policy.hard_limit(text)
                        or subtitle_visual_width(text) > visual_hard
                    ):
                        invalid.append(source_group)
                        break
            return invalid

        def process_chunk(
            number: int,
            chunk: list[dict[str, Any]],
        ) -> tuple[
            int,
            list[dict[str, Any]],
            dict[str, Any],
            dict[int, str],
            dict[str, Any],
        ]:
            checkpoint = checkpoint_dir / f"chunk_{number:04d}.json"
            telemetry_path = checkpoint_dir / f"chunk_{number:04d}_api.json"
            translation_checkpoint = (
                translation_only_dir / f"chunk_{number:04d}.json"
            )
            translation_telemetry_path = (
                translation_only_dir / f"chunk_{number:04d}_api.json"
            )
            assembly_checkpoint = (
                checkpoint_dir / f"chunk_{number:04d}_assembly_v2.json"
            )
            fingerprint = stable_fingerprint(
                {
                    "stage": "stage2_translation",
                    "chunk": chunk,
                    "translation_prompt": translation_only_prompt,
                    "assembly_prompt": mapping_prompt,
                    "translation_model": translation_model,
                    "mapping_model": mapping_model,
                    "base_url": settings["translation_api_base_url"],
                    "translation_thinking": translation_thinking,
                    "mapping_thinking": mapping_thinking,
                    "target_mode": target_mode,
                    "policy": policy.to_dict(),
                    "glossary": active_terms,
                }
            )

            def save_result(value: dict[str, Any]) -> None:
                write_checkpoint(
                    checkpoint,
                    stage="stage2_translation",
                    fingerprint=fingerprint,
                    result=value,
                )

            resumed = (
                read_checkpoint(
                    checkpoint,
                    stage="stage2_translation",
                    fingerprint=fingerprint,
                )
                if args.resume
                else None
            )
            if resumed is not None:
                result = resumed
                call_info = read_json(telemetry_path) if telemetry_path.exists() else {}
                progress.event("T1", "accepted", block_id=f"t{number:04d}")
                progress.event("T2", "accepted", block_id=f"t{number:04d}")
                print(f"translation_chunk={number}/{len(chunks)} resumed=true", flush=True)
            else:
                print(f"translation_chunk={number}/{len(chunks)}", flush=True)
                t1_accepted = False
                try:
                    if not api_available:
                        raise Stage2Error("翻译 API 密钥未设置")
                    expected_ids = [str(group["group_id"]) for group in chunk]
                    source_by_id = {
                        str(group["group_id"]): group for group in chunk
                    }
                    translated_by_id: dict[str, dict[str, Any]] = {}
                    t1_retry_calls: list[dict[str, Any]] = []
                    t1_retry_budget = max(1, repair_budget)
                    translation_result: dict[str, Any] = {}
                    translation_info: dict[str, Any] = {}
                    t1_prompt = translation_only_prompt
                    for initial_attempt in range(t1_retry_budget + 1):
                        progress.event(
                            "T1",
                            "sent" if initial_attempt == 0 else "retry",
                            block_id=f"t{number:04d}",
                            detail=(
                                None
                                if initial_attempt == 0
                                else {"attempt": initial_attempt}
                            ),
                        )
                        try:
                            translation_result, translation_info = call_translation_model(
                                base_url=str(settings["translation_api_base_url"]),
                                api_key=api_key,
                                model=translation_model,
                                system_prompt=t1_prompt,
                                groups=chunk,
                                timeout=min(
                                    int(settings["translation_api_timeout_seconds"]),
                                    300,
                                ),
                                thinking_mode=translation_thinking,
                                reasoning_effort=args.translation_reasoning_effort,
                                max_tokens=args.translation_max_tokens,
                                temperature=args.translation_temperature,
                                request_attempts=int(settings.get("http_retry_attempts", 2)),
                            )
                            progress.event("T1", "response", block_id=f"t{number:04d}")
                            break
                        except (OSError, ValueError, Stage2Error) as initial_exc:
                            t1_retry_calls.append(
                                {
                                    "retry_attempt": initial_attempt,
                                    "requested_group_ids": expected_ids,
                                    "reason": "invalid_response",
                                    "error": str(initial_exc),
                                }
                            )
                            if initial_attempt >= t1_retry_budget:
                                raise
                            t1_prompt = "\n\n".join(
                                [
                                    translation_only_prompt,
                                    "# T1 RESPONSE RETRY",
                                    "上一响应不是可解析的完整 JSON。请严格只输出契约要求的 JSON，"
                                    "不得使用 Markdown 代码围栏，不得添加解释。",
                                    f"上一轮错误：{initial_exc}",
                                ]
                            )

                    def accept_t1_rows(value: dict[str, Any]) -> None:
                        if value.get("schema_version") != T1_ATOM_SCHEMA:
                            return
                        rows = value.get("groups")
                        if not isinstance(rows, list):
                            return
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            group_id = str(row.get("group_id", ""))
                            atoms = row.get("atoms")
                            if (
                                group_id in source_by_id
                                and isinstance(atoms, list)
                                and atoms
                            ):
                                translated_by_id[group_id] = row

                    accept_t1_rows(translation_result)
                    for retry_number in range(1, t1_retry_budget + 1):
                        missing_ids = [
                            group_id
                            for group_id in expected_ids
                            if group_id not in translated_by_id
                        ]
                        if not missing_ids:
                            break
                        missing_groups = [source_by_id[group_id] for group_id in missing_ids]
                        progress.event(
                            "T1",
                            "retry",
                            block_id=f"t{number:04d}",
                            detail={"missing_group_ids": missing_ids},
                        )
                        progress.event("T1", "sent", block_id=f"t{number:04d}")
                        retry_prompt = "\n\n".join(
                            [
                                translation_only_prompt,
                                "# T1 MISSING-GROUP RETRY",
                                "上一响应遗漏或返回了空译文。只翻译本次输入的意义组，"
                                "不得返回其他组。",
                                "groups 必须严格按下列 ID 顺序逐项返回；不得遗漏、"
                                "增加、重复或调序：",
                                json.dumps(missing_ids, ensure_ascii=False),
                            ]
                        )
                        try:
                            retry_result, retry_info = call_translation_model(
                                base_url=str(settings["translation_api_base_url"]),
                                api_key=api_key,
                                model=translation_model,
                                system_prompt=retry_prompt,
                                groups=missing_groups,
                                timeout=min(
                                    int(settings["translation_api_timeout_seconds"]),
                                    300,
                                ),
                                thinking_mode="disabled",
                                reasoning_effort=str(
                                    settings["translation_reasoning_effort"]
                                ),
                                request_attempts=int(
                                    settings.get("http_retry_attempts", 2)
                                ),
                            )
                            progress.event("T1", "response", block_id=f"t{number:04d}")
                            accept_t1_rows(retry_result)
                            t1_retry_calls.append(
                                {
                                    **retry_info,
                                    "retry_attempt": retry_number,
                                    "requested_group_ids": missing_ids,
                                }
                            )
                        except (OSError, ValueError, Stage2Error) as retry_exc:
                            t1_retry_calls.append(
                                {
                                    "retry_attempt": retry_number,
                                    "requested_group_ids": missing_ids,
                                    "error": str(retry_exc),
                                }
                            )
                    missing_ids = [
                        group_id
                        for group_id in expected_ids
                        if group_id not in translated_by_id
                    ]
                    if missing_ids:
                        raise Stage2Error(
                            "T1 重试后仍缺少意义组: " + ", ".join(missing_ids)
                        )

                    # A model can return every group while still collapsing one
                    # group into too few semantic atoms.  Treat that as a
                    # repairable contract response instead of aborting the
                    # whole translation run.  Re-ask only the invalid groups
                    # and replace their rows before the final validation.
                    def invalid_t1_ids() -> tuple[list[str], str]:
                        invalid: list[str] = []
                        errors: list[str] = []
                        for group_id in expected_ids:
                            row = translated_by_id.get(group_id)
                            if row is None:
                                invalid.append(group_id)
                                errors.append(f"{group_id} 缺少译文组")
                                continue
                            try:
                                validate_t1_atoms(
                                    {
                                        "schema_version": T1_ATOM_SCHEMA,
                                        "groups": [row],
                                    },
                                    [source_by_id[group_id]],
                                )
                            except (ValueError, Stage2Error) as exc:
                                invalid.append(group_id)
                                errors.append(str(exc))
                        return invalid, "; ".join(errors)

                    invalid_ids, t1_validation_error = invalid_t1_ids()
                    for retry_number in range(1, t1_retry_budget + 1):
                        if not invalid_ids:
                            break
                        progress.event(
                            "T1",
                            "retry",
                            block_id=f"t{number:04d}",
                            detail={
                                "invalid_group_ids": invalid_ids,
                                "error": t1_validation_error,
                            },
                        )
                        retry_prompt = "\n\n".join(
                            [
                                translation_only_prompt,
                                "# T1 CONTRACT REPAIR RETRY",
                                "上一响应的语义原子不够细，无法覆盖每一个固定源 Cue。",
                                "请为本次输入的每个意义组返回连续、按目标语序排列的语义原子；"
                                "每个源 Cue 至少被一个原子引用，且原子数量不得少于该组固定 Cue 数量。",
                                "只返回下列意义组，严格保留 group_id，不得遗漏或增加：",
                                json.dumps(invalid_ids, ensure_ascii=False),
                                f"上一轮校验错误：{t1_validation_error}",
                            ]
                        )
                        missing_groups = [source_by_id[group_id] for group_id in invalid_ids]
                        try:
                            retry_result, retry_info = call_translation_model(
                                base_url=str(settings["translation_api_base_url"]),
                                api_key=api_key,
                                model=translation_model,
                                system_prompt=retry_prompt,
                                groups=missing_groups,
                                timeout=min(
                                    int(settings["translation_api_timeout_seconds"]),
                                    300,
                                ),
                                thinking_mode="disabled",
                                reasoning_effort=args.translation_reasoning_effort,
                                max_tokens=args.translation_max_tokens,
                                temperature=args.translation_temperature,
                                request_attempts=int(settings.get("http_retry_attempts", 2)),
                            )
                            progress.event("T1", "response", block_id=f"t{number:04d}")
                            accept_t1_rows(retry_result)
                            t1_retry_calls.append(
                                {
                                    **retry_info,
                                    "retry_attempt": retry_number,
                                    "requested_group_ids": invalid_ids,
                                    "reason": "contract_validation",
                                }
                            )
                        except (OSError, ValueError, Stage2Error) as retry_exc:
                            t1_retry_calls.append(
                                {
                                    "retry_attempt": retry_number,
                                    "requested_group_ids": invalid_ids,
                                    "reason": "contract_validation",
                                    "error": str(retry_exc),
                                }
                            )
                        invalid_ids, t1_validation_error = invalid_t1_ids()
                    if invalid_ids:
                        raise Stage2Error(
                            "T1 契约重试后仍未覆盖固定 Cue："
                            + ", ".join(invalid_ids)
                            + (f"（{t1_validation_error}）" if t1_validation_error else "")
                        )
                    translation_result = {
                        "schema_version": T1_ATOM_SCHEMA,
                        "groups": [translated_by_id[group_id] for group_id in expected_ids],
                    }
                    validate_t1_atoms(translation_result, chunk)
                    if t1_retry_calls:
                        translation_info = {
                            **translation_info,
                            "missing_group_retries": t1_retry_calls,
                        }
                    mapping_chunk = copy.deepcopy(chunk)
                    for group in mapping_chunk:
                        atoms = list(
                            translated_by_id[str(group["group_id"])]["atoms"]
                        )
                        group["approved_atoms"] = atoms
                        group["approved_translation"] = str(
                            translated_by_id[str(group["group_id"])]["group_translation"]
                        ).strip()
                    progress.event("T1", "accepted", block_id=f"t{number:04d}")
                    t1_accepted = True
                    write_json(translation_checkpoint, translation_result)
                    write_json(
                        translation_telemetry_path,
                        {
                            **translation_info,
                            "stage": "T1_translation_atoms",
                            "model": translation_model,
                        },
                    )
                    t2_errors: list[str] = []
                    t2_retry_budget = max(1, repair_budget)
                    for t2_attempt in range(t2_retry_budget + 1):
                        progress.event(
                            "T2",
                            "sent" if t2_attempt == 0 else "retry",
                            block_id=f"t{number:04d}",
                            detail=(
                                None
                                if t2_attempt == 0
                                else {"attempt": t2_attempt, "error": t2_errors[-1]}
                            ),
                        )
                        retry_contract = (
                            mapping_prompt
                            if t2_attempt == 0
                            else "\n\n".join(
                                [
                                    mapping_prompt,
                                    "# STRICT T2 RETRY",
                                    "上一响应未通过 translation-assembly.v3 契约。"
                                    "只重新分配 atom_id；不得输出、改写或补充任何文本。",
                                    f"上一错误：{t2_errors[-1]}",
                                ]
                            )
                        )
                        try:
                            assembly_result, attempt_info = call_translation_model(
                                base_url=str(settings["translation_api_base_url"]),
                                api_key=api_key,
                                model=mapping_model,
                                system_prompt=retry_contract,
                                groups=mapping_chunk,
                                timeout=min(
                                    int(settings["translation_api_timeout_seconds"]),
                                    300,
                                ),
                                thinking_mode=mapping_thinking,
                                reasoning_effort=args.mapping_reasoning_effort,
                                max_tokens=args.mapping_max_tokens,
                                temperature=args.mapping_temperature,
                                request_attempts=int(settings.get("http_retry_attempts", 2)),
                            )
                            progress.event("T2", "response", block_id=f"t{number:04d}")
                            if assembly_result.get("schema_version") != T2_ASSEMBLY_SCHEMA:
                                raise Stage2Error("T2 必须返回 translation-assembly.v3")
                            materialized = stage2_result_from_assembly(
                                assembly_result, mapping_chunk
                            )
                            validate_translation(
                                materialized,
                                chunk,
                                target_baseline_punctuation=bottom_baseline,
                                target_raised_punctuation=bottom_raised,
                                top_baseline_punctuation=top_baseline,
                                top_raised_punctuation=top_raised,
                                display_order=display_order,
                            )
                            write_json(assembly_checkpoint, assembly_result)
                            result = materialized
                            call_info = {
                                **attempt_info,
                                "stage": "T2_atom_assembly",
                                "model": mapping_model,
                                "upstream_translation_model": translation_model,
                                "contract_retry_count": t2_attempt,
                                "contract_errors": t2_errors,
                            }
                            break
                        except (OSError, ValueError, Stage2Error) as t2_exc:
                            t2_errors.append(str(t2_exc))
                    else:
                        raise Stage2Error(
                            "T2 严格原子组装重试后仍失败：" + t2_errors[-1]
                        )
                except (OSError, ValueError, Stage2Error) as exc:
                    if not t1_accepted:
                        progress.event(
                            "T1",
                            "failed",
                            block_id=f"t{number:04d}",
                            detail={"error": str(exc)},
                        )
                    progress.event(
                        "T2",
                        "failed",
                        block_id=f"t{number:04d}",
                        detail={"error": str(exc)},
                    )
                    raise Stage2Error(str(exc)) from exc
                save_result(result)
                write_json(telemetry_path, call_info)
            # V2 ends the model contract here. T2 may only assemble authoritative
            # T1 atom IDs; it must never enter the legacy path that asks a model
            # to rewrite translated surface text. Display-length findings are
            # reported to the editor, where the user can decide how to resolve
            # them without silently changing translation content.
            targets = validate_translation(
                result,
                chunk,
                target_baseline_punctuation=bottom_baseline,
                target_raised_punctuation=bottom_raised,
                top_baseline_punctuation=top_baseline,
                top_raised_punctuation=top_raised,
                display_order=display_order,
            )
            overlength_groups = groups_over_hard_limits(result, chunk)
            if overlength_groups:
                call_info = {
                    **call_info,
                    "review_required": True,
                    "overlength_group_ids": [
                        str(group["group_id"]) for group in overlength_groups
                    ],
                }
                write_json(telemetry_path, call_info)
            progress.event("T2", "accepted", block_id=f"t{number:04d}")
            return number, chunk, result, targets, call_info

            # Legacy repair implementation retained below only until the V2
            # file is physically split; the unconditional return above makes it
            # unreachable and prevents any surface-text rewrite or fallback.
            attempts = 0
            while True:
                try:
                    targets = validate_translation(
                        result,
                        chunk,
                        target_baseline_punctuation=bottom_baseline,
                        target_raised_punctuation=bottom_raised,
                        top_baseline_punctuation=top_baseline,
                        top_raised_punctuation=top_raised,
                        display_order=display_order,
                    )
                    break
                except Stage2Error as exc:
                    if attempts >= repair_budget:
                        valid_groups, invalid_groups = (
                            valid_groups_and_invalid_sources(result, chunk)
                        )
                        isolated_prompt = "\n\n".join(
                            [
                                mapping_prompt,
                                "# ISOLATED VALIDATION REPAIR",
                                f"大块中只有下列 {len(invalid_groups)} 个组未通过：{exc}",
                                "只返回本次输入的这些组。逐 cue 核对目标语言、非空、ID、"
                                "上下标点和硬长度；不得返回其他组。",
                            ]
                        )
                        isolated_info: dict[str, Any] = {}
                        try:
                            if not invalid_groups:
                                raise Stage2Error("无法定位不合格翻译组")
                            isolated_result, isolated_info = call_translation_model(
                                base_url=str(settings["translation_api_base_url"]),
                                api_key=api_key,
                                model=mapping_model,
                                system_prompt=isolated_prompt,
                                groups=invalid_groups,
                                timeout=min(
                                    int(settings["translation_api_timeout_seconds"]),
                                    300,
                                ),
                                thinking_mode="disabled",
                                reasoning_effort=str(
                                    settings["translation_reasoning_effort"]
                                ),
                                request_attempts=int(
                                    settings.get("http_retry_attempts", 2)
                                ),
                            )
                            validate_translation(
                                isolated_result,
                                invalid_groups,
                                target_baseline_punctuation=bottom_baseline,
                                target_raised_punctuation=bottom_raised,
                                top_baseline_punctuation=top_baseline,
                                top_raised_punctuation=top_raised,
                                display_order=display_order,
                            )
                            repaired_by_id = {
                                str(item["group_id"]): item
                                for item in isolated_result["groups"]
                            }
                            fallback_cue_ids: list[int] = []
                        except (OSError, ValueError, Stage2Error) as isolated_exc:
                            fallback_result = delivery_fallback(
                                invalid_groups,
                                str(isolated_exc),
                            )
                            repaired_by_id = {
                                str(item["group_id"]): item
                                for item in fallback_result["groups"]
                            }
                            fallback_cue_ids = [
                                int(cue["cue_id"])
                                for group in invalid_groups
                                for cue in group["cues"]
                            ]
                            isolated_info = {
                                **isolated_info,
                                "fallback": "isolated_delivery",
                                "error": str(isolated_exc),
                            }
                        result = {
                            "schema_version": "substar.stage2.translation.v1",
                            "groups": [
                                valid_groups.get(str(group["group_id"]))
                                or repaired_by_id[str(group["group_id"])]
                                for group in chunk
                            ],
                            "terminology": result.get("terminology", []),
                        }
                        call_info = {
                            **isolated_info,
                            "isolated_repair": True,
                            "isolated_group_count": len(invalid_groups),
                            "initial_validation_error": str(exc),
                            "fallback_cue_ids": fallback_cue_ids,
                        }
                        targets = validate_translation(
                            result,
                            chunk,
                            target_baseline_punctuation=bottom_baseline,
                            target_raised_punctuation=bottom_raised,
                            top_baseline_punctuation=top_baseline,
                            top_raised_punctuation=top_raised,
                            display_order=display_order,
                        )
                        save_result(result)
                        write_json(telemetry_path, call_info)
                        break
                    attempts += 1
                    expected = [
                        {
                            "group_id": group["group_id"],
                            "cue_ids": [int(item["cue_id"]) for item in group["cues"]],
                        }
                        for group in chunk
                    ]
                    print(
                        f"translation_chunk={number}/{len(chunks)} "
                        f"repair={attempts} error={exc}",
                        flush=True,
                    )
                    progress.event(
                        "T2",
                        "retry",
                        block_id=f"t{number:04d}",
                        detail={"validation_error": str(exc)},
                    )
                    progress.event("T2", "sent", block_id=f"t{number:04d}")
                    repair_prompt = "\n\n".join(
                        [
                            prompt,
                            "# MANDATORY VALIDATION REPAIR",
                            f"上一版未通过程序验收：{exc}",
                            "必须完整重做本块。groups 和 targets 必须严格按下列清单返回；"
                            "不得遗漏、增加、重复或调序任何 ID：",
                            "还必须逐 cue 遵守输入中的 target_language。"
                            "target_language=zh-CN 不得返回英文润色稿；"
                            "target_language=en 不得返回中文原文。",
                            json.dumps(expected, ensure_ascii=False),
                        ]
                    )
                    try:
                        result, repair_info = call_translation_model(
                            base_url=str(settings["translation_api_base_url"]),
                            api_key=api_key,
                            model=mapping_model,
                            system_prompt=repair_prompt,
                            groups=chunk,
                            timeout=min(
                                int(settings["translation_api_timeout_seconds"]),
                                300,
                            ),
                            thinking_mode="disabled",
                            reasoning_effort=str(settings["translation_reasoning_effort"]),
                            request_attempts=int(settings.get("http_retry_attempts", 2)),
                        )
                        progress.event("T2", "response", block_id=f"t{number:04d}")
                    except (OSError, ValueError, Stage2Error) as repair_exc:
                        result = delivery_fallback(chunk, str(repair_exc))
                        repair_info = {
                            "fallback": "delivery",
                            "error": str(repair_exc),
                            "fallback_cue_ids": [
                                int(cue["cue_id"])
                                for group in chunk
                                for cue in group["cues"]
                            ],
                        }
                    call_info = {
                        **repair_info,
                        "repair_attempt": attempts,
                        "initial_validation_error": str(exc),
                    }
                    save_result(result)
                    write_json(telemetry_path, call_info)
            length_repairs = 0
            while True:
                invalid_groups = groups_over_hard_limits(result, chunk)
                if not invalid_groups:
                    break
                if length_repairs >= repair_budget:
                    invalid_ids = {group["group_id"] for group in invalid_groups}
                    current_by_id = {
                        str(group["group_id"]): group for group in result["groups"]
                    }
                    final_length_prompt = "\n\n".join(
                        [
                            mapping_prompt,
                            "# ISOLATED HARD-LENGTH FINALIZATION",
                            "这是最后一次、且只含仍超限的小组。必须用更简洁自然的译法"
                            "完整保留核心信息，不能返回失败说明或空文本。",
                            f"英文不超过 {english_hard} 个显示字符；"
                            f"中文不超过 {chinese_hard} 个显示字符；"
                            f"中英混合不超过 {mixed_hard} 个显示字符；"
                            f"日文不超过 {japanese_hard} 个显示字符；"
                            f"韩文不超过 {korean_hard} 个显示字符；"
                            f"视觉宽度不超过 {visual_hard} 列。",
                            "输出前逐 target 计数，只返回严格 JSON。",
                        ]
                    )
                    final_info: dict[str, Any] = {}
                    fallback_cue_ids: list[int] = []
                    try:
                        final_result, final_info = call_translation_model(
                            base_url=str(settings["translation_api_base_url"]),
                            api_key=api_key,
                            model=mapping_model,
                            system_prompt=final_length_prompt,
                            groups=invalid_groups,
                            timeout=min(
                                int(settings["translation_api_timeout_seconds"]),
                                300,
                            ),
                            thinking_mode="disabled",
                            reasoning_effort=str(
                                settings["translation_reasoning_effort"]
                            ),
                            request_attempts=int(
                                settings.get("http_retry_attempts", 2)
                            ),
                        )
                        progress.event("T2", "response", block_id=f"t{number:04d}")
                        validate_translation(
                            final_result,
                            invalid_groups,
                            target_baseline_punctuation=bottom_baseline,
                            target_raised_punctuation=bottom_raised,
                            top_baseline_punctuation=top_baseline,
                            top_raised_punctuation=top_raised,
                            display_order=display_order,
                        )
                        if groups_over_hard_limits(final_result, invalid_groups):
                            raise Stage2Error("隔离硬长度终稿仍然超限")
                        fallback = {
                            group["group_id"]: group
                            for group in final_result["groups"]
                        }
                    except (OSError, ValueError, Stage2Error) as final_exc:
                        # Formatting repair is subordinate to content
                        # preservation. If the bounded compression attempt
                        # fails, retain the already valid translation and let
                        # the delivery gate report its length issue. Never
                        # replace usable content with a failure placeholder.
                        fallback = {
                            str(group["group_id"]): current_by_id[
                                str(group["group_id"])
                            ]
                            for group in invalid_groups
                        }
                        final_info = {
                            **final_info,
                            "fallback": "retain_valid_overlength_translation",
                            "error": str(final_exc),
                        }
                    result["groups"] = [
                        fallback.get(group["group_id"], group)
                        if group["group_id"] in invalid_ids
                        else group
                        for group in result["groups"]
                    ]
                    targets = validate_translation(
                        result,
                        chunk,
                        target_baseline_punctuation=bottom_baseline,
                        target_raised_punctuation=bottom_raised,
                        top_baseline_punctuation=top_baseline,
                        top_raised_punctuation=top_raised,
                        display_order=display_order,
                    )
                    call_info = {
                        **final_info,
                        "isolated_hard_length": True,
                        "fallback_group_ids": sorted(invalid_ids),
                        "fallback_cue_ids": fallback_cue_ids,
                    }
                    save_result(result)
                    write_json(telemetry_path, call_info)
                    break
                length_repairs += 1
                print(
                    f"translation_chunk={number}/{len(chunks)} "
                    f"length_repair={length_repairs} groups={len(invalid_groups)}",
                    flush=True,
                )
                length_prompt = "\n\n".join(
                    [
                        mapping_prompt,
                        "# MANDATORY HARD-LENGTH REPAIR",
                        "这些组的上一版目标字幕超过产品硬上限。必须重新翻译并映射："
                        f"英文最多 {english_hard} 个显示字符，"
                        f"中文最多 {chinese_hard} 个显示字符，"
                        f"中英混合最多 {mixed_hard} 个显示字符，"
                        f"日文最多 {japanese_hard} 个显示字符，"
                        f"韩文最多 {korean_hard} 个显示字符，"
                        f"任何目标字幕视觉宽度最多 {visual_hard} 列（汉字按2列、拉丁字符按1列）。",
                        "可将过长 N:1 改成自然 N:M，把信息按语义分配给不同 cue；"
                        "禁止按字数比例机械拆分，禁止漏掉信息。",
                        "提交前逐条实际计数，任何 target 超限都不得输出。",
                    ]
                )
                try:
                    progress.event(
                        "T2",
                        "retry",
                        block_id=f"t{number:04d}",
                        detail={"length_error_groups": len(invalid_groups)},
                    )
                    progress.event("T2", "sent", block_id=f"t{number:04d}")
                    repaired_result, repair_info = call_translation_model(
                        base_url=str(settings["translation_api_base_url"]),
                        api_key=api_key,
                        model=mapping_model,
                        system_prompt=length_prompt,
                        groups=invalid_groups,
                        timeout=min(
                            int(settings["translation_api_timeout_seconds"]),
                            300,
                        ),
                        thinking_mode="disabled",
                        reasoning_effort=str(settings["translation_reasoning_effort"]),
                        request_attempts=int(settings.get("http_retry_attempts", 2)),
                    )
                    progress.event("T2", "response", block_id=f"t{number:04d}")
                except (OSError, ValueError, Stage2Error) as repair_exc:
                    invalid_ids = {
                        str(group["group_id"]) for group in invalid_groups
                    }
                    repaired_result = {
                        "schema_version": "substar.stage2.translation.v1",
                        "groups": [
                            group
                            for group in result["groups"]
                            if str(group["group_id"]) in invalid_ids
                        ],
                        "terminology": [],
                    }
                    repair_info = {
                        "fallback": "retain_valid_overlength_translation",
                        "error": str(repair_exc),
                    }
                try:
                    repaired_targets = validate_translation(
                        repaired_result,
                        invalid_groups,
                        target_baseline_punctuation=bottom_baseline,
                        target_raised_punctuation=bottom_raised,
                        top_baseline_punctuation=top_baseline,
                        top_raised_punctuation=top_raised,
                        display_order=display_order,
                    )
                except Stage2Error as validation_exc:
                    # Keep the valid pre-repair translation if compression
                    # output is malformed. Content loss is worse than a
                    # bounded, explicitly reported display-length violation.
                    invalid_ids = {
                        str(group["group_id"]) for group in invalid_groups
                    }
                    repaired_result = {
                        "schema_version": "substar.stage2.translation.v1",
                        "groups": [
                            group
                            for group in result["groups"]
                            if str(group["group_id"]) in invalid_ids
                        ],
                        "terminology": [],
                    }
                    repaired_targets = validate_translation(
                        repaired_result,
                        invalid_groups,
                        target_baseline_punctuation=bottom_baseline,
                        target_raised_punctuation=bottom_raised,
                        top_baseline_punctuation=top_baseline,
                        top_raised_punctuation=top_raised,
                        display_order=display_order,
                    )
                    repair_info = {
                        **repair_info,
                        "fallback": "retain_valid_overlength_translation",
                        "error": str(validation_exc),
                    }
                repaired_by_id = {
                    str(group["group_id"]): group for group in repaired_result["groups"]
                }
                result["groups"] = [
                    repaired_by_id.get(str(group["group_id"]), group)
                    for group in result["groups"]
                ]
                targets.update(repaired_targets)
                call_info = {
                    **repair_info,
                    "length_repair_attempt": length_repairs,
                    "length_repair_group_count": len(invalid_groups),
                }
                save_result(result)
                write_json(telemetry_path, call_info)
            progress.event("T2", "accepted", block_id=f"t{number:04d}")
            return number, chunk, result, targets, call_info

        completed: dict[
            int,
            tuple[
                list[dict[str, Any]],
                dict[str, Any],
                dict[int, str],
                dict[str, Any],
            ],
        ] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(process_chunk, number, chunk)
                for number, chunk in enumerate(chunks, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                number, chunk, result, targets, call_info = future.result()
                completed[number] = (chunk, result, targets, call_info)

        for number in range(1, len(chunks) + 1):
            chunk, result, targets, call_info = completed[number]
            all_targets.update(targets)
            telemetry.append({"chunk": number, **call_info})
            relation_groups.extend(
                {
                    "source_cue_ids": [int(item["cue_id"]) for item in source["cues"]],
                    "relation": translated.get("relation", "N:N"),
                    "strategy": translated.get("strategy", "direct"),
                    "target_mappings": [
                        {
                            "cue_id": int(target["cue_id"]),
                            "source_cue_ids": [
                                int(value)
                                for value in target.get(
                                    "source_cue_ids", [target["cue_id"]]
                                )
                            ],
                        }
                        for target in translated.get("targets", [])
                    ],
                    "target_unit_count": len(
                        {targets[int(item["cue_id"])] for item in source["cues"]}
                    ),
                }
                for source, translated in zip(chunk, result["groups"])
            )
            terminology.extend(result.get("terminology", []))
        progress.finish("T1")
        progress.finish("T2")
        for cue in cues:
            cue.target = all_targets[cue.cue_id]
        translation_revision_id = persist_translation_revision_v2(
            project_store,
            expected_revision_id=base_revision.revision_id,
            targets=all_targets,
            domain_cue_ids=domain_cue_ids,
        )
        write_json(
            args.output_dir / "translation_revision_v2.json",
            {
                "schema_version": "substar.translation-revision-pointer.v2",
                "source_revision_id": base_revision.revision_id,
                "translation_revision_id": translation_revision_id,
            },
        )
        srt = render_srt(cues, display_order=display_order)
        (args.output_dir / "substar_bilingual.srt").write_text(srt, encoding="utf-8-sig")
        report = validate_final(
            cues,
            source_baseline_punctuation=top_baseline,
            target_baseline_punctuation=bottom_baseline,
            target_language_mode=target_mode,
            display_order=display_order,
            english_hard_limit=english_hard,
            english_count_spaces=bool(settings["english_count_spaces"]),
            english_count_punctuation=bool(settings["english_count_punctuation"]),
            chinese_hard_limit=chinese_hard,
            mixed_hard_limit=mixed_hard,
            japanese_hard_limit=japanese_hard,
            korean_hard_limit=korean_hard,
            visual_width_limit=visual_hard,
            minimum_cue_duration_ms=int(settings["minimum_cue_duration_ms"]),
            maximum_cue_duration_ms=int(settings["maximum_cue_duration_ms"]),
            maximum_cps_latin=float(settings["maximum_cps_latin"]),
            maximum_cps_cjk=float(settings["maximum_cps_cjk"]),
        )
        fallback_cue_ids = sorted(
            {
                int(cue_id)
                for item in telemetry
                for cue_id in item.get("fallback_cue_ids", [])
            }
        )
        if fallback_cue_ids:
            report["review_items"].append(
                {
                    "cue_ids": fallback_cue_ids,
                    "type": "translation_fallback",
                    "reason": "翻译接口或硬校验失败 已生成可交付占位并要求人工复核",
                }
            )
            report["summary"]["review_required_count"] = len(
                report["review_items"]
            )
        report["relation_groups"] = relation_groups
        report["terminology"] = terminology
        write_json(args.output_dir / "translation_report.json", report)
        write_json(
            args.output_dir / "translation_api_usage.json",
            {
                "calls": telemetry,
                "duration_seconds": round(
                    sum(float(item.get("duration_seconds", 0)) for item in telemetry), 3
                ),
                "total_tokens": sum(
                    int(item.get("usage", {}).get("total_tokens", 0)) for item in telemetry
                ),
            },
        )
        print(
            f"complete cues={len(cues)} review={report['summary']['review_required_count']}"
        )
        return 0
    except (OSError, ValueError, KeyError, Stage2Error, ProjectStoreError) as exc:
        print(f"SUBSTAR_STAGE2_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
