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
from substar_core.stage1_optimizer import compact_mergeable_line_breaks  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="只删除可证明多余的 Stage1 组内切点")
    parser.add_argument("material", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--maximum-merge-pause", type=float, default=0.3)
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8-sig")
    master = extract_master(material)
    units = extract_alignment(material)
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    profile = source_punctuation_kwargs()
    compact_profile = {
        key: profile[key]
        for key in (
            "baseline_punctuation",
            "raised_punctuation",
            "english_hard_limit",
            "chinese_hard_limit",
            "english_count_spaces",
            "english_count_punctuation",
        )
    }
    compacted = compact_mergeable_line_breaks(
        master,
        units,
        plan,
        maximum_merge_pause_seconds=args.maximum_merge_pause,
        **compact_profile,
    )
    result = evaluate_direct_plan(master, units, compacted.plan, **profile)
    if not result.valid:
        raise SystemExit(
            "压缩计划未通过硬校验："
            + json.dumps(result.issues[:12], ensure_ascii=False)
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "stage1_direct_plan.json", compacted.plan)
    write_json(args.output_dir / "compaction_actions.json", compacted.actions)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft,
        encoding="utf-8",
    )
    write_json(
        args.output_dir / "stage03A_validation_report.json",
        {
            "schema_version": "substar.stage1.compaction-validation.v1",
            "valid": True,
            "actions": len(compacted.actions),
            "review_notices": result.review_notices,
            "draft_validation": result.validation,
        },
    )
    print(
        json.dumps(
            {
                "valid": True,
                "actions": len(compacted.actions),
                "cue_count": result.validation.get("stats", {}).get("cue_count"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
