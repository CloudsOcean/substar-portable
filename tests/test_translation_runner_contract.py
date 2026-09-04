from __future__ import annotations

from types import SimpleNamespace

import substar_core.editor.translation.runner as translation_runner
import substar_core.editor.translation.contextual as contextual_translation
from substar_core.editor.translation.artifacts import (
    TRANSLATION_PROGRESS_FILENAME,
    TRANSLATION_REVISION_FILENAME,
    TRANSLATION_SUBTITLE_FILENAME,
)
from substar_core.editor.translation.contextual import _presentation_plan
from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    DisplayCue,
    DisplayToken,
    EditorDocument,
    SourceToken,
)
from substar_core.editor.translation.result_policy import (
    accepted_translation_rows,
    source_hashes_by_lineage,
    translated_text_by_source_cue,
)
from substar_core.cue_script import output_contract
from substar_core.prompt_registry import render_prompt


def test_translation_runner_imports_the_canonical_export_module() -> None:
    assert translation_runner.render_document_srt is not None
    assert TRANSLATION_REVISION_FILENAME == "revision.json"
    assert TRANSLATION_SUBTITLE_FILENAME == "bilingual.srt"
    assert TRANSLATION_PROGRESS_FILENAME == "progress.json"


def test_translation_system_prompt_omits_empty_glossary_and_is_shared_by_repair() -> None:
    assert contextual_translation._translation_system_prompt(
        "primary", "direction", []
    ) == "primary\n\ndirection"
    assert contextual_translation._translation_system_prompt(
        "repair", "direction", [{
            "source": "ASR", "standard_source": "ASR", "aliases": [],
            "target": "识别", "do_not_translate": False,
        }]
    ).startswith("repair\n\ndirection\n\n")


def test_unrepaired_translation_units_remain_problem_cues_without_discarding_accepted_work() -> None:
    source = [
        {"cue_id": "cue-a", "source_hash": "hash-a"},
        {"cue_id": "cue-b", "source_hash": "hash-b"},
    ]
    rows, problems = accepted_translation_rows(source, {"cue-a": "译文"})
    assert rows == [
        {
            "cue_id": "cue-a", "source_hash": "hash-a", "target_text": "译文",
            "translation_status": "translated",
            "issue_code": None, "editable": True,
        },
        {
            "cue_id": "cue-b", "source_hash": "hash-b", "target_text": "",
            "translation_status": "manual_required",
            "issue_code": "translation_unresolved", "editable": True,
        },
    ]
    assert problems == ["cue-b"]


def test_translation_runner_has_no_retired_artifact_names() -> None:
    source = translation_runner.Path(translation_runner.__file__).read_text(encoding="utf-8")
    assert "export_v2" not in source
    assert '"T1"' not in source
    assert "translation_revision_v2" not in source


def test_translation_finalizer_resolves_new_cues_through_source_lineage() -> None:
    active = SimpleNamespace(value="active")
    document = SimpleNamespace(cues=[
        SimpleNamespace(
            cue_id="new-cue-1",
            state=active,
            target=SimpleNamespace(target_text="第一段"),
            mapping={"source_cue_ids": ["source-cue-1"]},
        ),
        SimpleNamespace(
            cue_id="new-cue-2",
            state=active,
            target=SimpleNamespace(target_text="第二段"),
            mapping={"source_cue_ids": ["source-cue-1"]},
        ),
        SimpleNamespace(
            cue_id="new-cue-3",
            state=active,
            target=SimpleNamespace(target_text="另一条"),
            mapping={"source_cue_ids": ["source-cue-2"]},
        ),
    ])
    assert translated_text_by_source_cue(document) == {
        "source-cue-1": "第一段\n第二段",
        "source-cue-2": "另一条",
    }


