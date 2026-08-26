from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.stage1 import extract_alignment  # noqa: E402


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return value


def transition_cuts(material: str, threshold: float) -> list[int]:
    units = extract_alignment(material)
    result: list[int] = []
    for left, right in zip(units, units[1:]):
        if (
            left.speaker_id
            and right.speaker_id
            and left.speaker_id != "speaker_unknown"
            and right.speaker_id != "speaker_unknown"
            and left.speaker_id != right.speaker_id
            and min(left.speaker_confidence, right.speaker_confidence) >= threshold
        ):
            result.append(int(left.index))
    return result


def recall_with_tolerance(
    transitions: list[int], cuts: set[int], tolerance: int
) -> dict[str, Any]:
    matched = [
        transition
        for transition in transitions
        if any(abs(cut - transition) <= tolerance for cut in cuts)
    ]
    return {
        "transition_count": len(transitions),
        "matched": len(matched),
        "recall": round(len(matched) / len(transitions), 4)
        if transitions
        else None,
        "missed": [
            transition for transition in transitions if transition not in matched
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="评价预测切点对说话人旁路变化的响应；该指标不等于人工真值"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--cuts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confidence", type=float, default=0.8)
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8-sig")
    transitions = transition_cuts(material, args.confidence)
    raw = read_json(args.cuts)
    cuts = {int(item) for item in raw.get("cuts_after", [])}
    report = {
        "schema_version": "substar.evaluation.speaker-metadata-effect.v1",
        "confidence_threshold": args.confidence,
        "warning": (
            "说话人变化来自自动模型，并非人工说话人真值；"
            "本指标只说明切分是否响应输入元数据。"
        ),
        "exact": recall_with_tolerance(transitions, cuts, 0),
        "within_one_alignment_unit": recall_with_tolerance(
            transitions, cuts, 1
        ),
        "cut_count": len(cuts),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"speaker_transition_recall_exact={report['exact']['recall']} "
        f"within_one={report['within_one_alignment_unit']['recall']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
