from dataclasses import replace
import pytest
from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind, TranslationTrack, DisplayOrder, EntityState
from substar_core.export import render_document_srt
from substar_core.editor.application.srt_import import preview_srt, import_srt, parse_srt
from substar_core.storage import ProjectStore, ProjectConflictError


def fixture():
    provenance = ChangeProvenance(ChangeKind.SOURCE, 'fixture')
    sources = tuple(SourceToken.create(index=i, text=t, start=i*3, end=i*3+2)
                    for i,t in enumerate(['今天开会', '明天出发', '好的']))
    tokens = tuple(DisplayToken.create(position=i, text=t.text, source_token_ids=(t.token_id,), provenance=provenance)
                   for i,t in enumerate(sources))
    targets = ['Today we discuss the proposal with 张先生 in Beijing.',
               'Tomorrow we leave together; the slogan is 加油.', '好的']
    cues = tuple(DisplayCue(cue_id=f'cue{i}', index=i, display_token_ids=(t.token_id,), start=i*3, end=i*3+2,
                           target=TranslationTrack(targets[i], provenance, language='en')) for i,t in enumerate(tokens))
    return EditorDocument('srt-fixture', sources, tokens, cues)


@pytest.mark.parametrize('mode', ['source', 'target', 'ab-single', 'ab-double'])
@pytest.mark.parametrize('reverse', [False, True])
def test_exported_tracks_round_trip_with_mixed_language(mode, reverse):
    doc = fixture()
    if reverse:
        doc = replace(doc, presentation=replace(doc.presentation, display_order=DisplayOrder.TARGET_ABOVE_SOURCE))
    text = render_document_srt(doc, mode)
    preview = preview_srt(doc, text)
    assert preview['format'] == mode
    if mode == 'source':
        assert preview['matched'] == 0
    else:
        assert preview['matched'] == 3
        assert preview['rows'][0]['target'] == doc.cues[0].target.target_text
        assert preview['rows'][1]['target'] == doc.cues[1].target.target_text
        assert preview['rows'][2]['target'] == '好的'


def test_foreign_track_keeps_internal_whitespace_and_same_language_rows():
    doc = fixture()
    text = render_document_srt(doc, 'target').replace('Tomorrow we', 'Tomorrow  we')
    preview = preview_srt(doc, text)
    assert 'Tomorrow  we' in preview['rows'][1]['target']
    assert preview['rows'][2]['target'] == '好的'


def test_ambiguous_and_unmatched_timing_are_not_guessed():
    doc = fixture()
    text = render_document_srt(doc, 'target')
    duplicated = text + '\n' + text.split('\n\n')[0] + '\n'
    preview = preview_srt(doc, duplicated, 'target')
    assert not preview['rows'][0]['importable']
    assert not preview['rows'][-1]['importable']
    shifted = text.replace('00:00:00,000', '00:00:01,000')
    assert not preview_srt(doc, shifted)['rows'][0]['importable']


def test_translation_import_is_atomic_idempotent_and_keeps_source(tmp_path):
    doc = fixture()
    text = render_document_srt(doc, 'target')
    empty = replace(doc, cues=tuple(replace(c, target=None) for c in doc.cues))
    store = ProjectStore.create(tmp_path/'project', project_id='srt')
    first = store.save(empty, provenance=ChangeProvenance(ChangeKind.IMPORT, 'init'))
    kwargs = dict(text=text, mode='target', expected_revision_id=first.revision_id, request_id='import1')
    saved = import_srt(store, **kwargs)
    assert import_srt(store, **kwargs).revision_id == saved.revision_id
    assert saved.document.source_tokens == empty.source_tokens
    assert saved.document.display_tokens == empty.display_tokens
    assert [(c.cue_id,c.start,c.end,c.display_token_ids) for c in saved.document.cues] == [(c.cue_id,c.start,c.end,c.display_token_ids) for c in empty.cues]
    assert [c.target.target_text for c in saved.document.cues] == [c.target.target_text for c in doc.cues]
    with pytest.raises(ProjectConflictError):
        import_srt(store, **{**kwargs, 'request_id':'stale'})


def test_same_language_track_and_deleted_cues_are_not_imported():
    doc = fixture()
    assert preview_srt(doc, render_document_srt(doc, 'source'), 'target')['matched'] == 0
    deleted = replace(doc, cues=(replace(doc.cues[0], state=EntityState.DELETED), *doc.cues[1:]))
    assert not preview_srt(deleted, render_document_srt(doc, 'target'), 'target')['rows'][0]['importable']


@pytest.mark.parametrize('text', ['', 'not srt', '1\n00:99:00,000 --> 00:00:02,000\nHi', '1\n00:00:03,000 --> 00:00:02,000\nHi'])
def test_invalid_srt_rejected(text):
    with pytest.raises(ValueError): parse_srt(text)


def test_http_preview_apply_and_stale_revision(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from substar_core.editor import http_api
    doc = fixture()
    store = ProjectStore.create(tmp_path/'http', project_id='srt-http')
    first = store.save(doc, provenance=ChangeProvenance(ChangeKind.IMPORT, 'init'))
    monkeypatch.setattr(http_api, 'open_project_store', lambda project_id: store)
    app = FastAPI()
    app.include_router(http_api.router)
    client = TestClient(app)
    route = '/api/projects/srt-http/import-srt-translation'
    payload = dict(text=render_document_srt(doc, 'ab-double'), format='auto',
                   expected_revision_id=first.revision_id, request_id='http-import')
    preview = client.post(route, json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()['matched'] == 3
    assert store.load_latest().revision_id == first.revision_id
    applied = client.post(route, json={**payload, 'apply': True})
    assert applied.status_code == 200, applied.text
    assert client.post(route, json={**payload, 'apply': True}).json() == applied.json()
    assert client.post(route, json=payload).status_code == 409
