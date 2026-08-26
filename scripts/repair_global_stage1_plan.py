from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_global_planner_ab import call_streaming_model  # noqa: E402
from scripts.run_stage1_pipeline import (  # noqa: E402
    Stage1PipelineError,
    _direct_report,
    resolve_api_key,
    source_punctuation_kwargs,
    stage_system,
    write_json,
    write_two_level_artifacts,
)
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan  # noqa: E402


def normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Normalize schema noise only; never choose or move a semantic boundary."""

    if plan.get("schema_version") != "substar.stage1.direct.v1":
        raise Stage1PipelineError("03A-R schema_version 错误")
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        raise Stage1PipelineError("03A-R 未返回完整 groups")
    for number, group in enumerate(groups, start=1):
        group["group_id"] = f"g{number:04d}"
        start = group.get("alignment_start")
        end = group.get("alignment_end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise Stage1PipelineError(f"03A-R 第 {number} 组索引无效")
        group["line_breaks_after"] = sorted(
            {
                value
                for value in group.get("line_breaks_after", [])
                if isinstance(value, int) and start <= value < end
            }
        )
        group["alternative_breaks_after"] = sorted(
            {
                value
                for value in group.get("alternative_breaks_after", [])
                if isinstance(value, int) and start <= value < end
            }
        )
        group.setdefault("confidence", 0.8)
        group.setdefault("needs_review", False)
        group.setdefault("protected_spans", [])
        group.setdefault("deletions", [])
        group.setdefault("corrections", [])
        group.setdefault("reason", "Pro full-program targeted review")
    plan["coverage_check"] = {"complete": True, "ordered": True}
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用一次 Pro 全片定向复议修复 P2；只做 schema 归一化，不运行旧优化器"
    )
    parser.add_argument("material", type=Path)
    parser.add_argument("--initial-plan", required=True, type=Path)
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=128000)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "max", "xhigh"), default="max")
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8")
    initial_plan = json.loads(args.initial_plan.read_text(encoding="utf-8"))
    validation = json.loads(args.validation.read_text(encoding="utf-8"))
    review = json.loads(args.review.read_text(encoding="utf-8"))
    master = extract_master(material)
    units = extract_alignment(material)
    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise Stage1PipelineError("未找到 Substar LLM API 密钥")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    repaired, telemetry = call_streaming_model(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        system=stage_system("repair", material),
        user="\n\n".join(
            [
                "# INPUT_MATERIAL\n" + material,
                "# INITIAL_PLAN\n" + json.dumps(initial_plan, ensure_ascii=False),
                "# PROGRAM_ISSUES\n"
                + json.dumps(
                    {
                        "hard_validation": validation.get("plan_issues", []),
                        "review_notices": validation.get("review_notices", []),
                        "p3_review": review,
                    },
                    ensure_ascii=False,
                ),
                "# EXECUTION_SCOPE\n"
                "这是一次且仅一次的全片定向复议。必须修复全部硬错误；"
                "只调整问题组及直接相邻边界。不得调用或模仿全局均衡优化器，"
                "不得为了组间等长改动正常区域。请输出完整替换计划。",
            ]
        ),
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
    )
    repaired = normalize_plan(repaired)
    result = evaluate_direct_plan(
        master,
        units,
        repaired,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )

    write_json(args.output_dir / "stage1_direct_plan.json", repaired)
    write_json(args.output_dir / "api_call_03A_R.json", telemetry)
    write_json(
        args.output_dir / "repair_manifest.json",
        {
            "schema_version": "substar.stage1.global-repair-run.v1",
            "model": args.model,
            "key_source": key_source,
            "legacy_optimizer_used": False,
            "model_attempts": 1,
        },
    )
    write_two_level_artifacts(args.output_dir, master, units, repaired)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft, encoding="utf-8"
    )
    write_json(
        args.output_dir / "stage03A_validation_report.json",
        _direct_report(result, repaired=True, attempts=1),
    )
    print(
        json.dumps(
            {
                "valid": result.valid,
                "issues": len(result.issues),
                "review_notices": len(result.review_notices),
                "output_dir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
