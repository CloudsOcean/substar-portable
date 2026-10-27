from unittest.mock import patch
from substar_core.editor.domain import cue_timing
from substar_core.document_operations import apply_document_operation
from test_editor_text_retiming import document

def test_timing_validation_builds_token_index_only_once():
    doc = document()
    from dataclasses import replace
    cues = [replace(doc.cues[0], cue_id=f"cue_{i}", index=i, start=i*4, end=i*4+3) for i in range(250)]
    with patch.object(cue_timing, '_token_map', wraps=cue_timing._token_map) as indexed:
        cue_timing._validate_non_manual_timeline(doc, cues)
        assert indexed.call_count == 1

def test_word_patch_uses_cue_operation_without_replacing_unrelated_timeline_values():
    doc = document()
    cue = doc.cues[0]
    token = cue.display_token_ids[1]
    updated = apply_document_operation(doc, {'operation_id':'word-patch-test','type':'set_cue_times','payload':{
        'cues':[{'cue_id':cue.cue_id,'start':cue.start,'end':cue.end}],
        'word_timings':{token:{'start':1100,'end':1900}}}})
    import json
    assert json.loads(updated.properties.timeline_json)['word_timings'] == {token:{'start':1100,'end':1900}}
    assert updated.source_tokens == doc.source_tokens
    assert updated.display_tokens == doc.display_tokens
