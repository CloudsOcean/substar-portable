from __future__ import annotations

import unittest

from substar_core.document_operations import DocumentOperationError, apply_document_operation
from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    DisplayCue,
    DisplayToken,
    EditorDocument,
    SourceToken,
)
from substar_core.editor.http_api import (
    BatchReplacement,
    _apply_ai_calibration_operations,
    _validated_calibration_contract_actions,
)


def _document() -> EditorDocument:
    source = [
        SourceToken.create(index=0, text="u", start=0.0, end=0.2),
        SourceToken.create(index=1, text="s", start=0.2, end=0.4),
        SourceToken.create(index=2, text="america", start=0.4, end=0.9),
    ]
    provenance = ChangeProvenance(
        kind=ChangeKind.SOURCE,
        operation="project_source_token",
    )
    display = [
        DisplayToken.create(
            position=index,
            text=token.text,
            source_token_ids=[token.token_id],
            provenance=provenance,
        )
        for index, token in enumerate(source)
    ]
    cue = DisplayCue.create(
        index=0,
        display_token_ids=[token.token_id for token in display],
        start=0.0,
        end=0.9,
    )
    return EditorDocument.create(
        source_tokens=source,
        display_tokens=display,
        cues=[cue],
        document_key="ai-calibration-merge-test",
    )


def _merge_action(document: EditorDocument) -> dict[str, object]:
    first, second = document.display_tokens[:2]
    return {
        "action_id": "merge-us",
        "kind": "merge_span",
        "token_ids": [first.token_id, second.token_id],
        "before_text": "u s",
        "after_text": "U.S.",
        "confidence": "high",
        "evidence": [{"kind": "context", "reference": "one acronym in this Cue"}],
        "disposition": "apply",
        "affects_translation": True,
    }


def _assert_merge_span_contract_accepts_only_one_cue() -> None:
    document = _document()
    action = _merge_action(document)
    token_map = {token.token_id: token for token in document.display_tokens}
    cue_id = document.cues[0].cue_id
    token_to_cue = {token.token_id: cue_id for token in document.display_tokens}

    accepted, rejected = _validated_calibration_contract_actions(
        {"actions": [action]},
        [token.token_id for token in document.display_tokens],
        token_map,
        token_to_cue,
    )
    assert rejected == []
    assert accepted == [action]

    token_to_cue[document.display_tokens[1].token_id] = "another-cue"
    accepted, rejected = _validated_calibration_contract_actions(
        {"actions": [action]},
        [token.token_id for token in document.display_tokens],
        token_map,
        token_to_cue,
    )
    assert accepted == []
    assert rejected[0]["detail"] == (
        "merge_span must merge contiguous tokens in one Cue into one token"
    )


def _assert_merge_span_reuses_editor_merge_and_is_reversible() -> None:
    document = _document()
    action = {
        **_merge_action(document),
        "cue_id": document.cues[0].cue_id,
    }
    third = document.display_tokens[2]
    merged = _apply_ai_calibration_operations(
        document,
        replacements=[
            BatchReplacement(
                token_id=third.token_id,
                text="America",
                expected_text="america",
            )
        ],
        merge_actions=[action],
        calibration_metadata={"test": True},
        operation_id="op_test_calibration",
    )

    assert [token.text for token in merged.display_tokens] == ["U.S.", "America"]
    merged_token = merged.display_tokens[0]
    assert merged_token.source_token_ids == tuple(
        source.token_id for source in document.source_tokens[:2]
    )
    assert merged.cues[0].display_token_ids[0] == merged_token.token_id
    assert merged_token.provenance.kind is ChangeKind.AI
    assert merged_token.provenance.metadata["ai_calibration"]["topology"] == "merge_span"

    manual = ChangeProvenance(
        kind=ChangeKind.MANUAL,
        operation="cancel_ai_calibration",
        actor="editor",
    )
    cancelled = apply_document_operation(merged, {
        "operation_id": "op_cancel_merge",
        "type": "set_ai_calibration",
        "payload": {
            "token_ids": [merged_token.token_id],
            "action": "cancel",
            "expected_texts": {merged_token.token_id: "U.S."},
            "provenance": manual.to_dict(),
        },
    })
    assert [token.text for token in cancelled.display_tokens] == ["u", "s", "America"]
    restored_ids = [token.token_id for token in cancelled.display_tokens[:2]]
    assert restored_ids == [token.token_id for token in document.display_tokens[:2]]
    assert all(
        token.provenance.metadata["ai_calibration"]["applied"] is False
        for token in cancelled.display_tokens[:2]
    )

    try:
        apply_document_operation(cancelled, {
            "operation_id": "op_restore_incomplete_merge",
            "type": "set_ai_calibration",
            "payload": {
                "token_ids": restored_ids[:1],
                "action": "restore",
                "expected_texts": {restored_ids[0]: "u"},
                "provenance": manual.to_dict(),
            },
        })
    except DocumentOperationError as exc:
        assert "every original token" in str(exc)
    else:
        raise AssertionError("partial merge restoration must be rejected")

    restored = apply_document_operation(cancelled, {
        "operation_id": "op_restore_merge",
        "type": "set_ai_calibration",
        "payload": {
            "token_ids": restored_ids,
            "action": "restore",
            "expected_texts": dict(zip(restored_ids, ["u", "s"], strict=True)),
            "provenance": manual.to_dict(),
        },
    })
    assert [token.text for token in restored.display_tokens] == ["U.S.", "America"]
    assert restored.display_tokens[0].source_token_ids == merged_token.source_token_ids
    assert restored.display_tokens[0].provenance.kind is ChangeKind.AI


class AiCalibrationMergeTests(unittest.TestCase):
    def test_merge_span_contract_accepts_only_one_cue(self) -> None:
        _assert_merge_span_contract_accepts_only_one_cue()

    def test_merge_span_reuses_editor_merge_and_is_reversible(self) -> None:
        _assert_merge_span_reuses_editor_merge_and_is_reversible()
