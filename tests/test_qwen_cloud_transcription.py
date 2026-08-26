from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from substar_core.qwen_cloud_asr import (
    _ACTIVE_STATUS_PROGRESS,
    QwenCloudAsrError,
    _encode_cloud_audio,
    _ffmpeg_executable,
    _submission_body,
    run_qwen_cloud_asr,
)


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def provider_result(text: str = "Substar works") -> dict:
    return {
        "transcripts": [
            {
                "text": text,
                "sentences": [
                    {
                        "begin_time": 0,
                        "end_time": 1000,
                        "language": "en",
                        "text": text,
                        "speaker_id": 1,
                        "words": [
                            {"begin_time": 0, "end_time": 500, "text": " Substar"},
                            {"begin_time": 500, "end_time": 1000, "text": " works"},
                        ],
                    }
                ],
            }
        ]
    }


class QwenCloudTranscriptionTests(unittest.TestCase):
    def test_provider_status_progress_never_assigns_terminal_regression(self) -> None:
        self.assertEqual(_ACTIVE_STATUS_PROGRESS["PENDING"], 0.40)
        self.assertEqual(_ACTIVE_STATUS_PROGRESS["RUNNING"], 0.46)
        self.assertNotIn("SUCCEEDED", _ACTIVE_STATUS_PROGRESS)
        self.assertNotIn("FAILED", _ACTIVE_STATUS_PROGRESS)

    def settings(self, checkpoint: Path, fingerprint: str) -> dict:
        return {
            "api_key": "private-key",
            "qwen_cloud_model": "qwen-audio-3.0-asr-flash-filetrans",
            "qwen_cloud_base_url": "https://provider.example/api/v1",
            "qwen_cloud_request_timeout_seconds": 10,
            "qwen_cloud_task_timeout_seconds": 60,
            "qwen_cloud_poll_interval_seconds": 1,
            "http_retry_attempts": 0,
            "language": "en",
            "context": "Product context\nRecognition vocabulary:\nSubstar",
            "hotwords": {"Substar": 5},
            "_checkpoint_dir": str(checkpoint),
            "_transcription_input_fingerprint": fingerprint,
            "_cancel_requested": lambda: False,
        }

    @staticmethod
    def fake_audio(_source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-mp3")

    def test_cloud_audio_uses_bundled_ffmpeg_and_publishes_atomically(self) -> None:
        self.assertEqual(Path(_ffmpeg_executable()).name.casefold(), "ffmpeg.exe")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "audio.wav"
            source.write_bytes(b"wav")
            output = root / "audio.mp3"

            def encode(command: list[str], **_kwargs):
                self.assertEqual(Path(command[0]).name.casefold(), "ffmpeg.exe")
                self.assertIn("-nostdin", command)
                self.assertEqual(Path(command[-1]).suffix.casefold(), ".mp3")
                Path(command[-1]).write_bytes(b"encoded-mp3")
                return SimpleNamespace(returncode=0, stderr="")

            with patch(
                "substar_core.qwen_cloud_asr.subprocess.run", side_effect=encode
            ):
                _encode_cloud_audio(source, output)

            self.assertEqual(output.read_bytes(), b"encoded-mp3")
            self.assertEqual(list(root.glob("*.encoding.mp3")), [])

    def test_cloud_audio_timeout_removes_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "audio.wav"
            source.write_bytes(b"wav")
            output = root / "audio.mp3"
            output.write_bytes(b"")

            def timeout(command: list[str], **_kwargs):
                Path(command[-1]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired(command, 30)

            with (
                patch(
                    "substar_core.qwen_cloud_asr.subprocess.run",
                    side_effect=timeout,
                ),
                self.assertRaises(QwenCloudAsrError),
            ):
                _encode_cloud_audio(source, output, timeout_seconds=30)

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("*.encoding.mp3")), [])

    def test_context_and_fingerprint_are_bound_to_resumable_provider_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wav = root / "audio.wav"
            wav.write_bytes(b"wav")
            checkpoint = root / "checkpoint"
            requests: list[tuple[str, str, dict]] = []

            def request(method: str, url: str, **kwargs):
                requests.append((method, url, kwargs))
                if method == "POST":
                    return FakeResponse({"output": {"task_id": "remote-1"}})
                if "/tasks/" in url:
                    return FakeResponse(
                        {
                            "output": {
                                "task_status": "SUCCEEDED",
                                "results": [
                                    {
                                        "subtask_status": "SUCCEEDED",
                                        "transcription_url": "https://result.example/one",
                                    }
                                ],
                            }
                        }
                    )
                return FakeResponse(provider_result())

            with (
                patch("substar_core.qwen_cloud_asr._encode_cloud_audio", self.fake_audio),
                patch("substar_core.qwen_cloud_asr.upload_temporary_file", return_value="oss://audio"),
                patch("substar_core.qwen_cloud_asr._request", side_effect=request),
            ):
                result = run_qwen_cloud_asr(
                    wav, self.settings(checkpoint, "a" * 64), lambda *_args: None
                )

            submission = next(item for item in requests if item[0] == "POST")
            body = submission[2]["json"]
            parameters = body["parameters"]
            state = json.loads((checkpoint / "qwen_cloud_state.json").read_text(encoding="utf-8"))
            audit = json.loads(
                (root / "provider_submission_audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(body["input"]["file_urls"], ["oss://audio"])
            self.assertEqual(
                body["input"]["context"][0]["content"][0]["text"],
                self.settings(checkpoint, "a" * 64)["context"],
            )
            self.assertEqual(parameters["vocabulary"], {"Substar": 5})
            self.assertEqual(parameters["language_hints"], ["en"])
            self.assertTrue(parameters["diarization_enabled"])
            self.assertNotIn("enable_words", parameters)
            self.assertEqual(state["input_fingerprint"], "a" * 64)
            self.assertEqual(
                audit["public_body"]["input"]["file_urls"], ["[MEDIA]"]
            )
            self.assertEqual(
                audit["compilation"]["submitted_vocabulary"], {"Substar": 5}
            )
            self.assertNotIn("private-key", json.dumps(audit))
            self.assertNotIn("oss://audio", json.dumps(audit))
            self.assertEqual(
                result["audit"]["engine"],
                "qwen-audio-3.0-asr-flash-filetrans",
            )
            self.assertEqual(result["units"][0]["speaker_id"], "speaker_1")

            with patch(
                "substar_core.qwen_cloud_asr._request",
                side_effect=AssertionError("matching completed checkpoint must be reused"),
            ):
                reused = run_qwen_cloud_asr(
                    wav, self.settings(checkpoint, "a" * 64), lambda *_args: None
                )
            self.assertEqual(reused["text"], result["text"])

    def test_changed_input_fingerprint_cannot_reuse_old_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wav = root / "audio.wav"
            wav.write_bytes(b"wav")
            checkpoint = root / "checkpoint"
            checkpoint.mkdir()
            (checkpoint / "qwen_cloud_state.json").write_text(
                json.dumps(
                    {
                        "model": "custom-filetrans-model",
                        "base_url": "https://provider.example/api/v1",
                        "input_fingerprint": "a" * 64,
                        "task_id": "old-task",
                    }
                ),
                encoding="utf-8",
            )
            (checkpoint / "qwen_cloud_result.json").write_text(
                json.dumps(provider_result("old result")), encoding="utf-8"
            )
            submitted: list[dict] = []

            def request(method: str, url: str, **kwargs):
                if method == "POST":
                    submitted.append(kwargs["json"])
                    return FakeResponse({"output": {"task_id": "new-task"}})
                if "/tasks/" in url:
                    return FakeResponse(
                        {
                            "output": {
                                "task_status": "SUCCEEDED",
                                "results": [
                                    {
                                        "subtask_status": "SUCCEEDED",
                                        "transcription_url": "https://result.example/new",
                                    }
                                ],
                            }
                        }
                    )
                return FakeResponse(provider_result("new result"))

            with (
                patch("substar_core.qwen_cloud_asr._encode_cloud_audio", self.fake_audio),
                patch("substar_core.qwen_cloud_asr.upload_temporary_file", return_value="oss://audio"),
                patch("substar_core.qwen_cloud_asr._request", side_effect=request),
            ):
                result = run_qwen_cloud_asr(
                    wav, self.settings(checkpoint, "b" * 64), lambda *_args: None
                )

            self.assertEqual(len(submitted), 1)
            self.assertEqual(result["text"], "new result")
            state = json.loads((checkpoint / "qwen_cloud_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["task_id"], "new-task")
            self.assertEqual(state["input_fingerprint"], "b" * 64)

    def test_qwen3_filetrans_uses_its_distinct_request_contract(self) -> None:
        body = _submission_body(
            "qwen3-asr-flash-filetrans",
            "oss://audio",
            {
                "language": "zh-CN",
                "context": "Substar product context",
                "hotwords": {"Substar": 5},
            },
        )
        self.assertEqual(body["input"], {"file_url": "oss://audio"})
        self.assertEqual(body["parameters"]["language"], "zh")
        self.assertEqual(
            body["parameters"]["corpus"], {"text": "Substar product context"}
        )
        self.assertTrue(body["parameters"]["enable_words"])
        self.assertNotIn("diarization_enabled", body["parameters"])


if __name__ == "__main__":
    unittest.main()
