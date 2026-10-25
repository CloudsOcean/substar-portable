from substar_core.ass_subtitles import AssStyle, AssOptions, render_ass
from substar_core.ass_profiles import Profile, default_profile, apply_profile, configuration, render_configuration
from substar_core.domain import ChangeProvenance
from substar_core.storage import ProjectStore
from test_editor_actions_ass_updates import document
import pytest


def test_maw_extended_style_reaches_ass_fields_and_animation_events():
    style=AssStyle(size=320,italic=True,underline=True,strike_out=True,scale_x=120,scale_y=80,
        spacing=4,angle=12,border_style=3,outline=22,shadow=18,outline_color='#123456',
        outline_opacity=50,back_color='#ABCDEF',back_opacity=0,margin_l=23,margin_r=91,
        override_tags=r'\fad(120,240)\move(10,20,30,40,0,1000)\t(0,500,1,\fs64)')
    output=render_ass(document(),AssOptions(style=style))
    fields=next(line for line in output.splitlines() if line.startswith('Style: Source,')).split(',')
    assert fields[3]=='&H00FFFFFF'
    assert fields[5]=='&H80563412'
    assert fields[6]=='&HFFEFCDAB'
    assert fields[8:11]==['-1','-1','-1']
    assert fields[11:18]==['120.0','80.0','4.0','12.0','3','22.0','18.0']
    assert fields[19:21]==['23','91']
    assert '{'+style.override_tags+'}' in output


def test_maw_library_and_extended_style_survive_revision_roundtrip(tmp_path):
    doc=document();profile=default_profile(doc)
    profile.maw_library={'schema':'moy.asr.ass_styles.v1','styles':[{'id':'custom','name':'测试样式'}]}
    profile.styles[profile.layers[0].style_id].override_tags=r'\fad(100,200)'
    edited=apply_profile(doc,Profile.model_validate(profile.model_dump()))
    store=ProjectStore.create(tmp_path/'project',project_id='test')
    revision=store.save(edited,provenance=ChangeProvenance(kind='manual',operation='ass_test'))
    loaded=store.load_revision(revision.revision_id).document
    saved=configuration(loaded);effective=saved.profiles[saved.default_profile]
    assert effective.maw_library==profile.maw_library
    assert r'\fad(100,200)' in render_configuration(loaded)


@pytest.mark.parametrize('tags',['\\fad(1,2)\nDialogue: injected','{\\fs72}','\\fs72\r'])
def test_maw_animation_cannot_break_ass_event_structure(tags):
    with pytest.raises(ValueError):AssStyle(override_tags=tags)
