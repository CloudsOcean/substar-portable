import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from substar_core.domain import ChangeKind, ChangeProvenance, DisplayCue, DisplayToken, DocumentRevision, EditorDocument, EntityState, SourceToken
from substar_core.document_operations import apply_document_operation
from substar_core.editor import text_tasks as tasks
from substar_core.editor.application.publication import publish_candidate
from substar_core.storage import ProjectStore


def document(words=("I", "I", "think", "we", "should", "wait."), sizes=(3,3)):
    provenance = ChangeProvenance(kind=ChangeKind.SOURCE,operation="test")
    source = [SourceToken.create(index=i,text=w,start=float(i),end=i+.8) for i,w in enumerate(words)]
    tokens = [DisplayToken.create(position=i,text=t.text,source_token_ids=[t.token_id],provenance=provenance) for i,t in enumerate(source)]
    cues=[]
    start=0
    for i,size in enumerate(sizes):
        cues.append(DisplayCue.create(index=i,display_token_ids=[t.token_id for t in tokens[start:start+size]],start=float(start),end=float(start+size),speaker="A"))
        start += size
    return EditorDocument.create(source_tokens=source,display_tokens=tokens,cues=cues,document_key="test")


@pytest.mark.parametrize("before,after",[("hello","Hello."),("word,","word."),("1.3","1.3?"),("C++","C++!"),("don't","Don't."),("co-host,","Co-host."),("甲","甲，")])
def test_punctuation_only_accepts_lexically_identical_text(before,after):
    assert tasks.punctuation_allowed(before,after)


@pytest.mark.parametrize("before,after",[("Iran","Iraq"),("1.3","13"),("-5","5"),("C++","C"),("C#","C"),("U.S.","US"),("U.S.","U.S"),("don't","dont"),("co-host","cohost"),(".5","5"),(".NET","NET"),("–5","5"),("https://example.com/Ab","https://example.com/ab"),("example.com","examplecom"),("word","wo,rd"),("hello","hello world")])
def test_punctuation_rejects_lexical_and_symbol_changes(before,after):
    assert not tasks.punctuation_allowed(before,after)


def test_delete_preserves_time_and_reversible_history():
    doc=document()
    block=tasks.task_blocks(doc,{})[0]
    ids=[t.token_id for t in doc.display_tokens]
    value={"delete_token_ids":[ids[1]],"cues":[{"token_ids":[ids[0],ids[2]]},{"token_ids":ids[3:]}]}
    tasks.validate_result("organize",value,block,doc,{})
    result=tasks.apply_organize(doc,block,value,"task")
    assert [(c.start,c.end,c.cue_id) for c in result.cues] == [(c.start,c.end,c.cue_id) for c in doc.cues]
    assert result.source_tokens == doc.source_tokens
    assert [t.text for t in result.display_tokens] == [t.text for t in doc.display_tokens]
    assert result.display_tokens[1].state is EntityState.DELETED
    restored=apply_document_operation(result,{"operation_id":"restore-test","type":"restore","payload":{"token_ids":[ids[1]]}})
    assert restored.display_tokens[1].state is EntityState.ACTIVE


def test_reframe_preserves_tokens_deleted_history_and_outer_times():
    doc=document()
    ids=[t.token_id for t in doc.display_tokens]
    doc=apply_document_operation(doc,{"operation_id":"old-delete","type":"delete","payload":{"token_ids":[ids[1]]}})
    block=tasks.task_blocks(doc,{})[0]
    value={"delete_token_ids":[],"cues":[{"token_ids":[ids[0],ids[2],ids[3]]},{"token_ids":ids[4:]}]}
    tasks.validate_result("organize",value,block,doc,{})
    result=tasks.apply_organize(doc,block,value,"task")
    result.validate()
    assert result.source_tokens == doc.source_tokens
    assert result.display_tokens == doc.display_tokens
    assert result.cues[0].start == doc.cues[0].start
    assert result.cues[-1].end == doc.cues[-1].end
    active={t.token_id for t in result.display_tokens if t.state is EntityState.ACTIVE}
    assert [[i for i in c.display_token_ids if i in active] for c in result.cues] == [r["token_ids"] for r in value["cues"]]


