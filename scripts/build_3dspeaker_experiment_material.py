from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.pipeline import _alignment_tsv, _chatbox_material  # noqa: E402


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def text_fingerprint(alignment: dict[str, Any]) -> str:
    payload = "\n".join(
        f'{unit["index"]}\t{unit["start"]}\t{unit["end"]}\t{unit["text"]}'
        for unit in alignment["units"]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_turns(path: Path) -> list[dict[str, Any]]:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("3D-Speaker JSON 顶层必须是对象")
    turns = [
        {
            "start": float(item["start"]),
            "end": float(item["stop"]),
            "speaker": f'speaker_{int(item["speaker"])}',
        }
        for item in raw.values()
    ]
    return sorted(turns, key=lambda item: (item["start"], item["end"]))


def shuffled_labels(
    turns: list[dict[str, Any]], seed: int
) -> list[dict[str, Any]]:
    result = copy.deepcopy(turns)
    labels = [turn["speaker"] for turn in result]
    random.Random(seed).shuffle(labels)
    for turn, label in zip(result, labels, strict=True):
        turn["speaker"] = label
    return result


def assign_units(
    alignment: dict[str, Any], turns: list[dict[str, Any]]
) -> dict[str, Any]:
    assigned = 0
    unknown = 0
    for unit in alignment["units"]:
        start = float(unit["start"])
        end = max(start, float(unit["end"]))
        duration = max(0.04, end - start)
        best_overlap = 0.0
        best_speaker = "speaker_unknown"
        for turn in turns:
            if turn["start"] >= end:
                break
            if turn["end"] <= start:
                continue
            overlap = max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = turn["speaker"]
        if best_overlap <= 0:
            unit["speaker_id"] = "speaker_unknown"
            unit["speaker_confidence"] = 0.0
            unknown += 1
        else:
            unit["speaker_id"] = best_speaker
            unit["speaker_confidence"] = round(min(1.0, best_overlap / duration), 4)
            assigned += 1

    transitions: list[dict[str, Any]] = []
    units = alignment["units"]
    for left, right in zip(units, units[1:]):
        left_speaker = str(left.get("speaker_id", "speaker_unknown"))
        right_speaker = str(right.get("speaker_id", "speaker_unknown"))
        confidence = min(
            float(left.get("speaker_confidence", 0)),
            float(right.get("speaker_confidence", 0)),
        )
        if (
            left_speaker != right_speaker
            and "speaker_unknown" not in {left_speaker, right_speaker}
            and confidence >= 0.8
        ):
            transitions.append(
                {
                    "cut_after": int(left["index"]),
                    "from": left_speaker,
                    "to": right_speaker,
                    "confidence": round(confidence, 4),
                }
            )
    return {
        "assigned_units": assigned,
        "unknown_units": unknown,
        "high_confidence_transitions": transitions,
    }


def write_variant(
    original: dict[str, Any],
    turns: list[dict[str, Any]],
    output: Path,
    *,
    variant: str,
    source_turns: Path,
) -> dict[str, Any]:
    alignment = copy.deepcopy(original)
    before = text_fingerprint(alignment)
    stats = assign_units(alignment, turns)
    after = text_fingerprint(alignment)
    if before != after:
        raise RuntimeError("映射说话人时意外修改了文字、顺序或时间")
    alignment["speaker_diarization"] = {
        "enabled": True,
        "backend": "modelscope.3d-speaker",
        "variant": variant,
        "source_turns": str(source_turns.resolve()),
        "modifies_transcript": False,
        "modifies_word_timing": False,
        "turns": turns,
        **stats,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "alignment.json", alignment)
    (output / "alignment.tsv").write_text(
        _alignment_tsv(alignment["units"]), encoding="utf-8"
    )
    (output / "chatbox_material.md").write_text(
        _chatbox_material(str(alignment["master_text"]), alignment),
        encoding="utf-8",
    )
    speaker_counts = Counter(
        str(unit.get("speaker_id", "speaker_unknown"))
        for unit in alignment["units"]
    )
    report = {
        "schema_version": "substar.speaker-experiment-material.v1",
        "variant": variant,
        "text_timing_fingerprint_before": before,
        "text_timing_fingerprint_after": after,
        "source_unchanged": before == after,
        "unit_count": len(alignment["units"]),
        "turn_count": len(turns),
        "speaker_unit_counts": dict(sorted(speaker_counts.items())),
        **stats,
    }
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把3D-Speaker区间映射到冻结词级对齐，并生成真实/乱序对照材料"
    )
    parser.add_argument("--alignment", required=True, type=Path)
    parser.add_argument("--turns", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--shuffle-seed", type=int, default=20260728)
    args = parser.parse_args()

    alignment = read_json(args.alignment)
    turns = load_turns(args.turns)
    real = write_variant(
        alignment,
        turns,
        args.output_root / "real",
        variant="real_fixed4",
        source_turns=args.turns,
    )
    shuffled = write_variant(
        alignment,
        shuffled_labels(turns, args.shuffle_seed),
        args.output_root / "shuffled",
        variant=f"shuffled_fixed4_seed_{args.shuffle_seed}",
        source_turns=args.turns,
    )
    write_json(
        args.output_root / "experiment_manifest.json",
        {
            "schema_version": "substar.speaker-experiment.v1",
            "alignment": str(args.alignment.resolve()),
            "turns": str(args.turns.resolve()),
            "shuffle_seed": args.shuffle_seed,
            "real": real,
            "shuffled": shuffled,
        },
    )
    print(json.dumps({"real": real, "shuffled": shuffled}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
