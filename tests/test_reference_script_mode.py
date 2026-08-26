from __future__ import annotations

from substar_core.export import SubtitleExportMode, render_document_srt
from substar_core.manuscript_matching import (
    editor_reference_operations,
    extract_reference_text,
    materialize_reference_script,
    reference_break_symbols_for_language,
    reference_tokens,
)
from substar_core.document_operations import apply_document_operation
from substar_core.segmentation.contracts import build_segmentation_request
from substar_core.segmentation.document_builder import build_reference_script_document
from substar_core.validation import ValidationPolicy, validate_revision


def _asr_units(*texts: str) -> list[dict[str, object]]:
    return [
        {
            "index": index,
            "text": text,
            "start": index * 0.4,
            "end": (index + 1) * 0.4,
            "speaker_id": None,
        }
        for index, text in enumerate(texts)
    ]


def test_reference_tokenizer_covers_supported_unicode_languages() -> None:
    assert extract_reference_text("これはテストです。".encode(), "reference.txt")
    assert extract_reference_text("자막을 확인합니다.".encode(), "reference.txt")
    assert [token.lexical for token in reference_tokens("L’école est déjà prête.", "fr")] == [
        "L’école", "est", "déjà", "prête",
    ]
    assert reference_tokens("café", "en")[0].normalized == reference_tokens(
        "cafe\u0301", "en"
    )[0].normalized
    assert [token.lexical for token in reference_tokens("𠀀字幕", "zh")] == [
        "𠀀", "字", "幕",
    ]


def test_reference_script_supports_pure_kana_and_hangul() -> None:
    for language, script, symbols in (
        ("ja", "これはテストです。", "。！？"),
        ("ko", "자막을 확인합니다.", ".?!"),
    ):
        material, _breaks, report = materialize_reference_script(
            script, _asr_units(script), symbols, language
        )
        assert report["quality"] == "good"
        assert report["matched_token_ratio"] == 1.0
        assert report["tokenization"]["reference"]["ignored_character_count"] == 0
        assert material["source_transcript"] == script


def test_japanese_kana_difference_is_not_reported_as_perfect_alignment() -> None:
    _material, _breaks, report = materialize_reference_script(
        "字幕を確認します。",
        _asr_units("字幕を確認しました。"),
        "。！？",
        "ja",
    )
    assert report["similarity"] < 1.0
    assert report["matched_token_ratio"] < 1.0


def test_language_mismatch_fails_closed_and_keeps_changes_advisory() -> None:
    _material, _breaks, report = materialize_reference_script(
        "字幕。", _asr_units("字幕"), "。", "ko"
    )
    assert report["quality"] == "failed"
    assert report["tokenization"]["reference"]["script_mismatch"] is True
    assert report["replacements"][0]["status"] == "suggested"


def test_editor_reference_low_confidence_does_not_apply_replacements() -> None:
    result = editor_reference_operations(
        "completely different wording",
        [{"index": 0, "text": "hello"}, {"index": 1, "text": "world"}],
        "en",
    )
    assert result["quality"] == "failed"
    assert result["edits"] == []
    assert all(
        item.get("status") in {"suggested", "retained"}
        for item in result["reference_changes"]
    )


def test_reference_break_presets_follow_source_language() -> None:
    assert reference_break_symbols_for_language("zh-CN") == "，。？！"
    assert reference_break_symbols_for_language("en") == ".?!"
    assert reference_break_symbols_for_language("ja") == "。！？"
    assert reference_break_symbols_for_language("ko") == ".?!"
    assert reference_break_symbols_for_language("mixed") == "，。？！.?!"


def test_reference_script_preserves_left_punctuation_and_splits_provider_han_words() -> None:
    material, breaks, report = materialize_reference_script(
        "我们来到重庆，看看这座城市。",
        _asr_units("我们", "来到", "重庆", "看看这座城市"),
        "，。",
    )

    assert breaks == [5]
    assert material["units"][5]["text"] == "庆"
    assert material["units"][-1]["text"] == "市"
    assert report["replacements"] == [
            {
                "source_index": 5, "reference_index": 5,
                "before": "庆", "after": "庆，", "lexical_match": True,
                "status": "applied",
            },
            {
                "source_index": 11, "reference_index": 11,
                "before": "市", "after": "市。", "lexical_match": True,
                "status": "applied",
            },
    ]
    assert report["quality"] == "good"
    assert report["matched_token_ratio"] == 1.0


def test_reference_alignment_tolerates_asr_insert_delete_and_substitute() -> None:
    material, breaks, report = materialize_reference_script(
        "我们今天来到重庆，再见。",
        _asr_units("我们", "今天", "来", "了", "重庆", "在见"),
        "，。",
    )

    assert breaks == [7]
    assert "".join(unit["text"] for unit in material["units"]) == "我们今天来了重庆在见"
    assert report["replacements"]
    assert report["changes"]
    assert report["quality"] in {"good", "warning"}


