from dataclasses import replace

import pytest
from substar_core.ass_profiles import configuration, default_profile, apply_profile, render_configuration, Profile
from substar_core.domain import DisplayCue, ChangeProvenance
from substar_core.storage import ProjectStore
from test_editor_actions_ass_updates import document


def two_cues():
    doc = document()
    a = replace(doc.cues[0], display_token_ids=(doc.display_tokens[0].token_id,), end=2)
    b = replace(doc.cues[0], cue_id="second", index=1, display_token_ids=(doc.display_tokens[1].token_id,), start=2)
    return replace(doc,cues=(a,b))


def test_default_layers_and_reference_resolution():
    doc=two_cues()
    p=default_profile(doc)
    assert [x.content for x in p.layers] == ["source","target"]
    assert p.styles['upper'].color=='#FFFFFF'
    assert p.styles['lower'].color=='#FFE083'
    assert p.styles['upper'].bold and p.styles['lower'].background
    a=render_configuration(doc,1920,1080)
    b=render_configuration(doc,1280,720)
    assert a==b
    assert 'Style: P0L0Source' in a and 'Box' in a


def test_assignment_is_deduplicated_and_survives_revision_replay(tmp_path):
    doc=two_cues(); p=default_profile(doc)
    p.styles['upper'].color='#00FF00'
    edited=apply_profile(doc,p,[c.cue_id for c in doc.cues],layer_id='upper')
    c=configuration(edited)
    assert len(set(c.cue_profiles.values()))==1
    assert len(c.profiles)==2
    assert edited.source_tokens==doc.source_tokens and edited.cues==doc.cues
    store=ProjectStore.create(tmp_path/'p',project_id='test')
    original=store.save(doc,provenance=ChangeProvenance(kind='manual',operation='initial'))
    saved=store.save(edited,provenance=edited.changes[-1],expected_revision_id=original.revision_id)
    assert configuration(store.load_revision(saved.revision_id).document)==c
    assert configuration(store.load_revision(original.revision_id).document).cue_profiles=={}
    reset=apply_profile(edited,None,[doc.cues[0].cue_id],reset=True)
    assert doc.cues[0].cue_id not in configuration(reset).cue_profiles
    assert doc.cues[1].cue_id in configuration(reset).cue_profiles


def test_edit_one_layer_preserves_other_cue_overrides():
    doc=two_cues(); first,second=[c.cue_id for c in doc.cues]
    p=default_profile(doc);p.styles['lower'].size=90
    doc=apply_profile(doc,p,[first],layer_id='lower')
    p=default_profile(doc);p.styles['upper'].size=100
    doc=apply_profile(doc,p,[first,second],layer_id='upper')
    config=configuration(doc)
    sizes=[]
    for id in [first,second]:
        profile=config.profiles[config.cue_profiles[id]]
        sizes.append([profile.styles[layer.style_id].size for layer in profile.layers])
    assert sizes==[[100,90],[100,64]]
    assert len([x for x in doc.changes if x.operation=='ass_configuration'])==1


def test_word_highlight_is_persistent_and_partitioned_by_cue():
    doc=two_cues();p=default_profile(doc);p.layers[0].word_highlight=True
    edited=apply_profile(doc,p,[doc.cues[0].cue_id])
    result=render_configuration(edited)
    assert r'\c&H0000FFFF&' in result
    assert result.count('hello')==2  # background and foreground, one active word interval
    assert not configuration(doc).cue_profiles
    with pytest.raises(ValueError):
        p.layers[1].word_highlight=True;Profile.model_validate(p.model_dump())


def test_extra_layers_and_dangling_references():
    doc=two_cues();p=default_profile(doc)
    p.layers.append(p.layers[0].model_copy(update={'id':'third','name':'第三层'}))
    result=render_configuration(apply_profile(doc,p))
    assert 'P0L2Source' in result
    with pytest.raises(ValueError): apply_profile(doc,p,['missing'])
    p.layers[0].style_id='missing'
    with pytest.raises(ValueError): Profile.model_validate(p.model_dump())


