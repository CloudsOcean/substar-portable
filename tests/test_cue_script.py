from __future__ import annotations

import pytest

from substar_core.cue_script import (
    CueScriptError,
    finalize_calibration,
    finalize_calibration_candidate,
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
    wire, ledger = render_segmentation_request(request)
    assert "OWN_RANGE\tW0001-W0006" in wire
    assert "W0001\tOWN\tMAX_END=W0006\t10\t10.1\twe" in wire
    assert "RETURN_FORMAT\t1|W####[-W####]" in wire
    assert "CHECK_MAX" not in wire
    assert "one-word preview" not in wire
    binding = {
        "input_fingerprint": "fingerprint",
        "block_id": "c0001",
        "ownership": {"alignment_start": 10, "alignment_end": 15},
    }
    result = parse_segmentation(
        "\n".join((
            "SUBSTAR-CUE-SCRIPT/1\tSEGMENT",
            "CUE\tC001\tW0001-W0003",
            "CUE\tC002\tW0004-W0006",
            "END",
        )),
        ledger,
        binding,
    )
    assert result["schema_version"] == "substar.semantic-grouping-result.v1"
    assert result["meaning_groups"] == [
        {"alignment_start": 10, "alignment_end": 12, "line_breaks_after": [12]},
        {"alignment_start": 13, "alignment_end": 15, "line_breaks_after": [15]},
    ]


def test_segmentation_minimal_two_column_rows_compile() -> None:
    request = {
        "rows": [
            {"index": index, "start": index, "end": index + 0.1, "text": text, "owner": True}
            for index, text in enumerate(("one", "two", "three"), start=20)
        ],
        "active_output_profile": {"source_language": "en", "hard_limit": 55},
    }
    _wire, ledger = render_segmentation_request(request)
    result = parse_segmentation(
        "C001\tW0001\nC002\tW0002-W0003",
        ledger,
        {"ownership": {"alignment_start": 20, "alignment_end": 22}},
    )
    assert [
        (row["alignment_start"], row["alignment_end"])
        for row in result["meaning_groups"]
    ] == [(20, 20), (21, 22)]


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
            "SUBSTAR-CUE-SCRIPT/1\tSEGMENT\nCUE\tC001\tW0001\ta\nEND",
            ledger,
            {"ownership": {"alignment_start": 1, "alignment_end": 2}},
        )


def test_segmentation_candidate_salvages_rows_around_a_missing_word() -> None:
    request = {
        "rows": [
            {"index": index, "start": index, "end": index + 0.1, "text": text, "owner": True}
            for index, text in enumerate(("one", "two", "three"), start=20)
        ]
    }
    _wire, ledger = render_segmentation_request(request)
    result = parse_segmentation(
        "SUBSTAR-CUE-SCRIPT/1\tSEGMENT\n"
        "CUE\tC001\tW0001\tone\n"
        "CUE\tC002\tW0003\tthree\nEND",
        ledger,
        {"ownership": {"alignment_start": 20, "alignment_end": 22}},
        require_all=False,
    )
    assert [
        (row["alignment_start"], row["alignment_end"])
        for row in result["meaning_groups"]
    ] == [(20, 20), (22, 22)]


def test_segmentation_local_repair_canonicalizes_global_cue_ordinals() -> None:
    request = {
        "rows": [
            {"index": index, "start": index, "end": index + 0.1, "text": text, "owner": True}
            for index, text in enumerate(("one", "two", "three"), start=480)
        ]
    }
    _wire, ledger = render_segmentation_request(request)
    result = parse_segmentation(
        "CUE\tC480\tW0001\tone\n"
        "CUE\tC480-482-02\tW0002-W0003\ttwo three\nEND",
        ledger,
        {"ownership": {"alignment_start": 480, "alignment_end": 482}},
    )
    assert [
        (row["alignment_start"], row["alignment_end"])
        for row in result["meaning_groups"]
    ] == [(480, 480), (481, 482)]


def test_segmentation_accepts_singleton_word_alias_as_range() -> None:
    request = {
        "rows": [
            {"index": index, "start": index, "end": index + 0.1, "text": text, "owner": True}
            for index, text in enumerate(("one", "two", "three"), start=20)
        ]
    }
    _wire, ledger = render_segmentation_request(request)
    result = parse_segmentation(
        "SUBSTAR-CUE-SCRIPT/1\tSEGMENT\n"
        "CUE\tC001\tW0001\tone\n"
        "CUE\tC002\tW0002-W0003\ttwo three\nEND",
        ledger,
        {"ownership": {"alignment_start": 20, "alignment_end": 22}},
    )
    assert [
        (row["alignment_start"], row["alignment_end"])
        for row in result["meaning_groups"]
    ] == [(20, 20), (21, 22)]


