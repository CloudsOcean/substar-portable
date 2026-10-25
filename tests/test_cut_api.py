import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from substar_core.editor import cut_api


def test_media_export_has_same_duration_as_subtitle_projection(tmp_path, monkeypatch):
    ffmpeg = os.environ.get("SUBSTAR_TEST_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is required for the real media integration test")
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    monkeypatch.setattr(cut_api,"executable",lambda name:ffmpeg if name=="ffmpeg" else ffprobe)
    source=tmp_path/"source.mp4"
    subprocess.run([ffmpeg,"-nostdin","-y","-v","error","-f","lavfi","-i","color=c=blue:s=160x90:r=25:d=3",
                    "-f","lavfi","-i","sine=frequency=440:duration=3","-c:v","libx264","-c:a","aac","-shortest",str(source)],check=True,capture_output=True)
    from substar_core.domain import EditorDocument, DisplayCue, DocumentProperties
    state={"markers":[],"active_plan_id":"p","plans":[{"id":"p","name":"test","keep":None,"cuts":[{"id":"c","start":1,"end":2,"enabled":True}]}]}
    doc=EditorDocument("export",(),(),(DisplayCue("cue",0,(),0,3,source_text="保持原文"),),properties=DocumentProperties(timeline_json=json.dumps(state)))
    monkeypatch.setattr(cut_api,"revision",lambda *args:SimpleNamespace(document=doc))
    monkeypatch.setitem(sys.modules,"substar_core.editor.http_api",SimpleNamespace(_project_media_source=lambda *args:(source,{})))
    video=cut_api.cut_export("p",cut_api.CutRequest(revision_id="r",plan_id="p",format="video"))
    directory=Path(video.path).parent
    try:
        duration,_,_=cut_api.inspect_media(Path(video.path))
        assert abs(duration-2)<.06
        srt=cut_api.cut_export("p",cut_api.CutRequest(revision_id="r",plan_id="p",format="srt"))
        assert "00:00:02,000" in srt.body.decode()
        assert "保持原文" in srt.body.decode()
        xml=cut_api.cut_export("p",cut_api.CutRequest(revision_id="r",plan_id="p",format="xml"))
        assert "<duration>50</duration>" in xml.body.decode()
        ass=cut_api.cut_export("p",cut_api.CutRequest(revision_id="r",plan_id="p",format="ass",subtitle_mode="source"))
        assert "保持原文" in ass.body.decode()
        wav=cut_api.cut_export("p",cut_api.CutRequest(revision_id="r",plan_id="p",format="audio"))
        try:
            audio_duration,video_stream,audio_stream=cut_api.inspect_media(Path(wav.path))
            assert abs(audio_duration-2)<.01
            assert video_stream is None and audio_stream is not None
        finally:
            shutil.rmtree(Path(wav.path).parent)
    finally:
        shutil.rmtree(directory)


def test_ai_endpoint_proposes_real_ranges_without_mutating_document(monkeypatch):
    from substar_core.domain import EditorDocument, DisplayCue
    from substar_core import model_gateway
    doc=EditorDocument("ai",(),(),(DisplayCue("a",0,(),0,1,source_text="开始"),DisplayCue("b",1,(),2,3,source_text="结尾")))
    before=doc.content_hash()
    monkeypatch.setattr(cut_api,"revision",lambda *args:SimpleNamespace(document=doc))
    monkeypatch.setitem(sys.modules,"substar_core.editor.http_api",SimpleNamespace(_project_llm_settings=lambda *a,**k:{}))
    monkeypatch.setattr(cut_api,"cut_media_duration",lambda _:4)
    captured={}
    def reply(**kwargs):
        captured.update(kwargs)
        return json.dumps({"name":"精简","operations":[{"action":"remove","start":1,"end":2,"reason":"用户要求移除空隙"}]}),{}
    monkeypatch.setattr(model_gateway,"call_text_model",reply)
    response=cut_api.cut_ai("p",cut_api.AiCutRequest(revision_id="r",goal="保留内容，去除中间空隙"))
    assert response["revision_id"]=="r"
    assert response["plan"]["keep"] is None
    assert captured["user_payload"]["duration_seconds"]==4
    assert captured["user_payload"]["cues"][0]["number"]==1
    assert [(c["start"],c["end"]) for c in response["plan"]["cuts"]]==[(1,2)]
    assert captured["user_payload"]["cues"][0]["id"]=="a"
    assert doc.content_hash()==before and not doc.properties.timeline_json
