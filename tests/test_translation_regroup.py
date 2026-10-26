import copy
import json

import pytest

from substar_core.editor.translation import regroup
from substar_core.prompt_registry import render_prompt, frozen_prompt


def groups(sizes=(10, 10, 10)):
    result, start = [], 0
    for index, size in enumerate(sizes):
        result.append({"group_id": f"g{index}", "execution_block_id": f"g{index}",
                       "cues": [{"cue_id": f"c{i}", "source_text": f"English phrase {i}",
                                 "start": i, "end": i + 0.9,
                                 "target_language": "zh-CN", "hard_limit": 20}
                                for i in range(start, start + size)]})
        start += size
    return result


def test_disabled_regroup_makes_no_requests(monkeypatch):
    monkeypatch.setattr(regroup, "resolve_stage_request", lambda *_: pytest.fail("model called"))
    original = groups()
    assert regroup.review_request_groups(original, {}) is original
    assert regroup.review_request_groups(groups((10,)), {"translation_regroup": True})[0]["group_id"] == "g0"


def test_regroup_preserves_source_and_uses_five_cues_each_side(monkeypatch):
    original = groups()
    frozen = copy.deepcopy(original)
    requests = []

    def model(**kwargs):
        value = json.loads(kwargs["user_text"])
        requests.append(value)
        return str(value["CURRENT_END"] + 2), {}

    monkeypatch.setattr(regroup, "call_text_model", model)
    result = regroup.review_request_groups(original, {"translation_regroup": True})
    assert [len(g["cues"]) for g in result] == [12, 10, 8]
    assert [c for g in result for c in g["cues"]] == [c for g in original for c in g["cues"]]
    assert original == frozen
    assert len(requests) == 2
    for value in requests:
        end = value["CURRENT_END"]
        assert [c["number"] for c in value["cues"]] == list(range(end - 4, end + 6))


@pytest.mark.parametrize("raw", ["0", "999", "10 and 12", "```10```"])
def test_invalid_regroup_is_rejected_without_mutating_input(monkeypatch, raw):
    original = groups((10, 10))
    before = copy.deepcopy(original)
    monkeypatch.setattr(regroup, "call_text_model", lambda **_: (raw, {}))
    result = regroup.review_request_groups(original, {"translation_regroup": True})
    assert [g["cues"] for g in result] == [g["cues"] for g in original]
    assert original == before


def test_route_boundaries_cannot_move(monkeypatch):
    original = groups((10, 10))
    for cue in original[1]["cues"]:
        cue["target_language"] = "en"
    monkeypatch.setattr(regroup, "call_text_model", lambda **_: pytest.fail("cross-route review"))
    result = regroup.review_request_groups(original, {"translation_regroup": True})
    assert [g["cues"] for g in result] == [g["cues"] for g in original]


def test_parallel_windows_never_cross(monkeypatch):
    original = groups((2, 2, 2, 2))
    def model(**kwargs):
        return str(json.loads(kwargs["user_text"])["MAX_END"]), {}
    monkeypatch.setattr(regroup, "call_text_model", model)
    result = regroup.review_request_groups(original, {"translation_regroup": True})
    assert all(g["cues"] for g in result)
    assert [c for g in result for c in g["cues"]] == [c for g in original for c in g["cues"]]


def test_one_to_one_reviews_requests_without_changing_single_cue_mapping(monkeypatch):
    original = groups((10, 10))
    lines = [{"group_id": "line:" + c["cue_id"], "execution_block_id": g["group_id"], "cues": [c]}
             for g in original for c in g["cues"]]
    monkeypatch.setattr(regroup, "call_text_model", lambda **_: ("12", {}))
    result = regroup.review_request_groups(lines, {"translation_regroup": True, "translation_mapping_mode": "one_to_one"})
    assert all(len(g["cues"]) == 1 for g in result)
    assert [g["cues"] for g in result] == [g["cues"] for g in lines]
    assert sum(g["execution_block_id"] == "enhanced_0001" for g in result) == 12


def test_accepted_prompts_are_exact_and_scoped():
    primary = render_prompt("contextual_translation", variant="en_to_zh", mode="many_to_many")
    repair = render_prompt("contextual_translation_repair", variant="en_to_zh", mode="many_to_many")
    assert primary.sha256 == "e9e204c56246f859fc7506471a18cbe67daeb28f65ddc742439ac67570e677f3"
    assert repair.sha256 == "95030a2d94e43995788893edbc0949e6e7a7df4e9740e49a28651afd8924b403"
    assert repair.text.startswith(primary.text + "\n\n")
    for variant, mode in [("en_to_zh", "one_to_one"), ("zh_to_en", "many_to_many"), ("generic", "many_to_many")]:
        assert "approved_en_zh" not in str(render_prompt("contextual_translation", variant=variant, mode=mode).files)
    # Existing frozen tasks retain their original prompt.
    assert frozen_prompt({"prompt_snapshot": {"contextual_translation": {
        **primary.__dict__, "files": list(primary.files),
    }}}, "contextual_translation").sha256 == primary.sha256


