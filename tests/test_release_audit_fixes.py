"""Regression checks for the release audit. All media and projects are disposable."""
import json
import wave
import zipfile
from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from substar_core import qwen_cloud_asr as qwen, updater
from substar_core.ass_profiles import apply_profile, configuration, default_profile
from substar_core.document_operations import apply_document_operation
from substar_core.domain import ChangeKind, ChangeProvenance, DisplayCue, DisplayToken, EditorDocument, EntityState, SourceToken
from substar_core.editor.application.selective_restore import restore_fields
from substar_core.translation_units import all_units, install_units, reconcile_units, target_events
from substar_core.transcription import cloud_pipeline as audio

PROVENANCE = ChangeProvenance(ChangeKind.MANUAL, "fixture")


def document():
    sources = tuple(SourceToken.create(index=i, text="word" + str(i), start=i + 4, end=i + 5) for i in range(2))
    tokens = tuple(DisplayToken.create(position=i, text=s.text, source_token_ids=[s.token_id], provenance=PROVENANCE)
                   for i, s in enumerate(sources))
    cue = DisplayCue("cue_a", 0, tuple(t.token_id for t in tokens), 4, 6, speaker="s1")
    return EditorDocument("fixture", sources, tokens, (cue,))


def translated(doc):
    return install_units(doc, [dict(unit_id="unit", source_cue_ids=["cue_a"], start=4, end=6,
                                    text="原译文", language="zh-CN")], PROVENANCE)


def op(doc, kind, **payload):
    return apply_document_operation(doc, {"type": kind, "operation_id": kind, "payload": payload})


def test_independent_translation_move_hide_restore_and_selective_fields():
    initial = translated(document())
    moved = reconcile_units(initial, replace(initial, cues=(replace(initial.cues[0], start=1, end=3),)))
    assert [(x["start"], x["end"]) for x in target_events(moved)] == [(1, 3)]
    hidden = op(moved, "delete", cue_ids=["cue_a"])
    assert not target_events(hidden) and all_units(hidden)[0]["text"] == "原译文"
    assert target_events(op(hidden, "restore", cue_ids=["cue_a"]))[0]["text"] == "原译文"
    edited = install_units(moved, [dict(all_units(moved)[0], text="新译文")], PROVENANCE)
    for scope, expected in [("translation", (1, 3, "原译文")), ("timing", (4, 6, "新译文"))]:
        event, = target_events(restore_fields(edited, initial, ["cue_a"], scope))
        assert (event["start"], event["end"], event["text"]) == expected


def test_split_inherits_style_and_keeps_shared_translation():
    doc = translated(document())
    doc = apply_profile(doc, default_profile(doc), ["cue_a"])
    split = op(doc, "split_cue", cue_id="cue_a", after_token_id=doc.display_tokens[0].token_id, right_cue_id="cue_b")
    config = configuration(split)
    assert config.cue_profiles["cue_a"] == config.cue_profiles["cue_b"]
    assert all_units(split)[0]["source_cue_ids"] == ["cue_a", "cue_b"]


def test_restore_preserves_other_speaker():
    doc = document()
    a = replace(doc.cues[0], display_token_ids=(doc.display_tokens[0].token_id,), state=EntityState.DELETED)
    b = replace(a, cue_id="cue_b", index=1, display_token_ids=(doc.display_tokens[1].token_id,),
                state=EntityState.ACTIVE, speaker="s2")
    restored = op(replace(doc, cues=(a, b)), "restore", cue_ids=["cue_a"])
    assert all(c.state is EntityState.ACTIVE for c in restored.cues)


def test_purging_one_owner_preserves_shared_words():
    doc = document()
    a = replace(doc.cues[0], mapping={"source_repeat_group": "g"})
    b = replace(a, cue_id="cue_b", index=1, start=8, end=10)
    result = op(replace(doc, cues=(a,b)), "purge_cue", cue_ids=["cue_a"])
    result.validate()
    assert (result.display_tokens,result.source_tokens) == (doc.display_tokens,doc.source_tokens)


