import pytest
from substar_core.editor.application.subtitle_project_import import preview_subtitle_project, build_subtitle_document


def srt(text, start="00:00:01,000", end="00:00:03,000"):
    return f"1\n{start} --> {end}\n{text}\n"


def test_multiline_monolingual_is_not_guessed_as_bilingual():
    result = preview_subtitle_project(srt("This is\none sentence"))
    assert result["tracks"]["source"][0]["text"] == "This is\none sentence"
    assert not result["tracks"]["target"]


def test_explicit_bilingual_and_reverse_order():
    result = preview_subtitle_project(srt("你好\nHello"), mode="bilingual-lines", first_track="target")
    assert result["tracks"]["source"][0]["text"] == "Hello"
    assert len(result["pairs"]) == 1


def test_inline_requires_unambiguous_separator():
    result = preview_subtitle_project(srt("Hello | 你好 | 注释"), mode="bilingual-inline", separator="|")
    assert not result["can_import"]
    assert result["issues"][0]["original_text"] == "Hello | 你好 | 注释"
    with pytest.raises(ValueError):
        preview_subtitle_project(srt("Hello 你好"), mode="bilingual-inline", separator=" ")


def test_two_files_preserve_independent_timing():
    result = preview_subtitle_project(srt("Hello"), mode="two-files",
                                      secondary=srt("你好", end="00:00:04,000"))
    assert result["can_import"]
    assert not result["pairs"]
    assert len(result["unmatched"]) == 2
    assert result["tracks"]["target"][0]["end_ms"] == 4000


def test_duplicate_times_are_not_arbitrarily_paired():
    result = preview_subtitle_project(srt("Hello") + "\n" + srt("World"),
                                      mode="two-files", secondary=srt("你好"))
    assert not result["pairs"]
    assert len(result["unmatched"]) == 3


def test_empty_track_is_rejected():
    result = preview_subtitle_project(srt("Hello |"), mode="bilingual-inline", separator="|")
    assert not result["can_import"]


def test_token_free_document_roundtrip_and_export():
    from substar_core.domain import EditorDocument
    from substar_core.export import render_document_srt
    preview = preview_subtitle_project(srt("Hello\n你好"), mode="bilingual-lines")
    doc = build_subtitle_document(preview, "subtitle-test")
    assert not doc.source_tokens and not doc.display_tokens
    assert all(not cue.display_token_ids for cue in doc.cues)
    restored = EditorDocument.from_dict(doc.to_dict())
    assert restored == doc
    assert "Hello\n你好" in render_document_srt(restored, "ab-double")


def test_token_free_document_keeps_unmatched_target_time():
    from substar_core.export import render_document_srt
    preview = preview_subtitle_project(srt("Hello"), mode="two-files", secondary=srt("你好", end="00:00:04,000"))
    doc = build_subtitle_document(preview, "subtitle-test")
    assert len(doc.cues) == 2
    assert "00:00:04,000" in render_document_srt(doc, "target")
    assert "00:00:04,000" not in render_document_srt(doc, "source")


def test_token_free_revision_survives_store(tmp_path):
    from substar_core.domain import ChangeKind, ChangeProvenance
    from substar_core.storage import ProjectStore
    doc = build_subtitle_document(preview_subtitle_project(srt("Hello")), "subtitle-test")
    store = ProjectStore.create(tmp_path / "project", project_id="subtitle-test")
    saved = store.save(doc, provenance=ChangeProvenance(ChangeKind.IMPORT, "subtitle_import"))
    assert store.load_latest().document == saved.document