def test_segmentation_keeps_legacy_explicit_singleton_range_readable() -> None:
    request = {
        "rows": [
            {"index": 20, "start": 0.0, "end": 0.1, "text": "one", "owner": True},
        ]
    }
    _wire, ledger = render_segmentation_request(request)
    result = parse_segmentation(
        "CUE\tC001\tW0001-W0001\tone\nEND",
        ledger,
        {"ownership": {"alignment_start": 20, "alignment_end": 20}},
    )
    assert result["meaning_groups"] == [
        {"alignment_start": 20, "alignment_end": 20, "line_breaks_after": [20]},
    ]


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
    assert "G001" not in wire
    assert "1|we are feeding" in wire
    result = finalize_translation(
        "C001+C002 -> C001+C002<TAB>我们正在供养这台机器",
        groups,
        ledger,
        mapping_mode="many_to_many",
    )
    row = result["group_results"][0]
    assert row["group_id"] == "real-private-group-id"
    assert [item["cue_id"] for item in row["cue_assignments"]] == [
        "real-private-cue-1", "real-private-cue-2"
    ]
    assert row["meaning_units"] == [{
        "meaning_unit_id": "unit_1",
        "target_text": "我们正在供养这台机器",
        "source_evidence_cue_ids": [
            "real-private-cue-1", "real-private-cue-2",
        ],
    }]


def test_translation_script_salvages_a_dropped_separator() -> None:
    groups = [{
        "group_id": "component_0001",
        "cues": [{"cue_id": "cue-a", "source_text": "hello"}],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001 -> C001<TAB>你好", groups, ledger, mapping_mode="many_to_many"
    )

    assert result["group_results"][0]["meaning_units"][0]["target_text"] == "你好"


def test_translation_script_freezes_first_duplicate_and_repairs_only_missing_alias() -> None:
    groups = [
        {"group_id": "g1", "cues": [{"cue_id": "c1", "source_text": "one"}]},
        {"group_id": "g2", "cues": [{"cue_id": "c2", "source_text": "two"}]},
    ]
    _wire, ledger = render_translation_request(groups, mapping_mode="one_to_one")
    result = finalize_translation(
        "C001\t甲\nC001\t重复应忽略\nC002\t",
        groups,
        ledger,
        mapping_mode="one_to_one",
    )
    assert result["group_results"] == [{
        "group_id": "g1", "cue_id": "c1", "target_text": "甲",
    }]
    assert result["_covered_cue_ids"] == ["c1"]
    assert [row["cue_id"] for row in result["_cue_script_issues"] if row["code"] == "missing_cue_translation"] == ["c2"]
    assert result["_cue_script_issues"][0]["code"] == "duplicate_display_alias"


def test_translation_script_salvages_complete_groups_from_partial_output() -> None:
    groups = [
        {"group_id": "g1", "cues": [{"cue_id": "c1", "source_text": "one"}]},
        {"group_id": "g2", "cues": [{"cue_id": "c2", "source_text": "two"}]},
    ]
    _wire, ledger = render_translation_request(groups, mapping_mode="one_to_one")
    result = finalize_translation(
        "C001\t甲\nC002\t", groups, ledger, mapping_mode="one_to_one"
    )
    assert result["group_results"] == [{
        "group_id": "g1", "cue_id": "c1", "target_text": "甲",
    }]
    assert result["_covered_cue_ids"] == ["c1"]
    assert [row["cue_id"] for row in result["_cue_script_issues"] if row["code"] == "missing_cue_translation"] == ["c2"]


def test_one_to_one_restores_dropped_aliases_from_exact_frozen_row_order() -> None:
    groups = [
        {"group_id": "g1", "cues": [{"cue_id": "c1", "source_text": "one"}]},
        {"group_id": "g2", "cues": [{"cue_id": "c2", "source_text": "two"}]},
    ]
    _wire, ledger = render_translation_request(groups, mapping_mode="one_to_one")

    result = finalize_translation(
        "甲\n乙", groups, ledger, mapping_mode="one_to_one"
    )

    assert result["group_results"] == []
    assert {row["cue_id"] for row in result["_cue_script_issues"] if row["code"] == "missing_cue_translation"} == {"c1", "c2"}


def test_one_to_one_restores_repeated_aliases_and_honors_valid_reordering() -> None:
    groups = [
        {"group_id": "g1", "cues": [{"cue_id": "c1", "source_text": "one"}]},
        {"group_id": "g2", "cues": [{"cue_id": "c2", "source_text": "two"}]},
    ]
    _wire, ledger = render_translation_request(groups, mapping_mode="one_to_one")
    restored = finalize_translation(
        "C001\t甲\nC001\t乙", groups, ledger, mapping_mode="one_to_one"
    )
    contradicted = finalize_translation(
        "C002\t乙\nC001\t甲", groups, ledger, mapping_mode="one_to_one"
    )

    assert any(row["code"] == "duplicate_display_alias" for row in restored["_cue_script_issues"])
    assert any(row["code"] == "display_alias_order_mismatch" for row in contradicted["_cue_script_issues"])


def test_one_to_one_does_not_positionally_bind_repair_with_context() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one", "editable": False},
            {"cue_id": "c2", "source_text": "two", "editable": True},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="one_to_one")
    result = finalize_translation("乙", groups, ledger, mapping_mode="one_to_one")

    assert result["_cue_script_issues"]