def test_delete_whole_cue_marks_deleted_without_empty_active_cue():
    doc=document()
    block=tasks.task_blocks(doc,{})[0]
    ids=[t.token_id for t in doc.display_tokens]
    value={"delete_token_ids":ids[:3],"cues":[{"token_ids":ids[3:]}]}
    result=tasks.apply_organize(doc,block,tasks.validate_result("organize",value,block,doc,{}),"task")
    assert result.cues[0].state is EntityState.DELETED
    assert (result.cues[1].start,result.cues[1].end)==(3,6)


@pytest.mark.parametrize("case",["duplicate","missing","reorder","foreign","text","speaker"])
def test_organize_contract_rejects_invalid_topology(case):
    doc=document()
    if case=="speaker":
        doc=replace(doc,cues=(doc.cues[0],replace(doc.cues[1],speaker="B")))
    block=tasks.task_blocks(doc,{})[0]
    ids=[t.token_id for t in doc.display_tokens]
    value={"delete_token_ids":[],"cues":[{"token_ids":ids.copy()}]}
    if case=="duplicate": value["delete_token_ids"]=[ids[0]]
    if case=="missing": value["cues"][0]["token_ids"].pop()
    if case=="reorder": value["cues"][0]["token_ids"].reverse()
    if case=="foreign": value["cues"][0]["token_ids"][0]="context-id"
    if case=="text": value["cues"][0]["text"]="rewritten"
    with pytest.raises(ValueError): tasks.validate_result("organize",value,block,doc,{})


def test_deleted_cue_separates_segments():
    doc=document(("one","two","three"),(1,1,1))
    doc=apply_document_operation(doc,{"operation_id":"old","type":"delete","payload":{"cue_ids":[doc.cues[1].cue_id]}})
    block=tasks.task_blocks(doc,{})[0]
    assert block["cues"][0]["segment"] != block["cues"][1]["segment"]


def test_punctuation_rejects_context_duplicate_ids_and_time_edits():
    doc=document()
    block=tasks.task_blocks(doc,{})[0]
    tid=doc.display_tokens[0].token_id
    change={"token_id":tid,"expected_text":"I","text":"I,"}
    for changes in ([change,change],[{**change,"token_id":"context"}],[{**change,"start":4}]):
        with pytest.raises(ValueError): tasks.validate_result("punctuation",{"changes":changes},block,doc,{})