def test_project_changes_flow_through_unassigned_layers_and_restore_style_only():
    from substar_core.ass_profiles import effective_profile
    from substar_core.editor.application.selective_restore import restore_fields
    doc=two_cues();id=doc.cues[0].cue_id
    lower=default_profile(doc);lower.styles['lower'].size=90
    assigned=apply_profile(doc,lower,[id],layer_id='lower')
    upper=default_profile(doc);upper.styles['upper'].size=120
    changed=apply_profile(assigned,upper,layer_id='upper')
    p=effective_profile(configuration(changed),id)
    assert [p.styles[x.style_id].size for x in p.layers]==[120,90]
    restored=restore_fields(changed,doc,[id],'style')
    p=effective_profile(configuration(restored),id)
    assert [p.styles[x.style_id].size for x in p.layers]==[72,64]
    assert restored.cues==changed.cues and restored.source_tokens==changed.source_tokens


def test_http_apply_export_stale_guard_and_library_isolation(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from substar_core import ass_api
    from substar_core.editor import http_api
    doc=two_cues()
    store=ProjectStore.create(tmp_path/'project',project_id='test')
    original=store.save(doc,provenance=ChangeProvenance(kind='manual',operation='initial'))
    monkeypatch.setattr(http_api,'open_project_store',lambda _:store)
    monkeypatch.setattr(ass_api,'APP_DATA_DIR',tmp_path/'data')
    app=FastAPI();app.include_router(ass_api.router);client=TestClient(app)
    p=default_profile(doc);p.layers[0].word_highlight=True;p.styles['lower'].size=110
    payload={'expected_revision_id':original.revision_id,'profile':p.model_dump(),
        'cue_ids':[doc.cues[0].cue_id], 'layer_ids':['upper','lower']}
    saved=client.post('/api/projects/test/ass/configuration',json=payload)
    assert saved.status_code==200,saved.text
    assert client.post('/api/projects/test/ass/configuration',json=payload).status_code==409
    latest=store.load_latest()
    body={'expected_revision_id':latest.revision_id,'use_configuration':True}
    exported=client.post('/api/projects/test/ass/export',json=body)
    assert exported.status_code==200 and r'\c&H0000FFFF&' in exported.text
    before=exported.text
    assert client.put('/api/ass/profiles/MyStyle',json=p.model_dump()).status_code==200
    assert client.delete('/api/ass/profiles/MyStyle').status_code==200
    assert client.post('/api/projects/test/ass/export',json=body).text==before
    body['expected_revision_id']=original.revision_id
    assert client.post('/api/projects/test/ass/export',json=body).status_code==409


def test_highlight_survives_punctuation_projection():
    from substar_core.domain import PresentationSettings
    doc=document()
    doc=replace(doc,display_tokens=(replace(doc.display_tokens[0],text='hello,'),doc.display_tokens[1]),
        presentation=PresentationSettings(upper_remove=','))
    p=default_profile(doc);p.layers[0].word_highlight=True
    result=render_configuration(apply_profile(doc,p))
    assert r'\c&H0000FFFF&' in result
    assert 'hello,' not in result


def test_long_subtitles_use_margin_aware_wrapping():
    doc = document()
    text = "The country of Israel joins me in this commitment. " * 4
    doc = replace(doc, display_tokens=(replace(doc.display_tokens[0], text=text.strip()), *doc.display_tokens[1:]))
    ass = render_configuration(doc, 1280, 720)
    assert "WrapStyle: 0" in ass
    assert text.strip() in ass.replace(r"\N", " ")
    assert "WrapStyle: 2" not in ass


def test_unicode_wrapping_preserves_text_highlight_and_explicit_lines():
    from substar_core.ass_subtitles import AssStyle, wrap_ass_text
    style = AssStyle(size=96, margin_x=60)
    text = "因此这丝毫不会有助于任何把人民与政府及革命割裂的计划。"
    result = wrap_ass_text(text, style, 1920)
    assert r"\N" in result
    assert result.replace(r"\N", "") == text
    assert all(len(line) <= 18 for line in result.split(r"\N"))
    tagged = "{\\c&H0000FFFF&}" + text + "{\\c&H00FFFFFF&}"
    assert wrap_ass_text(tagged, style, 1920).replace(r"\N", "") == tagged
    assert wrap_ass_text(r"你好\N世界", style, 1920) == r"你好\N世界"
    assert wrap_ass_text("W" * 80, style, 1920).count(r"\N") >= 4
