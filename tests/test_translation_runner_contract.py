from __future__ import annotations

import json
from types import SimpleNamespace

import scripts.run_production_translation as translation_runner
import substar_core.editor.translation.contextual as contextual_translation
from substar_core.editor.translation.artifacts import (
    TRANSLATION_PROGRESS_FILENAME,
    TRANSLATION_PROGRESS_SCHEMA,
    TRANSLATION_REVISION_FILENAME,
    TRANSLATION_STAGE_ID,
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
from substar_core.editor.translation.service import (
    _accepted_translation_rows,
    _progress,
    _source_hashes_by_lineage,
    _translated_text_by_source_cue,
)
from substar_core.prompt_registry import render_prompt


def test_translation_runner_imports_the_canonical_export_module() -> None:
    assert translation_runner.render_document_srt is not None
    assert TRANSLATION_REVISION_FILENAME == "revision.json"
    assert TRANSLATION_SUBTITLE_FILENAME == "bilingual.srt"
    assert TRANSLATION_PROGRESS_FILENAME == "progress.json"


def test_unrepaired_translation_units_remain_problem_cues_without_discarding_accepted_work() -> None:
    source = [
        {"cue_id": "cue-a", "source_hash": "hash-a"},
        {"cue_id": "cue-b", "source_hash": "hash-b"},
    ]
    accepted, problems = _accepted_translation_rows(source, {"cue-a": "译文"})
    assert accepted == [{"cue_id": "cue-a", "source_hash": "hash-a", "target_text": "译文"}]
    assert problems == ["cue-b"]


def test_translation_progress_uses_the_status_reader_contract() -> None:
    payload = json.dumps({
        "schema_version": TRANSLATION_PROGRESS_SCHEMA,
        "stages": {
            TRANSLATION_STAGE_ID: {
                "status": "completed",
                "planned": 1,
                "accepted": 1,
            }
        },
    })

    class ProgressPath:
        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            return payload

    progress, _message = _progress(ProgressPath())
    assert abs(progress - 0.95) < 1e-9


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
    assert _translated_text_by_source_cue(document) == {
        "source-cue-1": "第一段\n第二段",
        "source-cue-2": "另一条",
    }


def test_unresolved_translation_is_editable_but_not_counted_as_accepted() -> None:
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
        document, [plan], "zh-CN"
    )

    unresolved = next(
        cue for cue in candidate.cues
        if cue.mapping.get("translation_unresolved") is True
    )
    assert unresolved.cue_id == cues[1].cue_id
    assert unresolved.target is not None
    assert unresolved.target.target_text == "unresolved"
    assert unresolved.mapping["requires_manual_translation"] is True
    assert report["unresolved_source_cue_ids"] == [cues[1].cue_id]
    assert _translated_text_by_source_cue(candidate) == {
        cues[0].cue_id: "已翻译",
    }


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
    hashes = _source_hashes_by_lineage(document)
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
    prompt = render_prompt("contextual_translation", variant="en_to_zh").text
    assert "拒绝上限，不是推荐长度、填充目标或合并标准" in prompt
    assert "即使合并后的文本仍未超过 `hard_limit`" in prompt
    assert "母语字幕编辑者是否会自然期待显示文字向前推进" in prompt
    assert "具有明确关系标记的自然目标语从句" in prompt
    assert "由源文上下文唯一确定的成分" in prompt
    assert "不能因为整句未超限就直接决定 `1-1-1`" in prompt
    assert "中国网友也表示 / 路易威登不够了解中国市场" in prompt
    assert "许多用户已纷纷给出新的标志方案" in prompt
    assert "c1 我们必须结束这场争端 / c2 恢复正常贸易" not in prompt