def test_candidate_is_atomic_revision_bound_and_resumable(tmp_path,monkeypatch):
    project=tmp_path/"project-root"
    project.mkdir()
    store=ProjectStore.create(project/"project",project_id="test")
    doc=document()
    revision=store.save(doc,provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    route={"base_url":"https://example.invalid","api_key":"test","auth_mode":"bearer","model":"test","thinking_mode":"disabled","reasoning_effort":"low","max_tokens":1000,"temperature":0}
    monkeypatch.setattr(tasks,"resolve_stage_request",lambda *_:route)
    tid=doc.display_tokens[-1].token_id
    model=Mock(return_value=(json.dumps({"changes":[{"token_id":tid,"expected_text":"wait.","text":"Wait!"}]}),{}))
    monkeypatch.setattr(tasks,"call_text_model",model)
    payload={"expected_revision_id":revision.revision_id,"optimize_boundaries":False,"settings":{"text_task_prompt":tasks.PROMPTS["punctuation"]}}
    directory=tmp_path/"candidate"
    summary=tasks.compute("punctuation",project,directory,payload,"task",lambda *_:None)
    assert store.load_latest().revision_id==revision.revision_id
    tasks.compute("punctuation",project,directory,payload,"task",lambda *_:None)
    assert model.call_count==1
    candidate=publish_candidate(store,directory,task_id="task",expected_revision_id=revision.revision_id,summary=json.loads((directory/"result.json").read_text("utf-8")))
    assert candidate.document.cues==doc.cues
    assert candidate.document.source_tokens==doc.source_tokens
    changed=candidate.document.display_tokens[-1]
    assert changed.text=="Wait!" and changed.provenance.metadata["stage"]=="punctuation"
    assert candidate.document.display_tokens[:-1]==doc.display_tokens[:-1]
    restored=apply_document_operation(candidate.document,{"operation_id":"cancel","type":"set_ai_calibration","payload":{"action":"cancel","token_ids":[tid]}})
    assert restored.display_tokens[-1].text=="wait."
    with pytest.raises(ValueError,match="版本"):
        tasks.compute("punctuation",project,directory,payload,"task",lambda *_:None)


def test_invalid_result_does_not_publish_anything(tmp_path,monkeypatch):
    project=tmp_path/"root";project.mkdir()
    store=ProjectStore.create(project/"project",project_id="test")
    revision=store.save(document(),provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    route={k:"test" for k in ("base_url","api_key","auth_mode","model","thinking_mode","reasoning_effort")}
    route.update(max_tokens=100,temperature=0)
    monkeypatch.setattr(tasks,"resolve_stage_request",lambda *_:route)
    monkeypatch.setattr(tasks,"call_text_model",lambda **_:('{"delete_token_ids":[],"cues":[]}',{}))
    with pytest.raises(ValueError):
        tasks.compute("organize",project,tmp_path/"candidate",{"expected_revision_id":revision.revision_id,"settings":{"text_task_prompt":tasks.PROMPTS["organize"]}},"task",lambda *_:None)
    assert store.load_latest().revision_id==revision.revision_id
    assert not (tmp_path/"candidate"/"candidate.json").exists()


def test_raw_asr_projection_restores_punctuation_without_reference_provenance(monkeypatch):
    from substar_core.segmentation import worker
    from substar_core.contracts.editor_document import build_editor_document, source_tokens_from_asr
    monkeypatch.setattr(worker,"recognition_source_from_evidence",lambda e:e)
    evidence={"master_text":"Geopolitics. co-host,","units":[
        {"index":0,"text":"Geopolitics.","start":0,"end":1},
        {"index":1,"text":"co-host,","start":1,"end":2}]}
    material,_,audit,projection,_=worker._effective_material(evidence,None,{})
    assert [u["text"] for u in material["units"]]==["Geopolitics","co-host"]
    assert audit["applied"] is False
    doc=build_editor_document(source_tokens=source_tokens_from_asr(material["units"],source_asset_id="test"),
        source_kind="asr",source_asset_id="test",execution_plan={"blocks":[],"boundaries_after":[],"skipped_reason":None},
        semantic_grouping={"protections":[],"meaning_groups":[],"review_regions":[]},cue_layout={"display_breaks":[0]})
    restored=worker.apply_semantic_display_projection(doc,projection,reference=False)
    assert [t.text for t in restored.display_tokens]==["Geopolitics.","co-host,"]
    assert restored.source_tokens==doc.source_tokens and restored.cues==doc.cues
    assert restored.changes[-1].operation=="asr_display_projection"
    assert restored.changes[-1].metadata["reference"] is False


def test_independent_punctuation_is_preserved_in_projection():
    from substar_core.segmentation.input_contract import build_segmentation_material_with_reference_projection
    evidence={"units":[{"index":i,"text":w,"start":i,"end":i+1} for i,w in enumerate(['“','Hello',',','world','!”'])]}
    material,projection,_=build_segmentation_material_with_reference_projection("",evidence)
    assert [u["text"] for u in material["units"]]==["Hello","world"]
    assert [u["text"] for u in projection]==['“Hello,','world!”']


def test_cancelled_compute_does_not_invoke_model(tmp_path,monkeypatch):
    import threading
    project=tmp_path/"root";project.mkdir()
    store=ProjectStore.create(project/"project",project_id="test")
    revision=store.save(document(),provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    event=threading.Event();event.set()
    monkeypatch.setattr(tasks,"call_text_model",lambda **_:pytest.fail("cancelled task called provider"))
    with pytest.raises(InterruptedError):
        tasks.compute("organize",project,tmp_path/"candidate",{"expected_revision_id":revision.revision_id,"settings":{}},"task",lambda *_:None,cancel_event=event)


@pytest.mark.parametrize("kind",["organize","punctuation"])
def test_worker_protocol_and_publication_end_to_end(kind,tmp_path,monkeypatch,capsys):
    import io
    import sys
    from types import SimpleNamespace
    from substar_core.editor import text_task_runtime as runtime
    from substar_core.runtime.registry import TaskWorkContext, TaskRegistry
    from substar_core.runtime.worker_protocol import WorkerCommand, WorkerPaths, credential_environment_key
    from substar_core.credential_store import model_provider_credential_ref
    projects=tmp_path/"projects";project=projects/"test";project.mkdir(parents=True)
    store=ProjectStore.create(project/"project",project_id="test")
    doc=document()
    revision=store.save(doc,provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    route={k:"test" for k in ("base_url","api_key","auth_mode","model","thinking_mode","reasoning_effort")}
    route.update(max_tokens=1000,temperature=0)
    monkeypatch.setattr(tasks,"resolve_stage_request",lambda *_:route)
    def model(**kwargs):
        owned=json.loads(kwargs["user_text"])["OWN"]
        if kind=="organize":
            ids=[t["token_id"] for c in owned for t in c["tokens"]]
            return json.dumps({"delete_token_ids":[ids[1]],"cues":[{"token_ids":[ids[0],ids[2]]},{"token_ids":ids[3:]}]}),{}
        token=owned[-1]["tokens"][-1]
        return json.dumps({"changes":[{"token_id":token["token_id"],"expected_text":token["text"],"text":"Wait!"}]}),{}
    monkeypatch.setattr(tasks,"call_text_model",model)
    reference=model_provider_credential_ref("test")
    payload={"schema_version":tasks.INPUT_SCHEMA,"expected_revision_id":revision.revision_id,"provider_id":"test",
             "credential_ref":reference,"optimize_boundaries":False,"settings":{"text_task_prompt":tasks.PROMPTS[kind]}}
    artifacts=tmp_path/"artifacts";artifacts.mkdir()
    command=WorkerCommand(task_id="task",attempt=1,task_type=kind,input_schema=tasks.INPUT_SCHEMA,input=payload,
        project_id="test",credential_refs=(reference,),paths=WorkerPaths(project_root=str(project),work_directory=str(tmp_path/"work"),artifact_directory=str(artifacts)))
    monkeypatch.setenv(credential_environment_key(reference),"test-key")
    monkeypatch.setattr(sys,"stdin",io.StringIO(command.to_json_line()))
    assert runtime.main()==0
    messages=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[0]["message_type"]=="ready"
    result=messages[-1]["data"]
    assert messages[-1]["message_type"]=="result"
    assert store.load_latest().revision_id==revision.revision_id
    handler=runtime.build_text_task_handler(kind,projects,project)
    registry=TaskRegistry();registry.register(handler)
    context=TaskWorkContext(task={"task_id":"task","project_id":"test"},input_payload=payload,
        attempt_directory=tmp_path,work_directory=tmp_path/"work",artifact_directory=artifacts)
    summary=handler.finalize(context,SimpleNamespace(result=result))
    assert store.load_latest().revision_id==summary["result_revision_id"]
    assert store.load_latest().document.source_tokens==doc.source_tokens


def test_api_default_off_locks_and_frozen_narrow_prompt(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from substar_core.editor import http_api as api
    store=ProjectStore.create(tmp_path/"project",project_id="test")
    revision=store.save(document(),provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    monkeypatch.setattr(api,"open_project_store",lambda _:store)
    monkeypatch.setattr(api,"_project_llm_settings",lambda *a,**k:{"active_model_provider":"test","translation_api_key":"must-not-freeze"})
    monkeypatch.setattr(api,"load_task_info",lambda *a,**k:{})
    monkeypatch.setattr(api,"task_info_settings",lambda *_:{})
    monkeypatch.setattr(api,"project_job_path",lambda _:tmp_path)
    service=SimpleNamespace(list_tasks=Mock(return_value=[]),create_task=Mock(return_value={"task_type":"organize","task_id":"task","state":"queued"}))
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(task_service=service)))
    result=api.start_ai_organize("test",api.SubtitleTextTaskRequest(expected_revision_id=revision.revision_id),request)
    frozen=service.create_task.call_args.args[2]
    assert result["kind"]=="organize" and frozen["optimize_boundaries"] is False
    assert "translation_api_key" not in frozen["settings"]
    assert "不得插入" in frozen["settings"]["text_task_prompt"]
    service.list_tasks.return_value=[{"task_type":"punctuation"}]
    with pytest.raises(HTTPException) as caught:
        api.start_ai_organize("test",api.SubtitleTextTaskRequest(expected_revision_id=revision.revision_id),request)
    assert caught.value.status_code==423


def test_over_200_native_operations_remain_a_single_candidate(tmp_path):
    doc=document(tuple("word" for _ in range(630)),tuple(3 for _ in range(210)))
    # Explicitly one OWN span to exercise >200 native edits without HTTP batching.
    rows=[c for b in tasks.task_blocks(doc,{}) for c in b["cues"]]
    block={"group_id":"large","cues":rows}
    ids=[t.token_id for t in doc.display_tokens]
    value={"delete_token_ids":[],"cues":[{"token_ids":ids[i:i+2]} for i in range(0,len(ids),2)]}
    result=tasks.apply_organize(doc,block,tasks.validate_result("organize",value,block,doc,{}),"large-task")
    assert len(result.changes)>200
    assert len(result.cues)==315 and result.source_tokens==doc.source_tokens
    assert result.display_tokens==doc.display_tokens


def test_translation_context_never_joins_output_coverage():
    from substar_core.cue_script import render_translation_request,finalize_translation
    groups=[{"group_id":"g","cues":[{"cue_id":"owned","source_text":"then wait"}],
             "context_before":[{"cue_id":"before","source_text":"if it rains"}],
             "context_after":[{"cue_id":"after","source_text":"until morning"}]}]
    wire,ledger=render_translation_request(groups,mapping_mode="many_to_many")
    assert "CONTEXT if it rains" in wire and "CONTEXT until morning" in wire
    assert len(ledger.cues)==1
    assert next(iter(ledger.cues.values()))["cue_id"]=="owned"
    result=finalize_translation("1|那就等一等",groups,ledger,mapping_mode="many_to_many")
    assert not result["_cue_script_issues"]
    assert result["_wire_units"][0]["cue_ids"]==["owned"]


def test_local_boundary_change_keeps_unrelated_cues_and_manual_times():
    doc=document(tuple("word" for _ in range(90)),tuple(6 for _ in range(15)))
    cues=list(doc.cues)
    cues[8]=replace(cues[8],start=cues[8].start+.1,end=cues[8].end-.2)
    doc=replace(doc,cues=tuple(cues))
    block=tasks.task_blocks(doc,{})[0]
    ids=[i for c in block["cues"] for i in c["token_ids"]]
    value={"delete_token_ids":[],"cues":[{"token_ids":ids[:7]},{"token_ids":ids[7:12]}]+
           [{"token_ids":c["token_ids"]} for c in block["cues"][2:]]}
    result=tasks.apply_organize(doc,block,value,"task")
    assert result.cues[2:]==doc.cues[2:]
    assert result.display_tokens==doc.display_tokens
    assert len(result.changes)-len(doc.changes)==2


def test_punctuation_does_not_hash_or_rebuild_full_document_per_block(tmp_path,monkeypatch):
    project=tmp_path/"root";project.mkdir()
    doc=document(tuple("word" for _ in range(240)),tuple(6 for _ in range(40)))
    store=ProjectStore.create(project/"project",project_id="test")
    revision=store.save(doc,provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    assert len(tasks.task_blocks(doc,{}))>2
    route={k:"test" for k in ("base_url","api_key","auth_mode","model","thinking_mode","reasoning_effort")}
    route.update(max_tokens=1000,temperature=0)
    monkeypatch.setattr(tasks,"resolve_stage_request",lambda *_:route)
    monkeypatch.setattr(tasks,"call_text_model",lambda **_:('{"changes":[]}',{}))
    hashes=[]
    original=EditorDocument.content_hash
    monkeypatch.setattr(EditorDocument,"content_hash",lambda self:(hashes.append(1),original(self))[1])
    tasks.compute("punctuation",project,tmp_path/"candidate",{"expected_revision_id":revision.revision_id,
                  "settings":{"text_task_prompt":tasks.PROMPTS["punctuation"]}},"task",lambda *_:None)
    assert len(hashes)<=3


def test_publication_retry_after_manual_edit_never_reapplies_ai_version(tmp_path):
    from substar_core.editor.application.publication import write_candidate
    store=ProjectStore.create(tmp_path/"project",project_id="test")
    doc=document()
    initial=store.save(doc,provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    artifacts=tmp_path/"candidate"
    candidate=write_candidate(artifacts,initial,doc,ChangeProvenance(kind=ChangeKind.AI,operation="ai_organize_apply"))
    summary={"result_revision_id":candidate.revision_id}
    published=publish_candidate(store,artifacts,task_id="task",expected_revision_id=initial.revision_id,summary=summary)
    manual=apply_document_operation(published.document,{"operation_id":"manual-after-ai","type":"delete",
                "payload":{"token_ids":[doc.display_tokens[0].token_id]}})
    latest=store.save(manual,provenance=ChangeProvenance(kind=ChangeKind.MANUAL,operation="delete"),expected_revision_id=published.revision_id)
    retried=publish_candidate(store,artifacts,task_id="task",expected_revision_id=initial.revision_id,summary=summary)
    assert retried.revision_id==published.revision_id
    assert store.load_latest().revision_id==latest.revision_id
    assert len(store.list_revision_metadata())==3
    ProjectStore.clear_memory_cache(store.root)
    assert ProjectStore.open(store.root).load_latest().document==manual


def test_ai_candidate_conflict_preserves_new_manual_edit(tmp_path):
    from substar_core.editor.application.publication import write_candidate
    store=ProjectStore.create(tmp_path/"project",project_id="test")
    doc=document()
    initial=store.save(doc,provenance=ChangeProvenance(kind=ChangeKind.IMPORT,operation="test"))
    artifacts=tmp_path/"candidate"
    candidate=write_candidate(artifacts,initial,doc,ChangeProvenance(kind=ChangeKind.AI,operation="ai_organize_apply"))
    manual=apply_document_operation(doc,{"operation_id":"manual-during-ai","type":"delete",
                "payload":{"token_ids":[doc.display_tokens[0].token_id]}})
    latest=store.save(manual,provenance=ChangeProvenance(kind=ChangeKind.MANUAL,operation="delete"),expected_revision_id=initial.revision_id)
    with pytest.raises(ValueError,match="source revision changed"):
        publish_candidate(store,artifacts,task_id="task",expected_revision_id=initial.revision_id,summary={"result_revision_id":candidate.revision_id})
    assert store.load_latest().revision_id==latest.revision_id
    assert store.publication_result("task") is None


@pytest.mark.parametrize("seed",range(25))
def test_minimal_reframe_preserves_coverage_and_deleted_history(seed):
    import random
    rng=random.Random(seed)
    doc=document(tuple("word" for _ in range(36)),tuple(6 for _ in range(6)))
    ids=[t.token_id for t in doc.display_tokens]
    old_deleted=[i for i in ids if rng.random()<.1]
    if old_deleted:
        doc=apply_document_operation(doc,{"operation_id":"old-delete","type":"delete","payload":{"token_ids":old_deleted}})
    block=tasks.task_blocks(doc,{})[0]
    owned=[i for c in block["cues"] for i in c["token_ids"]]
    deleted=[i for i in owned if rng.random()<.15]
    remaining=[i for i in owned if i not in deleted]
    rows=[]
    while remaining:
        size=rng.randint(1,8)
        rows.append({"token_ids":remaining[:size]});remaining=remaining[size:]
    value={"delete_token_ids":deleted,"cues":rows}
    result=tasks.apply_organize(doc,block,tasks.validate_result("organize",value,block,doc,{}),"task")
    active={t.token_id for t in result.display_tokens if t.state is EntityState.ACTIVE}
    visible=[[i for i in c.display_token_ids if i in active] for c in result.cues if c.state is EntityState.ACTIVE]
    assert visible==[r["token_ids"] for r in rows]
    assert not active.intersection(old_deleted)
    assert result.source_tokens==doc.source_tokens
    assert [t.text for t in result.display_tokens]==[t.text for t in doc.display_tokens]
