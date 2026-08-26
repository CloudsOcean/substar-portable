from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    DisplayCue,
    DisplayToken,
    EditorDocument,
    SourceToken,
)
from substar_core.editor.translation.contextual import _presentation_plan, materialize_presentation


def document() -> EditorDocument:
    provenance = ChangeProvenance(kind=ChangeKind.SOURCE, operation="test")
    sources = tuple(
        SourceToken.create(index=i, text=text, start=float(i), end=float(i + 1))
        for i, text in enumerate(("alpha", "beta"))
    )
    displays = tuple(
        DisplayToken.create(
            position=i, text=source.text, source_token_ids=(source.token_id,),
            provenance=provenance,
        )
        for i, source in enumerate(sources)
    )
    cues = tuple(
        DisplayCue.create(
            index=i, display_token_ids=(display.token_id,), start=float(i),
            end=float(i + 1),
        )
        for i, display in enumerate(displays)
    )
    return EditorDocument.create(
        source_tokens=sources, display_tokens=displays, cues=cues,
        document_key="contextual_translation-presentation-test",
    )


def plan(mapping_type: str) -> list[dict]:
    source = document()
    ids = [cue.cue_id for cue in source.cues]
    if mapping_type == "N:1":
        rows = [
            {"source_cue_ids": [cue_id], "target_unit_id": "t1", "target_text": "共同译文", "weight": 1}
            for cue_id in ids
        ]
    elif mapping_type == "1:N":
        rows = [
            {"source_cue_ids": [ids[0]], "target_unit_id": "t1", "target_text": "第一段", "weight": 1},
            {"source_cue_ids": [ids[0]], "target_unit_id": "t2", "target_text": "较长的第二段", "weight": 2},
            {"source_cue_ids": [ids[1]], "target_unit_id": "t3", "target_text": "末段", "weight": 1},
        ]
    else:
        rows = [
            {"source_cue_ids": [cue_id], "target_unit_id": f"t{i}", "target_text": text, "weight": 1}
            for i, (cue_id, text) in enumerate(zip(ids, ("甲", "乙")), start=1)
        ]
    return [{
        "group_id": "g1", "mapping_type": mapping_type,
        "reorder_type": "none", "presentation_cues": rows,
    }]


def main() -> None:
    for mapping_type in ("N:1", "1:N", "N:M"):
        candidate, report = materialize_presentation(document(), plan(mapping_type), "zh-CN")
        candidate.validate()
        assert report["cues"]
        if mapping_type == "N:1":
            assert len({cue.target.target_text for cue in candidate.cues}) == 1
        if mapping_type == "1:N":
            assert len(candidate.cues) == 3
            assert candidate.cues[0].display_token_ids == candidate.cues[1].display_token_ids
            assert candidate.cues[0].end == candidate.cues[1].start
    source = document()
    ids = [cue.cue_id for cue in source.cues]
    normalized = _presentation_plan(
        {"group_id": "g1", "cues": [{"cue_id": value} for value in ids]},
        {
            "mapping_type": "1:1",
            "reorder_type": "none",
            "presentation_cues": [
                {"source_cue_ids": [ids[0]], "target_unit_id": "t1", "target_text": "甲"},
                {"source_cue_ids": [ids[1]], "target_unit_id": "t2", "target_text": "乙"},
            ],
        },
    )
    assert normalized is not None
    assert normalized["mapping_type"] == "N:M"
    local_many_to_one = [{
        "group_id": "g1", "mapping_type": "N:M", "reorder_type": "none",
        "presentation_cues": [{
            "source_cue_ids": ids, "target_unit_id": "shared", "target_text": "共同译文", "weight": 1,
        }],
    }]
    candidate, _ = materialize_presentation(source, local_many_to_one, "zh-CN")
    candidate.validate()
    assert len(candidate.cues) == 2
    assert all(cue.mapping["mapping_type"] == "N:1" for cue in candidate.cues)
    print("contextual_translation presentation mapping tests passed")


if __name__ == "__main__":
    main()
