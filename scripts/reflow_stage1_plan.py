from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_stage1_pipeline import source_punctuation_kwargs, write_json  # noqa: E402
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan  # noqa: E402
from substar_core.stage1_optimizer import optimize_direct_plan  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="冻结意义组边界，对全部组执行精确显示行重排实验"
    )
    parser.add_argument("material", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--risky-only",
        action="store_true",
        help="仅重排存在高置信结构风险的显示行，冻结其余组",
    )
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8-sig")
    master = extract_master(material)
    units = extract_alignment(material)
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    profile = source_punctuation_kwargs()
    optimized = optimize_direct_plan(
        master,
        units,
        plan,
        force_reflow=not args.risky_only,
        **profile,
    )
    result = evaluate_direct_plan(master, units, optimized.plan, **profile)
    if not result.valid:
        raise SystemExit(
            "重排计划未通过硬校验："
            + json.dumps(result.issues[:12], ensure_ascii=False)
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "stage1_direct_plan.json", optimized.plan)
    write_json(args.output_dir / "reflow_actions.json", optimized.actions)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft,
        encoding="utf-8",
    )
    write_json(
        args.output_dir / "stage03A_validation_report.json",
        {
            "schema_version": "substar.stage1.reflow-validation.v1",
            "valid": True,
            "actions": len(optimized.actions),
            "review_notices": result.review_notices,
            "draft_validation": result.validation,
        },
    )
    print(
        json.dumps(
            {
                "valid": True,
                "actions": len(optimized.actions),
                "cue_count": result.validation.get("stats", {}).get("cue_count"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
