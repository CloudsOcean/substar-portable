from dataclasses import replace
from io import BytesIO
import json
import wave
import zipfile

import pytest

from test_architecture_audit import document
from substar_core.domain import SourceToken, ChangeProvenance, TranslationTrack
from substar_core.document_operations import apply_document_batch, apply_document_operation
from substar_core.editor.application.editing_service import EditingService, StaleOperationError, InvalidEditorOperationError
from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository
from substar_core.editor.application.selective_restore import split_token, restore_fields
from substar_core.project_exchange import _safe_members, import_subtitle_project, export_subtitle_project, stream_subtitle_project, ProjectExchangeError
from substar_core.storage import ProjectStore


def operation(doc, number, before, after):
    return {"operation_id":str(number), "type":"replace", "payload":{
        "token_id":doc.display_tokens[0].token_id, "text":after, "expected_text":before,
        "provenance":{"kind":"manual", "operation":"inline_replace"}}}


def setup(tmp_path):
    doc = document()
    store = ProjectStore.create(tmp_path/"project", project_id="audit")
    rev = store.save(doc, provenance=ChangeProvenance("import", "fixture"))
    base = {"document_id":doc.document_id, "revision_id":rev.revision_id, "document_hash":rev.document_hash}
    return doc, store, base, EditingService(lambda _:SQLiteProjectRepository(store))


@pytest.mark.parametrize("name", ["project//escape", "project/C:/escape", "project/C:escape", "project/../escape", "project/\\escape", "project/./x", "project/a//x", "project/NUL", "project/a:stream", "project/a. /x", "/absolute"])
def test_archive_rejects_unsafe_paths_before_writing(tmp_path, name):
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("project/innocent.txt", "first")
        z.writestr(name, "unsafe")
    archive.seek(0)
    outside = tmp_path/"escape"
    outside.write_text("untouched")
    with pytest.raises(ProjectExchangeError, match="不安全路径"):
        import_subtitle_project(archive, projects_root=tmp_path/"imports")
    assert outside.read_text() == "untouched"
    assert list((tmp_path/"imports").iterdir()) == []


def test_archive_symlink_and_case_collision():
    for names in [("project/a", "project/A"), ("project/link",)]:
        data=BytesIO()
        with zipfile.ZipFile(data,"w") as z:
            for name in names:
                info=zipfile.ZipInfo(name)
                if name.endswith("link"): info.external_attr = 0o120777 << 16
                z.writestr(info,"x")
        with zipfile.ZipFile(data) as z, pytest.raises(ProjectExchangeError):
            _safe_members(z)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["start", "end"])
def test_nonfinite_time_rejected(value, field):
    with pytest.raises(ValueError):
        replace(document().source_tokens[0], **{field:value})
    with pytest.raises(ValueError):
        replace(document().cues[0], **{field:value})


@pytest.mark.parametrize("separate", [False,True])
def test_repeated_inline_edits_keep_identity(tmp_path, separate):
    doc,store,base,service=setup(tmp_path)
    a=operation(doc,1,"word0","Word0"); b=operation(doc,2,"Word0","WORD0")
    if separate:
        service.commit_batch("audit",base=base,operations=[a],batch_id="a",preserve_user_actions=True)
        ops=[b]
    else: ops=[a,b]
    saved=service.commit_batch("audit",base=base,operations=ops,batch_id="b",preserve_user_actions=True)
    assert saved.after.document.display_tokens[0].text == "WORD0"
    assert saved.after.document.source_tokens == doc.source_tokens
    assert saved.after.document.cues == doc.cues
    assert restore_fields(saved.after.document,doc,[doc.cues[0].cue_id],"text").display_tokens[0].text == "word0"
    assert len(store.list_revision_metadata()) == 3


def test_lost_ack_mixed_journal_and_hash_mismatch(tmp_path):
    doc,store,base,service=setup(tmp_path)
    a=operation(doc,1,"word0","Word0"); b=operation(doc,2,"Word0","WORD0")
    service.commit_batch("audit",base=base,operations=[a],batch_id="a")
    saved=service.commit_batch("audit",base=base,operations=[a,b],batch_id="refresh",preserve_user_actions=True)
    assert saved.after.document.display_tokens[0].text == "WORD0"
    assert len(store.list_revision_metadata()) == 3
    replay=service.commit_batch("audit",base=base,operations=[a],batch_id="old")
    assert replay.after.revision_id == saved.after.revision_id
    forged=operation(doc,1,"word0","forged")
    with pytest.raises(InvalidEditorOperationError):
        service.commit_batch("audit",base=base,operations=[forged,b],batch_id="bad")
    conflict=operation(doc,3,"Word0","outdated")
    with pytest.raises(StaleOperationError):
        service.commit_batch("audit",base=base,operations=[a,conflict],batch_id="conflict")
    assert len(store.list_revision_metadata()) == 3


def test_text_cue_invalidates_translation():
    doc=document()
    target=TranslationTrack("旧译文",ChangeProvenance("ai","translation"),translation_status="translated")
    doc=replace(doc,source_tokens=(),display_tokens=(),cues=tuple(replace(c,display_token_ids=(),source_text="red",target=target) for c in doc.cues))
    new=apply_document_operation(doc,{"operation_id":"text","type":"set_source_text","payload":{"cue_id":doc.cues[0].cue_id,"text":"blue"}})
    assert new.cues[0].target.target_text == "旧译文"
    assert new.cues[0].target.translation_status == "needs_review"
    assert new.cues[1].target.translation_status == "translated"


