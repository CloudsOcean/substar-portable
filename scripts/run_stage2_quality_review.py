from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
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
from substar_core.policy import SubtitlePolicy, track_lines  # noqa: E402
from substar_core.stage1 import display_normalize  # noqa: E402
from substar_core.stage_settings import overlay_relay_profile  # noqa: E402
from substar_core.stage2 import (  # noqa: E402
    Stage2Error,
    build_cues,
    call_translation_model,
    chunk_groups,
    classify_source_language,
    render_srt,
    subtitle_visual_width,
    target_language_for,
    validate_final,
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Stage2Error(f"{path.name} 顶层必须是对象")
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def chunk_by_execution_blocks(
    groups: list[dict[str, Any]], blocks: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
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
        claimed.update(str(group.get("group_id")) for group in members)
        if members:
            chunks.append(members)
    expected = {str(group.get("group_id")) for group in groups}
    if claimed != expected:
        raise Stage2Error("T3 润色组越过统一执行块边界")
    return chunks


def read_srt_targets(
    path: Path,
    cues: list[Any],
    *,
    display_order: str,
) -> dict[int, str]:
    targets: dict[int, str] = {}
    cue_by_id = {int(cue.cue_id): cue for cue in cues}
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = block.splitlines()
        if len(lines) < 4:
            continue
        cue_id = int(lines[0])
        cue = cue_by_id.get(cue_id)
        if cue is None:
            continue
        expected_top, _ = track_lines(
            source=cue.source,
            target="__TARGET__",
            display_order=display_order,
            source_language=classify_source_language(cue.source),
        )
        source_is_top = expected_top == cue.source
        targets[cue_id] = lines[3 if source_is_top else 2].strip()
    return targets


def valid_target(
    source: str,
    text: str,
    *,
    baseline_punctuation: str = "normalize",
    raised_punctuation: str = "preserve",
    chinese_hard_limit: int = 24,
    mixed_hard_limit: int = 25,
    english_hard_limit: int = 55,
    japanese_hard_limit: int = 25,
    korean_hard_limit: int = 32,
    english_count_spaces: bool = True,
    english_count_punctuation: bool = True,
    visual_width_limit: int = 48,
    target_language_mode: str = "auto_opposite",
) -> tuple[bool, str]:
    value = display_normalize(
        text,
        baseline_punctuation=baseline_punctuation,
        raised_punctuation=raised_punctuation,
    )
    if not value:
        return False, "空译文"
    if subtitle_visual_width(value) > visual_width_limit:
        return False, f"视觉宽度超过{visual_width_limit}列"
    target_language = (
        target_language_mode
        if target_language_mode in {"zh-CN", "en", "ja", "ko"}
        else target_language_for(classify_source_language(source))
    )
    kana = len(re.findall(r"[\u3040-\u30ff]", value))
    hangul = len(re.findall(r"[\uac00-\ud7af]", value))
    if target_language == "ja" and kana == 0:
        return False, "target must be Japanese"
    if target_language == "ko" and hangul == 0:
        return False, "target must be Korean"
    han = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if target_language == "zh-CN" and han == 0:
        return False, "目标应为简体中文"
    if target_language == "en" and latin == 0:
        return False, "目标应为英文"
    policy = SubtitlePolicy(
        english_hard_limit=english_hard_limit,
        english_count_spaces=english_count_spaces,
        english_count_punctuation=english_count_punctuation,
        chinese_hard_limit=chinese_hard_limit,
        mixed_hard_limit=mixed_hard_limit,
        japanese_hard_limit=japanese_hard_limit,
        korean_hard_limit=korean_hard_limit,
        target_visual_width_limit=visual_width_limit,
    )
    if policy.line_length(value) > policy.hard_limit(value):
        return False, f"目标字幕超过{policy.hard_limit(value)}字符"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Substar T2 译文润色")
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--stage1-dir", required=True, type=Path)
    parser.add_argument("--input-srt", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model")
    parser.add_argument("--workers", type=int, default=64, choices=range(1, 257))
    parser.add_argument("--thinking-mode", choices=["enabled", "disabled"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--whole-program",
        action="store_true",
        help="把完整节目作为一个润色上下文提交，不进行语义分块",
    )
    args = parser.parse_args()

    try:
        settings = overlay_relay_profile(load_settings(include_secret=True), args.stage1_dir)
        api_key = str(settings.get("translation_api_key", ""))
        args.output_dir.mkdir(parents=True, exist_ok=True)
        draft = (args.stage1_dir / "stage03A_source_draft.txt").read_text(encoding="utf-8")
        plan = read_json(args.stage1_dir / "stage1_direct_plan.json")
        alignment = read_json(args.job_dir / "alignment.json")
        top_baseline = str(settings.get("top_baseline_punctuation", "preserve"))
        top_raised = str(settings.get("top_raised_punctuation", "preserve"))
        bottom_baseline = str(settings.get("bottom_baseline_punctuation", "normalize"))
        bottom_raised = str(settings.get("bottom_raised_punctuation", "preserve"))
        display_order = str(settings.get("display_order", "en_zh"))
        policy = SubtitlePolicy.from_settings(settings)
        cues, groups = build_cues(
            draft,
            plan,
            alignment,
            source_baseline_punctuation=top_baseline,
            source_raised_punctuation=top_raised,
            bottom_baseline_punctuation=bottom_baseline,
            bottom_raised_punctuation=bottom_raised,
            display_order=display_order,
            tail_padding_ms=int(settings.get("tail_padding_ms", 120)),
        )
        current_targets = read_srt_targets(
            args.input_srt,
            cues,
            display_order=display_order,
        )
        if set(current_targets) != {cue.cue_id for cue in cues}:
            raise Stage2Error("输入 SRT cue_id 与当前 Stage1 不一致")
        cue_by_id = {cue.cue_id: cue for cue in cues}
        for cue in cues:
            cue.target = current_targets[cue.cue_id]
        for group in groups:
            for item in group["cues"]:
                cue_id = int(item["cue_id"])
                item["current_target"] = current_targets[cue_id]
        relation_path = args.input_srt.parent / "translation_report.json"
        relation_by_cues: dict[tuple[int, ...], str] = {}
        if relation_path.exists():
            relation_report = read_json(relation_path)
            for item in relation_report.get("relation_groups", []):
                if not isinstance(item, dict):
                    continue
                cue_ids = item.get("source_cue_ids")
                if not isinstance(cue_ids, list):
                    continue
                try:
                    key = tuple(int(cue_id) for cue_id in cue_ids)
                except (TypeError, ValueError):
                    continue
                relation_by_cues[key] = str(item.get("relation", ""))
        for group in groups:
            cue_ids = tuple(int(item["cue_id"]) for item in group["cues"])
            group["current_relation"] = relation_by_cues.get(cue_ids, "unknown")

        base_prompt = (PROJECT_ROOT / "prompts" / "03C_译文质量复审_JSON.md").read_text(
            encoding="utf-8"
        )
        company_translation_examples = (
            PROJECT_ROOT / "prompts" / "references" / "构造翻译案例库.md"
        ).read_text(encoding="utf-8")
        translation_model = str(
            args.model or settings["translation_api_model"]
        )
        context_path = args.job_dir / "stage03T_translation_context.json"
        if context_path.exists():
            context = read_json(context_path)
            matched_terms = [
                item
                for item in context.get("matched_glossary", [])
                if isinstance(item, dict)
            ]
        else:
            matched_terms = active_glossary(str(settings.get("project_name", "")))
        prompt = "\n\n".join(
            [
                base_prompt,
                company_translation_examples,
                "# ACTIVE_OUTPUT_PROFILE",
                f"翻译风格：{settings['translation_style']}",
                f"文件级显示顺序：{display_order}",
                f"最终上行上/下标点：{top_raised}/{top_baseline}",
                f"最终下行上/下标点：{bottom_raised}/{bottom_baseline}",
                f"英文硬上限：{settings['english_hard_limit']}",
                f"中文硬上限：{settings['chinese_hard_limit']}",
                f"中英混合硬上限：{settings.get('mixed_hard_limit', 25)}",
                f"日文硬上限：{settings['japanese_hard_limit']}",
                f"韩文硬上限：{settings['korean_hard_limit']}",
                f"视觉宽度硬上限：{settings['target_visual_width_limit']}",
                glossary_prompt(matched_terms),
            ]
        )
        qa_groups = [
            group
            for group in groups
            if not any(
                "翻译失败" in str(item.get("current_target", ""))
                or "Translation failed" in str(item.get("current_target", ""))
                for item in group["cues"]
            )
        ]
        execution_path = args.stage1_dir / "execution_blocks.json"
        if not execution_path.is_file() and not args.whole_program:
            raise Stage2Error("T3 缺少统一执行块 execution_blocks.json")
        execution = read_json(execution_path) if execution_path.is_file() else {}
        blocks = list(execution.get("blocks") or [])
        chunks = [qa_groups] if args.whole_program else chunk_by_execution_blocks(qa_groups, blocks)
        checkpoint_dir = args.output_dir / "qa_chunks"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        thinking = args.thinking_mode or str(
            settings.get("translation_thinking_mode", "enabled")
        )
        if not api_key or args.offline:
            output_srt = args.output_dir / "substar_bilingual_polished.srt"
            output_srt.write_text(
                render_srt(cues, display_order=display_order),
                encoding="utf-8-sig",
            )
            (args.output_dir / "substar_bilingual_reviewed.srt").write_text(
                output_srt.read_text(encoding="utf-8-sig"),
                encoding="utf-8-sig",
            )
            report = validate_final(
                cues,
                source_baseline_punctuation=top_baseline,
                target_baseline_punctuation=bottom_baseline,
                target_language_mode=str(
                    settings.get("target_language_mode", "auto_opposite")
                ),
                display_order=display_order,
                english_hard_limit=int(settings["english_hard_limit"]),
                english_count_spaces=bool(settings["english_count_spaces"]),
                english_count_punctuation=bool(settings["english_count_punctuation"]),
                chinese_hard_limit=int(settings["chinese_hard_limit"]),
                mixed_hard_limit=int(settings.get("mixed_hard_limit", 25)),
                japanese_hard_limit=int(settings["japanese_hard_limit"]),
                korean_hard_limit=int(settings["korean_hard_limit"]),
                visual_width_limit=int(settings["target_visual_width_limit"]),
                minimum_cue_duration_ms=int(settings["minimum_cue_duration_ms"]),
                maximum_cue_duration_ms=int(settings["maximum_cue_duration_ms"]),
                maximum_cps_latin=float(settings["maximum_cps_latin"]),
                maximum_cps_cjk=float(settings["maximum_cps_cjk"]),
            )
            report["qa_skipped"] = (
                "离线模式"
                if args.offline
                else "翻译 API 密钥未设置"
            )
            report["qa_applied"] = []
            report["qa_rejected"] = []
            report["qa_review_items"] = [
                {
                    "cue_ids": [],
                    "reason": (
                        "03C 未执行 离线模式"
                        if args.offline
                        else "03C 未执行 翻译 API 密钥未设置"
                    ),
                }
            ]
            write_json(args.output_dir / "translation_polish_report.json", report)
            write_json(args.output_dir / "quality_review_report.json", report)
            print("complete qa_skipped=no_api_key", flush=True)
            return 0

        def process(number: int, chunk: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
            checkpoint = checkpoint_dir / f"chunk_{number:04d}.json"
            fingerprint = stable_fingerprint(
                {
                    "stage": "stage3_quality",
                    "chunk": chunk,
                    "current_targets": {
                        int(cue["cue_id"]): cue.get("current_target", "")
                        for group in chunk
                        for cue in group["cues"]
                    },
                    "prompt": prompt,
                    "model": translation_model,
                    "base_url": settings["translation_api_base_url"],
                    "thinking": thinking,
                    "policy": policy.to_dict(),
                }
            )
            resumed = (
                read_checkpoint(
                    checkpoint,
                    stage="stage3_quality",
                    fingerprint=fingerprint,
                )
                if args.resume
                else None
            )
            if resumed is not None:
                result = resumed
            else:
                try:
                    result, telemetry = call_translation_model(
                        base_url=str(settings["translation_api_base_url"]),
                        api_key=api_key,
                        model=translation_model,
                        system_prompt=prompt,
                        groups=chunk,
                        timeout=min(
                            int(settings["translation_api_timeout_seconds"]),
                            300,
                        ),
                        thinking_mode=thinking,
                        reasoning_effort=str(settings["translation_reasoning_effort"]),
                        max_tokens=131072,
                        request_attempts=int(settings.get("http_retry_attempts", 2)),
                    )
                except (OSError, ValueError, Stage2Error) as exc:
                    result = {
                        "schema_version": "substar.stage2.qa.v1",
                        "corrections": [],
                        "review_items": [
                            {
                                "cue_ids": [
                                    int(cue["cue_id"])
                                    for group in chunk
                                    for cue in group["cues"]
                                ],
                                "reason": f"03C 请求失败 已保留03B译文：{exc}",
                            }
                        ],
                    }
                    telemetry = {"fallback": "keep_stage2", "error": str(exc)}
                write_checkpoint(
                    checkpoint,
                    stage="stage3_quality",
                    fingerprint=fingerprint,
                    result=result,
                )
                write_json(checkpoint_dir / f"chunk_{number:04d}_api.json", telemetry)
            if (
                result.get("schema_version") is None
                and isinstance(result.get("corrections"), list)
                and isinstance(result.get("review_items"), list)
            ):
                # Some compatible models omit only the constant version field
                # while returning the exact requested body. Normalize that
                # harmless omission; never repair missing task content.
                result["schema_version"] = "substar.stage2.qa.v1"
                write_checkpoint(
                    checkpoint,
                    stage="stage3_quality",
                    fingerprint=fingerprint,
                    result=result,
                )
            if result.get("schema_version") != "substar.stage2.qa.v1":
                raise Stage2Error(f"03C 分块 {number} schema_version 错误")
            return number, result

        completed: dict[int, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(process, number, chunk)
                for number, chunk in enumerate(chunks, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                number, result = future.result()
                completed[number] = result
                print(f"qa_chunk={number}/{len(chunks)}", flush=True)

        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []
        proposals_by_group: dict[str, list[dict[str, Any]]] = {}
        for number in range(1, len(chunks) + 1):
            result = completed[number]
            review_items.extend(result.get("review_items", []))
            for correction in result.get("corrections", []):
                cue_id = int(correction.get("cue_id", -1))
                group_id = cue_by_id[cue_id].group_id if cue_id in cue_by_id else "__invalid__"
                proposals_by_group.setdefault(group_id, []).append(correction)

        seen: set[int] = set()
        for group_id, proposals in proposals_by_group.items():
            group_rejection = ""
            normalized: dict[int, tuple[str, dict[str, Any]]] = {}
            for correction in proposals:
                cue_id = int(correction.get("cue_id", -1))
                confidence = float(correction.get("confidence", 0))
                minimum_confidence = float(
                    settings.get("quality_review_confidence", 0.92)
                )
                if cue_id not in cue_by_id or cue_id in seen or confidence < minimum_confidence:
                    group_rejection = "同组存在ID重复/不存在或置信度不足"
                    break
                text = display_normalize(
                    str(correction.get("text", "")).strip(),
                    baseline_punctuation=bottom_baseline,
                    raised_punctuation=bottom_raised,
                )
                valid, reason = valid_target(
                    cue_by_id[cue_id].source,
                    text,
                    baseline_punctuation=bottom_baseline,
                    raised_punctuation=bottom_raised,
                    chinese_hard_limit=int(settings["chinese_hard_limit"]),
                    mixed_hard_limit=int(settings.get("mixed_hard_limit", 25)),
                    english_hard_limit=int(settings["english_hard_limit"]),
                    japanese_hard_limit=int(settings["japanese_hard_limit"]),
                    korean_hard_limit=int(settings["korean_hard_limit"]),
                    english_count_spaces=bool(settings["english_count_spaces"]),
                    english_count_punctuation=bool(settings["english_count_punctuation"]),
                    visual_width_limit=int(settings["target_visual_width_limit"]),
                    target_language_mode=str(
                        settings.get("target_language_mode", "auto_opposite")
                    ),
                )
                if not valid:
                    group_rejection = f"同组存在无效目标：{reason}"
                    break
                normalized[cue_id] = (text, correction)

            group_cues = [cue for cue in cues if cue.group_id == group_id]
            current_runs: dict[str, list[int]] = {}
            for cue in group_cues:
                current_runs.setdefault(cue.target, []).append(cue.cue_id)
            if not group_rejection:
                for current, cue_ids in current_runs.items():
                    if len(cue_ids) < 2 or not any(cue_id in normalized for cue_id in cue_ids):
                        continue
                    final_values = {
                        normalized.get(cue_id, (current, {}))[0]
                        for cue_id in cue_ids
                    }
                    if not all(cue_id in normalized for cue_id in cue_ids) or len(final_values) != 1:
                        group_rejection = "同组修正会破坏现有N:1共享完整译文"
                        break
            if not group_rejection and len(current_runs) > 1:
                final_values = {
                    normalized.get(cue.cue_id, (cue.target, {}))[0]
                    for cue in group_cues
                }
                source_group = next(
                    (
                        group
                        for group in groups
                        if str(group.get("group_id")) == group_id
                    ),
                    {},
                )
                if (
                    len(final_values) == 1
                    and str(source_group.get("current_relation")) != "N:1"
                ):
                    group_rejection = (
                        "03B关系不是N:1 禁止把原本不同的译文全部折叠为共享译文"
                    )

            if group_rejection:
                rejected.extend(
                    {**correction, "rejection": group_rejection}
                    for correction in proposals
                )
                continue

            for cue_id, (text, correction) in normalized.items():
                seen.add(cue_id)
                before = cue_by_id[cue_id].target
                if text == before:
                    continue
                cue_by_id[cue_id].target = text
                applied.append(
                    {
                        "cue_id": cue_id,
                        "before": before,
                        "after": text,
                        "confidence": float(correction.get("confidence", 0)),
                        "reason": correction.get("reason", ""),
                    }
                )

        output_srt = args.output_dir / "substar_bilingual_polished.srt"
        output_srt.write_text(
            render_srt(cues, display_order=display_order),
            encoding="utf-8-sig",
        )
        (args.output_dir / "substar_bilingual_reviewed.srt").write_text(
            output_srt.read_text(encoding="utf-8-sig"),
            encoding="utf-8-sig",
        )
        report = validate_final(
            cues,
            source_baseline_punctuation=top_baseline,
            target_baseline_punctuation=bottom_baseline,
            target_language_mode=str(
                settings.get("target_language_mode", "auto_opposite")
            ),
            display_order=display_order,
            english_hard_limit=int(settings["english_hard_limit"]),
            english_count_spaces=bool(settings["english_count_spaces"]),
            english_count_punctuation=bool(settings["english_count_punctuation"]),
            chinese_hard_limit=int(settings["chinese_hard_limit"]),
            mixed_hard_limit=int(settings.get("mixed_hard_limit", 25)),
            japanese_hard_limit=int(settings["japanese_hard_limit"]),
            korean_hard_limit=int(settings["korean_hard_limit"]),
            visual_width_limit=int(settings["target_visual_width_limit"]),
            minimum_cue_duration_ms=int(settings["minimum_cue_duration_ms"]),
            maximum_cue_duration_ms=int(settings["maximum_cue_duration_ms"]),
            maximum_cps_latin=float(settings["maximum_cps_latin"]),
            maximum_cps_cjk=float(settings["maximum_cps_cjk"]),
        )
        report["qa_applied"] = applied
        report["qa_rejected"] = rejected
        report["qa_review_items"] = review_items
        write_json(args.output_dir / "translation_polish_report.json", report)
        write_json(args.output_dir / "quality_review_report.json", report)
        print(
            f"complete cues={len(cues)} applied={len(applied)} "
            f"rejected={len(rejected)} review={len(review_items)}"
        )
        return 0
    except (OSError, ValueError, KeyError, Stage2Error) as exc:
        print(f"SUBSTAR_STAGE2_QA_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