def test_translation_script_allows_consecutive_mapping_across_legacy_groups() -> None:
    groups = [
        {"group_id": "g1", "cues": [{"cue_id": "c1", "source_text": "one"}]},
        {"group_id": "g2", "cues": [{"cue_id": "c2", "source_text": "two"}]},
    ]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    result = finalize_translation(
        "C001+C002 -> C001+C002<TAB>跨旧组合并", groups, ledger,
        mapping_mode="many_to_many"
    )
    assert len(result["group_results"]) == 2


def test_translation_script_accepts_two_space_delimiter_and_markdown_bullet() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    result = finalize_translation(
        "- C001 + C002 -> C001 + C002<TAB>合并译文", groups, ledger,
        mapping_mode="many_to_many"
    )
    assert result["group_results"][0]["meaning_units"][0]["target_text"] == "合并译文"


def test_translation_script_derives_evidence_from_display_slots() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    result = finalize_translation(
        "C001<TAB>甲\nC002<TAB>乙", groups, ledger,
        mapping_mode="many_to_many"
    )
    assert [
        row["target_text"] for row in result["group_results"][0]["meaning_units"]
    ] == ["甲", "乙"]
    assert [
        row["source_evidence_cue_ids"]
        for row in result["group_results"][0]["meaning_units"]
    ] == [["c1"], ["c2"]]


def test_translation_script_accepts_literal_tab_marker_and_safe_alias_echo() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [{"cue_id": "c1", "source_text": "one"}],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    result = finalize_translation(
        "C001 -> C001<TAB>译文",
        groups,
        ledger,
        mapping_mode="many_to_many",
    )
    assert result["group_results"][0]["meaning_units"][0]["target_text"] == "译文"


def test_many_to_many_rejects_bare_control_alias_as_target_text() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [{"cue_id": "c1", "source_text": "one"}],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001 -> C001<TAB>C008",
        groups,
        ledger,
        mapping_mode="many_to_many",
    )

    assert [row["code"] for row in result["_cue_script_issues"]] == [
        "target_is_control_token"
    ]


def test_many_to_many_rejects_control_alias_embedded_in_target_text() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [{"cue_id": "c1", "source_text": "one"}],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001 -> C001<TAB>C006里情况也类似",
        groups,
        ledger,
        mapping_mode="many_to_many",
    )

    assert [row["code"] for row in result["_cue_script_issues"]] == [
        "target_is_control_token"
    ]


def test_many_to_many_accepts_plain_space_instead_of_dropped_tab() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001+C002 -> C001+C002 完整译文",
        groups,
        ledger,
        mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert result["_covered_cue_ids"] == ["c1", "c2"]


def test_many_to_many_joins_tab_separated_display_aliases() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001+C002 -> C001<TAB>C002<TAB>完整译文",
        groups,
        ledger,
        mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert result["_covered_cue_ids"] == ["c1", "c2"]


