from __future__ import annotations

from substar_core.config import DEFAULTS
from substar_core.segmentation.handler import build_segmentation_handler
from substar_core.transcription.handler import build_transcription_handler


def test_cloud_concurrency_defaults_to_four() -> None:
    assert DEFAULTS["runtime_worker_concurrency"] == 4
    assert DEFAULTS["runtime_cloud_concurrency"] == 4


def test_cloud_handlers_do_not_hold_global_project_or_gpu_locks(tmp_path) -> None:
    segmentation = build_segmentation_handler(tmp_path / "projects", tmp_path)
    transcription = build_transcription_handler(tmp_path / "projects", tmp_path)

    assert segmentation.resources == ("worker", "provider_io")
    assert transcription.resources == ("worker", "media_cpu", "provider_io")
    assert "project_write" not in segmentation.resources
    assert "local_gpu" not in transcription.resources
