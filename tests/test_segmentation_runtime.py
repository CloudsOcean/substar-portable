from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import wave
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from substar_core.runtime import (
    RuntimeStore,
    TaskHandler,
    TaskRegistry,
    TaskScheduler,
    TaskService,
)
from substar_core.runtime.model import InvalidTaskError
from substar_core.segmentation import (
    build_segmentation_handler,
    build_segmentation_request,
    validate_segmentation_request,
)
from substar_core.segmentation.worker import (
    _public_algorithm_error,
    semantic_segmentation_arguments,
)
from substar_core.segmentation.input_contract import (
    SEGMENTATION_MATERIAL_SCHEMA,
    build_segmentation_material,
    load_segmentation_material,
)
from substar_core.segmentation.material import AlignmentUnit
from substar_core.storage import ProjectStore
from substar_core.transcription import (
    build_transcription_handler,
    build_transcription_request,
)
from substar_core.creation import (
    create_subtitle_creation_graph,
    freeze_prompt_snapshot,
    reference_document_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "architecture" / "target" / "contracts"


def validate_schema(value: dict, filename: str) -> None:
    contract = json.loads((CONTRACTS / filename).read_text(encoding="utf-8"))
    Draft202012Validator(contract).validate(value)


def write_wave(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 8000)


class SegmentationContractTests(unittest.TestCase):
    def request(self) -> dict:
        return build_segmentation_request(
            transcription_task_id="tsk_" + "a" * 32,
            transcription_input_fingerprint="b" * 64,
            media_sha256="c" * 64,
            source_asset_id="project-1",
            language="en",
            segmentation_enabled=True,
            reference_document=None,
            prompt_snapshot={
                "relative_path": "task_inputs/segmentation_prompts",
                "sha256": "d" * 64,
                "file_count": 3,
            },
            glossary_snapshot=[],
            settings={
                "translation_api_base_url": "https://api.example.test",
                "translation_api_model": "model-a",
            },
        )

    def test_request_is_strict_fingerprinted_and_secret_free(self) -> None:
        request = self.request()
        self.assertEqual(validate_segmentation_request(request), request)
        validate_schema(request, "segmentation-request.schema.json")
        rendered = json.dumps(request, ensure_ascii=False)
        self.assertNotIn("api_key", rendered.casefold())
        self.assertNotIn("E:\\", rendered)

        changed = json.loads(json.dumps(request))
        changed["constraints"]["target_seconds"] += 1
        with self.assertRaisesRegex(InvalidTaskError, "fingerprint"):
            validate_segmentation_request(changed)

    def test_request_rejects_absolute_snapshot_path(self) -> None:
        request = self.request()
        request["prompt_snapshot"]["relative_path"] = "C:/private/prompts"
        with self.assertRaisesRegex(InvalidTaskError, "relative path"):
            validate_segmentation_request(request)

    def test_in_process_algorithm_receives_material_exactly_once(self) -> None:
        request = self.request()
        material = Path("C:/portable/data/task/segmentation_material.json")
        arguments = semantic_segmentation_arguments(
            material_path=material,
            scratch=Path("C:/portable/data/task/algorithm"),
            progress_path=Path("C:/portable/data/task/progress.json"),
            glossary_path=Path("C:/portable/data/task/glossary.json"),
            request=request,
        )

        self.assertEqual(arguments[0], str(material))
        self.assertEqual(arguments.count(str(material)), 1)
        self.assertNotIn("--worker-script", arguments)
        self.assertNotIn("scripts/run_semantic_segmentation.py", arguments)

    def test_algorithm_error_tail_is_bounded_redacted_and_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stderr = Path(temporary) / "algorithm.stderr.log"
            stderr.write_text(
                "usage: semantic material\n"
                "error: unrecognized arguments: "
                "C:\\private\\task\\segmentation_material.json\n"
                "api_key=must-not-be-public\n",
                encoding="utf-8",
            )
            public = _public_algorithm_error(stderr, 2)

        self.assertIn("segmentation_material.json", public)
        self.assertNotIn("C:\\private", public)
        self.assertNotIn("must-not-be-public", public)
        self.assertLessEqual(len(public), 1600)

    def test_transcription_material_round_trips_through_algorithm_contract(self) -> None:
        evidence = {
            "units": [
                {
                    "index": 1,
                    "start": 0.0,
                    "end": 0.4,
                    "text": "Hello",
                    "sentence_id": 1,
                    "sentence_start": True,
                    "sentence_end": False,
                    "speaker_id": "speaker-1",
                    "speaker_confidence": 0.9,
                },
                {
                    "index": 2,
                    "start": 0.4,
                    "end": 0.8,
                    "text": "world.",
                    "sentence_id": 1,
                    "sentence_start": False,
                    "sentence_end": True,
                    "speaker_id": "speaker-1",
                    "speaker_confidence": 0.9,
                },
            ]
        }
        material = build_segmentation_material("Hello world.", evidence)
        self.assertEqual(material["schema_version"], SEGMENTATION_MATERIAL_SCHEMA)
        validate_schema(material, "segmentation-material.schema.json")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "segmentation_material.json"
            path.write_text(json.dumps(material, ensure_ascii=False), encoding="utf-8")
            master, units = load_segmentation_material(path)
        self.assertEqual(master, "Hello world")
        self.assertEqual([unit.text for unit in units], ["Hello", "world"])

    def test_material_removes_display_punctuation_but_preserves_lexical_punctuation(self) -> None:
        material = build_segmentation_material("ignored", {"units": [
            {"index": 8, "start": 0.0, "end": 0.2, "text": "\"Hello,"},
            {"index": 9, "start": 0.2, "end": 0.4, "text": "U.S."},
            {"index": 10, "start": 0.4, "end": 0.5, "text": "..."},
            {"index": 11, "start": 0.5, "end": 0.8, "text": "don't"},
        ]})
        self.assertEqual(
            [(row["index"], row["text"]) for row in material["units"]],
            [(0, "Hello"), (1, "U.S"), (2, "don't")],
        )

    def test_material_expands_cjk_blocks_but_keeps_alphabetic_words(self) -> None:
        material = build_segmentation_material("ignored", {"units": [
            {"index": 0, "start": 0.0, "end": 0.6, "text": "重庆市"},
            {"index": 1, "start": 0.6, "end": 1.0, "text": "Substar"},
        ]})

        self.assertEqual(
            [row["text"] for row in material["units"]],
            ["重", "庆", "市", "Substar"],
        )
        self.assertEqual(
            [round(float(row["start"]), 6) for row in material["units"][:3]],
            [0.0, 0.2, 0.4],
        )
        self.assertEqual(
            [round(float(row["end"]), 6) for row in material["units"][:3]],
            [0.2, 0.4, 0.6],
        )

    def test_algorithm_contract_rejects_markdown_instead_of_guessing_headings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "segmentation_material.json"
            path.write_text(
                "## SOURCE_TRANSCRIPT\n```text\nHello\n```", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
                load_segmentation_material(path)

    def test_algorithm_entry_can_validate_material_without_provider_access(self) -> None:
        from scripts.run_semantic_segmentation import main

        fixture = ROOT / "tests" / "fixtures" / "segmentation_material_v1.json"
        with tempfile.TemporaryDirectory() as temporary:
            result = main(
                [
                    str(fixture),
                    "--output-dir",
                    temporary,
                    "--route",
                    "semantic",
                    "--grouping-model",
                    "contract-test",
                    "--source-kind",
                    "asr",
                    "--source-asset-id",
                    "contract-test",
                    "--validate-material-only",
                ]
            )
        self.assertEqual(result, 0)

    def test_over_limit_repair_feedback_names_exact_cue_range(self) -> None:
        from scripts.run_semantic_segmentation import validate_semantic_grouping_result
        from scripts.segmentation_support import SegmentationError

        units = [
            AlignmentUnit(index=10, start=0.0, end=0.2, text="A" * 30),
            AlignmentUnit(index=11, start=0.2, end=0.4, text="B" * 29),
        ]
        binding = {
            "input_fingerprint": "a" * 64,
            "block_id": "c0001",
            "ownership": {"alignment_start": 10, "alignment_end": 11},
        }
        value = {
            "schema_version": "substar.semantic-grouping-result.v1",
            **binding,
            "meaning_groups": [
                {
                    "alignment_start": 10,
                    "alignment_end": 11,
                    "line_breaks_after": [11],
                }
            ],
            "exceptions": [],
        }
        with self.assertRaises(SegmentationError) as raised:
            validate_semantic_grouping_result(value, units, (0, 1), 1, binding, 55)
        message = str(raised.exception)
        self.assertIn("alignment 10-11", message)
        self.assertIn("line_breaks_after", message)
        self.assertIn('"line_break_after": 10', message)
        self.assertIn('"left_length": 30', message)
        self.assertIn("不得删除、改写或重排词元", message)

    def test_failed_model_block_is_delivered_as_reviewable_fallback_cues(self) -> None:
        from scripts.run_semantic_segmentation import request_semantic_grouping_block

        units = [
            AlignmentUnit(index=10, start=0.0, end=0.6, text="A" * 30),
            AlignmentUnit(index=11, start=0.7, end=1.3, text="B" * 29),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            args = Namespace(
                source_language="en",
                hard_limit=55,
                english_hard_limit=55,
                chinese_hard_limit=25,
                mixed_hard_limit=25,
                japanese_hard_limit=25,
                korean_hard_limit=32,
                repair_attempts=0,
                output_dir=Path(temporary),
                sentence_boundary_policy="unpunctuated",
            )
            row = request_semantic_grouping_block(
                units,
                (0, 1),
                1,
                args,
                "unused",
                [],
                cached_value={"invalid": True},
            )
            audit = json.loads(
                (Path(temporary) / "semantic_grouping_fallback_c0001.json").read_text(
                    encoding="utf-8"
                )
            )

        _number, _spans, groups, _corrections, cuts, exceptions = row
        self.assertEqual(groups[0]["alignment_start"], 10)
        self.assertEqual(cuts, set())
        self.assertEqual(len(exceptions), 1)
        self.assertTrue(
            all(item["code"] == "semantic_grouping_unresolved" for item in exceptions)
        )
        self.assertEqual(audit["problem_cue_count"], 1)
        self.assertIn("validation_error", audit)

    def test_partial_model_success_is_frozen_before_repair(self) -> None:
        from scripts.run_semantic_segmentation import _salvage_semantic_groups

        units = [
            AlignmentUnit(index=0, start=0.0, end=0.2, text="good"),
            AlignmentUnit(index=1, start=0.2, end=0.4, text="X" * 20),
            AlignmentUnit(index=2, start=0.4, end=0.6, text="also-good"),
        ]
        binding = {
            "input_fingerprint": "f" * 64,
            "block_id": "c0001",
            "ownership": {"alignment_start": 0, "alignment_end": 2},
        }
        value = {
            "schema_version": "substar.semantic-grouping-result.v1",
            **binding,
            "meaning_groups": [
                {"alignment_start": 0, "alignment_end": 0, "line_breaks_after": [0]},
                {"alignment_start": 1, "alignment_end": 1, "line_breaks_after": [1]},
                {"alignment_start": 2, "alignment_end": 2, "line_breaks_after": [2]},
            ],
            "exceptions": [],
        }
        groups, cuts = _salvage_semantic_groups(
            value, units, (0, 2), 1, binding, hard_limit=10
        )
        self.assertEqual(
            [(row["alignment_start"], row["alignment_end"]) for row in groups],
            [(0, 0), (2, 2)],
        )
        self.assertEqual(cuts, {0})


class SegmentationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.project_id = "project-segmentation"
        self.project = self.projects / self.project_id
        self.media = self.project / "input" / "sample.wav"
        write_wave(self.media)
        self.service = TaskService(
            RuntimeStore(self.root / "runtime.sqlite3"), "segmentation-test"
        )
        self.registry = TaskRegistry()
        self.registry.register(build_transcription_handler(self.projects, ROOT))
        self.registry.register(build_segmentation_handler(self.projects, ROOT))
        self.scheduler = TaskScheduler(
            self.service,
            self.registry,
            self.root / "task-runtime",
            poll_seconds=0.01,
            heartbeat_seconds=0.1,
            resource_limits={
                "worker": 2,
                "local_gpu": 1,
                "media_cpu": 1,
                "provider_io": 2,
                "project_write": 1,
                "download_io": 1,
            },
            credential_resolver=lambda refs: {
                reference: "test-credential" for reference in refs
            },
        )
        self.environment = patch.dict(
            os.environ,
            {
                "SUBSTAR_EDITION": "standard",
                "SUBSTAR_MOCK_QWEN": "1",
                "SUBSTAR_MOCK_SEGMENTATION": "1",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.scheduler.shutdown(grace_seconds=0.2, timeout_seconds=8.0)
        self.environment.stop()
        self.temporary.cleanup()

    def test_dependency_graph_materializes_one_canonical_initial_revision(self) -> None:
        prompt_snapshot = freeze_prompt_snapshot(self.project, ROOT)
        transcription_request = build_transcription_request(
            media_path=self.media,
            project_directory=self.project,
            profile_id="qwen_cloud",
            language="en",
            prompt="",
            hotwords={},
            settings={
                "recognition_profile_id": "qwen_cloud",
                "qwen_cloud_model": "qwen-audio-3.0-asr-flash-filetrans",
            },
        )
        transcription, segmentation = create_subtitle_creation_graph(
            service=self.service,
            project_id=self.project_id,
            transcription_request=transcription_request,
            segmentation_enabled=True,
            language="en",
            reference_document=None,
            prompt_snapshot=prompt_snapshot,
            glossary_snapshot=[],
            settings={
                "translation_api_base_url": "https://api.example.test",
                "translation_api_model": "model-a",
                "stage_timeout_seconds": 120,
            },
        )
        replay_transcription, replay_segmentation = create_subtitle_creation_graph(
            service=self.service,
            project_id=self.project_id,
            transcription_request=transcription_request,
            segmentation_enabled=True,
            language="en",
            reference_document=None,
            prompt_snapshot=prompt_snapshot,
            glossary_snapshot=[],
            settings={
                "translation_api_base_url": "https://api.example.test",
                "translation_api_model": "model-a",
                "stage_timeout_seconds": 120,
            },
        )
        self.assertEqual(transcription["task_id"], replay_transcription["task_id"])
        self.assertEqual(segmentation["task_id"], replay_segmentation["task_id"])
        self.assertEqual(segmentation["parent_task_id"], transcription["task_id"])

        self.scheduler.start()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            current = self.service.get_task(segmentation["task_id"])
            if current["state"] in {"succeeded", "failed", "interrupted", "cancelled"}:
                break
            time.sleep(0.02)
        self.assertEqual(
            current["state"],
            "succeeded",
            {
                "segmentation": current,
                "transcription": self.service.get_task(transcription["task_id"]),
                "scheduler": self.scheduler.snapshot(),
                "stderr": [
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in (self.root / "task-runtime").rglob("stderr.log")
                ],
            },
        )
        self.assertEqual(
            self.service.get_task(transcription["task_id"])["state"], "succeeded"
        )
        self.assertEqual(len(self.service.list_artifacts(segmentation["task_id"])), 6)
        validate_schema(current["result"], "segmentation-result.schema.json")

        latest = ProjectStore.open(self.project / "project").load_latest()
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.revision_number, 1)
        operations = {change.operation for change in latest.document.changes}
        self.assertIn("build_from_segmentation", operations)
        self.assertNotIn("build_from_split_stages", operations)
        self.assertTrue((self.project / "segmentation" / "segmentation_candidate.json").is_file())
        validate_schema(
            json.loads(
                (
                    self.project
                    / "segmentation"
                    / "segmentation_candidate.json"
                ).read_text(encoding="utf-8")
            ),
            "segmentation-candidate.schema.json",
        )
        self.assertFalse((self.project / "stage1_experiment").exists())
        canonical = json.dumps(latest.document.to_dict(), ensure_ascii=False)
        self.assertNotIn('"origin": "stage1"', canonical)
        self.assertNotIn("P2mix", canonical)
        self.assertNotIn("experiment", canonical.casefold())

    def test_retry_after_finalization_failure_reuses_accepted_worker_artifacts(self) -> None:
        original = self.registry.get("segmentation")
        finalize_calls = 0

        def fail_once(context, completion):
            nonlocal finalize_calls
            finalize_calls += 1
            if finalize_calls == 1:
                raise InvalidTaskError("simulated finalization interruption")
            return original.finalize(context, completion)

        self.registry.unregister("segmentation")
        self.registry.register(
            TaskHandler(
                task_type=original.task_type,
                prepare=original.prepare,
                validate_input=original.validate_input,
                handle_worker_event=original.handle_worker_event,
                finalize=fail_once,
                resources=original.resources,
            )
        )
        prompt_snapshot = freeze_prompt_snapshot(self.project, ROOT)
        transcription_request = build_transcription_request(
            media_path=self.media,
            project_directory=self.project,
            profile_id="qwen_cloud",
            language="en",
            prompt="",
            hotwords={},
            settings={
                "recognition_profile_id": "qwen_cloud",
                "qwen_cloud_model": "qwen-audio-3.0-asr-flash-filetrans",
            },
        )
        _transcription, segmentation = create_subtitle_creation_graph(
            service=self.service,
            project_id=self.project_id,
            transcription_request=transcription_request,
            segmentation_enabled=True,
            language="en",
            reference_document=None,
            prompt_snapshot=prompt_snapshot,
            glossary_snapshot=[],
            settings={
                "translation_api_base_url": "https://api.example.test",
                "translation_api_model": "model-a",
                "stage_timeout_seconds": 120,
            },
        )
        self.scheduler.start()

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            first = self.service.get_task(segmentation["task_id"])
            if first["state"] in {"failed", "interrupted"}:
                break
            time.sleep(0.02)
        self.assertEqual(first["error"]["code"], "finalization_failed", first)

        retried = self.service.retry(segmentation["task_id"])
        self.assertEqual(retried["attempt"], 2)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            completed = self.service.get_task(segmentation["task_id"])
            if completed["state"] in {"succeeded", "failed", "interrupted"}:
                break
            time.sleep(0.02)
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertEqual(finalize_calls, 2)
        self.assertFalse(
            (
                self.root
                / "task-runtime"
                / segmentation["task_id"]
                / "attempts"
                / "2"
                / "work"
                / "algorithm"
            ).exists()
        )
        latest = ProjectStore.open(self.project / "project").load_latest()
        self.assertIsNotNone(latest)

    def test_reference_document_is_audited_without_mutating_recognition_evidence(self) -> None:
        reference = self.project / "input" / "reference.txt"
        reference.write_text(
            "Goodbye everyone. 欢迎继续使用 Substar。", encoding="utf-8"
        )
        prompt_snapshot = freeze_prompt_snapshot(self.project, ROOT)
        transcription_request = build_transcription_request(
            media_path=self.media,
            project_directory=self.project,
            profile_id="qwen_cloud",
            language="en",
            prompt="",
            hotwords={},
            settings={
                "recognition_profile_id": "qwen_cloud",
                "qwen_cloud_model": "qwen-audio-3.0-asr-flash-filetrans",
            },
        )
        transcription, segmentation = create_subtitle_creation_graph(
            service=self.service,
            project_id=self.project_id,
            transcription_request=transcription_request,
            segmentation_enabled=False,
            language="en",
            reference_document=reference_document_snapshot(self.project, reference),
            prompt_snapshot=prompt_snapshot,
            glossary_snapshot=[],
            settings={"stage_timeout_seconds": 120},
        )
        self.scheduler.start()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            current = self.service.get_task(segmentation["task_id"])
            if current["state"] in {"succeeded", "failed", "interrupted", "cancelled"}:
                break
            time.sleep(0.02)
        self.assertEqual(
            current["state"],
            "succeeded",
            {
                "segmentation": current,
                "transcription": self.service.get_task(transcription["task_id"]),
                "scheduler": self.scheduler.snapshot(),
                "stderr": [
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in (self.root / "task-runtime").rglob("stderr.log")
                ],
            },
        )

        evidence = json.loads(
            (self.project / "recognition_evidence.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["master_text"], "Substar works.")
        latest = ProjectStore.open(self.project / "project").load_latest()
        assert latest is not None
        rendered = " ".join(token.text for token in latest.document.source_tokens)
        self.assertIn("Goodbye", rendered)
        audit = json.loads(
            (self.project / "segmentation" / "reference_match.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(audit["applied"])
        self.assertEqual(
            audit["reference_sha256"],
            reference_document_snapshot(self.project, reference)["sha256"],
        )
        self.assertEqual(
            self.service.get_task(transcription["task_id"])["state"], "succeeded"
        )


if __name__ == "__main__":
    unittest.main()
