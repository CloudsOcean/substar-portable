from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_flash_map_pro_editor import build_plan_from_cuts  # noqa: E402
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


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise Stage1PipelineError("direct cuts 顶层必须是对象")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 Sol 直接切点导入统一 Stage1 并做硬校验"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--cuts", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--allow-missing-coverage-for-audit",
        action="store_true",
        help="仅为实验评分推导覆盖；产物仍标记为不可交付",
    )
    args = parser.parse_args()

    material = read(args.material)
    master = extract_master(material)
    units = extract_alignment(material)
    first = int(units[0].index)
    last = int(units[-1].index)
    raw = load(args.cuts)
    if raw.get("schema_version") != "substar.sol.direct-cuts.v1":
        raise Stage1PipelineError("direct cuts schema_version 错误")
    raw_cuts = raw.get("cuts_after")
    if not isinstance(raw_cuts, list):
        raise Stage1PipelineError("cuts_after 必须是数组")
    cuts = {
        int(item)
        for item in raw_cuts
        if isinstance(item, int) and first <= item < last
    }
    if len(cuts) != len(raw_cuts):
        raise Stage1PipelineError("cuts_after 含重复、越界或非整数")
    coverage = raw.get("coverage_check", {})
    declared_complete = isinstance(coverage, dict) and (
        (
            coverage.get("complete") is True
            and coverage.get("alignment_start") == first
            and coverage.get("alignment_end") == last
        )
        or (
            coverage.get("all_alignments_covered_once") is True
            and coverage.get("first_index") == first
            and coverage.get("last_index") == last
        )
        or (
            coverage.get("covered_exactly_once") is True
            and coverage.get("first_index") == first
            and coverage.get("last_index") == last
        )
        or (
            coverage.get("covers_first") == first
            and coverage.get("covers_last") == last
            and coverage.get("total_indexes") == last - first + 1
            and coverage.get("cuts_count") == len(cuts)
            and coverage.get("cues_count") == len(cuts) + 1
        )
    )
    if not declared_complete and not args.allow_missing_coverage_for_audit:
        raise Stage1PipelineError("direct cuts 未声明完整扫描范围")

    protection = {
        "schema_version": "substar.stage1.protection.v1",
        "spans": [],
        "preferred_breaks_after": [],
        "forbidden_breaks_after": [],
        "coverage_check": {
            "alignment_start": first,
            "alignment_end": last,
            "complete": True,
        },
    }
    meaning_groups = [
        {
            "group_id": "g0001",
            "alignment_start": first,
            "alignment_end": last,
            "continuity_after": {
                "relation": "separate",
                "confidence": 1.0,
                "reason": "direct whole-program experiment",
                "speaker_transition": "unknown",
            },
        }
    ]
    plan = build_plan_from_cuts(cuts, units, protection, meaning_groups)
    result = evaluate_direct_plan(
        master,
        units,
        plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "direct_cuts_raw.json", raw)
    write_json(output / "stage1_direct_plan.json", plan)
    write_json(output / "stage1_display_layout_plan.json", plan)
    write_two_level_artifacts(output, master, units, plan)
    (output / "stage03A_source_draft.txt").write_text(
        result.draft, encoding="utf-8"
    )
    write_json(
        output / "stage1_validation.json",
        _direct_report(result, repaired=False, attempts=0),
    )
    write_json(
        output / "direct_import_audit.json",
        {
            "schema_version": "substar.sol-direct-import-audit.v1",
            "valid": result.valid,
            "cue_count": len(plan["groups"]),
            "change_count": len(raw.get("changes", [])),
            "review_flag_count": len(raw.get("review_flags", [])),
            "issues": result.issues,
            "implicit_relayout": False,
            "coverage_declared": declared_complete,
            "delivery_valid": result.valid and declared_complete,
        },
    )
    if not result.valid:
        raise Stage1PipelineError(
            "Sol 直接切分未通过硬校验，禁止程序补切："
            f"{result.issues[:10]}"
        )
    print(
        f"imported direct cuts cues={len(plan['groups'])} "
        f"delivery_valid={result.valid and declared_complete} output={output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