def test_unresolved_translation_preserves_model_candidate_but_is_not_accepted() -> None:
    provenance = ChangeProvenance(
        kind=ChangeKind.SOURCE,
        operation="translation-unresolved-test",
    )
    source_tokens = [
        SourceToken.create(index=0, text="translated", start=0.0, end=0.5),
        SourceToken.create(index=1, text="unresolved", start=0.5, end=1.0),
    ]
    display_tokens = [
        DisplayToken.create(
            position=index,
            text=source.text,
            source_token_ids=[source.token_id],
            provenance=provenance,
        )
        for index, source in enumerate(source_tokens)
    ]
    cues = [
        DisplayCue.create(
            index=index,
            display_token_ids=[display.token_id],
            start=source.start,
            end=source.end,
        )
        for index, (source, display) in enumerate(zip(source_tokens, display_tokens, strict=True))
    ]
    document = EditorDocument.create(
        source_tokens=source_tokens,
        display_tokens=display_tokens,
        cues=cues,
        document_key="translation-unresolved-test",
    )
    plan = {
        "group_id": "group-1",
        "meaning_units": [{
            "meaning_unit_id": "unit-1",
            "target_text": "已翻译",
            "source_evidence_cue_ids": [cues[0].cue_id],
        }],
        "cue_assignments": [{
            "cue_id": cues[0].cue_id,
            "meaning_unit_id": "unit-1",
        }],
    }

    candidate, report = contextual_translation.materialize_presentation(
        document, [plan], "zh-CN", {cues[1].cue_id: "待人工确认的候选译文"}
    )

    unresolved = next(
        cue for cue in candidate.cues
        if cue.mapping.get("translation_unresolved") is True
    )
    assert unresolved.cue_id == cues[1].cue_id
    assert unresolved.target is not None
    assert unresolved.target.target_text == "待人工确认的候选译文"
    assert unresolved.target.translation_status == "manual_required"
    assert unresolved.target.issue_code == "translation_unresolved"
    assert unresolved.target.editable is True
    assert unresolved.mapping["requires_manual_translation"] is True
    assert unresolved.mapping["candidate_preserved"] is True
    assert report["unresolved_source_cue_ids"] == [cues[1].cue_id]
    assert report["preserved_candidate_cue_ids"] == [cues[1].cue_id]
    assert translated_text_by_source_cue(candidate) == {
        cues[0].cue_id: "已翻译",
    }


def test_translation_repair_is_exactly_one_request_per_failed_group(monkeypatch) -> None:
    group = {"group_id": "g1", "cues": [_cue("c1", 0)]}
    calls = 0

    def invalid_repair(**_kwargs):
        nonlocal calls
        calls += 1
        return {"group_results": []}, {"transport_attempt_count": 1}

    monkeypatch.setattr(contextual_translation, "api_call", invalid_repair)
    _plans, report = contextual_translation.complete_results(
        settings={"translation_repair_attempts": 99},
        repair_prompt="repair",
        groups=[group],
        response={"group_results": []},
    )

    assert calls == 1
    assert report["model_repair"]["repair_phase_entered"] is True
    assert report["model_repair"]["groups"][0]["repair_request_count"] == 1


def test_translation_repairs_all_invalid_groups_in_one_block_request(monkeypatch) -> None:
    groups = [
        {"group_id": "g1", "cues": [_cue("c1", 0)]},
        {"group_id": "g2", "cues": [_cue("c2", 1)]},
    ]
    calls = []
    progress = []

    def repair_block(**kwargs):
        calls.append(kwargs["groups"])
        return {
            "group_results": [],
            "_wire_units": [
                {"cue_ids": [cue["cue_id"]], "target_text": "译文"}
                for group in kwargs["groups"] for cue in group["cues"]
                if cue.get("editable", True)
            ],
            "_covered_cue_ids": [
                cue["cue_id"] for group in kwargs["groups"] for cue in group["cues"]
                if cue.get("editable", True)
            ],
            "_cue_script_issues": [],
        }, {}

    monkeypatch.setattr(contextual_translation, "api_call", repair_block)
    monkeypatch.setattr(
        contextual_translation, "_presentation_plan",
        lambda group, row, _mapping_mode="many_to_many": (
            {"group_id": group["group_id"]} if row else None
        ),
    )

    plans, report = contextual_translation.complete_results(
        settings={"translation_workers": 2}, repair_prompt="repair",
        groups=groups, response={"group_results": []},
        mapping_mode="one_to_one",
        group_block_ids={"g1": "b1", "g2": "b1"},
        progress_callback=lambda done, total, accepted: progress.append(
            (done, total, accepted)
        ),
    )

    assert len(calls) == 1
    assert len(calls[0]) == 2
    assert {row["group_id"] for row in plans} == {"g1", "g2"}
    assert report["invalid_group_ids"] == []
    assert progress == [(0, 1, 0), (1, 1, 1)]


