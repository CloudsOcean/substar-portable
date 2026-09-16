import io
import wave
from fastapi import FastAPI
from fastapi.testclient import TestClient
from substar_core.editor import http_api, subtitle_api


def client_for(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_projects_root", lambda: tmp_path)
    app = FastAPI()
    app.include_router(http_api.router)
    app.include_router(subtitle_api.router)
    return TestClient(app)


def test_create_edit_review_and_export_without_tokens(tmp_path, monkeypatch):
    client = client_for(tmp_path, monkeypatch)
    srt = "1\n00:00:00,000 --> 00:00:01,000\nHello\n你好\n"
    fields = {"mode": "bilingual-lines"}
    files = {"subtitle": ("sub.srt", srt.encode())}
    preview = client.post("/api/subtitle-projects/preview", data=fields, files=files)
    assert preview.status_code == 200, preview.text
    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        import random
        output.writeframes(random.Random(42).randbytes(32000))
    created = client.post("/api/subtitle-projects", data={**fields, "confirmation": preview.json()["confirmation"]},
                          files={**files, "media": ("media.wav", wav.getvalue())})
    assert created.status_code == 200, created.text
    project = created.json()["project_id"]
    listed = client.get("/api/projects").json()["projects"]
    assert next(row for row in listed if row["project_id"] == project)["subtitle_basis"] == "sentence"
    store = http_api.open_project_store(project)
    revision = store.load_latest()
    assert not revision.document.display_tokens
    cue_id = revision.document.cues[0].cue_id
    assert client.get(f"/api/projects/{project}/waveform?start=0&end=1&points=128").status_code == 200
    body = {"action": "add", "cue_id": cue_id, "track": "source", "text": "Check", "expected_version": 0, "expected_revision_id": revision.revision_id}
    note = client.post(f"/api/projects/{project}/review-notes", json=body)
    assert note.status_code == 200, note.text
    assert client.post(f"/api/projects/{project}/review-notes", json=body).status_code == 409
    edit = client.put(f"/api/projects/{project}/cues/{cue_id}/source-text", json={"expected_revision_id": revision.revision_id, "text": "Hello again"})
    assert edit.status_code == 200, edit.text
    notes = client.get(f"/api/projects/{project}/review-notes").json()
    assert notes["notes"][0]["anchor_status"] == "text_changed"
    exported = client.get(f"/api/projects/{project}/export/ab-double")
    assert exported.status_code == 200
    assert "Hello again" in exported.text and "你好" in exported.text
    from substar_core.document_operations import apply_document_operation
    latest = store.load_latest()
    operation = {"operation_id":"source-queue", "type":"set_source_text", "payload":{"cue_id":cue_id, "text":"Queued source"}}
    assert apply_document_operation(latest.document, operation).cues[0].source_text == "Queued source"
    operation["base"] = {"document_id": latest.document.document_id,
                         "revision_id": latest.revision_id, "document_hash": latest.document_hash}
    queued = client.post(f"/api/projects/{project}/operations", json=operation)
    assert queued.status_code == 200, queued.text
    latest = store.load_latest()
    assert latest.document.cues[0].source_text == "Queued source"
    from substar_core.project_exchange import export_subtitle_project, import_subtitle_project
    package = tmp_path / "portable.substar"
    export_subtitle_project(package, project_id=project, job_dir=tmp_path / project, revision=latest)
    with package.open("rb") as source:
        imported = import_subtitle_project(source, projects_root=tmp_path / "received")
    import json
    reviews = json.loads((tmp_path / "received" / imported / "review" / "notes.json").read_text(encoding="utf-8"))
    assert reviews["notes"][0]["text"] == "Check"


def test_preview_rejects_ambiguous_and_creation_requires_media(tmp_path, monkeypatch):
    client = client_for(tmp_path, monkeypatch)
    files = {"subtitle": ("sub.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")}
    preview = client.post("/api/subtitle-projects/preview", data={"mode": "single"}, files=files).json()
    result = client.post("/api/subtitle-projects", data={"confirmation": preview["confirmation"]}, files=files)
    assert result.status_code == 422
    assert not list(tmp_path.iterdir())
