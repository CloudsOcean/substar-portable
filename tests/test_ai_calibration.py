from substar_core.contracts.editor_document import (
    build_editor_document,
    source_tokens_from_asr,
)


def test_segmentation_cannot_apply_calibration_output() -> None:
    source = source_tokens_from_asr(
        [
            {"index": 0, "start": 0, "end": 0.5, "text": "molly"},
            {"index": 1, "start": 0.5, "end": 1, "text": "t"},
        ],
        source_asset_id="test-segmentation-boundary",
    )
    document = build_editor_document(
        source_tokens=source,
        source_kind="asr",
        source_asset_id="test-segmentation-boundary",
        execution_plan={
            "blocks": [
                {"block_id": "block-1", "alignment_start": 0, "alignment_end": 1}
            ],
            "boundaries_after": [],
            "skipped_reason": "test",
        },
        semantic_grouping={
            "protections": [],
            "meaning_groups": [
                {"group_id": "group-1", "alignment_start": 0, "alignment_end": 1}
            ],
            # Even if an invalid caller tries to smuggle a correction through
            # segmentation, document construction ignores it.
            "canonicalizations": [
                {
                    "alignment_start": 0,
                    "alignment_end": 1,
                    "canonical_text": "Molly Tea",
                }
            ],
            "review_regions": [],
        },
        cue_layout={"display_breaks": []},
    )

    assert [token.text for token in document.display_tokens] == ["molly", "t"]
    assert all(token.provenance.kind.value == "source" for token in document.display_tokens)
