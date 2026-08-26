from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from substar_core.editor import http_api


def _project(root: Path, project_id: str, filename: str, streams: list[dict]) -> None:
    job = root / project_id
    (job / "input").mkdir(parents=True)
    (job / "input" / filename).write_bytes(b"media")
    (job / "run_manifest.json").write_text(
        json.dumps(
            {
                "source_file": filename,
                "source_path": f"input/{filename}",
                "media": {"duration_seconds": 12.5, "streams": streams},
            }
        ),
        encoding="utf-8",
    )


def test_media_info_routes_audio_only_projects() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
        root = Path(directory)
        _project(
            root,
            "audio-project",
            "speech.mp3",
            [{"codec_type": "audio", "codec_name": "mp3"}],
        )
        with patch.object(http_api, "_projects_root", return_value=root):
            info = http_api.get_project_media_info("audio-project")

    assert info == {
        "schema_version": "substar.media-info.v1",
        "kind": "audio",
        "filename": "speech.mp3",
        "content_type": "audio/mpeg",
        "duration": 12.5,
    }


def test_media_info_prefers_video_stream_evidence() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
        root = Path(directory)
        _project(
            root,
            "video-project",
            "lesson.mp4",
            [
                {"codec_type": "audio", "codec_name": "aac"},
                {"codec_type": "video", "codec_name": "h264"},
            ],
        )
        with patch.object(http_api, "_projects_root", return_value=root):
            info = http_api.get_project_media_info("video-project")

    assert info["kind"] == "video"
    assert info["content_type"] == "video/mp4"