def test_one_to_one_rejects_bare_control_alias_as_target_text() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [{"cue_id": "c1", "source_text": "one"}],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="one_to_one")

    result = finalize_translation(
        "C001<TAB>C008", groups, ledger, mapping_mode="one_to_one"
    )

    assert [row["code"] for row in result["_cue_script_issues"]] == [
        "target_is_control_token"
    ]


def test_many_to_many_translation_salvages_tab_separated_join_aliases() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001+C002 -> C001+C002<TAB>合并译文",
        groups,
        ledger,
        mapping_mode="many_to_many",
    )

    assert result["group_results"][0]["meaning_units"][0][
        "source_evidence_cue_ids"
    ] == ["c1", "c2"]


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
        "C001\tU.S. Policy.",
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
        "C001\tso-called",
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
        "C001\tactually‡",
        ledger,
    )
    assert result["actions"][0]["disposition"] == "review"
    assert result["actions"][0]["confidence"] == "low"


def test_calibration_candidate_freezes_valid_rows_and_scopes_only_missing_cue() -> None:
    cues = [
        {
            "cue_id": "cue-1", "editable": True,
            "tokens": [{"token_id": "t1", "text": "hello"}],
        },
        {
            "cue_id": "cue-2", "editable": True,
            "tokens": [{"token_id": "t2", "text": "world"}],
        },
    ]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )
    result = finalize_calibration_candidate(
        "C001\tHello,",
        ledger,
    )
    assert result["_covered_cue_ids"] == ["cue-1"]
    assert result["_cue_script_issues"] == [{
        "code": "missing_cue_text",
        "alias": "C002",
        "cue_id": "cue-2",
        "detail": "C002 缺少可绑定的完整校准文本",
    }]
    assert {row["kind"] for row in result["actions"]} == {
        "set_case", "set_punctuation",
    }


def test_calibration_candidate_accepts_unambiguous_bare_alias_rows() -> None:
    cues = [{
        "cue_id": "cue-1", "editable": True,
        "tokens": [{"token_id": "t1", "text": "hello"}],
    }]
    wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )
    result = finalize_calibration_candidate("C001\tHello.", ledger)

    assert "1|OWN|hello" in wire
    assert result["_cue_script_issues"] == []
    assert result["_covered_cue_ids"] == ["cue-1"]


def test_calibration_repair_wire_contains_errors_and_frozen_aliases() -> None:
    cues = [
        {"cue_id": "cue-1", "editable": False, "source_text": "accepted"},
        {"cue_id": "cue-2", "editable": True, "source_text": "missing"},
    ]
    wire, _ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text",
        repair_feedback={
            "program_validation_errors": [{
                "code": "missing_cue_text", "cue_id": "cue-2",
                "detail": "missing output",
            }],
            "raw_model_response": "C001\taccepted",
        },
    )

    assert "ERROR\t2\tmissing_cue_text\tmissing output" in wire
    assert "1|CONTEXT|accepted" in wire
    assert "FROZEN\t" not in wire
    assert "REJECTED MODEL OUTPUT" not in wire
    assert "C001\taccepted" not in wire


def test_translation_limit_error_is_rendered_once_with_actionable_fields() -> None:
    groups = [{
        "group_id": "g1",
        "program_validation_errors": [{
            "code": "target_over_limit",
            "cue_ids": ["c1", "c2"],
            "count": 30,
            "limit": 24,
            "target_text": "过长译文",
        }],
        "cues": [
            {"cue_id": "c1", "source_text": "one", "editable": True},
            {"cue_id": "c2", "source_text": "two", "editable": True},
        ],
    }]

    wire, _ledger = render_translation_request(groups, mapping_mode="many_to_many")

    assert wire.count("target_over_limit") == 1
    assert "1,2: target_over_limit" in wire


def test_translation_wire_exposes_one_uniform_target_limit() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {
                "cue_id": "c1", "source_text": "one", "hard_limit": 24,
                "count_rule": "characters_excluding_spaces",
            },
            {
                "cue_id": "c2", "source_text": "two", "hard_limit": 24,
                "count_rule": "characters_excluding_spaces",
            },
        ],
    }]

    wire, _ledger = render_translation_request(groups, mapping_mode="many_to_many")

    assert wire.count("TARGET_LIMIT 24") == 1
    assert wire.count("COUNT_RULE characters_excluding_spaces") == 1
    assert "1|one\n2|two" in wire
    assert "REQUIRED_SOURCE_COVERAGE" not in wire


