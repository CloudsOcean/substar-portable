from dataclasses import replace

import pytest

from substar_core.domain import ChangeProvenance, DisplayCue, EditorDocument
from substar_core.document_operations import apply_document_operation, DocumentOperationError
from substar_core.editor import http_api
from substar_core.editor.api.editing_endpoints import DocumentOperationBatchRequest, commit_operation_batch
from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository
from substar_core.storage.project_store import ProjectStore


@pytest.fixture
def project(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / 'project', project_id='save-audit')
    document = EditorDocument('document', (), (), (DisplayCue('cue', 0, (), 0, 2, source_text='original'),))
    initial = store.save(document, provenance=ChangeProvenance(kind='import', operation='test'))
    monkeypatch.setattr(http_api, 'open_project_store', lambda _: store)
    return store, initial


def test_storage_failure_mid_sequence_rolls_back_all_user_action_revisions(project,monkeypatch):
    from substar_core.storage import ProjectStoreError
    store, initial = project
    first = replace(initial.document,cues=(replace(initial.document.cues[0],source_text="first edit"),))
    second = replace(initial.document,cues=(replace(initial.document.cues[0],source_text="second edit"),))
    original_save = store.save
    calls = []
    def failing_save(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:
            raise ProjectStoreError("injected storage failure")
        return original_save(*args,**kwargs)
    monkeypatch.setattr(store,"save",failing_save)
    provenance = ChangeProvenance(kind="manual",operation="operation_batch")
    with pytest.raises(ProjectStoreError,match="injected"):
        store.save_sequence([(first,provenance),(second,provenance)],expected_revision_id=initial.revision_id)
    ProjectStore.clear_memory_cache(store.root)
    reloaded = ProjectStore.open(store.root)
    assert reloaded.load_latest().revision_id==initial.revision_id
    assert reloaded.load_latest().document==initial.document
    assert len(reloaded.list_revision_metadata())==1


def batch(revision, operation, batch_id):
    return DocumentOperationBatchRequest(
        batch_id=batch_id, preserve_user_actions=True,
        base={'document_id': revision.document.document_id, 'revision_id': revision.revision_id,
              'document_hash': revision.document_hash},
        operations=[{**operation, 'base': {}}],
    )


def commit(store, request):
    return commit_operation_batch('save-audit', request,
        repository_factory=lambda _: SQLiteProjectRepository(store), serialize_delta=http_api.revision_delta_payload)


def test_lost_save_response_retry_after_undo_keeps_restored_document_and_navigation(project):
    store, initial = project
    operation = {'operation_id': 'edit-1', 'type': 'set_target',
                 'payload': {'cue_id': 'cue', 'expected_target_text': '', 'target_text': 'translation'}}
    request = batch(initial, operation, 'first')
    edited = commit(store, request)
    restored = http_api.restore_project_revision('save-audit', http_api.RestoreRevisionRequest(
        expected_revision_id=edited['revision_id'], revision_id=initial.revision_id,
        navigation='undo', undo_revision_ids=[], redo_revision_ids=[edited['revision_id']]))
    count = len(store.list_revision_metadata())
    retry = commit(store, request.model_copy(update={'batch_id': 'retry'}))
    assert len(store.list_revision_metadata()) == count
    assert retry['revision_id'] == restored['revision_id']
    assert retry['provenance']['operation'] == 'restore_revision'
    assert retry['provenance']['metadata']['redo_revision_ids'] == [edited['revision_id']]
    assert retry['acknowledged_operation_ids'] == ['edit-1']
    ProjectStore.clear_memory_cache()
    assert store.load_latest().document.cues[0].target is None


def test_checkpoint_payload_keeps_navigation_recoverable_after_restart(project):
    store, initial = project
    checkpoint = http_api.create_project_checkpoint('save-audit', http_api.CheckpointRequest(
        expected_revision_id=initial.revision_id, label='snapshot'))
    assert checkpoint['provenance']['operation'] == 'checkpoint'
    assert checkpoint['provenance']['metadata']['label'] == 'snapshot'
    ProjectStore.clear_memory_cache()
    loaded = http_api.revision_payload(store.load_latest())
    assert loaded['provenance'] == checkpoint['provenance']
    assert store.load_latest().document == initial.document


def test_source_edit_guards_the_original_text_even_on_a_new_revision(project):
    store, initial = project
    newer = replace(initial.document, cues=(replace(initial.document.cues[0], source_text='someone else'),))
    operation = {'operation_id': 'source-edit', 'type': 'set_source_text',
                 'payload': {'cue_id': 'cue', 'expected_source_text': 'original', 'text': 'my input'}}
    with pytest.raises(DocumentOperationError, match='原文已被其他编辑修改'):
        apply_document_operation(newer, operation)
    equal = {**operation, 'payload': {**operation['payload'], 'text': 'someone else'}}
    assert apply_document_operation(newer, equal) is newer


def test_stale_checkpoint_and_restore_never_replace_a_newer_edit(project):
    from fastapi import HTTPException
    store, initial = project
    newer = store.save(initial.document, provenance=ChangeProvenance(kind='manual', operation='other-edit'),
                       expected_revision_id=initial.revision_id)
    for action in [
        lambda: http_api.create_project_checkpoint('save-audit', http_api.CheckpointRequest(
            expected_revision_id=initial.revision_id, label='stale')),
        lambda: http_api.restore_project_revision('save-audit', http_api.RestoreRevisionRequest(
            expected_revision_id=initial.revision_id, revision_id=initial.revision_id)),
    ]:
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 409
        assert store.load_latest().revision_id == newer.revision_id


def test_converted_translation_keeps_raw_guard_for_edits(project):
    store, initial = project
    document = apply_document_operation(initial.document, {'operation_id': 'translation', 'type': 'set_target',
        'payload': {'cue_id': 'cue', 'target_text': '鼠标软件网络'}})
    document = replace(document, properties=replace(document.properties, script_projection='traditional_tw'))
    converted = store.save(document, provenance=ChangeProvenance(kind='manual', operation='set_script_projection'),
                           expected_revision_id=initial.revision_id)
    target = http_api.revision_payload(converted)['document']['cues'][0]['target']
    assert target['target_text'] == '滑鼠軟體網路'
    assert target['canonical_target_text'] == '鼠标软件网络'
    response = commit(store, batch(converted, {'operation_id': 'edit-converted', 'type': 'set_target',
        'payload': {'cue_id': 'cue', 'expected_target_text': target['canonical_target_text'],
                    'target_text': target['target_text'] + ' 2'}}, 'converted'))
    assert response['cues']['upsert'][0]['target']['canonical_target_text'].endswith(' 2')
    assert store.load_latest().document.cues[0].target.target_text == '滑鼠軟體網路 2'
