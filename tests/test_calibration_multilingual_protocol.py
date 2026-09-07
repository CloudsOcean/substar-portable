from __future__ import annotations

from substar_core.cue_script import finalize_calibration, render_cue_request
from substar_core.editor.http_api import _validated_calibration_contract_actions


def test_chinese_internal_punctuation_preserves_the_bound_token() -> None:
    cues = [{
        "cue_id": "cue-zh",
        "editable": True,
        "tokens": [{"token_id": "t-zh", "text": "今天发布消息"}],
    }]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )

    result = finalize_calibration("C001\t今天，发布消息。", ledger)

    assert [action["kind"] for action in result["actions"]] == ["set_punctuation"]
    assert result["actions"][0]["disposition"] == "apply"
    token_map = {"t-zh": type("Token", (), {"text": "今天发布消息"})()}
    accepted, rejected = _validated_calibration_contract_actions(
        result, ["t-zh"], token_map, {"t-zh": "cue-zh"}
    )
    assert rejected == []
    assert accepted[0]["after_text"] == "今天，发布消息。"


def test_cjk_punctuation_cannot_hide_a_lexical_change() -> None:
    cues = [{
        "cue_id": "cue-zh",
        "editable": True,
        "tokens": [{"token_id": "t-zh", "text": "今天发布消息"}],
    }]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )

    result = finalize_calibration("C001\t今天，删除消息。", ledger)

    assert result["actions"][0]["kind"] == "replace_token"
    assert result["actions"][0]["affects_translation"] is True