def test_shared_cue_edit_merge_and_split():
    doc=document(2)
    first=replace(doc.cues[0],mapping={"source_repeat_group":"g"})
    second=replace(first,cue_id="copy",index=1,start=2,end=3.8)
    doc=replace(doc,cues=(first,second))
    edited=apply_document_batch(doc,[operation(doc,1,"word0","Word0")])
    edited.validate()
    split=split_token(edited,doc.display_tokens[0].token_id,2)
    assert split.cues[0].display_token_ids == split.cues[1].display_token_ids
    merged=apply_document_operation(edited,{"operation_id":"merge","type":"merge_cues","payload":{"cue_ids":[first.cue_id,second.cue_id]}})
    assert merged.cues[0].display_token_ids == first.display_token_ids
    retimed=apply_document_operation(doc,{"operation_id":"retime","type":"retime_text","payload":{"cue_id":first.cue_id,"token_ids":list(first.display_token_ids),"text":"a b c"}})
    assert retimed.cues[0].display_token_ids == retimed.cues[1].display_token_ids
    retimed.validate()


@pytest.mark.parametrize("stream", [False,True])
def test_silent_media_and_evidence_roundtrip(tmp_path,stream):
    doc,store,base,service=setup(tmp_path/"job")
    doc=replace(doc,source_tokens=tuple(replace(s,timing={"kind":"native","native_indexes":[s.index],"evidence_digest":"fixture"}) for s in doc.source_tokens))
    original=doc.source_tokens[0]
    doc=split_token(doc,doc.display_tokens[0].token_id,2)
    revision=store.save(doc,provenance=ChangeProvenance("manual","split"))
    job=tmp_path/"job"; (job/"input").mkdir()
    with wave.open(str(job/"input"/"silence.wav"),"wb") as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(bytes(160000))
    (job/"recognition_evidence.json").write_text('{"fixture":true}')
    if stream: data=b"".join(stream_subtitle_project(project_id="audit",job_dir=job,revision=revision))
    else:
        target=tmp_path/"out.zip"
        export_subtitle_project(target,project_id="audit",job_dir=job,revision=revision)
        data=target.read_bytes()
    imported=import_subtitle_project(BytesIO(data),projects_root=tmp_path/"imports")
    root=tmp_path/"imports"/imported
    restored=ProjectStore(root/"project").load_latest().document
    child=next(s for s in restored.source_tokens if s.timing.get("kind")=="subdivided")
    assert child.timing["parent_sources"] == [original.to_dict()]
    assert (root/"recognition_evidence.json").is_file()


def test_legacy_parent_evidence_is_recovered_from_history(tmp_path):
    from substar_core.project_exchange import _portable_document_bytes
    doc,store,base,service=setup(tmp_path)
    original=doc.source_tokens[0]
    split=split_token(doc,doc.display_tokens[0].token_id,2)
    legacy=replace(split,source_tokens=tuple(replace(t,timing={k:v for k,v in t.timing.items() if k!="parent_sources"}) if t.timing else t for t in split.source_tokens))
    revision=store.save(legacy,provenance=ChangeProvenance("manual","legacy_split"))
    value=json.loads(_portable_document_bytes(tmp_path,revision))
    child=next(s for s in value["source_tokens"] if s.get("timing",{}).get("kind")=="subdivided")
    assert child["timing"]["parent_sources"] == [original.to_dict()]
    assert "parent_sources" not in next(s for s in revision.document.source_tokens if s.timing).timing


def test_action_stages_are_computed_once(tmp_path,monkeypatch):
    import substar_core.editor.application.editing_service as module
    doc,store,base,service=setup(tmp_path)
    original=module.apply_document_batch
    calls=[]
    def tracked(document,operations):
        calls.append(len(operations))
        return original(document,operations)
    monkeypatch.setattr(module,"apply_document_batch",tracked)
    service.commit_batch("audit",base=base,operations=[operation(doc,1,"word0","Word0"),operation(doc,2,"Word0","WORD0")],batch_id="stages",preserve_user_actions=True)
    assert calls == [1,1]


def test_valid_manifest_cannot_overwrite_outside_import_root(tmp_path):
    import hashlib
    from substar_core.project_exchange import EXCHANGE_SCHEMA
    outside=tmp_path/"sentinel.txt"
    outside.write_text("untouched")
    name="project/"+outside.as_posix()
    files={"document/latest.json":json.dumps(document().to_dict()).encode(), name:b"overwrite"}
    manifest={"schema_version":EXCHANGE_SCHEMA,"files":[{"path":key,"size":len(value),"sha256":hashlib.sha256(value).hexdigest()} for key,value in files.items()]}
    data=BytesIO()
    with zipfile.ZipFile(data,"w") as archive:
        archive.writestr("manifest.json",json.dumps(manifest))
        for key,value in files.items():archive.writestr(key,value)
    data.seek(0)
    with pytest.raises(ProjectExchangeError,match="不安全路径"):
        import_subtitle_project(data,projects_root=tmp_path/"imports")
    assert outside.read_text()=="untouched"
    assert not list((tmp_path/"imports").iterdir())
