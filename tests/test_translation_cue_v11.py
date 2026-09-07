from substar_core.cue_script import render_translation_request, finalize_translation
from substar_core.editor.translation.contextual import complete_results


def fixture():
    return [{"group_id": "g1", "cues": [
        {"cue_id": f"id{i}", "source_text": f"text {i}", "hard_limit": 24}
        for i in range(1, 5)
    ]}]


def test_range_copies_all_four_original_slots():
    groups = fixture()
    wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    assert "1|text 1" in wire
    assert "OWN" not in wire and "id1" not in wire
    result = finalize_translation("cue1-cue4<TAB>共同译文", groups, ledger, mapping_mode="many_to_many")
    assert result["_cue_script_issues"] == []
    assert result["_wire_units"][0]["cue_ids"] == ["id1", "id2", "id3", "id4"]
    adjacent = finalize_translation("cue1-cue4共同译文", groups, ledger, mapping_mode="many_to_many")
    assert adjacent["_wire_units"] == result["_wire_units"]


def test_modes_share_protocol_and_reject_ambiguous_binding():
    groups = fixture()
    _, ledger = render_translation_request(groups, mapping_mode="one_to_one")
    valid = finalize_translation("\n".join(f"cue{i} 译文{i}" for i in range(1, 5)),
                                 groups, ledger, mapping_mode="one_to_one")
    assert not valid["_cue_script_issues"]
    for raw in ("cue1-cue4\t共同译文", "cue1\t甲\ncue1\t乙", "甲\n乙"):
        assert finalize_translation(raw, groups, ledger, mapping_mode="one_to_one")["_cue_script_issues"]


def test_quality_does_not_discard_complete_new_translation(monkeypatch):
    groups = fixture()
    _, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    target = "有逗号，但是仍然应该保留的本轮译文" * 3
    response = finalize_translation("cue1-cue4\t" + target, groups, ledger, mapping_mode="many_to_many")
    assert not response["_cue_script_issues"]
    def no_repair(**kwargs):
        raise AssertionError("Quality alone must not trigger structural fallback")
    monkeypatch.setattr("substar_core.editor.translation.contextual.api_call", no_repair)
    plans, report = complete_results(settings={}, repair_prompt="", groups=groups,
                                     response=response, mapping_mode="many_to_many")
    assert plans and not report["invalid_group_ids"]
    assert report["quality_issues"]


def test_all_block_errors_are_sent_in_one_repair(monkeypatch):
    groups = fixture()
    _, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    bad = finalize_translation("cue1\t甲\ncue1\t乙", groups, ledger, mapping_mode="many_to_many")
    calls = []
    def repair(**kwargs):
        calls.append(kwargs["groups"])
        return finalize_translation("cue1-cue4\t修复译文", groups, ledger,
                                    mapping_mode="many_to_many"), {}
    monkeypatch.setattr("substar_core.editor.translation.contextual.api_call", repair)
    plans, report = complete_results(settings={}, repair_prompt="", groups=groups,
                                     response=bad, mapping_mode="many_to_many")
    assert len(calls) == 1 and len(calls[0][0]["cues"]) == 4
    assert len(calls[0][0]["program_validation_errors"]) >= 4
    assert plans and not report["invalid_group_ids"]
