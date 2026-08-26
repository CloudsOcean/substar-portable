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
from substar_core.stage1_direct import evaluate_direct_plan, merge_direct_plans  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按原始索引合并各 Stage1 局部优化计划并执行全片硬校验"
    )
    parser.add_argument("material", type=Path)
    parser.add_argument("--plan", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--review-confidence", type=float, default=0.75)
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8-sig")
    master = extract_master(material)
    units = extract_alignment(material)
    plans = [
        json.loads(path.read_text(encoding="utf-8-sig"))
        for path in args.plan
    ]
    merged = merge_direct_plans(plans)
    result = evaluate_direct_plan(
        master,
        units,
        merged,
        review_confidence=args.review_confidence,
        **source_punctuation_kwargs(),
    )
    if not result.valid:
        raise SystemExit(
            "合并计划未通过硬校验："
            + json.dumps(result.issues[:12], ensure_ascii=False)
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "stage1_direct_plan.json", merged)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft,
        encoding="utf-8",
    )
    write_json(
        args.output_dir / "stage03A_validation_report.json",
        {
            "schema_version": "substar.stage1.direct-validation.v1",
            "valid": True,
            "plan_issues": result.issues,
            "review_notices": result.review_notices,
            "draft_validation": result.validation,
            "input_plans": [str(path.resolve()) for path in args.plan],
        },
    )
    print(
        json.dumps(
            {
                "valid": True,
                "plans": len(plans),
                "groups": len(merged["groups"]),
                "review_notices": len(result.review_notices),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
