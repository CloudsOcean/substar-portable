from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

import app as application
from substar_core.runtime import RuntimeStore, TaskRegistry, TaskScheduler, TaskService
from substar_core.segmentation import build_segmentation_handler
from substar_core.storage import ProjectStore
from substar_core.transcription import build_transcription_handler


ROOT = Path(__file__).resolve().parents[1]


def wave_bytes(path: Path) -> bytes:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 8000)
    return path.read_bytes()


class ProjectCreationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.service = TaskService(
            RuntimeStore(self.root / "runtime.sqlite3"), "project-creation-test"
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
                "worker": 1,
                "local_gpu": 1,
                "media_cpu": 1,
                "provider_io": 1,
                "project_write": 1,
                "download_io": 1,
            },
            credential_resolver=lambda references: {
                reference: "mock-credential" for reference in references
            },
        )
        application.app.state.task_store = self.service.store
        application.app.state.task_service = self.service
        application.app.state.task_registry = self.registry
        application.app.state.task_scheduler = self.scheduler
        with application.JOBS_LOCK:
            application.JOBS.clear()
        self.output_patch = patch.object(
            application, "_relay_output_root", return_value=self.projects
        )
        self.environment_patch = patch.dict(
            os.environ,
            {"SUBSTAR_EDITION": "standard", "SUBSTAR_MOCK_QWEN": "1"},
        )
        self.output_patch.start()
        self.environment_patch.start()
        self.scheduler.start()

    def tearDown(self) -> None:
        self.scheduler.shutdown(grace_seconds=0.2, timeout_seconds=8.0)
        with application.JOBS_LOCK:
            application.JOBS.clear()
        self.environment_patch.stop()
        self.output_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def settings(language: str = "en") -> str:
        return json.dumps(
            {
                "recognition_profile_id": "qwen_cloud",
                "language": language,
                "segmentation_enabled": False,
                "segmentation_chunk_seconds": 90,
                "translation_enabled": False,
                "calibration_enabled": False,
                "review_enabled": False,
            }
        )

    async def post(self, media: bytes, *, language: str = "en") -> httpx.Response:
        transport = httpx.ASGITransport(app=application.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://substar.test"
        ) as client:
            return await client.post(
                "/api/project-creations",
                headers={"Idempotency-Key": "browser-submission-1"},
                data={
                    "mode": "asr",
                    "settings_json": self.settings(language),
                    "debug_merged": "false",
                },
                files={"media": ("sample.wav", media, "audio/wav")},
            )

    async def post_beginner_tutorial(self, media: bytes) -> httpx.Response:
        transport = httpx.ASGITransport(app=application.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://substar.test"
        ) as client:
            return await client.post(
                "/api/project-creations",
                headers={"Idempotency-Key": "beginner-tutorial-submission"},
                data={
                    "mode": "asr",
                    "settings_json": self.settings("zh"),
                    "tutorial_case_id": "reference-script-v1",
                },
                files={"media": ("教程音频.wav", media, "audio/wav")},
            )

    async def post_reference(self, media: bytes) -> httpx.Response:
        settings = json.loads(self.settings("en"))
        settings.update(
            {
                "reference_script_mode": True,
                "reference_break_symbols": ",.",
            }
        )
        transport = httpx.ASGITransport(app=application.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://substar.test"
        ) as client:
            return await client.post(
                "/api/project-creations",
                headers={"Idempotency-Key": "reference-script-submission"},
                data={"mode": "asr", "settings_json": json.dumps(settings)},
                files={
                    "media": ("sample.wav", media, "audio/wav"),
                    "reference_document": (
                        "reference.txt",
                        b"Substar, works.",
                        "text/plain",
                    ),
                },
            )

    async def post_batch(
        self,
        media: list[tuple[str, bytes]],
        *,
        key: str,
        language: str = "en",
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=application.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://substar.test"
        ) as client:
            return await client.post(
                "/api/project-batches",
                headers={"Idempotency-Key": key},
                data={
                    "mode": "asr",
                    "settings_json": self.settings(language),
                },
                files=[
                    ("media", (name, value, "audio/wav"))
                    for name, value in media
                ],
            )

    def wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with application.JOBS_LOCK:
                job = application.JOBS[job_id]
            application._refresh_canonical_job_projection(job)
            with application.JOBS_LOCK:
                current = job.public()
            if current["status"] in {
                "awaiting_edit",
                "failed",
                "interrupted",
                "cancelled",
            }:
                return current
            time.sleep(0.02)
        self.fail(f"job did not settle: {current}")

    @staticmethod
    async def delete(job_id: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=application.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://substar.test"
        ) as client:
            return await client.delete(f"/api/project-creations/{job_id}")

    def test_lost_response_replay_creates_one_task_and_editor_ready_project(self) -> None:
        media = wave_bytes(self.root / "sample.wav")
        first = asyncio.run(self.post(media))
        replay = asyncio.run(self.post(media))
        conflict = asyncio.run(self.post(media, language="zh"))

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        created = first.json()
        self.assertEqual(replay.json()["id"], created["id"])
        self.assertNotEqual(created["id"], created["transcription_task_id"])
        self.assertNotEqual(created["id"], created["segmentation_task_id"])

        self.assertEqual(len(self.service.list_tasks(task_type="transcription")), 1)
        self.assertEqual(len(self.service.list_tasks(task_type="segmentation")), 1)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            with application.JOBS_LOCK:
                job = application.JOBS[created["id"]]
            application._refresh_canonical_job_projection(job)
            with application.JOBS_LOCK:
                current = job.public()
            if current["status"] in {
                "awaiting_edit",
                "failed",
                "interrupted",
                "cancelled",
            }:
                break
            time.sleep(0.02)
        self.assertEqual(current["status"], "awaiting_edit", current)
        task = self.service.get_task(created["transcription_task_id"])
        self.assertEqual(task["state"], "succeeded", task)
        latest = ProjectStore.open(
            self.projects / created["id"] / "project"
        ).load_latest()
        self.assertIsNotNone(latest)

    def test_beginner_tutorial_has_fixed_display_name(self) -> None:
        response = asyncio.run(
            self.post_beginner_tutorial(wave_bytes(self.root / "tutorial.wav"))
        )
        self.assertEqual(response.status_code, 202, response.text)
        created = response.json()
        self.assertEqual(created["display_name"], "初级教程")
        with application.JOBS_LOCK:
            job = application.JOBS[created["id"]]
        job.display_name = "教程音频 · Qwen 云端"
        self.assertEqual(job.public()["display_name"], "初级教程")
        current = self.wait_job(created["id"])
        self.assertEqual(current["status"], "awaiting_edit", current)
        binding = json.loads(
            (self.projects / created["id"] / "tutorial_project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(binding["case_id"], "reference-script-v1")
        self.assertEqual(binding["level"], "beginner")

    def test_reference_script_mode_reaches_editor_without_segmentation_provider(self) -> None:
        media = wave_bytes(self.root / "reference.wav")
        response = asyncio.run(self.post_reference(media))
        self.assertEqual(response.status_code, 202, response.text)
        created = response.json()
        current = self.wait_job(created["id"])
        self.assertEqual(current["status"], "awaiting_edit", current)
        segmentation = json.loads(
            (
                self.projects
                / created["id"]
                / "segmentation"
                / "segmentation_request.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(segmentation["mode"], "reference_script")
        self.assertEqual(segmentation["constraints"]["target_seconds"], 90)
        self.assertEqual(
            segmentation["constraints"]["reference_break_symbols"],
            ",.",
        )
        revision = ProjectStore.open(
            self.projects / created["id"] / "project"
        ).load_latest()
        self.assertIsNotNone(revision)
        assert revision is not None
        self.assertEqual(len(revision.document.cues), 2)
        srt = application.render_document_srt(
            revision.document, application.SubtitleExportMode.SOURCE
        )
        self.assertIn("Substar,", srt)
        self.assertIn("works.", srt)

    def test_cancel_preserves_project_until_separate_delete(self) -> None:
        self.scheduler.shutdown(grace_seconds=0.1, timeout_seconds=5.0)
        media = wave_bytes(self.root / "cancel.wav")
        created_response = asyncio.run(self.post(media))
        self.assertEqual(created_response.status_code, 202, created_response.text)
        created = created_response.json()

        cancel = asyncio.run(self.delete(created["id"]))
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(cancel.json()["cancel_requested"], created["id"])
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with application.JOBS_LOCK:
                job = application.JOBS[created["id"]]
            application._refresh_canonical_job_projection(job)
            with application.JOBS_LOCK:
                current = job.public()
            if current["status"] == "cancelled":
                break
            time.sleep(0.02)
        self.assertEqual(current["status"], "cancelled", current)
        project = self.projects / created["id"]
        self.assertTrue(project.is_dir())
        self.assertEqual(
            self.service.get_task(created["transcription_task_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.service.get_task(created["segmentation_task_id"])["state"],
            "cancelled",
        )

        deleted = asyncio.run(self.delete(created["id"]))
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(project.exists())

    def test_delete_reconciles_a_stale_running_projection(self) -> None:
        media = wave_bytes(self.root / "stale-delete.wav")
        created_response = asyncio.run(self.post(media))
        self.assertEqual(created_response.status_code, 202, created_response.text)
        job_id = created_response.json()["id"]
        current = self.wait_job(job_id)
        self.assertEqual(current["status"], "awaiting_edit", current)

        with application.JOBS_LOCK:
            application.JOBS[job_id].status = "running"
            application._persist_job(application.JOBS[job_id])

        deleted = asyncio.run(self.delete(job_id))
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted"], job_id)
        self.assertFalse((self.projects / job_id).exists())

    def test_batch_idempotency_binds_order_members_and_settings(self) -> None:
        first_media = wave_bytes(self.root / "batch-a.wav")
        second_media = wave_bytes(self.root / "batch-b.wav")
        third_media = wave_bytes(self.root / "batch-c.wav")
        original = [("a.wav", first_media), ("b.wav", second_media)]

        first = asyncio.run(self.post_batch(original, key="batch-submission"))
        replay = asyncio.run(self.post_batch(original, key="batch-submission"))
        settings_changed = asyncio.run(
            self.post_batch(original, key="batch-submission", language="zh")
        )
        reordered = asyncio.run(
            self.post_batch(list(reversed(original)), key="batch-submission")
        )
        appended = asyncio.run(
            self.post_batch(
                [*original, ("c.wav", third_media)], key="batch-submission"
            )
        )
        removed = asyncio.run(
            self.post_batch(original[:1], key="batch-submission")
        )

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        for conflict in (settings_changed, reordered, appended, removed):
            self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(len(self.service.list_tasks(task_type="transcription")), 2)

    def test_restart_uses_editor_readiness_and_preserves_cancel_intent(self) -> None:
        media = wave_bytes(self.root / "restart.wav")
        created_response = asyncio.run(self.post(media))
        self.assertEqual(created_response.status_code, 202, created_response.text)
        job_id = created_response.json()["id"]
        current = self.wait_job(job_id)
        self.assertEqual(current["status"], "awaiting_edit", current)

        with application.JOBS_LOCK:
            job = application.JOBS[job_id]
            job.status = "running"
            application._persist_job(job)
            application.JOBS.clear()
        application._restore_persisted_jobs(job_id)
        with application.JOBS_LOCK:
            restored = application.JOBS[job_id]
            self.assertEqual(restored.status, "awaiting_edit")
            restored.status = "running"
            restored.cancel_requested = True
            application._persist_job(restored)
            application.JOBS.clear()

        application._restore_persisted_jobs(job_id)
        with application.JOBS_LOCK:
            cancelled = application.JOBS[job_id]
            self.assertEqual(cancelled.status, "cancelled")
            self.assertTrue(cancelled.cancel_requested)
            cancelled.cancel_requested = False
            cancelled.status = "running"
            application._persist_job(cancelled)
            application.JOBS.clear()

        (self.projects / job_id / "project" / "manifest.json").write_text(
            "{}", encoding="utf-8"
        )
        application._restore_persisted_jobs(job_id)
        with application.JOBS_LOCK:
            self.assertEqual(application.JOBS[job_id].status, "failed")
            self.assertIn("不可读", application.JOBS[job_id].error)

    def test_removed_media_creation_route_is_not_registered(self) -> None:
        async def post_legacy() -> httpx.Response:
            transport = httpx.ASGITransport(app=application.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://substar.test"
            ) as client:
                return await client.post(
                    "/api/jobs",
                    params={
                        "filename": "legacy.wav",
                        "workflow_mode": "whisper_native",
                    },
                    content=b"legacy-media",
                )

        response = asyncio.run(post_legacy())

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.service.list_tasks(task_type="transcription"), [])


if __name__ == "__main__":
    unittest.main()
