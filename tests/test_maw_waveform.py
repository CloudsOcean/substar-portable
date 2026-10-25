import base64
import wave
from array import array
from substar_core.editor.maw_waveform_api import encode_waveform

def write_audio(path, values, channels=1):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(array('h',values).tobytes())

def test_fixed_resolution_keeps_silence_and_transients_without_normalizing(tmp_path):
    path=tmp_path/'source.wav'
    write_audio(path,[0]*160+[-32768]+[32767]+[0]*158+[0]*80)
    payload=encode_waveform(path)
    peaks=array('b',base64.b64decode(payload['data']))
    assert list(peaks)==[0,0,-127,127,0,0]
    assert payload['duration_ms']==25
    assert payload['sample_rate']/payload['division']==100
    assert payload['peak_count']==3

def test_stereo_uses_extrema_from_both_channels(tmp_path):
    path=tmp_path/'stereo.wav'
    write_audio(path,[-32768,16384]*160,2)
    peaks=array('b',base64.b64decode(encode_waveform(path)['data']))
    assert list(peaks)==[-127,64]

def test_word_timing_overlay_moves_with_cue_without_rewriting_asr():
    import json
    from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind
    from substar_core.document_operations import apply_document_operation
    source=SourceToken.create(index=0,text='hello',start=.2,end=1.2)
    token=DisplayToken.create(position=0,text='hello',source_token_ids=(source.token_id,),provenance=ChangeProvenance(ChangeKind.SOURCE,'test'))
    doc=EditorDocument('word-timing',(source,),(token,),(DisplayCue('cue',0,(token.token_id,),.2,1.2),))
    changed=apply_document_operation(doc,{'operation_id':'move','type':'set_cue_times','payload':{'cues':[{'cue_id':'cue','start':.4,'end':1.4,'expected_start':.2,'expected_end':1.2}],'word_timings':{token.token_id:{'start':400,'end':1400}}}})
    assert changed.source_tokens==doc.source_tokens
    assert changed.cues[0].start==.4
    assert json.loads(changed.properties.timeline_json)['word_timings'][token.token_id]=={'start':400,'end':1400}
    # A manually added cue must not reclassify an ASR project as subtitle-only.
    from dataclasses import replace
    mixed=replace(doc,cues=(*doc.cues,DisplayCue('manual',1,(),2,3,source_text='人工字幕')))
    inserted=apply_document_operation(mixed,{'operation_id':'add-manual','type':'insert_cue','payload':{'start':4,'end':5,'text':'第二条人工字幕'}})
    assert len(inserted.cues)==3
