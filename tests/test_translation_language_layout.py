from dataclasses import replace
import json

import pytest

from substar_core.config import DEFAULTS
from substar_core.domain import ChangeKind, ChangeProvenance, DisplayCue, DisplayToken, EditorDocument, SourceToken, DocumentRevision
from substar_core.editor.translation.contextual import clean_groups, _save_translation
from substar_core.cue_script import render_translation_request
from substar_core.export import render_document_srt
from substar_core.ass_profiles import render_configuration
from substar_core.translation_layout import normalize_layout, route_text
from substar_core.task_info import save_task_info, load_task_info, task_info_settings


LAYOUT = {"enabled": True, "upper_languages": ["en", "ja", "ko"],
          "lower_languages": ["zh-CN"], "upper_target": "en", "lower_target": "zh-CN"}
TEXTS = ["今天很好", "Good morning", "おはようございます", "今天的 meeting 取消了", "Let's go、出発しよう"]
TARGETS = ["Today is nice", "早上好", "早上好", "今天的会议取消了", "我们出发吧"]


def document():
    provenance = ChangeProvenance(kind=ChangeKind.SOURCE, operation="test")
    source = [SourceToken.create(index=i, text=text, start=i, end=i + .9) for i, text in enumerate(TEXTS)]
    display = [DisplayToken.create(position=i, text=t.text, source_token_ids=[t.token_id], provenance=provenance)
               for i, t in enumerate(source)]
    cues = [DisplayCue.create(index=i, display_token_ids=[t.token_id], start=i, end=i + .9)
            for i, t in enumerate(display)]
    return EditorDocument.create(source_tokens=source, display_tokens=display, cues=cues, document_key="routing")


def test_routing_is_per_cue_and_mixed_always_uses_lower_target():
    assert [route_text(text, LAYOUT)["target_language"] for text in TEXTS] == ["en", "zh-CN", "zh-CN", "zh-CN", "zh-CN"]
    assert [route_text(text, LAYOUT)["source_upper"] for text in TEXTS] == [False, True, True, True, True]
    reversed_layout = {**LAYOUT, "upper_languages": ["zh-CN"], "lower_languages": ["en", "ja", "ko"],
                       "upper_target": "zh-CN", "lower_target": "ja"}
    assert route_text(TEXTS[3], reversed_layout)["target_language"] == "ja"
    assert route_text(TEXTS[3], reversed_layout)["source_upper"] is True


def test_wire_and_group_boundaries_bind_correct_target_limits():
    groups = clean_groups(document(), {**DEFAULTS, "translation_layout": LAYOUT})
    rows = [cue for g in groups for cue in g["cues"]]
    assert rows[0]["hard_limit"] == DEFAULTS["english_hard_limit"]
    assert rows[1]["hard_limit"] == DEFAULTS["chinese_hard_limit"]
    for group in groups:
        assert len({cue["target_language"] for cue in group["cues"]}) == 1
    text, ledger = render_translation_request(groups, mapping_mode="many_to_many")
    assert "TARGET_LANGUAGE 1 en" in text and "TARGET_LANGUAGE 2 zh-CN" in text