def test_translation_repair_splits_large_failed_block(monkeypatch) -> None:
    groups = [
        {"group_id": f"g{index}", "cues": [_cue(f"c{index}", index)]}
        for index in range(7)
    ]
    calls = []

    def repair_block(**kwargs):
        calls.append(kwargs["groups"])
        return {
            "group_results": [],
            "_wire_units": [
                {"cue_ids": [cue["cue_id"]], "target_text": "译文"}
                for group in kwargs["groups"] for cue in group["cues"]
                if cue.get("editable", True)
            ],
            "_covered_cue_ids": [
                cue["cue_id"] for group in kwargs["groups"] for cue in group["cues"]
                if cue.get("editable", True)
            ],
            "_cue_script_issues": [],
        }, {}

    monkeypatch.setattr(contextual_translation, "api_call", repair_block)
    monkeypatch.setattr(
        contextual_translation, "_presentation_plan",
        lambda group, row, _mapping_mode="many_to_many": (
            {"group_id": group["group_id"]} if row else None
        ),
    )
    plans, report = contextual_translation.complete_results(
        settings={
            "translation_workers": 2,
            "translation_repair_max_groups": 3,
            "translation_repair_max_cues": 12,
        },
        repair_prompt="repair",
        groups=groups,
        response={"group_results": []},
        mapping_mode="one_to_one",
        group_block_ids={group["group_id"]: "b1" for group in groups},
    )

    assert [len(call) for call in calls] == [7]
    assert len(plans) == 7
    assert report["invalid_group_ids"] == []


def test_many_to_many_translation_repairs_each_local_scope_independently(monkeypatch) -> None:
    groups = [
        {"group_id": "g1", "cues": [_cue("c1", 0)]},
        {"group_id": "g2", "cues": [_cue("c2", 1)]},
    ]
    calls = []

    def repair_block(**kwargs):
        calls.append(kwargs["groups"])
        return {
            "group_results": [],
            "_wire_units": [
                {"cue_ids": [cue["cue_id"]], "target_text": "译文"}
                for group in kwargs["groups"] for cue in group["cues"]
                if cue.get("editable", True)
            ],
            "_covered_cue_ids": [
                cue["cue_id"] for group in kwargs["groups"] for cue in group["cues"]
                if cue.get("editable", True)
            ],
            "_cue_script_issues": [],
        }, {}

    monkeypatch.setattr(contextual_translation, "api_call", repair_block)
    monkeypatch.setattr(
        contextual_translation, "_presentation_plan",
        lambda group, row, _mapping_mode="many_to_many": (
            {"group_id": group["group_id"]} if row else None
        ),
    )

    plans, report = contextual_translation.complete_results(
        settings={"translation_workers": 2}, repair_prompt="repair",
        groups=groups, response={"group_results": []},
        mapping_mode="many_to_many",
        group_block_ids={"g1": "b1", "g2": "b1"},
    )

    assert [len(call) for call in calls] == [2]
    assert {row["group_id"] for row in plans} == {"g1", "g2"}
    assert report["invalid_group_ids"] == []


