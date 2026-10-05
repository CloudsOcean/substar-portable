import pytest
from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind
from substar_core.storage.project_store import ProjectStore, _batch_content_hash
from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository
from substar_core.editor.application.editing_service import EditingService, InvalidEditorOperationError

def setup(tmp_path):
    source = SourceToken.create(index=0,text="Hello",start=1,end=2)
    token = DisplayToken.create(position=0,text="Hello",source_token_ids=(source.token_id,),provenance=ChangeProvenance(ChangeKind.SOURCE,"test"))
    doc = EditorDocument("project",(source,),(token,),(DisplayCue("cue",0,(token.token_id,),1,2),))
    store = ProjectStore.create(tmp_path / "project",project_id="test")
    # create returns a store in the project storage API.
    r = store.save(doc,provenance=ChangeProvenance(ChangeKind.SOURCE,"test"))
    base={"document_id":doc.document_id,"revision_id":r.revision_id,"document_hash":r.document_hash}
    return store,base

def op(i,before,after):
    return {"operation_id":str(i),"type":"set_target","payload":{"cue_id":"cue","expected_target_text":before,"target_text":after}}

def test_batch_preserves_steps_and_retry_receipts(tmp_path):
    store,base=setup(tmp_path); service=EditingService(lambda _:SQLiteProjectRepository(store))
    operations=[op(1,"","one"),op(2,"one","two"),op(3,"two","three")]
    result=service.commit_batch("test",base=base,operations=operations,batch_id="batch",preserve_user_actions=True)
    assert [r.document.cues[0].target.target_text for r in result.action_revisions]==["one","two","three"]
    for r in result.action_revisions:
        assert r.document_hash==r.document.content_hash()
    count=len(store.list_revision_metadata())
    replay=service.commit_batch("test",base=base,operations=operations,batch_id="retry",preserve_user_actions=True)
    assert len(replay.action_revisions)==3
    assert len(store.list_revision_metadata())==count
    ProjectStore.clear_memory_cache()
    assert store.load_latest().document.cues[0].target.target_text=="three"
    assert store.load_revision(result.action_revisions[0].revision_id).document.cues[0].target.target_text=="one"

def test_invalid_later_edit_rolls_back_whole_batch(tmp_path):
    store,base=setup(tmp_path); service=EditingService(lambda _:SQLiteProjectRepository(store))
    with pytest.raises(InvalidEditorOperationError):
        service.commit_batch("test",base=base,operations=[op(1,"","one"),op(2,"wrong","two")],batch_id="bad",preserve_user_actions=True)
    assert store.load_latest().revision_id==base['revision_id']

def test_one_user_action_remains_one_undo_step(tmp_path):
    store,base=setup(tmp_path); service=EditingService(lambda _:SQLiteProjectRepository(store))
    operations=[op(1,'','one'),op(2,'one','two'),op(3,'two','three')]
    for item in operations[:2]:
        item['payload']['provenance']={'kind':'manual','operation':'test','metadata':{'user_action_id':'one-action'}}
    result=service.commit_batch('test',base=base,operations=operations,batch_id='batch',preserve_user_actions=True)
    assert [r.document.cues[0].target.target_text for r in result.action_revisions]==['two','three']

def test_database_error_rolls_back_intermediate_revisions(tmp_path,monkeypatch):
    store,base=setup(tmp_path); original=store.save; calls=0
    def failing(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==2: raise RuntimeError('injected write failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(store,'save',failing)
    service=EditingService(lambda _:SQLiteProjectRepository(store))
    with pytest.raises(RuntimeError):
        service.commit_batch('test',base=base,operations=[op(1,'','one'),op(2,'one','two')],batch_id='bad',preserve_user_actions=True)
    ProjectStore.clear_memory_cache()
    assert store.load_latest().revision_id==base['revision_id']