def test_reference_script_document_ignores_source_hard_limit_and_exports_script() -> None:
    script = "这是一个非常非常非常非常非常非常非常长的句子，结束。"
    material, breaks, report = materialize_reference_script(
        script,
        _asr_units(script.replace("，", "").replace("。", "")),
        "，。",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    assert len(document.cues) == 2
    validation = validate_revision(
        document,
        revision_id="rev_reference",
        policy=ValidationPolicy(source_hard_limit=10, target_hard_limit=10),
    )
    source_issues = [issue for issue in validation.issues if issue.track.value == "source"]
    assert source_issues
    assert all(issue.severity.value == "warning" for issue in source_issues)
    assert validation.passes_hard_validation
    rendered = render_document_srt(document, SubtitleExportMode.SOURCE)
    assert "句子，" in rendered
    assert "结束。" in rendered


def test_reference_script_applies_reference_only_insertions() -> None:
    material, breaks, report = materialize_reference_script(
        "销量达到二十万（内部统计），你相信吗？",
        _asr_units("销量达到二十万你相信吗"),
        "，。？",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    rendered = render_document_srt(document, SubtitleExportMode.SOURCE)
    assert "（内部统计）" in rendered
    assert "你相信吗？" in rendered
    inserted = [
        token
        for token in document.display_tokens
        if token.provenance.operation == "reference_manuscript_insert"
    ]
    assert inserted
    assert all(token.state.value == "active" for token in inserted)
    assert all(not token.source_token_ids for token in inserted)
    audit = document.changes[-1]
    assert audit.operation == "reference_manuscript_alignment"
    assert audit.metadata["insertion_count"] > 0


def test_reference_script_never_deletes_asr_only_content() -> None:
    material, breaks, report = materialize_reference_script(
        "销量达到二十万。",
        _asr_units("销量达到二十万并且强势增长"),
        "，。？",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    rendered = render_document_srt(document, SubtitleExportMode.SOURCE)
    assert "并且强势增长" in rendered
    assert report["retained_source"]


def _cue_texts(document) -> list[str]:
    by_id = {token.token_id: token.text for token in document.display_tokens}
    return [
        "".join(by_id[token_id] for token_id in cue.display_token_ids)
        for cue in document.cues
    ]


def test_reference_script_uses_reference_boundaries_for_retained_asr_content() -> None:
    material, breaks, report = materialize_reference_script(
        "项羽来了。",
        _asr_units("开场，", "继续。", "项羽来了。"),
        "，。？",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    assert _cue_texts(document) == ["开场，继续。项羽来了。"]
    assert breaks == []


def test_reference_omission_removes_aligned_asr_punctuation() -> None:
    material, breaks, report = materialize_reference_script(
        "项羽杀宋义掌握兵权。",
        _asr_units("项羽杀宋义，", "掌握兵权。"),
        "，。？",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    assert _cue_texts(document) == ["项羽杀宋义掌握兵权。"]
    assert any(
        item["before"] == "义，" and item["after"] == "义"
        for item in report["replacements"]
    )


def test_reference_boundary_reconciles_to_asr_oral_particle_ending() -> None:
    material, breaks, report = materialize_reference_script(
        "争夺？因为",
        _asr_units("争夺呢？", "因为"),
        "，。？",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    assert breaks == [2]
    assert _cue_texts(document) == ["争夺呢？", "因为"]
    assert report["boundary_reconciliations"]


def test_reference_insertion_after_boundary_belongs_to_right_cue() -> None:
    material, breaks, report = materialize_reference_script(
        "梁地。所以昌邑",
        _asr_units("梁地。", "昌邑"),
        "，。？",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    assert _cue_texts(document) == ["梁地。", "所以昌邑"]
    assert report["insertions"][0]["placement"] == "right"


def test_reference_phrase_rewrite_is_suggested_without_overwriting_asr() -> None:
    material, breaks, report = materialize_reference_script(
        "快速进攻梁地。",
        _asr_units("快速支援梁地。"),
        "，。？",
    )
    document = build_reference_script_document(
        material,
        source_asset_id="reference-fixture",
        display_breaks=breaks,
        reference_report=report,
    )

    assert _cue_texts(document) == ["快速支援梁地。"]
    suggestions = [
        item for item in report["replacements"]
        if item.get("status") == "suggested"
    ]
    assert [(item["before"], item["after"]) for item in suggestions] == [
        ("支", "进"),
        ("援", "攻"),
    ]


def test_reference_script_request_requires_document_and_freezes_symbols() -> None:
    request = build_segmentation_request(
        transcription_task_id="tsk_" + "a" * 32,
        transcription_input_fingerprint="1" * 64,
        media_sha256="2" * 64,
        source_asset_id="asset-1",
        language="zh",
        segmentation_enabled=False,
        reference_document={
            "relative_path": "input/reference.txt",
            "sha256": "3" * 64,
            "byte_size": 20,
        },
        prompt_snapshot={
            "relative_path": "task_inputs/segmentation_prompts",
            "sha256": "4" * 64,
            "file_count": 1,
        },
        glossary_snapshot=[],
        settings={
            "reference_script_mode": True,
            "reference_break_symbols": "，。， ",
            "stage_timeout_seconds": 120,
        },
    )

    assert request["mode"] == "reference_script"
    assert request["constraints"]["reference_break_symbols"] == "，。"
