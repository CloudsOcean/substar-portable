from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_flash_map_pro_editor import (  # noqa: E402
    build_plan_from_cuts,
    cuts_from_group_breaks,
    normalize_local_result,
    normalize_p1,
    normalize_p2,
)
from scripts.run_stage1_pipeline import (  # noqa: E402
    Stage1PipelineError,
    _direct_report,
    read,
    source_punctuation_kwargs,
    write_json,
    write_two_level_artifacts,
)
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan  # noqa: E402


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise Stage1PipelineError(f"JSON 顶层必须是对象：{path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验并导入独立盲测 Sol P1/P2/P3，不做隐式重排"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--p1", required=True, type=Path)
    parser.add_argument("--p2", required=True, type=Path)
    parser.add_argument("--p3", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    material = read(args.material)
    master = extract_master(material)
    units = extract_alignment(material)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)

    p1 = normalize_p1(
        load_json(args.p1),
        units,
        require_coverage=True,
    )
    p2, p2_merge_actions = normalize_p2(load_json(args.p2), units, p1)
    meaning_groups = p2["groups"]
    hard_spans = [
        span
        for span in p1["spans"]
        if span["protection_level"] == "hard"
    ]
    group_breaks, p3_audit = normalize_local_result(
        load_json(args.p3),
        meaning_groups,
        hard_spans,
    )
    cuts = cuts_from_group_breaks(
        meaning_groups,
        group_breaks,
        int(units[-1].index),
    )
    plan = build_plan_from_cuts(cuts, units, p1, meaning_groups)
    result = evaluate_direct_plan(
        master,
        units,
        plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )

    write_json(output / "p1_protection.json", p1)
    write_json(output / "p2_meaning_groups.json", p2)
    write_json(
        output / "p2_protection_merge_actions.json",
        {"actions": p2_merge_actions},
    )
    write_json(output / "p3_initial_plan.json", p3_audit)
    write_json(output / "stage1_direct_plan.json", plan)
    write_json(output / "stage1_display_layout_plan.json", plan)
    write_json(output / "stage1_translation_group_plan.json", p2)
    write_two_level_artifacts(output, master, units, plan)
    (output / "stage03A_source_draft.txt").write_text(
        result.draft, encoding="utf-8"
    )
    write_json(
        output / "stage1_validation.json",
        _direct_report(result, repaired=False, attempts=0),
    )
    write_json(
        output / "sol_import_audit.json",
        {
            "schema_version": "substar.sol-blind-import-audit.v1",
            "implicit_relayout": False,
            "p1_span_count": len(p1["spans"]),
            "p2_group_count": len(meaning_groups),
            "p3_selected_candidate_id": p3_audit[
                "selected_candidate_id"
            ],
            "final_cue_count": len(plan["groups"]),
            "valid": result.valid,
            "issues": result.issues,
        },
    )
    if not result.valid:
        raise Stage1PipelineError(
            "Sol P3 原始方案未通过硬校验；已保存原始结果和审计，"
            f"不得隐式重排：{result.issues[:10]}"
        )
    print(
        f"imported valid Sol route: cues={len(plan['groups'])} "
        f"groups={len(meaning_groups)} output={output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
