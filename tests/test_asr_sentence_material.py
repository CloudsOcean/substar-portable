import json

from substar_core.segmentation.document_builder import build_sentence_boundary_document
from substar_core.segmentation.input_contract import (
    build_segmentation_material,
    load_segmentation_material,
    validate_segmentation_material,
)


def test_native_sentences_survive_material_round_trip_and_create_separate_cues(tmp_path):
    # Short pauses cannot recover these boundaries after metadata is lost.
    evidence = {"units": [
        {"index": 0, "text": "Hello", "start": 0.0, "end": 0.2,
         "sentence_id": 0, "sentence_start": True, "sentence_end": False},
        {"index": 1, "text": "world.", "start": 0.2, "end": 0.4,
         "sentence_id": 0, "sentence_start": False, "sentence_end": True},
        {"index": 2, "text": "重庆市。", "start": 0.5, "end": 1.1,
         "sentence_id": 1, "sentence_start": True, "sentence_end": True},
        {"index": 3, "text": "Goodbye.", "start": 1.2, "end": 1.4,
         "sentence_id": 2, "sentence_start": True, "sentence_end": True},
    ]}
    material = validate_segmentation_material(build_segmentation_material("", evidence))
    assert [u["index"] for u in material["units"] if u["sentence_end"]] == [1, 4, 5]
    path = tmp_path / "material.json"
    path.write_text(json.dumps(material), encoding="utf-8")
    _, units = load_segmentation_material(path)
    assert [u.index for u in units if u.sentence_start] == [0, 2, 5]
    assert [u.index for u in units if u.sentence_end] == [1, 4, 5]
    document = build_sentence_boundary_document(material, source_asset_id="test-audio")
    assert len(document.cues) == 3
    assert len(document.source_tokens) == 6
