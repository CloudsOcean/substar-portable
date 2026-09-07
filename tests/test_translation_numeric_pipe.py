from substar_core.cue_script import render_translation_request, finalize_translation, output_contract

def test_numeric_protocol_modes_and_payload():
    groups=[{'group_id':'g','cues':[{'cue_id':str(i),'source_text':'source'} for i in range(1,5)]}]
    wire,ledger=render_translation_request(groups,mapping_mode='many_to_many')
    assert wire.splitlines()==[f'{i}|source' for i in range(1,5)]
    assert 'cue1' not in output_contract('TRANSLATE')
    def parse(raw,mode='many_to_many'):
        return finalize_translation(raw,groups,ledger,mapping_mode=mode)
    assert not parse('1-4|译文')['_cue_script_issues']
    assert parse('1-4|译文','one_to_one')['_cue_script_issues']
    assert not parse('\n'.join(f'{i}|译文' for i in range(1,5)),'one_to_one')['_cue_script_issues']
    for raw in ('1|甲\n1-4|乙','0-4|甲','4-1|甲','1|','1|甲','2-4|甲\n1|乙'):
        assert parse(raw)['_cue_script_issues']
    assert parse('1-4|甲|乙')['_wire_units'][0]['target_text']=='甲|乙'
    assert not parse('cue1-cue4<TAB>旧格式')['_cue_script_issues']
