from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from substar_core.runtime import (
    RuntimeStore,
    TaskRegistry,
    TaskScheduler,
    TaskService,
    TaskWorkContext,
)
from substar_core.transcription import (
    RECOGNITION_EVIDENCE_SCHEMA,
    TRANSCRIPTION_INPUT_SCHEMA,
    build_transcription_handler,
    build_transcription_request,
    validate_recognition_evidence,
    validate_transcription_request,
)
from substar_core.transcription.contracts import recognition_source_from_evidence
from substar_core.transcription.contracts import TRANSCRIPTION_OPTION_KEYS


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "architecture" / "target" / "contracts"


def validate_schema(value: dict, filename: str) -> None:
    contract = json.loads((CONTRACTS / filename).read_text(encoding="utf-8"))
    Draft202012Validator(
        contract, format_checker=FormatChecker()
    ).validate(value)


def write_silence(path: Path, seconds: float = 0.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * max(1, int(16000 * seconds)))


class TranscriptionContractTests(unittest.TestCase):
    def test_documented_option_names_match_runtime_allowlist(self) -> None:
        contract = json.loads(
            (CONTRACTS / "transcription-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        documented = set(
            contract["properties"]["options"]["propertyNames"]["enum"]
        )
        self.assertEqual(documented, set(TRANSCRIPTION_OPTION_KEYS))

    def test_request_is_fingerprinted_and_contains_no_host_media_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "projects" / "project-1"
            media = project / "input" / "sample.wav"
            write_silence(media)
            request = build_transcription_request(
                media_path=media,
                project_directory=project,
                profile_id="qwen_cloud",
                language="en",
                prompt="Product briefing",
                hotwords={"Substar": 5},
                settings={
                    "whisper_model": "large-v3-turbo",
                    "model_cache_dir": "C:/private/models",
                    "api_key": "must-not-leak",
                },
            )

        self.assertEqual(request["schema_version"], TRANSCRIPTION_INPUT_SCHEMA)
        self.assertEqual(request["media"]["relative_path"], "input/sample.wav")
        self.assertNotIn("private", json.dumps(request))
        self.assertNotIn("must-not-leak", json.dumps(request))
        self.assertEqual(validate_transcription_request(request), request)
        validate_schema(request, "transcription-request.schema.json")

        changed = json.loads(json.dumps(request))
        changed["language"] = "zh"
        with self.assertRaisesRegex(Exception, "input_fingerprint"):
            validate_transcription_request(changed)

    def test_evidence_round_trips_to_recognition_source_without_mutating_authority(self) -> None:
        evidence = {
            "schema_version": RECOGNITION_EVIDENCE_SCHEMA,
            "source_schema_version": "substar.recognition-source.v1",
            "created_at": "2026-08-16T00:00:00+00:00",
            "media": {"sha256": "a" * 64, "duration_seconds": 1.0},
            "engines": {"profile_id": "qwen_cloud"},
            "language": "en",
            "master_text": "Hello",
            "chunks": [{"index": 0, "start": 0.0, "end": 1.0, "text": "Hello"}],
            "units": [
                {
                    "index": 0,
                    "start": 0.0,
                    "end": 1.0,
                    "text": "Hello",
                    "kind": "word",
                    "sentence_id": 0,
                    "sentence_start": True,
                    "sentence_end": True,
                }
            ],
            "request": {
                "input_fingerprint": "b" * 64,
                "profile_id": "qwen_cloud",
                "requested_language": "en",
                "prompt_sha256": "c" * 64,
                "hotwords_sha256": "d" * 64,
                "hotword_count": 1,
                "provider_submission": {
                    "relative_path": "provider_submission_audit.json",
                    "sha256": "e" * 64,
                },
                "provider_response": None,
            },
        }
        validated = validate_recognition_evidence(evidence)
        validate_schema(validated, "recognition-evidence.schema.json")
        source = recognition_source_from_evidence(validated)
        source["master_text"] = "edited projection"

        self.assertEqual(source["schema_version"], "substar.recognition-source.v1")
        self.assertNotIn("request", source)
        self.assertEqual(validated["master_text"], "Hello")


class ProductionTranscriptionHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.project = self.projects / "project-1"
        self.media = self.project / "input" / "sample.wav"
        write_silence(self.media)
        self.store = RuntimeStore(self.root / "runtime.sqlite3")
        self.service = TaskService(self.store, "transcription-test")
        self.registry = TaskRegistry()
        self.registry.register(build_transcription_handler(self.projects, ROOT))
        self.scheduler: TaskScheduler | None = None

    def tearDown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(grace_seconds=0.1, timeout_seconds=5.0)
        self.temporary.cleanup()

    def wait_terminal(self, task_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.service.get_task(task_id)
            if task["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return task
            time.sleep(0.02)
        self.fail(f"task did not finish: {self.service.get_task(task_id)}")

    def test_real_supervisor_worker_publishes_canonical_evidence(self) -> None:
        request = build_transcription_request(
            media_path=self.media,
            project_directory=self.project,
            profile_id="qwen_cloud",
            language="en",
            prompt="",
            hotwords={"Substar": 5},
            settings={"qwen_cloud_model": "qwen-audio-3.0-asr-flash-filetrans"},
        )
        task = self.service.create_task(
            task_type="transcription",
            input_schema=TRANSCRIPTION_INPUT_SCHEMA,
            input_payload=request,
            project_id="project-1",
        )
        self.scheduler = TaskScheduler(
            self.service,
            self.registry,
            self.root / "task-runtime",
            poll_seconds=0.01,
            heartbeat_seconds=0.1,
            resource_limits={
                "worker": 1,
                "media_cpu": 1,
                "provider_io": 1,
                "local_gpu": 1,
            },
            credential_resolver=lambda refs: {
                reference: "test-credential" for reference in refs
            },
        )
        with patch.dict(os.environ, {"SUBSTAR_MOCK_QWEN": "1"}):
            self.scheduler.start()
            completed = self.wait_terminal(task["task_id"])

        self.assertEqual(completed["state"], "succeeded", completed.get("error"))
        self.assertEqual(completed["step"], "transcription.artifact_finalize")
        self.assertEqual(
            completed["progress_message"], "Validating transcription artifacts."
        )
        validate_schema(completed["result"], "transcription-result.schema.json")
        self.assertEqual(completed["task_type"], "transcription")
        self.assertEqual(completed["project_id"], "project-1")
        self.assertNotEqual(completed["task_id"], completed["project_id"])
        evidence_path = self.project / "recognition_evidence.json"
        evidence = validate_recognition_evidence(
            json.loads(evidence_path.read_text(encoding="utf-8"))
        )
        self.assertEqual(evidence["schema_version"], RECOGNITION_EVIDENCE_SCHEMA)
        self.assertFalse((self.project / "alignment.json").exists())
        material_path = self.project / "segmentation_material.json"
        self.assertTrue(material_path.is_file())
        material = json.loads(material_path.read_text(encoding="utf-8"))
        self.assertEqual(
            material["schema_version"], "substar.segmentation-material.v1"
        )
        self.assertTrue((self.project / "audio_16k_mono.wav").is_file())
        self.assertGreaterEqual(len(self.service.list_artifacts(task["task_id"])), 8)
        persisted_input = self.service.get_task_input(task["task_id"])
        self.assertNotIn(str(self.project), json.dumps(persisted_input))
        public_manifest = json.loads(
            (self.project / "run_manifest.json").read_text(encoding="utf-8")
        )
        validate_schema(
            json.loads(
                (self.project / "provider_submission_audit.json").read_text(
                    encoding="utf-8"
                )
            ),
            "provider-submission-audit.schema.json",
        )
        public_manifest_text = json.dumps(public_manifest)
        self.assertNotIn(str(self.project), public_manifest_text)
        self.assertFalse(
            any(
                fragment in key.lower()
                for key in public_manifest.get("configuration", {})
                for fragment in ("api_key", "secret", "token", "path", "directory")
            )
        )

    def test_retry_finds_latest_usable_checkpoint_across_empty_attempt(self) -> None:
        request = build_transcription_request(
            media_path=self.media,
            project_directory=self.project,
            profile_id="qwen_cloud",
            language="en",
            prompt="",
            hotwords={},
            settings={"qwen_cloud_model": "qwen-audio-3.0-asr-flash-filetrans"},
        )
        attempts = self.root / "task-runtime" / "task-1" / "attempts"
        first = attempts / "1" / "artifacts"
        second = attempts / "2" / "artifacts"
        third = attempts / "3"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / "recognition_request.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        write_silence(first / "audio_16k_mono.wav")
        (second / "recognition_request.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        context = TaskWorkContext(
            task={"attempt": 3, "project_id": "project-1"},
            input_payload=request,
            attempt_directory=third,
            work_directory=third / "work",
            artifact_directory=third / "artifacts",
        )
        handler = self.registry.get("transcription")

        launch = handler.prepare(context)

        self.assertEqual(
            Path(str(launch.worker_input["resume_artifact_directory"])),
            first.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
