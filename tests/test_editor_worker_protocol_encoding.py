from __future__ import annotations

from unittest.mock import patch

from substar_core.editor.calibration import worker as calibration_worker
from substar_core.editor.translation import worker as translation_worker


class ReconfigurableStream:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def _assert_utf8_protocol_boundary(module) -> None:
    streams = [ReconfigurableStream() for _ in range(3)]
    with (
        patch.object(module.sys, "stdin", streams[0]),
        patch.object(module.sys, "stdout", streams[1]),
        patch.object(module.sys, "stderr", streams[2]),
    ):
        module._configure_stdio_utf8()
    assert [stream.calls for stream in streams] == [
        [{"encoding": "utf-8", "errors": "replace"}],
        [{"encoding": "utf-8", "errors": "replace"}],
        [{"encoding": "utf-8", "errors": "replace"}],
    ]


def test_calibration_worker_protocol_is_utf8_on_windows() -> None:
    _assert_utf8_protocol_boundary(calibration_worker)


def test_translation_worker_protocol_is_utf8_on_windows() -> None:
    _assert_utf8_protocol_boundary(translation_worker)
