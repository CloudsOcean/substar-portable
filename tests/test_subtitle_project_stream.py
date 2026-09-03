from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from substar_core.project_exchange import EXCHANGE_SCHEMA, stream_subtitle_project


class _Document:
    document_id = "doc_test"

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": "test", "document_id": self.document_id, "cues": []}


def test_subtitle_project_stream_is_valid_and_self_describing(tmp_path: Path) -> None:
    job_dir = tmp_path / "project"
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True)
    media = b"media-content"
    (input_dir / "clip.mp4").write_bytes(media)
    (job_dir / "task_info.json").write_text('{"display_name":"demo"}', encoding="utf-8")
    revision = SimpleNamespace(revision_id="rev_test", document=_Document())

    payload = b"".join(stream_subtitle_project(
        project_id="project_test", job_dir=job_dir, revision=revision
    ))

    assert payload.startswith(b"PK")
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        assert archive.testzip() is None
        assert archive.namelist()[-1] == "manifest.json"
        assert archive.read("project/input/clip.mp4") == media
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == EXCHANGE_SCHEMA
        declared = {row["path"]: row for row in manifest["files"]}
        assert declared["project/input/clip.mp4"] == {
            "path": "project/input/clip.mp4",
            "size": len(media),
            "sha256": hashlib.sha256(media).hexdigest(),
        }