def test_partial_decode_is_never_reused(tmp_path, monkeypatch):
    final = tmp_path / "音频.wav"
    def interrupted(command, **kwargs):
        with open(command[-1], "wb") as f:
            f.write(bytes(4096))
        raise RuntimeError("interrupted")
    monkeypatch.setattr(audio, "_run", interrupted)
    with pytest.raises(RuntimeError):
        audio._extract_audio(tmp_path/"媒体.mp4", final, "off", media_duration_seconds=1)
    assert not list(tmp_path.iterdir())
    def complete(command, **kwargs):
        with wave.open(command[-1], "wb") as out:
            out.setparams((1, 2, 16000, 0, "NONE", ""))
            out.writeframes(bytes(32000))
    runner = Mock(side_effect=complete)
    monkeypatch.setattr(audio, "_run", runner)
    for _ in range(2):
        audio._extract_audio(tmp_path/"媒体.mp4", final, "off", media_duration_seconds=1)
    assert runner.call_count == 1 and audio.has_complete_audio(final)


def test_unknown_submission_is_not_sent_again(tmp_path, monkeypatch):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"input")
    monkeypatch.setattr(qwen, "_encode_cloud_audio", lambda *a: None)
    monkeypatch.setattr(qwen, "upload_temporary_file", lambda *a, **kw: "oss://fixture")
    transport = Mock(side_effect=requests.ReadTimeout("lost reply"))
    monkeypatch.setattr(qwen.requests, "request", transport)
    settings = {"api_key":"fixture", "_checkpoint_dir":str(tmp_path), "_transcription_input_fingerprint":"a"*64}
    for _ in range(2):
        with pytest.raises(qwen.QwenCloudAsrError):
            qwen.run_qwen_cloud_asr(wav, settings, lambda *a: None)
    assert transport.call_count == 1
    assert json.loads((tmp_path/"qwen_cloud_state.json").read_text())["submission_pending"]


def test_policy_refresh_changes_uploaded_key(tmp_path, monkeypatch):
    path = tmp_path/"a.mp3"
    path.write_bytes(b"audio")
    policy = dict(upload_host="fixture", oss_access_key_id="id", signature="sig", policy="p",
                  x_oss_object_acl="private", x_oss_forbid_overwrite="true")
    monkeypatch.setattr(qwen, "get_upload_policy", Mock(side_effect=[dict(policy,upload_dir=x) for x in ("one","two")]))
    def response(code):
        r = requests.Response()
        r.status_code, r._content = code, b"{}"
        return r
    transport = Mock(side_effect=[response(403),response(200)])
    monkeypatch.setattr(qwen.requests, "post", transport)
    monkeypatch.setattr(qwen.time, "sleep", lambda _: None)
    assert qwen.upload_temporary_file(path,api_key="k",model="m",base_url="fixture",timeout=2,attempts=2) == "oss://two/a.mp3"
    assert [c.kwargs["files"]["key"][1] for c in transport.call_args_list] == ["one/a.mp3","two/a.mp3"]


def test_estimated_word_boundaries_keep_parent_envelope():
    units, _, _ = qwen._natural_qwen_units([dict(text="one,two",begin_time=200,end_time=900)],{})
    from substar_core.segmentation.input_contract import build_segmentation_material
    material = build_segmentation_material("", {"units": units})
    assert len(units) == 2
    evidence = [u["timing"] for u in material["units"]]
    assert all(u["kind"] == "subdivided" for u in evidence)
    assert evidence[0]["native_token_ids"] == evidence[1]["native_token_ids"]
    assert all(u["timing"]["kind"] == "subdivided" and
               (u["timing"]["native_start"], u["timing"]["native_end"]) == (.2,.9) for u in units)


@pytest.mark.parametrize("launcher", ["Substar.exe", "启动_Substar.cmd"])
def test_full_update_accepts_both_launch_layouts(tmp_path, launcher):
    archive = tmp_path/"包.zip"
    with zipfile.ZipFile(archive,"w") as z:
        for name in ("AGENTS.md","SKILL.md",launcher,"app.py","launcher.py","runtime/python/python.exe"):
            z.writestr("Substar/"+name, b"fixture")
        z.writestr("Substar/portable_manifest.json",json.dumps({"version":"2.4.1","package_layout":"transparent-source-runtime"}))
    assert (updater.validate_archive(archive,tmp_path/"out","2.4.1")/launcher).is_file()


