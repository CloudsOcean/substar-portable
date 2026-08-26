from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .material import AlignmentUnit
from substar_core.language_layout import editor_token_fragments, layout_tokens


SEGMENTATION_MATERIAL_SCHEMA = "substar.segmentation-material.v1"
_GRAMMATICAL_EDGE_PUNCTUATION = re.compile(
    r"^[\s\"“”‘’'《》〈〉「」『』（）()\[\]{}，。？！!?；;：:、….,]+|"
    r"[\s\"“”‘’'《》〈〉「」『』（）()\[\]{}，。？！!?；;：:、….,]+$"
)


def _unpunctuated_word(value: object) -> str:
    """Remove ASR display punctuation without rewriting lexical token content."""

    text = str(value or "").strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _GRAMMATICAL_EDGE_PUNCTUATION.sub("", text).strip()
    return text


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"segmentation material {field} must be a number")
    return float(value)


def build_segmentation_material(
    source_transcript: str, evidence: Mapping[str, Any]
) -> dict[str, Any]:
    del source_transcript  # Raw ASR prose remains in recognition_evidence.json only.
    units: list[dict[str, Any]] = []
    for item in evidence.get("units", []):
        text = _unpunctuated_word(item.get("text", item.get("word", "")))
        if not text:
            continue
        fragments = editor_token_fragments(text) or [text]
        start = float(item["start"])
        end = max(start, float(item["end"]))
        width = (end - start) / len(fragments)
        for offset, fragment in enumerate(fragments):
            units.append(
                {
                    "index": len(units),
                    "start": start + offset * width,
                    "end": end if offset + 1 == len(fragments) else start + (offset + 1) * width,
                    "text": fragment,
                    "speaker_id": item.get("speaker_id"),
                }
            )
    value = {
        "schema_version": SEGMENTATION_MATERIAL_SCHEMA,
        "source_transcript": layout_tokens(item["text"] for item in units),
        "units": units,
    }
    validate_segmentation_material(value)
    return value


def validate_segmentation_material(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "source_transcript",
        "units",
    }:
        raise ValueError("segmentation material must be a strict JSON object")
    if value.get("schema_version") != SEGMENTATION_MATERIAL_SCHEMA:
        raise ValueError("segmentation material schema_version is unsupported")
    transcript = value.get("source_transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("segmentation material source_transcript is empty")
    raw_units = value.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("segmentation material units are empty")

    previous_index: int | None = None
    normalized_units: list[dict[str, Any]] = []
    required = {
        "index",
        "start",
        "end",
        "text",
        "speaker_id",
    }
    for position, raw in enumerate(raw_units):
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError(f"segmentation material unit {position} has invalid fields")
        index = raw["index"]
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"segmentation material unit {position} index is invalid")
        if previous_index is not None and index != previous_index + 1:
            raise ValueError("segmentation material unit indexes must be contiguous")
        previous_index = index
        start = _number(raw["start"], f"unit {position} start")
        end = _number(raw["end"], f"unit {position} end")
        if start < 0 or end < start:
            raise ValueError(f"segmentation material unit {position} timing is invalid")
        text = raw["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"segmentation material unit {position} text is empty")
        speaker_id = raw["speaker_id"]
        if speaker_id is not None and not isinstance(speaker_id, str):
            raise ValueError(f"segmentation material unit {position} speaker_id is invalid")
        normalized_units.append(
            {
                "index": index,
                "start": start,
                "end": end,
                "text": text,
                "speaker_id": speaker_id,
            }
        )
    return {
        "schema_version": SEGMENTATION_MATERIAL_SCHEMA,
        "source_transcript": transcript.strip(),
        "units": normalized_units,
    }


def load_segmentation_material(path: Path) -> tuple[str, list[AlignmentUnit]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("segmentation material is not valid UTF-8 JSON") from exc
    value = validate_segmentation_material(raw)
    return value["source_transcript"], [
        AlignmentUnit(
            index=item["index"],
            start=item["start"],
            end=item["end"],
            text=item["text"],
            speaker_id=item["speaker_id"],
            speaker_confidence=1.0 if item["speaker_id"] else 0.0,
        )
        for item in value["units"]
    ]