def test_english_routing_uses_accepted_prompt_only_for_english_cues():
    settings = {"translation_layout": {"lower_target": "zh-CN"}}
    original = groups()
    assert regroup.translation_prompt_variant(settings, "en", "zh-CN", original) == "en_to_zh"
    original[0]["cues"][0]["source_text"] = "今日はいい天気です"
    assert regroup.translation_prompt_variant(settings, "en", "zh-CN", original) == "generic"


def test_comma_does_not_trigger_repair(monkeypatch):
    from substar_core.cue_script import render_translation_request, finalize_translation
    from substar_core.editor.translation import contextual
    original = groups((1,))
    _, ledger = render_translation_request(original, mapping_mode="many_to_many")
    response = finalize_translation("1|条件成立，就执行", original, ledger, mapping_mode="many_to_many")
    monkeypatch.setattr(contextual, "api_call", lambda **_: pytest.fail("comma triggered repair"))
    plans, report = contextual.complete_results(settings={}, repair_prompt="repair", groups=original, response=response)
    assert plans[0]["meaning_units"][0]["target_text"] == "条件成立，就执行"
    assert not report["model_repair"]["repair_phase_entered"]


def test_enhanced_groups_reach_translation_and_preserve_candidate_source(tmp_path, monkeypatch):
    from substar_core.config import DEFAULTS
    from substar_core.domain import (
        ChangeKind, ChangeProvenance, SourceToken, DisplayToken, DisplayCue,
        EditorDocument, DocumentRevision,
    )
    from substar_core.editor.translation import contextual
    provenance = ChangeProvenance(kind=ChangeKind.SOURCE, operation="test")
    tokens = [SourceToken.create(index=i, text=f"word{i}", start=i, end=i + .9) for i in range(20)]
    display = [DisplayToken.create(position=i, text=t.text, source_token_ids=[t.token_id], provenance=provenance)
               for i, t in enumerate(tokens)]
    cues = [DisplayCue.create(index=i, display_token_ids=[t.token_id], start=i, end=i + .9)
            for i, t in enumerate(display)]
    document = EditorDocument.create(source_tokens=tokens, display_tokens=display, cues=cues, document_key="test")
    revision = DocumentRevision.create(revision_number=1, document=document, parent_revision_id=None, provenance=provenance)
    original = groups((10, 10))
    for i, cue in enumerate(c for g in original for c in g["cues"]):
        cue.update(cue_id=cues[i].cue_id, source_text=tokens[i].text)
    monkeypatch.setattr(contextual, "clean_groups", lambda *_: original)
    monkeypatch.setattr(regroup, "call_text_model", lambda **_: ("12", {}))
    requests = []
    def translate(**kwargs):
        rows = [c for g in kwargs["groups"] for c in g["cues"]]
        requests.append([c["cue_id"] for c in rows])
        return "\n".join(f"{i+1}|译文" for i in range(len(rows))), {}
    monkeypatch.setattr(contextual, "call_translation_model", translate)
    settings = {**DEFAULTS, "translation_source_language": "en", "target_language_mode": "zh-CN",
                "translation_mapping_mode": "many_to_many", "translation_regroup": True,
                "translation_layout": {}, "glossary_snapshot": []}
    from substar_core.task_info import save_task_info
    save_task_info(tmp_path, tmp_path.name, {"display_name": "regroup test"})
    contextual.run_contextual_translation(tmp_path, settings, source_revision=revision, artifact_dir=tmp_path / "candidate")
    assert sorted(map(len, requests)) == [8, 12]
    candidate = DocumentRevision.from_dict(json.loads((tmp_path / "candidate/candidate.json").read_text("utf-8"))["revision"])
    assert candidate.document.source_tokens == document.source_tokens
    assert [(c.start, c.end, c.display_token_ids) for c in candidate.document.cues] == [(c.start, c.end, c.display_token_ids) for c in document.cues]
    assert [c.mapping["source_cue_ids"] for c in candidate.document.cues] == [[c.cue_id] for c in document.cues]
    assert all(c.target.target_text == "译文" for c in candidate.document.cues)
    assert not list(tmp_path.rglob("*regroup*"))
