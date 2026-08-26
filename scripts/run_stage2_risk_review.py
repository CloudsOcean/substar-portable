from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.config import load_settings  # noqa: E402
from substar_core.glossary import active_glossary, glossary_prompt  # noqa: E402
from substar_core.policy import track_lines  # noqa: E402
from substar_core.stage_settings import overlay_relay_profile  # noqa: E402
from substar_core.stage2 import (  # noqa: E402
    Stage2Error,
    build_cues,
    call_translation_model,
    classify_source_language,
)


ALLOWED_RISK = {"low", "medium", "high"}
ALLOWED_CATEGORY = {
    "mistranslation",
    "omission",
    "terminology",
    "asr_source",
    "allocation",
    "factual_overclaim",
    "readability",
    "other",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Stage2Error(f"{path.name} 顶层必须是对象")
    return value


def read_srt_targets(
    path: Path,
    cues: list[Any],
    *,
    display_order: str,
) -> dict[int, str]:
    targets: dict[int, str] = {}
    cue_by_id = {int(cue.cue_id): cue for cue in cues}
    body = path.read_text(encoding="utf-8-sig").strip()
    for block in re.split(r"\r?\n\s*\r?\n", body):
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
        targets[cue_id] = lines[3 if expected_top == cue.source else 2].strip()
    return targets


def validate_result(result: dict[str, Any], valid_cue_ids: set[int]) -> dict[str, Any]:
    if result.get("schema_version") != "substar.stage2.risk.v1":
        raise Stage2Error("T3 schema_version 错误")
    overall = str(result.get("overall_risk", ""))
    if overall not in ALLOWED_RISK:
        raise Stage2Error("T3 overall_risk 错误")
    issues = result.get("issues")
    if not isinstance(issues, list):
        raise Stage2Error("T3 issues 必须是数组")
    normalized: list[dict[str, Any]] = []
    for item in issues:
        if not isinstance(item, dict):
            raise Stage2Error("T3 issue 必须是对象")
        cue_ids = item.get("cue_ids")
        if not isinstance(cue_ids, list):
            raise Stage2Error("T3 cue_ids 必须是数组")
        parsed_ids = [int(cue_id) for cue_id in cue_ids]
        if any(cue_id not in valid_cue_ids for cue_id in parsed_ids):
            raise Stage2Error("T3 引用了不存在的 cue_id")
        category = str(item.get("category", ""))
        severity = str(item.get("severity", ""))
        reason = str(item.get("reason", "")).strip()
        if category not in ALLOWED_CATEGORY or severity not in ALLOWED_RISK or not reason:
            raise Stage2Error("T3 issue 字段错误")
        normalized.append(
            {
                "cue_ids": parsed_ids,
                "category": category,
                "severity": severity,
                "reason": reason,
            }
        )
    return {
        "schema_version": "substar.stage2.risk.v1",
        "overall_risk": overall,
        "issues": normalized,
        "summary": str(result.get("summary", "")).strip(),
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Substar T3 译文风险只读审阅")
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--stage1-dir", required=True, type=Path)
    parser.add_argument("--input-srt", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model")
    parser.add_argument("--thinking-mode", choices=["enabled", "disabled"])
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    try:
        settings = overlay_relay_profile(load_settings(include_secret=True), args.stage1_dir)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        draft = (args.stage1_dir / "stage03A_source_draft.txt").read_text(
            encoding="utf-8"
        )
        plan = read_json(args.stage1_dir / "stage1_direct_plan.json")
        alignment = read_json(args.job_dir / "alignment.json")
        display_order = str(settings.get("display_order", "en_zh"))
        cues, groups = build_cues(
            draft,
            plan,
            alignment,
            source_baseline_punctuation=str(
                settings.get("top_baseline_punctuation", "preserve")
            ),
            source_raised_punctuation=str(
                settings.get("top_raised_punctuation", "preserve")
            ),
            bottom_baseline_punctuation=str(
                settings.get("bottom_baseline_punctuation", "normalize")
            ),
            bottom_raised_punctuation=str(
                settings.get("bottom_raised_punctuation", "preserve")
            ),
            display_order=display_order,
            tail_padding_ms=int(settings.get("tail_padding_ms", 120)),
            snap_threshold_ms=int(settings.get("snap_threshold_ms", 500)),
        )
        targets = read_srt_targets(args.input_srt, cues, display_order=display_order)
        if set(targets) != {cue.cue_id for cue in cues}:
            raise Stage2Error("输入 SRT cue_id 与当前 Stage1 不一致")
        for group in groups:
            for item in group["cues"]:
                item["current_target"] = targets[int(item["cue_id"])]

        output_path = args.output_dir / "translation_risk_report.json"
        api_key = str(settings.get("translation_api_key", ""))
        if args.offline or not api_key:
            report = {
                "schema_version": "substar.stage2.risk.v1",
                "overall_risk": "medium",
                "issues": [],
                "summary": (
                    "T3 未执行 离线模式"
                    if args.offline
                    else "T3 未执行 翻译 API 密钥未设置"
                ),
                "read_only": True,
                "skipped": True,
            }
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("complete risk_skipped", flush=True)
            return 0

        base_prompt = (
            PROJECT_ROOT / "prompts" / "T3_译文风险只读审阅_JSON.md"
        ).read_text(encoding="utf-8")
        context_path = args.job_dir / "stage03T_translation_context.json"
        if context_path.exists():
            context = read_json(context_path)
            terms = [
                item
                for item in context.get("matched_glossary", [])
                if isinstance(item, dict)
            ]
        else:
            terms = active_glossary(str(settings.get("project_name", "")))
        prompt = "\n\n".join(
            [
                base_prompt,
                "# ACTIVE_OUTPUT_PROFILE",
                f"翻译风格：{settings['translation_style']}",
                f"显示顺序：{display_order}",
                glossary_prompt(terms),
            ]
        )
        result, telemetry = call_translation_model(
            base_url=str(settings["translation_api_base_url"]),
            api_key=api_key,
            model=str(args.model or settings["translation_api_model"]),
            system_prompt=prompt,
            groups=groups,
            timeout=min(int(settings["translation_api_timeout_seconds"]), 600),
            thinking_mode=args.thinking_mode
            or str(settings.get("translation_thinking_mode", "enabled")),
            reasoning_effort=str(settings["translation_reasoning_effort"]),
            request_attempts=int(settings.get("http_retry_attempts", 2)),
        )
        report = validate_result(result, {int(cue.cue_id) for cue in cues})
        report["telemetry"] = telemetry
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"complete risk={report['overall_risk']} issues={len(report['issues'])}",
            flush=True,
        )
        return 0
    except (OSError, ValueError, KeyError, Stage2Error) as exc:
        print(f"SUBSTAR_STAGE2_RISK_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