@pytest.mark.parametrize("reverse", [False, True])
def test_translation_candidate_reload_srt_and_ass_share_order(tmp_path, reverse):
    doc = document()
    if reverse:
        from substar_core.domain import DisplayOrder
        doc = replace(doc, presentation=replace(doc.presentation, display_order=DisplayOrder.TARGET_ABOVE_SOURCE))
    # Use the real candidate materializer and serialization, without provider calls.
    revision = DocumentRevision.create(revision_number=1, document=doc, parent_revision_id=None,
        provenance=ChangeProvenance(kind=ChangeKind.SOURCE, operation="test"))
    plans = []
    for i, cue in enumerate(doc.cues):
        plans.append({"group_id": f"g{i}", "meaning_units": [{"meaning_unit_id": f"u{i}",
            "target_text": TARGETS[i], "source_evidence_cue_ids": [cue.cue_id]}],
            "cue_assignments": [{"cue_id": cue.cue_id, "meaning_unit_id": f"u{i}"}]})
    _save_translation(work=tmp_path, plans=plans, settings={**DEFAULTS, "translation_layout": LAYOUT},
        metadata={}, target_language="zh-CN", expected_revision_id=revision.revision_id,
        source_revision=revision, artifact_dir=tmp_path / "candidate")
    value = json.loads((tmp_path / "candidate/candidate.json").read_text("utf-8"))
    result = DocumentRevision.from_dict(value["revision"]).document
    assert result.source_tokens == doc.source_tokens
    assert [(c.start, c.end) for c in result.cues] == [(c.start, c.end) for c in doc.cues]
    assert [c.target.language for c in result.cues] == ["en", "zh-CN", "zh-CN", "zh-CN", "zh-CN"]
    srt = render_document_srt(result, "ab-double")
    assert "Today is nice\n今天很好" in srt
    assert "Good morning\n早上好" in srt
    assert "今天的 meeting 取消了\n今天的会议取消了" in srt
    upper = render_document_srt(result, "source")
    assert "Today is nice" in upper and "今天很好" not in upper
    ass = render_configuration(result)
    upper_events = [line for line in ass.splitlines() if line.startswith("Dialogue:") and "P0L0" in line]
    assert any("Today is nice" in line for line in upper_events)
    assert not any("今天很好" in line for line in upper_events)
    from substar_core.editor.application.srt_import import preview_srt
    translated = render_document_srt(result, "target", language_rows=False)
    imported = preview_srt(result, translated, mode="target")
    assert imported["matched"] == len(TEXTS)
    assert [row["target_language"] for row in imported["rows"]] == ["en", "zh-CN", "zh-CN", "zh-CN", "zh-CN"]
    from substar_core.project_exchange import external_translation_text
    external = external_translation_text(replace(revision, document=result), source_language="mixed", target_language="zh-CN")
    assert "TARGET_LANGUAGE 1 en" in external and "TARGET_LANGUAGE 2 zh-CN" in external
    assert "今天很好" in external


def test_failed_translation_does_not_relabel_previous_wrong_language():
    from substar_core.domain import TranslationTrack
    from substar_core.editor.translation.contextual import materialize_presentation
    doc = document()
    cue = replace(doc.cues[0], target=TranslationTrack(target_text="旧中文", original_text="旧中文", language="zh-CN",
        provenance=ChangeProvenance(kind=ChangeKind.MANUAL, operation="test")))
    doc = replace(doc, cues=(cue, *doc.cues[1:]))
    result, report = materialize_presentation(doc, [], "zh-CN", target_languages_by_cue={cue.cue_id:"en"})
    assert result.cues[0].target.target_text == ""
    assert result.cues[0].target.language == "en"
    assert cue.cue_id in report["unresolved_source_cue_ids"]


def test_project_settings_roundtrip_and_old_project_default(tmp_path):
    info = {"display_name": "test", "language": "mixed", "target_language_mode": "zh-CN",
            "source_hard_limit": 55, "target_hard_limit": 25, "translation_layout": LAYOUT}
    save_task_info(tmp_path, "test", info)
    assert task_info_settings(load_task_info(tmp_path, "test"))["translation_layout"] == LAYOUT
    del info["translation_layout"]
    save_task_info(tmp_path, "test", info)
    assert load_task_info(tmp_path, "test")["translation_layout"] == {}
    assert "translation_layout" not in document().presentation.to_dict()


@pytest.mark.parametrize("bad", [{**LAYOUT, "upper_target": "zh-CN"},
    {**LAYOUT, "upper_languages": ["zh-CN", "en"]}, [], {**LAYOUT, "lower_languages": []}])
def test_invalid_language_assignment_is_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        normalize_layout(bad)


def test_single_target_and_bidirectional_switch(tmp_path):
    policy = {**LAYOUT, "bidirectional": False}
    assert normalize_layout(policy)["bidirectional"] is False
    assert route_text("今天很好", policy)["target_language"] == "zh-CN"
    assert route_text("今天很好", {**policy, "bidirectional": True})["target_language"] == "en"
    assert route_text("今日は meeting", policy)["source_upper"] is True
    with pytest.raises(ValueError):
        normalize_layout({**policy, "lower_languages": ["zh-CN", "ja"], "upper_languages": ["en", "ko"]})
    doc = document()
    revision = DocumentRevision.create(revision_number=1, document=doc, parent_revision_id=None,
        provenance=ChangeProvenance(kind=ChangeKind.SOURCE, operation="test"))
    plans = [{"group_id":"g", "meaning_units":[{"meaning_unit_id":"u", "target_text":"今天很好",
        "source_evidence_cue_ids":[doc.cues[0].cue_id]}],
        "cue_assignments":[{"cue_id":doc.cues[0].cue_id,"meaning_unit_id":"u"}]}]
    _save_translation(work=tmp_path, plans=plans, settings={**DEFAULTS,"translation_layout":policy},
        metadata={}, target_language="zh-CN", expected_revision_id=revision.revision_id,
        source_revision=revision, artifact_dir=tmp_path / "candidate")
    result = DocumentRevision.from_dict(json.loads((tmp_path / "candidate/candidate.json").read_text("utf-8"))["revision"]).document
    assert result.cues[0].target is None
    assert result.cues[0].mapping["translation_not_required"] is True
    assert "今天很好" in render_document_srt(result,"target")
    assert "今天很好" not in render_document_srt(result,"source")


