from __future__ import annotations

from typing import Any, Mapping, Sequence

from substar_core.segmentation.input_contract import build_segmentation_material


def alignment_tsv(units: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "index\tstart\tend\tkind\ttext\tsentence_id\t"
        "sentence_start\tsentence_end\tspeaker_id\tspeaker_confidence"
    ]
    for item in units:
        text = str(item["text"]).replace("\t", " ").replace("\r", " ").replace("\n", " ")
        lines.append(
            f'{item["index"]}\t{float(item["start"]):.3f}\t{float(item["end"]):.3f}\t'
            f'{item["kind"]}\t{text}\t{item.get("sentence_id", "-")}\t'
            f'{1 if item.get("sentence_start") else 0}\t'
            f'{1 if item.get("sentence_end") else 0}\t'
            f'{item.get("speaker_id", "-")}\t'
            f'{float(item.get("speaker_confidence", 0) or 0):.3f}'
        )
    return "\n".join(lines) + "\n"


def segmentation_material(master: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return build_segmentation_material(master, evidence)
