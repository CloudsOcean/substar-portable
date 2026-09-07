import pytest
from substar_core.cue_script import (render_cue_request, finalize_calibration,
    render_segmentation_request, parse_segmentation, CueScriptError)

def test_calibration_numeric_equivalence():
    wire,ledger=render_cue_request([{'cue_id':'x','tokens':[{'token_id':'t','text':'hello'}]}],task='CALIBRATE',instructions='')
    assert '1|OWN|hello' in wire
    assert finalize_calibration('1|Hello.',ledger)==finalize_calibration('C001\tHello.',ledger)
    with pytest.raises(CueScriptError): finalize_calibration('1-2|Hello.',ledger)
    with pytest.raises(CueScriptError): finalize_calibration('2|Hello.',ledger)

def test_segmentation_numeric_equivalence():
    request={'rows':[{'index':i,'text':'word','owner':True,'start':i,'end':i+1} for i in range(3)],'active_output_profile':{'hard_limit':55}}
    wire,ledger=render_segmentation_request(request)
    binding={'input_fingerprint':'x','block_id':'b','ownership':{'alignment_start':0,'alignment_end':2}}
    assert '1|W####[-W####]' in wire
    assert parse_segmentation('1|W0001-W0002\n2|W0003',ledger,binding)==parse_segmentation('C001\tW0001-W0002\nC002\tW0003',ledger,binding)
    with pytest.raises(CueScriptError): parse_segmentation('1|W0001',ledger,binding)
