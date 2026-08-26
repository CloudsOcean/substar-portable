from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_stage1_pipeline import (  # noqa: E402
    _direct_plan_from_blind_decision,
    source_punctuation_kwargs,
    write_json,
)
from substar_core.stage1 import extract_alignment, extract_master, load_json  # noqa: E402
from substar_core.stage1_chunking import build_segmentation_chunks  # noqa: E402
from substar_core.stage1_direct import (  # noqa: E402
    evaluate_direct_plan,
    merge_direct_plans,
)
from substar_core.stage1_hierarchy import (  # noqa: E402
    cuts_fingerprint,
    normalize_analysis_v2,
)


def boundary_delta(before: dict, after: dict) -> dict:
    before_groups = {
        int(group["alignment_end"])
        for group in before.get("groups", [])[:-1]
    }
    after_groups = {
        int(group["alignment_end"])
        for group in after.get("groups", [])[:-1]
    }
    before_cues = {
        int(value)
        for group in before.get("groups", [])
        for value in group.get("line_breaks_after", [])
    } | before_groups
    after_cues = {
        int(value)
        for group in after.get("groups", [])
        for value in group.get("line_breaks_after", [])
    } | after_groups
    return {
        "meaning_boundaries_added_after_a3": sorted(after_groups - before_groups),
        "meaning_boundaries_removed_after_a3": sorted(before_groups - after_groups),
        "cue_boundaries_added_after_a3": sorted(after_cues - before_cues),
        "cue_boundaries_removed_after_a3": sorted(before_cues - after_cues),
        "a3_cue_boundary_retention": (
            1.0 if not before_cues else len(before_cues & after_cues) / len(before_cues)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="复用已有 A1/A2/A3，中止旧优化器并重建冻结切点实验"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--stage1-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8-sig")
    master = extract_master(material)
    units = extract_alignment(material)
    manifest = load_json(args.stage1_dir / "stage1_chunk_manifest.json")
    chunk_seconds = float(manifest.get("chunk_seconds", 120))
    chunks = build_segmentation_chunks(material, chunk_seconds)
    plans: list[dict] = []
    replayed: list[dict] = []
    for chunk in chunks:
        chunk_dir = args.stage1_dir / "chunks" / chunk.chunk_id
        analysis_path = chunk_dir / "stage1_analysis.json"
        candidates_path = chunk_dir / "stage1_candidates_blinded.json"
        decision_path = chunk_dir / "stage1_decision.json"
        if not all(path.exists() for path in (analysis_path, candidates_path, decision_path)):
            raise FileNotFoundError(f"{chunk.chunk_id} 缺少 A1/A2/A3 中间产物")
        analysis = normalize_analysis_v2(load_json(analysis_path))
        candidates = load_json(candidates_path)
        decision = load_json(decision_path)
        plan = _direct_plan_from_blind_decision(analysis, candidates, decision)
        plans.append(plan)
        replayed.append(
            {
                "chunk_id": chunk.chunk_id,
                "alignment_start": int(chunk.units[0].index),
                "alignment_end": int(chunk.units[-1].index),
                "fingerprint": cuts_fingerprint(plan),
            }
        )

    frozen = merge_direct_plans(plans)
    result = evaluate_direct_plan(
        master,
        units,
        frozen,
        **source_punctuation_kwargs(),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "stage1_a3_frozen_plan.json", frozen)
    write_json(
        args.output_dir / "stage1_boundary_fingerprint.json",
        cuts_fingerprint(frozen),
    )
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft, encoding="utf-8"
    )

    comparison: dict = {
        "schema_version": "substar.stage1.frozen-a3-experiment.v1",
        "valid_without_relayout": result.valid,
        "issues": result.issues,
        "review_notices": result.review_notices,
        "chunks": replayed,
        "optimizer_called": False,
    }
    existing_path = args.stage1_dir / "stage1_direct_plan.json"
    if existing_path.exists():
        existing = load_json(existing_path)
        comparison["existing_final_fingerprint"] = cuts_fingerprint(existing)
        comparison["boundary_delta"] = boundary_delta(frozen, existing)
    write_json(args.output_dir / "experiment_a_report.json", comparison)
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    return 0 if result.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
