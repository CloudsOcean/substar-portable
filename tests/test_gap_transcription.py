import json,sys,tempfile,unittest
from pathlib import Path
from dataclasses import replace
import os

from substar_core.editor.gap_transcription import recognition_segments,replacement_document,restore_gap,range_signature,_run
from substar_core.domain import EditorDocument,EntityState,ChangeProvenance,ChangeKind
from substar_core.domain.editor_document import TranslationTrack
from substar_core.storage import ProjectStore



class GapTranscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_subtitle_text_tasks import document
        cls.document=document()
        cls.a,cls.b=0,3
    def segments(self):
        return recognition_segments({'units':[
            {'text':'What','start':.12,'end':.45,'sentence_start':True},
            {'text':'happened?','start':.55,'end':1.1,'sentence_end':True},
            {'text':'A new sentence.','start':2,'end':3,'sentence_start':True,'sentence_end':True}
        ]},self.a,self.b)
    def test_overwrite_keeps_outside_source_and_timing(self):
        result,provenance,count=replacement_document(self.document,self.segments(),self.a,self.b,'gap_'+'a'*32)
        result.validate();self.assertGreater(count,0)
        source_map={t.token_id:t for t in result.source_tokens}
        for token in self.document.source_tokens:
            if token.end<=self.a or token.start>=self.b:
                self.assertEqual(replace(source_map[token.token_id],index=token.index),token)
        self.assertEqual(result.presentation,self.document.presentation)
        newmap={c.cue_id:c for c in result.cues}
        for cue in self.document.cues:
            if cue.end<=self.a+.01 or cue.start>=self.b-.01 or cue.mapping.get('subtitle_track')=='overlay':
                self.assertEqual(cue,replace(newmap[cue.cue_id],index=cue.index))
        created=[c for c in result.cues if c.cue_id not in {v.cue_id for v in self.document.cues}]
        self.assertEqual(len(created),2)
        self.assertTrue(all(self.a<=c.start<c.end<=self.b for c in created))
        for c in created:
            for tid in c.display_token_ids:
                self.assertIn(tid,result.properties.to_dict()['timeline']['word_timings'])
    def test_range_conflict_and_boundary_protection(self):
        sig=range_signature(self.document,self.a,self.b)
        tokens=list(self.document.display_tokens)
        cue=next(c for c in self.document.cues if c.state is EntityState.ACTIVE and c.start>=self.a and c.end<=self.b)
        idx=next(i for i,t in enumerate(tokens) if t.token_id in cue.display_token_ids)
        tokens[idx]=replace(tokens[idx],text='edited')
        self.assertNotEqual(sig,range_signature(replace(self.document,display_tokens=tuple(tokens)),self.a,self.b))
        with self.assertRaises(ValueError):range_signature(self.document,cue.start+.1,cue.end-.1)
    def test_old_rows_removed_and_bilingual_targets_blank(self):
        affected={c.cue_id for c in self.document.cues if c.start>=self.a-.02 and c.end<=self.b+.02}
        provenance=ChangeProvenance(kind=ChangeKind.MANUAL,operation='fixture')
        bilingual=replace(self.document,cues=tuple(replace(c,target=TranslationTrack('旧译文',provenance,language='zh')) if c.cue_id in affected else c for c in self.document.cues))
        result,_,_=replacement_document(bilingual,self.segments(),self.a,self.b,'gap_'+'d'*32)
        self.assertFalse(affected & {c.cue_id for c in result.cues})
        created=[c for c in result.cues if c.cue_id not in {v.cue_id for v in self.document.cues}]
        self.assertTrue(all(c.target is not None and c.target.target_text=='' and c.target.language=='zh' for c in created))
        self.assertTrue(all(t.source_token_ids for t in result.display_tokens if any(t.token_id in c.display_token_ids for c in created)))
    def test_invalid_empty_and_offset(self):
        with self.assertRaises(ValueError):recognition_segments({'units':[]},0,1)
        with self.assertRaises(ValueError):recognition_segments({'units':[{'text':'x','start':float('nan'),'end':1}]},0,1)
        self.assertAlmostEqual(self.segments()[0][0]['start'],self.a+.12)
        self.assertEqual(len(recognition_segments({'units':[{'text':'x'*40,'start':0,'end':1},{'text':'y'*40,'start':1,'end':2}]},10,12)),2)
    def test_gap_restore_preserves_other_regions(self):
        gap={'gaps':[{'start':0,'end':5000,'removed':True}],'provenance':{'manual_overrides':[]}}
        timeline={'markers':[],'active_plan_id':'p','plans':[{'id':'p','cuts':[{'id':'c','start':0,'end':5,'enabled':True}],'gap_remove':gap}]}
        value=restore_gap(timeline,1,3,'gap_'+'b'*32)
        self.assertEqual([(c['start'],c['end']) for c in value['plans'][0]['cuts']],[(0,1),(3,5)])
        self.assertTrue(any(g['removed'] is False and g['start']==1000 and g['end']==3000 for g in value['gap_remove']['gaps']))
        self.assertEqual(timeline['plans'][0]['cuts'][0]['end'],5)
    def test_one_revision_reload_and_undo_target(self):
        result,provenance,_=replacement_document(self.document,self.segments(),self.a,self.b,'gap_'+'c'*32)
        with tempfile.TemporaryDirectory() as temp:
            store=ProjectStore.create(Path(temp)/'project',project_id='gap-test')
            before=store.save(self.document,provenance=ChangeProvenance(kind=ChangeKind.MANUAL,operation='fixture'))
            after=store.save(result,provenance=provenance,expected_revision_id=before.revision_id)
            self.assertEqual(after.parent_revision_id,before.revision_id)
            self.assertEqual(after.revision_number,before.revision_number+1)
            self.assertEqual(ProjectStore.open(Path(temp)/'project').load_latest().document,result)
            self.assertEqual(store.load_revision(after.parent_revision_id).document,self.document)
    def test_failed_or_cancelled_worker_never_overwrites(self):
        class Service:
            def get_task(self,task_id):return {'state':'failed','error':{'message':'test provider failure'}}
        for initial in ('running','cancelled'):
            with tempfile.TemporaryDirectory() as temp:
                project=Path(temp)/'main';folder=project/'gap-transcriptions'/('gap_'+'e'*32);folder.mkdir(parents=True)
                store=ProjectStore.create(project/'project',project_id='gap-failure-test')
                before=store.save(self.document,provenance=ChangeProvenance(kind=ChangeKind.MANUAL,operation='fixture'))
                (folder/'request.json').write_text(json.dumps({'job_id':folder.name,'state':initial,'start':self.a,'end':self.b,'task_id':'fake'}),encoding='utf-8')
                _run(project,folder,Service(),str(folder))
                state=json.loads((folder/'request.json').read_text(encoding='utf-8'))['state']
                self.assertEqual(state,'failed' if initial=='running' else 'cancelled')
                self.assertEqual(store.load_latest().revision_id,before.revision_id)

