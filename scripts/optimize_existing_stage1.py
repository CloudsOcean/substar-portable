from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.stage1 import AlignmentUnit  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan  # noqa: E402
from substar_core.stage1_optimizer import optimize_direct_plan  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="对已有 Stage1 计划执行确定性边界优化")
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--input-stage1-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    master = (args.job_dir / "master_transcript.txt").read_text(encoding="utf-8")
    alignment = json.loads((args.job_dir / "alignment.json").read_text(encoding="utf-8"))
    units = [
        AlignmentUnit(
            index=int(item["index"]),
            text=str(item["text"]),
            start=float(item["start"]),
            end=float(item["end"]),
        )
        for item in alignment["units"]
    ]
    plan = json.loads(
        (args.input_stage1_dir / "stage1_direct_plan.json").read_text(encoding="utf-8")
    )
    optimized = optimize_direct_plan(master, units, plan)
    result = evaluate_direct_plan(master, units, optimized.plan)
    if not result.valid:
        raise RuntimeError(
            "确定性优化后仍有硬错误："
            + json.dumps(result.issues[:10], ensure_ascii=False)
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "stage1_direct_plan.json", optimized.plan)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft,
        encoding="utf-8",
    )
    write_json(args.output_dir / "deterministic_actions.json", optimized.actions)
    write_json(
        args.output_dir / "stage03A_validation_report.json",
        {
            "schema_version": "substar.stage1.direct-validation.v1",
            "valid": True,
            "optimizer": "deterministic-dp-v1",
            "action_count": len(optimized.actions),
            "plan_issues": result.issues,
            "review_notices": result.review_notices,
            "draft_validation": result.validation,
        },
    )
    print(
        f"complete actions={len(optimized.actions)} "
        f"hard_issues={len(result.issues)} notices={len(result.review_notices)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