def test_many_to_many_repairs_short_join_alias_without_separator() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
            {"cue_id": "c3", "source_text": "three"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001+002 -> C001+002<TAB>前半\nC003 -> C003<TAB>后半", groups, ledger,
        mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert result["_covered_cue_ids"] == ["c1", "c2", "c3"]


def test_many_to_many_flattens_tab_separated_alias_and_join_fields() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
            {"cue_id": "c3", "source_text": "three"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C001 -> C001<TAB>前半\nC002+C003 -> C002+C003<TAB>合并译文", groups, ledger,
        mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert result["_covered_cue_ids"] == ["c1", "c2", "c3"]


def test_translation_patch_keeps_owned_alias_and_ignores_joined_context_alias() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "context", "editable": False},
            {"cue_id": "c2", "source_text": "owned", "editable": True},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")

    result = finalize_translation(
        "C002<TAB>修复译文", groups, ledger,
        mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert result["_covered_cue_ids"] == ["c2"]
    assert result["_cue_script_warnings"] == []


def test_translation_finalizer_normalizes_short_join_alias_and_stray_tag() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    result = finalize_translation(
        'C001-C002<TAB><COREDE="" translate="no">合并译文',
        groups, ledger, mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert result["group_results"][0]["meaning_units"][0]["target_text"] == "合并译文"


def test_many_to_many_derives_conservative_evidence_from_each_display_range() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [
            {"cue_id": "c1", "source_text": "one"},
            {"cue_id": "c2", "source_text": "two"},
            {"cue_id": "c3", "source_text": "three"},
        ],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    result = finalize_translation(
        "C001<TAB>甲\n"
        "C002-C003<TAB>乙。",
        groups, ledger, mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert result["_covered_cue_ids"] == ["c1", "c2", "c3"]
    assert [
        row["source_evidence_cue_ids"]
        for row in result["group_results"][0]["meaning_units"]
    ] == [["c1"], ["c2", "c3"]]


def test_many_to_many_marks_chinese_internal_comma_for_block_rewrite() -> None:
    groups = [{
        "group_id": "g1",
        "cues": [{"cue_id": "c1", "source_text": "one"}],
    }]
    _wire, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    result = finalize_translation(
        "C001 -> C001<TAB>他写道，美国已经准备就绪",
        groups, ledger, mapping_mode="many_to_many",
    )

    assert result["_cue_script_issues"] == []
    assert "target_contains_clause_boundary_punctuation" not in [
        row["code"] for row in result["_cue_script_warnings"]
    ]


def test_calibration_finalizer_surfaces_count_changing_rewrite_for_review() -> None:
    cues = [{
        "cue_id": "cue-real", "editable": True,
        "tokens": [
            {"token_id": "t1", "text": "military"},
            {"token_id": "t2", "text": "terror"},
            {"token_id": "t3", "text": "strength"},
        ],
    }]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )
    result = finalize_calibration_candidate(
        "C001\tmilitary strength", ledger
    )

    assert result["_cue_script_issues"] == []
    assert len(result["actions"]) == 1
    assert result["actions"][0]["kind"] == "replace_span"
    assert result["actions"][0]["disposition"] == "review"
    assert result["actions"][0]["token_ids"] == ["t1", "t2", "t3"]

def test_calibration_internal_periods_are_safe_punctuation_not_lexical_review() -> None:
    cues = [
        {
            "cue_id": "cue-us", "editable": True,
            "tokens": [{"token_id": "t1", "text": "US"}],
        },
        {
            "cue_id": "cue-usa", "editable": True,
            "tokens": [{"token_id": "t2", "text": "USA"}],
        },
    ]
    _wire, ledger = render_cue_request(
        cues, task="CALIBRATE", instructions="return corrected text"
    )
    result = finalize_calibration("C001\tU.S.\nC002\tU.S.A.", ledger)

    assert [action["kind"] for action in result["actions"]] == [
        "set_punctuation", "set_punctuation"
    ]
    assert all(action["disposition"] == "apply" for action in result["actions"])
    token_map = {
        "t1": type("Token", (), {"text": "US"})(),
        "t2": type("Token", (), {"text": "USA"})(),
    }
    accepted, rejected = _validated_calibration_contract_actions(
        result, ["t1", "t2"], token_map,
        {"t1": "cue-us", "t2": "cue-usa"},
    )
    assert rejected == []
    assert len(accepted) == 2
