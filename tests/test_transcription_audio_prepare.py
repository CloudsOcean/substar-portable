from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from substar_core.transcription.cloud_pipeline import _extract_audio, _run


class TranscriptionAudioPrepareTests(unittest.TestCase):
    @patch("substar_core.transcription.cloud_pipeline.subprocess.run")
    def test_subprocess_never_inherits_worker_protocol_stdin(self, runner) -> None:
        _run(["ffprobe", "sample.mp4"])
        self.assertIs(runner.call_args.kwargs["stdin"], subprocess.DEVNULL)

    @patch("substar_core.transcription.cloud_pipeline._run")
    def test_ffmpeg_disables_stdin_and_has_bounded_runtime(self, runner) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"media")
            _extract_audio(
                source,
                root / "audio.wav",
                "off",
                media_duration_seconds=900.0,
            )
        command = runner.call_args.args[0]
        self.assertIn("-nostdin", command)
        self.assertGreater(runner.call_args.kwargs["timeout_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