def test_multisource_request_preserves_hints_and_auto(tmp_path):
    from substar_core.transcription.contracts import build_transcription_request
    from substar_core.qwen_cloud_asr import _submission_body
    media = tmp_path / "audio.wav"
    media.write_bytes(b"audio")
    layout = {"enabled": True, "source_languages": ["zh-CN", "en", "ja", "ko"],
              "auto_detect": False, "upper_languages": ["en", "ja", "ko"],
              "lower_languages": ["zh-CN"], "upper_target": "en", "lower_target": "zh-CN", "bidirectional": False}
    def request(policy):
        return build_transcription_request(media_path=media, project_directory=tmp_path,
            profile_id="qwen_cloud", language="Auto", prompt="", hotwords={},
            settings={"translation_layout": policy})
    explicit = request(layout)
    settings = {**explicit["options"], "language": "Auto"}
    assert _submission_body("qwen-audio-3.0-asr-flash-filetrans", "https://example.com/a.wav", settings)["parameters"]["language_hints"] == ["zh", "en", "ja", "ko"]
    assert "language" not in _submission_body("qwen3-asr-flash-filetrans", "https://example.com/a.wav", settings)["parameters"]
    automatic = request({**layout, "source_languages": [], "auto_detect": True})
    assert automatic["input_fingerprint"] != explicit["input_fingerprint"]
    assert "language_hints" not in _submission_body("qwen-audio-3.0-asr-flash-filetrans", "https://example.com/a.wav", {**automatic["options"], "language": "en"})["parameters"]
    assert normalize_layout(layout)["source_languages"] == ["zh-CN", "en", "ja", "ko"]


def test_extended_languages_and_dialect_option():
    from substar_core.translation_layout import LANGUAGES, LANGUAGE_NAMES, cue_language
    from substar_core.qwen_cloud_asr import _submission_body
    assert len(LANGUAGES) == 30
    assert all(len(name) == 2 for name in LANGUAGE_NAMES.values())
    layout = {"enabled": True, "upper_languages": ["en", "ar", "th"], "lower_languages": ["zh-CN"],
              "upper_target": "en", "lower_target": "zh-CN", "source_languages": ["zh-CN", "en", "ar", "th"],
              "auto_detect": False, "bidirectional": False, "keep_dialect": True}
    assert normalize_layout(layout)["keep_dialect"] is True
    import pytest
    with pytest.raises(ValueError):
        normalize_layout({**layout, "source_languages": ["zh-CN", "en", "ar", "th", "fr"]})
    settings = {"qwen_keep_dialect": True, "qwen_language_hints": "zh-CN,en,ar"}
    assert _submission_body("qwen-audio-3.1-asr-flash-filetrans", "https://example.com/a.mp3", settings)["parameters"]["keep_dialect"] is True
    assert "keep_dialect" not in _submission_body("qwen-audio-3.0-asr-flash-filetrans", "https://example.com/a.mp3", settings)["parameters"]
    assert cue_language("مرحبا") == "ar"
    assert cue_language("สวัสดี") == "th"
    assert cue_language("你好 hello مرحبا") == "mixed"


@pytest.mark.parametrize("sources,auto", [([], True), (["zh-CN", "en", "ja", "ko"], False), (["en"], False)])
def test_reverse_translation_uses_actual_language_not_source_selection(sources, auto):
    policy = {**LAYOUT, "source_languages": sources, "auto_detect": auto, "bidirectional": True}
    texts = ["今天很好", "Good morning", "おはようございます", "안녕하세요"]
    routes = [route_text(text, policy) for text in texts]
    assert [r["target_language"] for r in routes] == ["en", "zh-CN", "zh-CN", "zh-CN"]
    assert [r["source_upper"] for r in routes] == [False, True, True, True]
    # Turning reverse translation off keeps Chinese on the lower row unchanged.
    chinese = route_text(texts[0], {**policy, "bidirectional": False})
    assert chinese["target_language"] == "zh-CN"
    assert chinese["source_upper"] is False
