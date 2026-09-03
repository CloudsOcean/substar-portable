from __future__ import annotations

import pytest

from substar_core.cue_script import (
    CueScriptError,
    finalize_calibration,
    finalize_translation,
    parse_segmentation,
    render_cue_request,
    render_segmentation_request,
    render_translation_request,
)
from substar_core.editor.http_api import _validated_calibration_contract_actions


def test_segmentation_script_compiles_to_frozen_contract() -> None:
    request = {
        "rows": [
            {"index": index, "start": index, "end": index + 0.1, "text": text, "owner": True}
            for index, text in enumerate(("we", "are", "feeding", "and", "I", "say"), start=10)
        ],
        "active_output_profile": {"source_language": "en", "hard_limit": 55},
    }
    _wire, ledger = render_segmentation_request(request)
    binding = {
        "input_fingerprint": "fingerprint",
        "block_id": "c0001",
        "ownership": {"alignment_start": 10, "alignment_end": 15},
    }
    result = parse_segmentation(
        "\n".join((
            "SUBSTAR-CUE-SCRIPT/1\tSEGMENT",
            "CUE\tC001\tG001\tW0001-W0003\twe are feeding",
            "CUE\tC002\tG001\tW0004-W0006\tand I say",
            "END",
        )),
        ledger,
        binding,
    )
    assert result["schema_version"] == "substar.semantic-grouping-result.v1"
    assert result["meaning_groups"] == [{
        "alignment_start": 10,
        "alignment_end": 15,
        "line_breaks_after": [12, 15],
    }]


def test_segmentation_script_rejects_incomplete_ownership() -> None:
    request = {
        "rows": [
            {"index": 1, "start": 0, "end": 1, "text": "a", "owner": True},
            {"index": 2, "start": 1, "end": 2, "text": "b", "owner": True},
        ]
    }
    _wire, ledger = render_segmentation_request(request)
    with pytest.raises(CueScriptError, match="完整覆盖"):
        parse_segmentation(
            "SUBSTAR-CUE-SCRIPT/1\tSEGMENT\nCUE\tC001\tG001\tW0001-W0001\ta\nEND",
            ledger,
            {"ownership": {"alignment_start": 1, "alignment_end": 2}},
        )


def test_translation_script_uses_local_aliases_and_finalizes_many_to_many() -> None:
    groups = [{
        "group_id": "real-private-group-id",
        "cues": [
            {"cue_id": "real-private-cue-1", "source_text": "we are feeding"},
            {"cue_id": "real-private-cue-2", "source_text": "the machine"},
        ],
    }]
    wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    assert "real-private-cue" not in wire
    result = finalize_translation(
        "\n".join((
            "SUBSTAR-CUE-SCRIPT/1\tTRANSLATE",
            "CUE\tC001\t我们正在供养",
            "CUE\tC002\t这台机器",
            "END",
        )),
        groups,
        ledger,
        mapping_mode="many_to_many",
    )
    row = result["group_results"][0]
    assert row["group_id"] == "real-private-group-id"
    assert [item["cue_id"] for item in row["cue_assignments"]] == [
        "real-private-cue-1", "real-private-cue-2"
    ]


def test_translation_script_salvages_valid_rows_from_partial_envelope_free_output() -> None:
    groups = [
        {"group_id": "g1", "cues": [{"cue_id": "c1", "source_text": "one"}]},
        {"group_id": "g2", "cues": [{"cue_id": "c2", "source_text": "two"}]},
    ]
    _wire, ledger = render_translation_request(groups, mapping_mode="one_to_one")
    result = finalize_translation(
        "CUE\tC001\t甲\nCUE\tC001\t重复应忽略\nCUE\tC002\t",
        groups,
        ledger,
        mapping_mode="one_to_one",
    )
    assert result == {"group_results": [{
        "group_id": "g1", "cue_id": "c1", "target_text": "甲",
    }]}


def test_calibration_full_cue_finalizer_binds_real_tokens_and_allows_reuse() -> None:
    cues = [{
        "cue_id": "cue-real",
        "editable": True,
        "tokens": [
            {"token_id": "t1", "text": "u"},
            {"token_id": "t2", "text": "s"},
            {"token_id": "t3", "text": "policy"},
        ],
    }]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )
    result = finalize_calibration(
        "SUBSTAR-CUE-SCRIPT/1\tCALIBRATE\nCUE\tC001\tU.S. Policy.\nEND",
        ledger,
    )
    assert [row["kind"] for row in result["actions"]] == [
        "merge_span", "set_case", "set_punctuation"
    ]
    token_map = {
        "t1": type("Token", (), {"text": "u"})(),
        "t2": type("Token", (), {"text": "s"})(),
        "t3": type("Token", (), {"text": "policy"})(),
    }
    accepted, rejected = _validated_calibration_contract_actions(
        result, ["t1", "t2", "t3"], token_map,
        {"t1": "cue-real", "t2": "cue-real", "t3": "cue-real"},
    )
    assert rejected == []
    assert len(accepted) == 3
    assert accepted[1]["token_ids"] == accepted[2]["token_ids"] == ["t3"]


def test_calibration_finalizer_keeps_semantic_lexical_rewrite_for_review() -> None:
    cues = [{
        "cue_id": "cue-real", "editable": True,
        "tokens": [{"token_id": "t1", "text": "sensible"}],
    }]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )
    result = finalize_calibration(
        "SUBSTAR-CUE-SCRIPT/1\tCALIBRATE\nCUE\tC001\tso-called\nEND",
        ledger,
    )
    assert result["actions"][0]["disposition"] == "review"
    assert result["actions"][0]["confidence"] == "low"


def test_calibration_finalizer_keeps_decorative_symbol_for_review() -> None:
    cues = [{
        "cue_id": "cue-real", "editable": True,
        "tokens": [{"token_id": "t1", "text": "actually"}],
    }]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )
    result = finalize_calibration(
        "SUBSTAR-CUE-SCRIPT/1\tCALIBRATE\nCUE\tC001\tactually‡\nEND",
        ledger,
    )
    assert result["actions"][0]["disposition"] == "review"
    assert result["actions"][0]["confidence"] == "low"
