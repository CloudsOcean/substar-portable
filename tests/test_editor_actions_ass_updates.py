import json
import zipfile
from dataclasses import replace

import pytest
from substar_core.domain import ChangeProvenance, DisplayCue, DisplayToken, EditorDocument, SourceToken
from substar_core.editor.application.selective_restore import restore_fields, split_token
from substar_core.ass_subtitles import AssOptions, AssStyle, render_ass
from substar_core.storage.project_store import ProjectStore
from substar_core import updater


def document():
    source = [SourceToken.create(index=0,text="hello",start=1,end=2),
              SourceToken.create(index=1,text="world",start=2,end=3)]
    provenance = ChangeProvenance(kind="source",operation="asr")
    tokens = [DisplayToken.create(position=i,text=s.text,source_token_ids=[s.token_id],provenance=provenance)
              for i,s in enumerate(source)]
    cue = DisplayCue.create(index=0,display_token_ids=[t.token_id for t in tokens],start=1,end=3)
    return EditorDocument.create(document_key="actions",source_tokens=source,display_tokens=tokens,cues=[cue])


def test_split_equal_time_and_replay_original(tmp_path):
    original = document()
    store = ProjectStore.create(tmp_path / "project",project_id="test")
    before = store.save(original,provenance=ChangeProvenance(kind="manual",operation="checkpoint",metadata={"label":"初稿"}))
    changed = split_token(original, original.display_tokens[0].token_id,1)
    after = store.save(changed,provenance=changed.changes[-1],expected_revision_id=before.revision_id)
    assert [(s.start,s.end) for s in changed.source_tokens[:2]] == [(1,1.5),(1.5,2)]
    assert [t.text for t in changed.display_tokens] == ["h","ello","world"]
    assert changed.source_tokens[0].timing["kind"] == "subdivided"
    assert store.load_revision(after.revision_id).document == changed
    assert store.load_revision(before.revision_id).document == original
    assert changed.schema_version == original.schema_version


def test_selective_text_restore_keeps_later_time_and_other_text():
    old = document()
    current = replace(old, display_tokens=(replace(old.display_tokens[0],text="HELLO"),old.display_tokens[1]),
                      cues=(replace(old.cues[0],start=0.8,end=3.1),))
    result = restore_fields(current,old,[old.cues[0].cue_id],"text")
    assert result.display_tokens[0].text == "hello"
    assert (result.cues[0].start,result.cues[0].end) == (0.8,3.1)
    result = restore_fields(current,old,[old.cues[0].cue_id],"timing")
    assert result.display_tokens[0].text == "HELLO"
    assert result.cues[0].start == 1


def test_selective_restore_rejects_split_dependency():
    old = document()
    current = split_token(old,old.display_tokens[0].token_id,2)
    with pytest.raises(ValueError,match="拆分或合并"):
        restore_fields(current,old,[old.cues[0].cue_id],"text")


def test_ass_highlights_current_word_and_preserves_silent_interval():
    old = document()
    text = render_ass(old,AssOptions(word_highlight=True))
    events = [l for l in text.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 2
    assert "0:00:01.00,0:00:02.00" in events[0]
    assert "hello{\\c" in events[0]
    assert "world{\\c" in events[1]
    assert "PlayResX: 1920" in text


def test_ass_rejects_tag_injection_in_style():
    with pytest.raises(ValueError): AssStyle(font="Arial\n[Events]")


def test_ass_literal_controls_do_not_change_visible_characters():
    from substar_core.ass_subtitles import escape_text
    assert escape_text(r"{\b1}hello\Nworld") == "\\{\\\u2060b1\\}hello\\\u2060Nworld"
    assert escape_text("hello\nworld") == r"hello\Nworld"


def test_named_checkpoint_preserves_completion(tmp_path):
    original = document()
    completed = replace(original, properties=replace(original.properties, complete=True))
    store = ProjectStore.create(tmp_path / "project", project_id="test")
    before = store.save(completed, provenance=ChangeProvenance(kind="manual", operation="set_complete_attribute"))
    after = store.save(before.document, expected_revision_id=before.revision_id,
                       provenance=ChangeProvenance(kind="manual", operation="checkpoint", metadata={"label":"定稿"}))
    assert after.document.complete


@pytest.mark.parametrize("integrity", ["valid", "missing", "mismatch"])
def test_update_download_requires_matching_github_digest(tmp_path, monkeypatch, integrity):
    import hashlib
    payload = archive(tmp_path).read_bytes()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if integrity == "missing": digest = None
    if integrity == "mismatch": digest = "sha256:" + "0" * 64
    monkeypatch.setattr(updater, "UPDATE_ROOT", tmp_path / "updates")
    monkeypatch.setattr(updater, "_state", {"status":"idle"})
    monkeypatch.setattr(updater, "check_update", lambda: {"available":True,"version":"v2.3.0",
        "asset":{"digest":digest,"size":len(payload),"browser_download_url":
                 f"https://github.com/{updater.REPOSITORY}/releases/download/v2.3.0/package.zip"}})
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield payload
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(updater.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(updater.requests, "get", lambda *args, **kwargs: Response())
    result = updater.download_update()
    assert result["status"] == ("ready" if integrity == "valid" else "failed")
    if integrity != "valid":
        assert "staged" not in result


def archive(tmp_path, extra=None, version="2.3.0"):
    path = tmp_path / "package.zip"
    with zipfile.ZipFile(path,"w") as output:
        for name in ("app.py","launcher.py","runtime/python/python.exe","启动_Substar.cmd"):
            output.writestr("Substar/"+name,"test")
        output.writestr("Substar/portable_manifest.json",json.dumps({"version":version,"package_layout":"transparent-source-runtime"}))
        if extra: output.writestr(extra,"unsafe")
    return path


@pytest.mark.parametrize("extra",["Substar/../outside.txt","Substar/data/key","Substar/web/CON.txt",
                                  "Substar/web/a:stream","Substar/web/../../outside"])
def test_update_rejects_unsafe_archive_before_extracting(tmp_path,extra):
    path = archive(tmp_path,extra)
    destination = tmp_path / "unpack"
    with pytest.raises(ValueError): updater.validate_archive(path,destination,"v2.3.0")
    assert not destination.exists()


def test_update_checks_version_and_required_runtime(tmp_path):
    path = archive(tmp_path)
    root = updater.validate_archive(path,tmp_path / "good","v2.3.0")
    assert (root / "app.py").is_file()
    with pytest.raises(ValueError,match="版本"):
        updater.validate_archive(path,tmp_path / "bad","v2.4.0")


def test_checkpoint_name_and_split_api_reject_stale_revision(tmp_path,monkeypatch):
    from substar_core.editor import http_api
    store = ProjectStore.create(tmp_path / "project",project_id="test")
    first = store.save(document(),provenance=ChangeProvenance(kind="manual",operation="create"))
    monkeypatch.setattr(http_api,"open_project_store",lambda _:store)
    # API save also updates the catalog; isolate that peripheral effect.
    monkeypatch.setattr(http_api,"_save_document",lambda project_id, **kw: store.save(kw["document"],
        expected_revision_id=kw["expected_revision_id"],provenance=kw["provenance"]))
    checkpoint = http_api.create_project_checkpoint("test",http_api.CheckpointRequest(
        expected_revision_id=first.revision_id,label="人工精修完成"))
    assert checkpoint.provenance.metadata["label"] == "人工精修完成"
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        http_api.split_project_token("test",http_api.SplitTokenRequest(expected_revision_id=first.revision_id,
            token_id=first.document.display_tokens[0].token_id,offset=1))
    assert error.value.status_code == 409