def test_failed_downloads_clean_only_their_staging(tmp_path, monkeypatch):
    from threading import Thread
    class Inline(Thread):
        def start(self):
            self.run()
    monkeypatch.setattr(updater.threading,"Thread",Inline)
    monkeypatch.setattr(updater,"_state",{"status":"idle"})
    monkeypatch.setattr(updater,"UPDATE_ROOT",tmp_path)
    monkeypatch.setattr(updater,"check_update",lambda: {"available":True,"asset":{"size":100},"version":"2.4.1"})
    def fail(asset,write,progress):
        write(b"partial")
        raise ValueError("interrupted")
    monkeypatch.setattr(updater,"stream_asset",fail)
    other = tmp_path/"existing"
    other.mkdir()
    for _ in range(2):
        assert updater.download_update()["status"] == "failed"
        assert list(tmp_path.iterdir()) == [other]


def test_active_output_blocks_install(monkeypatch):
    from fastapi import HTTPException
    from substar_core.editor import output_api
    monkeypatch.setattr(output_api,"_JOBS",{("fixture","export"):{"state":"running"}})
    with pytest.raises(HTTPException) as error, output_api.reserve_update():
        pytest.fail("should not start installation")
    assert error.value.status_code == 409


def test_merge_across_hidden_word_then_split_again():
    from substar_core.editor.application.selective_restore import split_token
    sources = tuple(SourceToken.create(index=i, text=t, start=i, end=i+1) for i,t in enumerate(("first","hidden","last")))
    tokens = tuple(DisplayToken.create(position=i, text=s.text, source_token_ids=[s.token_id], provenance=PROVENANCE)
                   for i,s in enumerate(sources))
    doc = EditorDocument("fixture", sources, tokens, (DisplayCue("cue_a",0,tuple(t.token_id for t in tokens),0,3),))
    hidden = op(doc,"delete",token_ids=[tokens[1].token_id])
    merged = op(hidden,"merge",cue_id="cue_a",token_ids=[tokens[0].token_id,tokens[2].token_id],text="firstlast")
    merged_id = next(t.token_id for t in merged.display_tokens if t.text == "firstlast")
    result = split_token(merged,merged_id,5)
    result.validate()
    assert [s.start for s in result.source_tokens] == [0,1,1.5]
    assert any(t.token_id == tokens[1].token_id and t.state is EntityState.DELETED for t in result.display_tokens)


def test_connection_probe_uses_same_reasoning_controls_as_model(monkeypatch):
    from substar_core import api_testing
    from substar_core.model_gateway import gateway
    reply = requests.Response()
    reply.status_code = 200
    reply._content = b'{"choices":[{"message":{"content":"ok"}}]}'
    transport = Mock(return_value=reply)
    monkeypatch.setattr(api_testing, "post", transport)
    monkeypatch.setattr(gateway.requests, "post", transport)
    args = dict(base_url="https://api.openai.com/v1", model="o3", api_key="fixture", auth_mode="bearer",
                timeout=10, thinking_mode="enabled",reasoning_effort="low",max_tokens=64)
    assert api_testing.test_chat(**args)["ok"]
    assert gateway.call_text_model(**args, system_prompt="system", user_text="input")[0] == "ok"
    for call in transport.call_args_list:
        body = call.kwargs["json"]
        assert body["max_completion_tokens"] == 64
        assert "max_tokens" not in body and "thinking" not in body
    assert transport.call_count == 2


def test_translation_cache_distinguishes_endpoints(tmp_path, monkeypatch):
    from substar_core.editor.translation import contextual
    model = Mock(return_value=("C001\t译文", {"model":"same"}))
    monkeypatch.setattr(contextual, "call_translation_model", model)
    cue = dict(cue_id="cue",start=0,end=3,hard_limit=25,count_rule="characters_excluding_spaces",maximum_cps=12)
    hits = []
    for endpoint in ["first","second","second"]:
        settings = dict(translation_api_base_url="https://"+endpoint+".invalid", translation_api_key="fixture",
                        translation_api_model="same")
        _, meta = contextual.api_call(settings=settings, system_prompt="prompt", groups=[{"group_id":"g","cues":[cue]}],
                                      mapping_mode="one_to_one", cache_directory=tmp_path)
        hits.append(meta["cache_hit"])
    assert hits == [False,False,True] and model.call_count == 2