if __name__=='__main__':unittest.main(verbosity=2)


def test_gap_uses_shared_task_projection_and_cancel(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from substar_core.editor import http_api as api
    from substar_core.editor import gap_transcription as gap
    project=tmp_path/'project'
    folder=project/'gap-transcriptions'/('gap_'+'f'*32)
    folder.mkdir(parents=True)
    record={'job_id':folder.name,'state':'preparing','start':0,'end':3,'message':'prepare','created_at':'2026-10-27T00:00:00+00:00'}
    gap._write(folder/'request.json',record)
    service=SimpleNamespace(list_tasks=lambda **kw:[])
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(task_service=service)))
    monkeypatch.setattr(api,'project_job_path',lambda _:project)
    task=api.get_project_editor_ai_task('project',request)
    assert task['kind']=='gap_transcription' and task['state']=='running'
    assert api.cancel_project_editor_ai_task('project',request)['state']=='cancelled'
    assert api.get_project_editor_ai_task('project',request)['state']=='cancelled'


def test_active_gap_blocks_writes_but_allows_cancel(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    from substar_core.editor import http_api as api
    from substar_core.editor import gap_transcription as gap
    folder=tmp_path/'gap-transcriptions'/('gap_'+'f'*32);folder.mkdir(parents=True)
    gap._write(folder/'request.json',{'job_id':folder.name,'state':'preparing','start':0,'end':3,'message':'prepare'})
    monkeypatch.setattr(api,'project_job_path',lambda _:tmp_path)
    request=SimpleNamespace(path_params={'project_id':'p'},method='POST',url=SimpleNamespace(path='/api/projects/p/ai-organize'),app=SimpleNamespace(state=SimpleNamespace(task_service=None)))
    with pytest.raises(HTTPException) as error: api._guard_gap_transcription_write(request)
    assert error.value.status_code==423
    request.method='DELETE';request.url.path='/api/projects/p/ai-task'
    api._guard_gap_transcription_write(request)


def test_no_words_error_has_safe_readable_public_message():
    from substar_core.qwen_cloud_asr import QwenCloudAsrError
    from substar_core.transcription.worker import _public_transcription_error
    error = _public_transcription_error(QwenCloudAsrError('private URL and credential', provider_code='ASR_RESPONSE_HAVE_NO_WORDS'))
    assert 'ASR_RESPONSE_HAVE_NO_WORDS' in error['public_message']
    assert '未识别出有效词语' in error['public_message']
    assert 'private' not in str(error)
    assert 'public_message' not in _public_transcription_error(QwenCloudAsrError('private secret'))
    assert 'public_message' not in _public_transcription_error(RuntimeError('ASR_RESPONSE_HAVE_NO_WORDS private'))
