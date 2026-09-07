from dataclasses import replace
import json
import pytest

from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind, TranslationTrack, EntityState
from substar_core.document_operations import apply_document_operation, apply_document_batch, DocumentOperationError
from substar_core.editor.application.editing_service import EditingService, StaleOperationError, InvalidEditorOperationError
from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository
from substar_core.editor.application.publication import write_candidate, publish_candidate
from substar_core.editor.translation.contextual import materialize_presentation
from substar_core.storage import ProjectStore


def document(count=4):
    provenance=ChangeProvenance(ChangeKind.SOURCE,'audit_fixture')
    sources=tuple(SourceToken.create(index=i,text=f'word{i}',start=i,end=i+0.8) for i in range(count))
    tokens=tuple(DisplayToken.create(position=i,text=t.text,source_token_ids=(t.token_id,),provenance=provenance) for i,t in enumerate(sources))
    cues=tuple(DisplayCue(cue_id=f'cue{i}',index=i//2,display_token_ids=tuple(t.token_id for t in tokens[i:i+2]),start=i,end=i+1.8) for i in range(0,count,2))
    return EditorDocument('audit',sources,tokens,cues)


def op(doc,kind,payload,id='edit'):
    return {'operation_id':id,'type':kind,'payload':payload}


def test_deleted_cue_survives_translation():
    doc=document()
    doc=apply_document_operation(doc,op(doc,'delete',{'cue_ids':[doc.cues[0].cue_id]}))
    candidate,_=materialize_presentation(doc,[],'zh-CN')
    assert next(c for c in candidate.cues if c.cue_id==doc.cues[0].cue_id)==doc.cues[0]
    candidate.validate()


@pytest.mark.parametrize('text',['','候选'])
def test_split_merge_inherits_unresolved(text):
    doc=document()
    track=TranslationTrack(text,ChangeProvenance(ChangeKind.AI,'translation'),translation_status='manual_required',issue_code='translation_unresolved')
    doc=replace(doc,cues=(replace(doc.cues[0],target=track),*doc.cues[1:]))
    split=apply_document_operation(doc,op(doc,'split_cue',{'cue_id':doc.cues[0].cue_id,'after_token_id':doc.display_tokens[0].token_id,'right_cue_id':'cue_right'}))
    assert all(c.target.translation_status=='manual_required' for c in split.cues[:2])
    merged=apply_document_operation(split,op(split,'merge_cues',{'cue_ids':[c.cue_id for c in split.cues[:2]]},'merge'))
    assert merged.cues[0].target.translation_status=='manual_required'
    assert merged.cues[0].mapping['translation_unresolved']


def test_source_edit_keeps_translation_but_marks_review():
    doc=document();track=TranslationTrack('译文',ChangeProvenance(ChangeKind.AI,'translation'))
    doc=replace(doc,cues=(replace(doc.cues[0],target=track),*doc.cues[1:]))
    candidate=apply_document_operation(doc,op(doc,'replace',{'token_id':doc.display_tokens[0].token_id,'text':'new','expected_text':'word0'}))
    assert candidate.cues[0].target.target_text=='译文'
    assert candidate.cues[0].target.translation_status=='needs_review'


def test_merging_copied_tracks_preserves_right_hand_review_status():
    doc=document()
    track=TranslationTrack('译文',ChangeProvenance(ChangeKind.AI,'translation'))
    doc=replace(doc,cues=(replace(doc.cues[0],target=track),*doc.cues[1:]))
    split=apply_document_operation(doc,op(doc,'split_cue',{'cue_id':doc.cues[0].cue_id,'after_token_id':doc.display_tokens[0].token_id,'right_cue_id':'cue_right'}))
    right=replace(split.cues[1],target=replace(split.cues[1].target,translation_status='needs_review',issue_code='source_changed'))
    split=replace(split,cues=(split.cues[0],right,*split.cues[2:]))
    merged=apply_document_operation(split,op(split,'merge_cues',{'cue_ids':[split.cues[0].cue_id,right.cue_id]},'merge'))
    assert merged.cues[0].target.translation_status=='needs_review'


def test_ack_loss_commits_exactly_once_and_id_reuse_is_rejected(tmp_path):
    store=ProjectStore.create(tmp_path/'project',project_id='audit')
    initial=store.save(document(),provenance=ChangeProvenance(ChangeKind.IMPORT,'init'))
    service=EditingService(lambda _:SQLiteProjectRepository(store))
    operation=op(initial.document,'replace',{'token_id':initial.document.display_tokens[0].token_id,'text':'new','expected_text':'word0'})
    base={'document_id':'audit','revision_id':initial.revision_id,'document_hash':initial.document_hash}
    first=service.commit_batch('audit',base=base,operations=[operation],batch_id='first')
    replay=service.commit_batch('audit',base=base,operations=[operation],batch_id='retry')
    assert replay.after.revision_id==first.after.revision_id
    assert store.load_latest().revision_number==2
    with pytest.raises(InvalidEditorOperationError):
        service.commit_batch('audit',base=base,operations=[{**operation,'payload':{**operation['payload'],'text':'different'}}],batch_id='bad')
    with pytest.raises(StaleOperationError):
        service.commit_batch('audit',base=base,operations=[{**operation,'operation_id':'different'}],batch_id='stale')


def test_batch_matches_sequential_and_failed_batch_is_atomic(monkeypatch):
    import substar_core.document_operations as reducers
    original = reducers._provenance
    monkeypatch.setattr(reducers, '_provenance', lambda *args, **kwargs: replace(original(*args, **kwargs), created_at='2026-01-01T00:00:00+00:00'))
    doc=document(20)
    operations=[op(doc,'replace',{'token_id':t.token_id,'text':f'new{i}','expected_text':t.text,'provenance':{'created_at':'2026-01-01T00:00:00+00:00'}},str(i)) for i,t in enumerate(doc.display_tokens[:10])]
    batch=apply_document_batch(doc,operations)
    sequential=doc
    for operation in operations:sequential=apply_document_operation(sequential,operation)
    assert batch.to_dict()==sequential.to_dict()
    with pytest.raises(DocumentOperationError):apply_document_batch(doc,[*operations,operations[0]])
    assert doc.display_tokens[0].text=='word0'


def test_candidate_io_failure_does_not_publish_and_receipt_is_durable(tmp_path,monkeypatch):
    store=ProjectStore.create(tmp_path/'project',project_id='audit')
    initial=store.save(document(),provenance=ChangeProvenance(ChangeKind.IMPORT,'init'))
    candidate=replace(initial.document,changes=(ChangeProvenance(ChangeKind.AI,'candidate'),))
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    predicted=write_candidate(artifacts,initial,candidate,candidate.changes[-1])
    from pathlib import Path
    import jsonschema
    schema_path=Path(__file__).resolve().parents[1]/'docs/architecture/target/contracts/editor-candidate.schema.json'
    jsonschema.validate(json.loads((artifacts/'candidate.json').read_text(encoding='utf-8')),json.loads(schema_path.read_text(encoding='utf-8')))
    assert store.load_latest().revision_id==initial.revision_id
    summary={'result_revision_id':predicted.revision_id,'problem_cue_ids':[]}
    saved=publish_candidate(store,artifacts,task_id='task',expected_revision_id=initial.revision_id,summary=summary)
    assert saved.revision_id==predicted.revision_id
    assert publish_candidate(store,artifacts,task_id='task',expected_revision_id=initial.revision_id,summary=summary).revision_id==saved.revision_id
    assert ProjectStore.open(tmp_path/'project').publication_result('task')['result_revision_id']==saved.revision_id
    (artifacts/'candidate.json').write_text('{}')
    with pytest.raises(Exception):publish_candidate(store,artifacts,task_id='other',expected_revision_id=initial.revision_id,summary=summary)
    assert store.load_latest().revision_number==2


def test_timing_survives_material_and_document_roundtrip(tmp_path):
    from substar_core.segmentation.input_contract import build_segmentation_material,load_segmentation_material
    from substar_core.contracts.editor_document import source_tokens_from_asr
    value=build_segmentation_material('',{'units':[{'index':7,'text':'重庆','start':1,'end':2}]})
    path=tmp_path/'material.json';path.write_text(json.dumps(value),encoding='utf-8')
    _,units=load_segmentation_material(path)
    sources=source_tokens_from_asr(units,source_asset_id='video')
    assert len(sources)==2
    for t in sources:
        assert t.timing['kind']=='subdivided'
        assert t.timing['native_token_ids']==['7']
        assert (t.timing['native_start'],t.timing['native_end'])==(1,2)
        assert SourceToken.from_dict(t.to_dict()).to_dict()==t.to_dict()


def test_hash_does_not_depend_on_optional_encoder(monkeypatch):
    import substar_core.domain.editor_document as domain
    doc=document();doc=replace(doc,source_tokens=(replace(doc.source_tokens[0],start=.00001),*doc.source_tokens[1:]))
    before=doc.content_hash()
    monkeypatch.setattr(domain,'orjson',None)
    assert doc.content_hash()==before


def test_disjoint_stale_replacement_rebases_but_single_ack_replay_is_idempotent(tmp_path):
    store=ProjectStore.create(tmp_path/'project',project_id='audit')
    initial=store.save(document(),provenance=ChangeProvenance(ChangeKind.IMPORT,'init'))
    service=EditingService(lambda _:SQLiteProjectRepository(store))
    base={'document_id':'audit','revision_id':initial.revision_id,'document_hash':initial.document_hash}
    first={**op(initial.document,'replace',{'token_id':initial.document.display_tokens[0].token_id,'text':'first','expected_text':'word0'}),'base':base}
    saved=service.commit_operation('audit',first)
    assert service.commit_operation('audit',first).after.revision_id==saved.after.revision_id
    second=op(initial.document,'replace',{'token_id':initial.document.display_tokens[1].token_id,'text':'second','expected_text':'word1'},'second')
    result=service.commit_batch('audit',base=base,operations=[second],batch_id='disjoint')
    assert [t.text for t in result.after.document.display_tokens[:2]]==['first','second']
    assert store.load_latest().revision_number==3


def test_report_write_failure_keeps_latest_and_retry_recovers_committed_receipt(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from substar_core.runtime import RuntimeStore, TaskService
    from substar_core.editor.translation.handler import build_translation_handler
    from substar_core.editor.application.publication import recover_publication
    import substar_core.editor.translation.handler as handler_module
    work=tmp_path/'projects'/'audit'
    store=ProjectStore.create(work/'project',project_id='audit')
    initial=store.save(document(),provenance=ChangeProvenance(ChangeKind.IMPORT,'init'))
    service=TaskService(RuntimeStore(tmp_path/'runtime.sqlite3'),'audit-instance')
    task=service.create_task(task_type='translation',input_schema='substar.translation-input.v2',input_payload={},project_id='audit')
    service.claim_next({'translation'})
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    predicted=write_candidate(artifacts,initial,initial.document,ChangeProvenance(ChangeKind.AI,'translation'))
    context=SimpleNamespace(task=task,artifact_directory=artifacts,input_payload={'expected_revision_id':initial.revision_id,'mapping_mode':'one_to_one'})
    completion=SimpleNamespace(result={'schema_version':'substar.translation-result.v2','summary':{'result_revision_id':predicted.revision_id,'planned':1,'problem_cue_ids':[]}})
    handler=build_translation_handler(tmp_path/'projects',tmp_path)
    with monkeypatch.context() as injection:
        injection.setattr(handler_module,'atomic_write_json',lambda *a,**k:(_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError,match='disk full'):handler.finalize(context,completion)
    assert store.load_latest().revision_id==initial.revision_id
    result=handler.finalize(context,completion)
    assert store.load_latest().revision_id==result['result_revision_id']
    assert service.get_task(task['task_id'])['state']=='running'  # Lost runtime ACK.
    service.publication_recoverer=lambda task_id:recover_publication(service,tmp_path/'projects',task_id)
    restored=service.retry(task['task_id'])
    assert restored['state']=='succeeded'
    assert restored['attempt']==1
    assert restored['result']==result
    assert store.load_latest().revision_number==2


def test_reference_cpu_worker_is_cancellable_without_blocking_loop(monkeypatch):
    import asyncio,sys,time
    from types import SimpleNamespace
    import substar_core.editor.application.reference as reference
    monkeypatch.setattr(reference,'python_script_command',lambda _: [sys.executable,'-c','import time; time.sleep(30)'])
    async def scenario():
        ticks=[]
        async def ticker():
            for _ in range(10):
                await asyncio.sleep(.025)
                ticks.append(1)
        async def disconnected():return True
        start=time.monotonic()
        task=asyncio.create_task(reference.match_reference(b'hello','reference.txt',[],'en',SimpleNamespace(is_disconnected=disconnected)))
        await ticker()
        with pytest.raises(asyncio.CancelledError):await task
        assert len(ticks)==10 and time.monotonic()-start<3
    asyncio.run(scenario())


def test_frozen_prompt_survives_registry_change_and_detects_tampering(monkeypatch):
    from dataclasses import asdict
    import substar_core.prompt_registry as prompts
    frozen=prompts.render_prompt('contextual_translation',variant='en_to_zh',mode='one_to_one')
    settings={'prompt_snapshot':{'contextual_translation':asdict(frozen)},'max_tokens':4096,'glossary_snapshot':[]}
    monkeypatch.setattr(prompts,'render_prompt',lambda *a,**k:(_ for _ in ()).throw(AssertionError('live prompt read')))
    assert prompts.frozen_prompt(settings,'contextual_translation').text==frozen.text
    settings['prompt_snapshot']['contextual_translation']['text']='modified'
    with pytest.raises(prompts.PromptRegistryError):prompts.frozen_prompt(settings,'contextual_translation')


def test_legacy_hash_reader_accepts_old_float_encoding():
    import hashlib
    import substar_core.domain.editor_document as domain
    value={'tiny':1e-7,'wide':1e20,'negative':-0.0,'unicode':'字幕'}
    old=hashlib.sha256(domain.orjson.dumps(value,option=domain.orjson.OPT_SORT_KEYS)).hexdigest()
    assert domain.compatible_content_hash(value,old)
    assert not domain.compatible_content_hash(value,'0'*64)


def test_cancel_cannot_overtake_the_publication_boundary(tmp_path):
    import threading
    from substar_core.runtime import RuntimeStore,TaskService,TaskStateConflictError
    service=TaskService(RuntimeStore(tmp_path/'runtime.sqlite3'),'audit-instance')
    task=service.create_task(task_type='translation',input_schema='substar.translation-input.v2',input_payload={},project_id='audit')
    service.claim_next({'translation'})
    started=threading.Event();finished=threading.Event();errors=[]
    def cancel():
        started.set()
        try:service.request_cancel(task['task_id'])
        except TaskStateConflictError as error:errors.append(error)
        finally:finished.set()
    with service.publication_lock:
        thread=threading.Thread(target=cancel);thread.start()
        assert started.wait(1)
        assert not finished.wait(.03)
        service.complete(task['task_id'],1,{'result_revision_id':'published'})
    thread.join(2)
    assert finished.is_set() and len(errors)==1
    assert service.get_task(task['task_id'])['state']=='succeeded'