def test_translation_block_patch_freezes_valid_aliases_and_repairs_only_gap(monkeypatch) -> None:
    groups = [
        {"group_id": f"g{index}", "cues": [_cue(f"c{index}", index)]}
        for index in range(1, 5)
    ]
    calls = []

    def repair(**kwargs):
        calls.append(kwargs["groups"])
        return {
            "group_results": [],
            "_wire_units": [{"cue_ids": ["c2"], "target_text": "二"}],
            "_covered_cue_ids": ["c2"],
            "_cue_script_issues": [],
        }, {}

    monkeypatch.setattr(contextual_translation, "api_call", repair)
    plans, report = contextual_translation.complete_results(
        settings={"translation_workers": 2}, repair_prompt="repair",
        groups=groups,
        response={
            "group_results": [],
            "_wire_units": [
                {"cue_ids": ["c1"], "target_text": "一"},
                {"cue_ids": ["c3"], "target_text": "三"},
                {"cue_ids": ["c4"], "target_text": "四"},
            ],
            "_cue_script_issues": [{
                "code": "missing_cue_translation", "cue_id": "c2",
                "detail": "C002 missing",
            }],
        },
        mapping_mode="one_to_one",
        group_block_ids={
            "g1": "b1", "g2": "b1", "g3": "b1", "g4": "b2",
        },
    )

    assert len(calls) == 1
    assert [
        cue.get("editable") for group in calls[0] for cue in group["cues"]
    ] == [False, True, False]
    assert calls[0][0]["program_validation_errors"][0]["cue_id"] == "c2"
    assert {plan["group_id"] for plan in plans} == {"g1", "g2", "g3", "g4"}
    assert set(
        report["model_repair"]["groups"][0]["attempts"][0]["frozen_cue_ids"]
    ) == {"c1", "c3"}
    assert report["invalid_group_ids"] == []


def test_translation_length_repairs_are_aggregated_once_per_source_block(monkeypatch) -> None:
    groups = [
        {"group_id": "g1", "cues": [_cue("c1", 0)]},
        {"group_id": "g2", "cues": [_cue("c2", 1)]},
    ]
    calls = []

    def repair(**kwargs):
        calls.append(kwargs["groups"])
        return {
            "group_results": [],
            "_wire_units": [
                {"cue_ids": ["c1"], "target_text": "短一"},
                {"cue_ids": ["c2"], "target_text": "短二"},
            ],
            "_covered_cue_ids": ["c1", "c2"],
            "_cue_script_issues": [],
        }, {}

    monkeypatch.setattr(contextual_translation, "api_call", repair)
    plans, report = contextual_translation.complete_results(
        settings={"translation_workers": 2}, repair_prompt="repair",
        groups=groups,
        response={
            "group_results": [],
            "_wire_units": [
                {"cue_ids": ["c1"], "target_text": "超" * 30},
                {"cue_ids": ["c2"], "target_text": "长" * 30},
            ],
            "_cue_script_issues": [],
        },
        mapping_mode="one_to_one",
        group_block_ids={"g1": "b1", "g2": "b1"},
    )

    assert len(calls) == 1
    assert len(calls[0][0]["program_validation_errors"]) == 2
    assert "_repair_context" not in calls[0][0]
    assert [plan["meaning_units"][0]["target_text"] for plan in plans] == ["短一", "短二"]
    assert report["model_repair"]["groups"][0]["repair_kind"] == "target_over_limit"


