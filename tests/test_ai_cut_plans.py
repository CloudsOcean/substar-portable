import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from substar_core.domain import EditorDocument, DisplayCue, DocumentProperties
from substar_core.cut_timeline import ai_review_markers, ai_plan_from_ids
from substar_core.editor import cut_api

def doc():
    return EditorDocument('test',(),(),(DisplayCue('a',0,(),0,1,source_text='开头'),DisplayCue('b',1,(),2,3,source_text='结尾')))

def test_markers_are_bound_to_real_cues_without_arbitrary_model_times():
    source=doc();before=source.content_hash()
    items=ai_review_markers(source,{'markers':[{'cue_id':'a','kind':'point','start':999},{'cue_id':'b','kind':'region','note':'待判断'}]})
    assert items[0]['start']==0 and 'end' not in items[0]
    assert (items[1]['start'],items[1]['end'])==(2,3)
    assert source.content_hash()==before
    with pytest.raises(ValueError):ai_review_markers(source,{'markers':[{'cue_id':'missing'}]})
    with pytest.raises(ValueError):ai_review_markers(source,{'markers':[{'cue_id':'a','kind':'move'}]})

def test_conflicting_keep_and_cut_ids_are_rejected():
    with pytest.raises(ValueError):ai_plan_from_ids(doc(),{'keep_cue_ids':['a'],'cuts':[{'cue_ids':['a']}]},'test')

def test_inference_rechecks_revision_and_returns_409_when_project_changed(monkeypatch):
    import sys
    from substar_core import model_gateway
    source=doc();calls=[]
    monkeypatch.setattr(cut_api,"cut_media_duration",lambda _:4)
    def current(*args):
        calls.append(1)
        if len(calls)>1:raise HTTPException(409,'工程已更新')
        return SimpleNamespace(document=source)
    monkeypatch.setattr(cut_api,'revision',current)
    monkeypatch.setitem(sys.modules,'substar_core.editor.http_api',SimpleNamespace(_project_llm_settings=lambda *a,**k:{}))
    monkeypatch.setattr(model_gateway,'call_text_model',lambda **k:(json.dumps({'operations':[]}),{}))
    with pytest.raises(HTTPException) as err:cut_api.cut_ai('p',cut_api.AiCutRequest(revision_id='r',goal='保留'))
    assert err.value.status_code==409


def test_current_plan_context_and_review_markers_are_returned_without_saving(monkeypatch):
    import sys
    from dataclasses import replace
    from substar_core import model_gateway
    timeline={"markers":[],"active_plan_id":"base","plans":[{"id":"base","name":"旧方案","keep":None,"cuts":[{"id":"c","start":1,"end":2}],"gap_remove":{"large_provenance":"not model input"}}]}
    source=replace(doc(),properties=DocumentProperties(timeline_json=json.dumps(timeline)))
    before=source.content_hash()
    monkeypatch.setattr(cut_api,"cut_media_duration",lambda _:4)
    monkeypatch.setattr(cut_api,'revision',lambda *a:SimpleNamespace(document=source))
    monkeypatch.setitem(sys.modules,'substar_core.editor.http_api',SimpleNamespace(_project_llm_settings=lambda *a,**k:{}))
    seen={}
    def reply(**kwargs):
        seen.update(kwargs)
        return json.dumps({'operations':[{'action':'restore','start':1,'end':2}],'summary':'恢复中间停顿','markers':[{'cue_id':'b','kind':'region','note':'检查语义'}]}),{}
    monkeypatch.setattr(model_gateway,'call_text_model',reply)
    result=cut_api.cut_ai('p',cut_api.AiCutRequest(revision_id='r',goal='恢复停顿',base_plan_id='base'))
    assert seen['user_payload']['current_plan']['cuts']==[{'start':1,'end':2}]
    assert 'gap_remove' not in seen['user_payload']['current_plan']
    assert result['plan']['summary']=='恢复中间停顿'
    assert (result['markers'][0]['start'],result['markers'][0]['end'])==(2,3)
    assert source.content_hash()==before


def test_cutting_overlapping_speaker_does_not_remove_retained_words():
    overlapping=EditorDocument('overlap',(),(),(DisplayCue('a',0,(),0,2,source_text='保留'),DisplayCue('b',1,(),1,3,source_text='重复')))
    with pytest.raises(ValueError):ai_plan_from_ids(overlapping,{'keep_cue_ids':['a'],'cuts':[{'cue_ids':['b']}]},'test')


def test_head_only_edit_keeps_tail_and_silences():
    from substar_core.cut_timeline import ai_plan_from_ranges, kept_intervals
    plan=ai_plan_from_ranges({'operations':[{'action':'remove','start':0,'end':8.53}]},'删cue1之前',177.656)
    assert plan['keep'] is None
    assert kept_intervals(plan,177.656)==[(8.53,177.656)]


def test_range_edits_preserve_base_and_restore_only_requested_part():
    from substar_core.cut_timeline import ai_plan_from_ranges, kept_intervals
    base={'keep':None,'cuts':[{'start':20,'end':30},{'start':50,'end':60}]}
    before=json.dumps(base)
    result=ai_plan_from_ranges({'operations':[{'action':'remove','start':0,'end':8.53},{'action':'restore','start':22,'end':25}]},'调整',100,base)
    assert kept_intervals(result,100)==[(8.53,20),(22,25),(30,50),(60,100)]
    assert json.dumps(base)==before


@pytest.mark.parametrize('op',[
    {'action':'remove','start':-1,'end':1},
    {'action':'remove','start':1,'end':101},
    {'action':'remove','start':1,'end':1},
    {'action':'remove','start':True,'end':2},
    {'action':'remove','start':0,'end':float('nan')},
    {'action':'move','start':0,'end':1},
    {'action':'remove','start':0,'end':100},
])
def test_invalid_range_operations_are_rejected(op):
    from substar_core.cut_timeline import ai_plan_from_ranges
    with pytest.raises(ValueError):ai_plan_from_ranges({'operations':[op]},'测试',100)


def test_old_keep_id_response_is_rejected_and_empty_operations_keep_full_media():
    from substar_core.cut_timeline import ai_plan_from_ranges, kept_intervals
    with pytest.raises(ValueError):ai_plan_from_ranges({'keep_cue_ids':['a']},'测试',100)
    assert kept_intervals(ai_plan_from_ranges({'operations':[]},'无修改',100),100)==[(0,100)]
