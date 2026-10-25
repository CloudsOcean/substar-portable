import json
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from substar_core.cut_timeline import (validate_timeline, kept_intervals, projected_subtitles,
                                      export_xml, frame_intervals, ai_plan_from_ids)
from substar_core.document_operations import apply_document_operation, DocumentOperationError
from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind
from substar_core.storage import ProjectStore


def fixture():
    source = SourceToken.create(index=0,text="hello",start=0,end=10)
    token = DisplayToken.create(position=0,text="hello",source_token_ids=(source.token_id,),provenance=ChangeProvenance(ChangeKind.SOURCE,"fixture"))
    return EditorDocument("cut",(source,),(token,),(DisplayCue("cue",0,(token.token_id,),0,10,speaker="speaker_0"),))


def state():
    return {"markers":[{"id":"m","start":1,"end":None,"name":"note"}],"active_plan_id":"p",
            "plans":[{"id":"p","name":"cut","keep":None,"cuts":[{"id":"c","start":2,"end":4,"enabled":True}]}]}


def test_cut_cover_is_reversible_without_changing_source_or_translation(tmp_path):
    doc = fixture()
    doc = apply_document_operation(doc,{"operation_id":"t","type":"set_target","payload":{"cue_id":"cue","target_text":"你好","expected_target_text":""}})
    changed = apply_document_operation(doc,{"operation_id":"cut","type":"set_timeline","payload":{"timeline":state(),"expected_timeline":None}})
    assert changed.cues == doc.cues and changed.source_tokens == doc.source_tokens and changed.display_tokens == doc.display_tokens
    projected = projected_subtitles(changed,kept_intervals(state()["plans"][0],10))
    assert [(c.start,c.end) for c in projected.cues] == [(0,2),(2,8)]
    assert all(c.source_text == "hello" and c.target.target_text == "你好" for c in projected.cues)
    store = ProjectStore.create(tmp_path/"p",project_id="cut")
    provenance = ChangeProvenance(ChangeKind.MANUAL,"cut")
    original = store.save(doc,provenance=provenance)
    current = store.save(changed,provenance=provenance,expected_revision_id=original.revision_id)
    reopened = ProjectStore.open(tmp_path/"p")
    assert json.loads(reopened.load_latest().document.properties.timeline_json) == state()
    assert reopened.load_revision(original.revision_id).document == doc
    assert reopened.load_revision(current.revision_id).document == changed
    with pytest.raises(DocumentOperationError):
        apply_document_operation(changed,{"operation_id":"stale","type":"set_timeline","payload":{"timeline":state(),"expected_timeline":None}})


def test_overlapping_cuts_union_and_disabled_cuts():
    plan={"keep":[{"start":0,"end":6},{"start":5,"end":10}],"cuts":[
        {"start":2,"end":4},{"start":3,"end":7},{"start":8,"end":10,"enabled":False}]}
    assert kept_intervals(plan,10)==[(0,2),(7,10)]
    assert kept_intervals({"keep":[],"cuts":[]},10)==[]
    assert kept_intervals({"cuts":[{"start":0,"end":20}]},10)==[]


@pytest.mark.parametrize("value", [float("nan"),float("inf"),-1,True])
def test_invalid_timing_rejected(value):
    data=state();data["markers"][0]["start"]=value
    with pytest.raises(ValueError):validate_timeline(data)


def test_separate_speakers_may_overlap():
    doc=fixture();cue=doc.cues[0]
    source=SourceToken.create(index=1,text="world",start=1,end=3)
    token=DisplayToken.create(position=1,text="world",source_token_ids=(source.token_id,),provenance=ChangeProvenance(ChangeKind.SOURCE,"fixture"))
    other=DisplayCue("other",1,(token.token_id,),1,3,speaker="speaker_1")
    overlap=replace(doc,source_tokens=(*doc.source_tokens,source),display_tokens=(*doc.display_tokens,token),cues=(cue,other))
    assert overlap.cues[1].start<overlap.cues[0].end


def test_new_track_is_speaker_registry_and_accepts_overlapping_manual_subtitle(tmp_path):
    doc=fixture()
    named=apply_document_operation(doc,{"operation_id":"track","type":"set_speaker_names","payload":{"speaker_names":{"speaker_0":"主播","speaker_1":"嘉宾"}}})
    operation={"operation_id":"insert","type":"insert_cue","payload":{"start":1,"end":2,"text":"新轨字幕","speaker":"speaker_1"}}
    changed=apply_document_operation(named,operation)
    created=next(c for c in changed.cues if c.cue_id!="cue")
    assert created.speaker=="speaker_1"
    assert dict(changed.properties.speaker_names)[created.speaker]=="嘉宾"
    assert changed.source_tokens==doc.source_tokens
    operation["payload"]["speaker"]="speaker_0"
    with pytest.raises(DocumentOperationError):apply_document_operation(named,operation)
    store=ProjectStore.create(tmp_path/"track",project_id="track")
    store.save(changed,provenance=ChangeProvenance(ChangeKind.MANUAL,"track"))
    assert dict(ProjectStore.open(tmp_path/"track").load_latest().document.properties.speaker_names)["speaker_1"]=="嘉宾"


def test_xml_uses_matching_av_boundaries_without_per_cut_drift():
    ranges=[(i*.17,i*.17+.083) for i in range(100)]
    framed=frame_intervals(ranges,"30000/1001")
    root=ET.fromstring(export_xml(Path("C:/example.mp4"),framed,"30000/1001"))
    video=root.findall(".//video/track/clipitem");audio=root.findall(".//audio/track/clipitem")
    pairs=lambda clips:[(int(c.findtext("start")),int(c.findtext("end"))) for c in clips]
    assert pairs(video)==pairs(audio)
    assert all(a[1]==b[0] for a,b in zip(pairs(video),pairs(video)[1:]))
    assert abs(int(root.findtext("sequence/duration"))-round(8.3*30000/1001))<=1


def test_ai_must_reference_real_cues_and_never_changes_document():
    doc=fixture();before=doc.content_hash()
    p=ai_plan_from_ids(doc,{"name":"clip","keep_cue_ids":["cue"],"cuts":[]},"goal")
    assert p["keep"]==[{"start":0,"end":10}]
    assert doc.content_hash()==before
    with pytest.raises(ValueError):ai_plan_from_ids(doc,{"keep_cue_ids":["invented"]},"goal")
