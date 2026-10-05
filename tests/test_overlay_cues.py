import pytest

from substar_core.document_operations import apply_document_operation, DocumentOperationError
from substar_core.domain import EditorDocument
from substar_core.export import render_document_srt
from substar_core.ass_subtitles import render_ass, AssOptions
from substar_core.manuscript_matching import materialize_reference_script
from substar_core.segmentation.document_builder import build_reference_script_document


def document():
    units = [{"index": 0, "text": "Hello", "start": 0., "end": 2., "speaker_id": None}]
    material, breaks, report = materialize_reference_script("Hello.", units, ".")
    return build_reference_script_document(material, source_asset_id="overlay-test",
                                         display_breaks=breaks, reference_report=report)


def insert(base, overlay=True, identity="overlay"):
    return apply_document_operation(base, {"operation_id": identity, "type": "insert_cue",
        "payload": {"start": .5, "end": 1.5, "text": "Note", "overlay": overlay}})


def test_overlay_roundtrip_exports_without_touching_words():
    base = document()
    result = EditorDocument.from_dict(insert(base).to_dict())
    overlay = next(c for c in result.cues if c.mapping.get("subtitle_track") == "overlay")
    assert overlay.display_token_ids == ()
    assert overlay.source_text == "Note"
    assert overlay.target is None
    assert result.source_tokens == base.source_tokens
    assert result.display_tokens == base.display_tokens
    assert "Note" in render_document_srt(result, "source")
    assert "Hello" in render_document_srt(result, "source")


def test_overlay_time_and_text_edits_are_independent():
    base = document()
    result = insert(base)
    overlay = next(c for c in result.cues if c.mapping.get("subtitle_track") == "overlay")
    result = apply_document_operation(result, {"operation_id": "time", "type": "set_cue_time",
        "payload": {"cue_id": overlay.cue_id, "start": .2, "end": 1.8}})
    result = apply_document_operation(result, {"operation_id": "text", "type": "set_source_text",
        "payload": {"cue_id": overlay.cue_id, "text": "Updated note"}})
    assert result.source_tokens == base.source_tokens
    assert result.display_tokens == base.display_tokens
    assert next(c for c in result.cues if c.cue_id == base.cues[0].cue_id) == base.cues[0]
    assert "Updated note" in render_document_srt(result, "source")


def test_main_boundary_can_move_across_an_overlay():
    base = document()
    result = apply_document_operation(insert(base), {"operation_id": "main-time", "type": "set_cue_time",
        "payload": {"cue_id": base.cues[0].cue_id, "start": 0., "end": 1.}})
    assert next(c for c in result.cues if c.cue_id == base.cues[0].cue_id).end == 1.
    assert result.source_tokens == base.source_tokens


def test_plain_manual_cue_still_rejects_main_overlap():
    with pytest.raises(DocumentOperationError, match="overlaps"):
        insert(document(), overlay=False)


def test_overlay_remains_plain_in_a_word_highlight_ass_project():
    result = insert(document())
    overlay = next(c for c in result.cues if c.mapping.get("subtitle_track") == "overlay")
    ass = render_ass(result, AssOptions(word_highlight=True), cue_ids={overlay.cue_id})
    assert "Note" in ass
    assert "0:00:00.50,0:00:01.50" in ass


def test_overlay_does_not_lock_main_token_edits_or_cue_deletion():
    base = document()
    result = insert(base)
    token = base.display_tokens[0]
    result = apply_document_operation(result, {"operation_id": "replace-main", "type": "replace",
        "payload": {"token_id": token.token_id, "text": "Changed"}})
    assert result.source_tokens == base.source_tokens
    result = apply_document_operation(result, {"operation_id": "purge-main", "type": "purge_cue",
        "payload": {"cue_ids": [base.cues[0].cue_id]}})
    assert all(c.cue_id != base.cues[0].cue_id for c in result.cues)
    overlay = next(c for c in result.cues if c.mapping.get("subtitle_track") == "overlay")
    result = apply_document_operation(result, {"operation_id": "purge-overlay", "type": "purge_cue",
        "payload": {"cue_ids": [overlay.cue_id]}})
    assert not result.cues