def test_translation_primary_progress_counts_execution_blocks(monkeypatch) -> None:
    batches = [
        {"block_id": "b1", "groups": [{"group_id": "g1"}, {"group_id": "g2"}]},
        {"block_id": "b2", "groups": [{"group_id": "g3"}]},
    ]
    progress = []

    monkeypatch.setattr(
        contextual_translation,
        "api_call",
        lambda **kwargs: ({
            "group_results": [
                {"group_id": group["group_id"]} for group in kwargs["groups"]
            ]
        }, {}),
    )

    contextual_translation.call_block_batches(
        settings={"translation_workers": 1},
        system_prompt="translate",
        batches=batches,
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert progress == [(1, 2), (2, 2)]


def test_non_repairable_translation_failure_creates_no_repair_request(monkeypatch) -> None:
    group = {"group_id": "g1", "cues": [_cue("c1", 0)]}
    calls = 0

    def unexpected_repair(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("transport failures must not enter content repair")

    monkeypatch.setattr(contextual_translation, "api_call", unexpected_repair)
    plans, report = contextual_translation.complete_results(
        settings={"translation_repair_attempts": 1},
        repair_prompt="repair",
        groups=[group],
        response={"group_results": []},
        non_repairable_group_ids={"g1"},
    )

    assert plans == []
    assert calls == 0
    assert report["model_repair"]["repair_phase_entered"] is False
    assert report["model_repair"]["attempted_group_ids"] == []
    assert report["invalid_group_ids"] == ["g1"]


def test_provider_wide_translation_transport_failure_is_not_false_delivery() -> None:
    batches = [
        {"block_id": "b1", "groups": []},
        {"block_id": "b2", "groups": []},
    ]
    failures = {
        "b1": {"error": "network unavailable", "non_repairable": True},
        "b2": {"error": "network unavailable", "non_repairable": True},
    }
    try:
        contextual_translation._reject_provider_wide_translation_failure(
            batches, failures
        )
    except RuntimeError as exc:
        assert "所有执行块均请求失败" in str(exc)
    else:
        raise AssertionError("a provider-wide outage must fail the task")

    contextual_translation._reject_provider_wide_translation_failure(
        batches,
        {**failures, "b2": {"response": {"group_results": []}}},
    )


def test_one_to_one_mode_freezes_one_source_cue_per_model_group(monkeypatch) -> None:
    groups = [{
        "group_id": "meaning-1",
        "execution_block_id": "block-1",
        "cues": [
            {"cue_id": "c1", "source_text": "first"},
            {"cue_id": "c2", "source_text": "second"},
        ],
    }]
    monkeypatch.setattr(
        contextual_translation,
        "translation_groups",
        lambda _document, _settings: (groups, {}, {}),
    )

    result = contextual_translation.clean_groups(
        object(), {"translation_mapping_mode": "one_to_one"}
    )

    assert [row["group_id"] for row in result] == ["line:c1", "line:c2"]
    assert [[cue["cue_id"] for cue in row["cues"]] for row in result] == [
        ["c1"], ["c2"],
    ]


def test_one_to_one_contract_accepts_only_direct_text_for_its_own_cue() -> None:
    group = {"group_id": "line:c1", "cues": [_cue("c1", 0)]}
    accepted = _presentation_plan(
        group,
        {"group_id": "line:c1", "cue_id": "c1", "target_text": "译文"},
        "one_to_one",
    )
    assert accepted is not None
    assert accepted["meaning_units"][0]["target_text"] == "译文"

    assert _presentation_plan(
        group,
        {"group_id": "line:c1", "cue_id": "c2", "target_text": "串组译文"},
        "one_to_one",
    ) is None
    assert _presentation_plan(
        group,
        {
            "group_id": "line:c1",
            "meaning_units": [{
                "meaning_unit_id": "u1", "target_text": "旧结构",
                "source_evidence_cue_ids": ["c1"],
            }],
            "cue_assignments": [{"cue_id": "c1", "meaning_unit_id": "u1"}],
        },
        "one_to_one",
    ) is None


def test_rejected_single_cue_binding_still_preserves_its_nonempty_candidate() -> None:
    group = {"group_id": "line:c1", "cues": [_cue("c1", 0)]}
    _plans, report = contextual_translation.complete_results(
        settings={"translation_workers": 1},
        repair_prompt="repair",
        groups=[group],
        response={
            "group_results": [{
                "group_id": "line:c1",
                "meaning_units": [{
                    "meaning_unit_id": "u1",
                    "target_text": "可保留的中文候选",
                    "source_evidence_cue_ids": ["foreign-cue"],
                }],
                "cue_assignments": [{
                    "cue_id": "foreign-cue", "meaning_unit_id": "u1",
                }],
            }]
        },
        mapping_mode="one_to_one",
        non_repairable_group_ids={"line:c1"},
    )

    assert report["invalid_group_ids"] == ["line:c1"]
    assert report["candidate_targets_by_cue"] == {
        "c1": "可保留的中文候选"
    }


def test_invalid_translation_response_is_not_written_to_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        contextual_translation,
        "call_translation_model",
        lambda **_kwargs: ({"group_results": []}, {"model": "test"}),
    )
    response, telemetry = contextual_translation.api_call(
        settings={
            "translation_api_base_url": "https://example.invalid",
            "translation_api_key": "secret",
            "translation_api_model": "test",
        },
        system_prompt="prompt",
        groups=[{"group_id": "g1", "cues": [_cue("c1", 0)]}],
        cache_directory=tmp_path,
        cache_scope="test",
        cache_validator=lambda _value: False,
    )
    assert response == {"group_results": []}
    assert telemetry["cache_hit"] is False
    assert list(tmp_path.glob("*.json")) == []


def test_translation_raw_wire_cache_rebinds_aliases_to_current_project_ids(
    tmp_path, monkeypatch,
) -> None:
    calls = 0

    def model(**_kwargs):
        nonlocal calls
        calls += 1
        return "C001\t译文", {"model": "test"}

    monkeypatch.setattr(contextual_translation, "call_translation_model", model)
    settings = {
        "translation_api_base_url": "https://example.invalid",
        "translation_api_key": "secret",
        "translation_api_model": "test",
    }
    first, first_telemetry = contextual_translation.api_call(
        settings=settings, system_prompt="prompt",
        groups=[{"group_id": "old-group", "cues": [_cue("old-cue", 0)]}],
        mapping_mode="one_to_one", cache_directory=tmp_path,
        cache_scope="raw-cache-rebind-test",
    )
    second, second_telemetry = contextual_translation.api_call(
        settings=settings, system_prompt="prompt",
        groups=[{"group_id": "new-group", "cues": [_cue("new-cue", 0)]}],
        mapping_mode="one_to_one", cache_directory=tmp_path,
        cache_scope="raw-cache-rebind-test",
    )

    assert calls == 1
    assert first_telemetry["cache_hit"] is False
    assert second_telemetry["cache_hit"] is True
    assert first["group_results"][0]["cue_id"] == "old-cue"
    assert second["group_results"][0]["cue_id"] == "new-cue"


def test_staleness_indexes_rematerialized_cues_by_source_lineage() -> None:
    active = SimpleNamespace(value="active")
    document = SimpleNamespace(
        display_tokens=[SimpleNamespace(token_id="token-1", text="Same source", state=active)],
        cues=[SimpleNamespace(
            cue_id="new-presentation-cue",
            state=active,
            display_token_ids=["token-1"],
            mapping={"source_cue_ids": ["translated-source-cue"]},
        )],
    )
    hashes = source_hashes_by_lineage(document)
    assert set(hashes) == {"translated-source-cue"}
    assert len(hashes["translated-source-cue"]) == 1


def _cue(cue_id: str, index: int, *, maximum_cps: float = 12.0) -> dict[str, object]:
    return {
        "cue_id": cue_id,
        "start": index * 3.0,
        "end": (index + 1) * 3.0,
        "hard_limit": 25,
        "count_rule": "characters_excluding_spaces",
        "maximum_cps": maximum_cps,
    }


def test_meaning_unit_contract_stores_text_once_and_accepts_112_assignment() -> None:
    group = {
        "group_id": "component_0001",
        "cues": [_cue(cue_id, index) for index, cue_id in enumerate(("c1", "c2", "c3"))],
    }
    row = {
        "group_id": "component_0001",
        "meaning_units": [
            {
                "meaning_unit_id": "t1",
                "target_text": "因此该公司被责令在10天内支付",
                "source_evidence_cue_ids": ["c1", "c3"],
            },
            {
                "meaning_unit_id": "t2",
                "target_text": "1030万元人民币",
                "source_evidence_cue_ids": ["c2"],
            },
        ],
        "cue_assignments": [
            {"cue_id": "c1", "meaning_unit_id": "t1"},
            {"cue_id": "c2", "meaning_unit_id": "t1"},
            {"cue_id": "c3", "meaning_unit_id": "t2"},
        ],
    }
    plan = _presentation_plan(group, row)
    assert plan is not None
    assert plan["source_cue_ids"] == ["c1", "c2", "c3"]
    assert [unit["target_text"] for unit in plan["meaning_units"]] == [
        "因此该公司被责令在10天内支付",
        "1030万元人民币",
    ]
    assert [cue["meaning_unit_id"] for cue in plan["cue_assignments"]] == ["t1", "t1", "t2"]
    assert all("target_text" not in cue for cue in plan["cue_assignments"])


def test_presentation_contract_rejects_assignment_reordering() -> None:
    group = {
        "group_id": "component_0001",
        "cues": [{"cue_id": cue_id} for cue_id in ("c1", "c2", "c3")],
    }
    row = {
        "meaning_units": [
            {"meaning_unit_id": "t1", "target_text": "甲", "source_evidence_cue_ids": ["c1", "c3"]},
            {"meaning_unit_id": "t2", "target_text": "乙", "source_evidence_cue_ids": ["c2"]},
        ],
        "cue_assignments": [
            {"cue_id": "c1", "meaning_unit_id": "t1"},
            {"cue_id": "c3", "meaning_unit_id": "t2"},
            {"cue_id": "c2", "meaning_unit_id": "t1"},
        ],
    }
    assert _presentation_plan(group, row) is None


def test_repeated_unit_cps_uses_combined_consecutive_display_duration() -> None:
    group = {
        "group_id": "component_0001",
        "cues": [
            {
                "cue_id": cue_id,
                "start": index,
                "end": index + 1,
                "hard_limit": 25,
                "count_rule": "characters_excluding_spaces",
                "maximum_cps": 3.0,
            }
            for index, cue_id in enumerate(("c1", "c2"))
        ],
    }
    row = {
        "meaning_units": [{
            "meaning_unit_id": "t1",
            "target_text": "完整译文",
            "source_evidence_cue_ids": ["c1", "c2"],
        }],
        "cue_assignments": [
            {"cue_id": "c1", "meaning_unit_id": "t1"},
            {"cue_id": "c2", "meaning_unit_id": "t1"},
        ],
    }
    assert _presentation_plan(group, row) is not None


def test_presentation_contract_rejects_unknown_unit_assignment() -> None:
    group = {"group_id": "component_0001", "cues": [{"cue_id": "c1"}]}
    row = {
        "meaning_units": [{
            "meaning_unit_id": "t1",
            "target_text": "译文",
            "source_evidence_cue_ids": ["c1"],
        }],
        "cue_assignments": [{"cue_id": "c1", "meaning_unit_id": "missing"}],
    }
    assert _presentation_plan(group, row) is None


def test_translation_materializer_contains_no_content_rewrite_helpers() -> None:
    contextual_source = translation_runner.Path(__file__).parents[1] / (
        "substar_core/editor/translation/contextual.py"
    )
    text = contextual_source.read_text(encoding="utf-8")
    assert "_monotonic_source_ranges" not in text
    assert "_split_target_text" not in text
    assert "_repair_dangling_target_boundaries" not in text


def test_translation_main_and_repair_use_their_configured_stage_policies(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_call_translation_model(**kwargs):
        calls.append(kwargs)
        return {"group_results": []}, {"model": kwargs["model"]}

    monkeypatch.setattr(
        contextual_translation,
        "call_translation_model",
        fake_call_translation_model,
    )
    settings = {
        "translation_api_base_url": "https://example.invalid",
        "translation_api_key": "secret",
        "translation_api_model": "global-model",
        "stage_translation_model": "main-model",
        "stage_translation_thinking_mode": "enabled",
        "stage_translation_reasoning_effort": "max",
        "stage_translation_max_tokens": 12000,
        "stage_translation_temperature": 0.2,
        "stage_translation_repair_model": "repair-model",
        "stage_translation_repair_thinking_mode": "disabled",
        "stage_translation_repair_reasoning_effort": "high",
        "stage_translation_repair_max_tokens": 6000,
        "stage_translation_repair_temperature": 0.0,
    }

    contextual_translation.api_call(settings=settings, system_prompt="p", groups=[])
    contextual_translation.api_call(
        settings=settings,
        system_prompt="p",
        groups=[],
        stage_name="translation_repair",
    )

    assert calls[0]["model"] == "main-model"
    assert calls[0]["thinking_mode"] == "enabled"
    assert calls[0]["max_tokens"] == 12000
    assert calls[1]["model"] == "repair-model"
    assert calls[1]["thinking_mode"] == "disabled"
    assert calls[1]["max_tokens"] == 6000


def test_translation_preserves_success_and_repairs_failed_groups_independently(monkeypatch) -> None:
    groups = [
        {"group_id": group_id, "cues": [_cue(cue_id, index)]}
        for index, (group_id, cue_id) in enumerate((
            ("g1", "c1"), ("g2", "c2"), ("g3", "c3")
        ))
    ]

    def row(group_id: str, cue_id: str) -> dict[str, object]:
        return {
            "group_id": group_id,
            "meaning_units": [{
                "meaning_unit_id": f"u-{group_id}",
                "target_text": f"译文-{group_id}",
                "source_evidence_cue_ids": [cue_id],
            }],
            "cue_assignments": [{"cue_id": cue_id, "meaning_unit_id": f"u-{group_id}"}],
        }

    repair_calls: list[list[str]] = []

    def fake_api_call(**kwargs):
        repair_calls.append([group["group_id"] for group in kwargs["groups"]])
        return {"group_results": [row("g2", "c2")]}, {"model": "repair"}

    monkeypatch.setattr(contextual_translation, "api_call", fake_api_call)
    plans, report = contextual_translation.complete_results(
        settings={"translation_repair_attempts": 1},
        repair_prompt="repair",
        groups=groups,
        response={"group_results": [row("g1", "c1")]},
    )

    assert repair_calls == [["g2"], ["g3"]]
    assert {plan["group_id"] for plan in plans} == {"g1", "g2"}
    assert report["invalid_group_ids"] == ["g3"]


def test_main_translation_prompt_uses_target_language_boundaries() -> None:
    prompt = render_prompt(
        "contextual_translation", variant="en_to_zh", mode="many_to_many"
    ).text
    assert "C 别名是唯一绑定依据" in prompt
    assert "C001+C002" in prompt
    assert "即使合并后的文本仍未超过 `hard_limit`" in prompt
    assert "JSON" in output_contract("TRANSLATE")
    assert "group_id" not in prompt


def test_one_to_one_prompt_has_a_distinct_direct_output_contract() -> None:
    prompt = render_prompt(
        "contextual_translation", variant="en_to_zh", mode="one_to_one"
    )
    assert prompt.mode == "one_to_one"
    assert "每行只能包含一个 C 别名" in prompt.text
    assert "JSON" in output_contract("TRANSLATE")
    assert "不得把相邻两条合成同一译文" in prompt.text
    assert "不得为了调整目标语语序" in prompt.text
    assert "否定词" in prompt.text
