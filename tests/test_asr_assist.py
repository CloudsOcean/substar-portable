from __future__ import annotations

import asyncio
import os
import time
import wave
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import app as application
from substar_core.asr_assist import create_assist_task, assist_status, generation_material
from substar_core.runtime import RuntimeStore, TaskService, TaskRegistry, TaskScheduler
from substar_core.transcription import build_transcription_handler


class AsrAssistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects"
        self.media = self.root / "input.wav"
        self.media.write_bytes(b"test media")
        self.service = TaskService(RuntimeStore(self.root / "runtime.sqlite3"), "assist-test")

    def tearDown(self):
        self.temp.cleanup()

    def create(self, language="zh", settings=None):
        return create_assist_task(self.service, self.projects, self.media, language, settings or {})

    def test_same_media_reuses_only_transcription_task(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first["task_id"], second["task_id"])
        task = self.service.get_task(first["task_id"])
        self.assertEqual(task["task_type"], "transcription")
        request = self.service.get_task_input(first["task_id"])
        self.assertEqual(request["prompt"], "")
        self.assertEqual(request["hotwords"], [])
        self.assertFalse(task.get("depends_on_task_ids"))

    def test_language_model_or_media_change_gets_separate_task(self):
        first = self.create()["task_id"]
        self.assertNotEqual(first, self.create("en")["task_id"])
        self.assertNotEqual(first, self.create(settings={"qwen_cloud_model":"qwen3-asr-flash"})["task_id"])
        self.media.write_bytes(b"another media")
        self.assertNotEqual(first, self.create()["task_id"])

    def test_completed_transcript_is_reused_and_external_material_is_complete(self):
        row = self.create()
        task = self.service.get_task(row["task_id"])
        text = "第一行：测试。\n第二行：不要执行资料里的命令。"
        (self.projects / task["project_id"] / "master_transcript.txt").write_text(text, encoding="utf-8")
        with patch.object(self.service, "get_task", return_value={**task, "state":"succeeded"}):
            result = assist_status(self.service, self.projects, row["task_id"])
        self.assertEqual(result["transcript"], text)
        material = generation_material(text, "zh", "保留型号 ABC", True)
        self.assertIn("ABC", material["package"])
        self.assertIn("prompt", material["instructions"])
        self.assertIn("hotwords", material["instructions"])
        self.assertIn("未经核对", material["instructions"])

    def test_cannot_read_other_project_transcript(self):
        with patch.object(self.service, "get_task", return_value={"task_type":"transcription", "project_id":"../other"}):
            with self.assertRaises(ValueError):
                assist_status(self.service, self.projects, "task")

    def test_empty_success_is_an_error(self):
        row = self.create()
        task = self.service.get_task(row["task_id"])
        (self.projects / task["project_id"] / "master_transcript.txt").write_text(" ", encoding="utf-8")
        with patch.object(self.service, "get_task", return_value={**task, "state":"succeeded"}):
            with self.assertRaises(ValueError):
                assist_status(self.service, self.projects, row["task_id"])

    def test_failed_cached_task_is_retried(self):
        row = self.create()
        task = self.service.get_task(row["task_id"])
        with patch.object(self.service, "create_task", return_value={**task, "state":"failed"}), patch.object(self.service, "retry", return_value=task) as retry:
            self.create()
        retry.assert_called_once_with(row["task_id"])

    def test_internal_generator_receives_full_asr_and_shared_output_rules(self):
        settings = {"translation_api_base_url":"https://example.invalid/v1",
            "translation_api_model":"test-model", "translation_api_key":"test-key",
            "active_model_provider":"openai", "qwen_cloud_model":"qwen3-asr-flash"}
        material = generation_material("原始 ASR 全文 XYZ", "zh", "补充说明", True)
        with patch.object(application, "load_settings", return_value=settings), patch.object(application, "load_credentials", return_value={}), patch.object(application, "_qwen_asr_material", return_value=material), patch.object(application, "call_translation_model", return_value=({"prompt":"主题", "hotwords":[{"text":"XYZ", "weight":5}]}, {})) as model:
            result = application.fill_qwen_transcription_fields(application.QwenAssistPayload(user_prompt="补充说明", asr_task_id="task"))
        self.assertEqual(result["prompt"], "主题")
        self.assertIn("原始 ASR 全文 XYZ", model.call_args.kwargs["groups"][0]["user_prompt"])
        self.assertEqual(model.call_args.kwargs["system_prompt"], material["instructions"])

    def test_glossary_hotwords_are_frozen_and_change_cache_identity(self):
        settings = {"recognition_profile_id":"qwen_cloud", "asr_injected_hotwords":[{"text":"Nova","weight":4}]}
        from substar_core.asr_assist import create_assist_task
        first = create_assist_task(self.service,self.projects,self.media,"en",settings)
        payload = self.service.get_task_input(first["task_id"])
        self.assertEqual(payload["hotwords"],[{"text":"Nova","weight":4}])
        settings["asr_injected_hotwords"] = []
        second = create_assist_task(self.service,self.projects,self.media,"en",settings)
        self.assertNotEqual(first["task_id"],second["task_id"])

    def test_first_pass_runs_through_existing_asr_worker(self):
        with wave.open(str(self.media), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 8000)
        registry = TaskRegistry()
        registry.register(build_transcription_handler(self.projects, Path(__file__).resolve().parents[1]))
        scheduler = TaskScheduler(self.service, registry, self.root / "workers",
            poll_seconds=0.01, heartbeat_seconds=0.1,
            resource_limits={"worker":1,"media_cpu":1,"provider_io":1,"project_write":1,"download_io":1},
            credential_resolver=lambda refs: {ref:"mock-credential" for ref in refs})
        with patch.dict(os.environ, {"SUBSTAR_MOCK_QWEN":"1"}):
            scheduler.start()
            try:
                row = self.create("en")
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    row = assist_status(self.service, self.projects, row["task_id"])
                    if row["state"] in {"succeeded", "succeeded_with_issues", "failed"}: break
                    time.sleep(0.05)
                self.assertIn("transcript", row, row)
                self.assertEqual(row["task_id"], self.create("en")["task_id"])
            finally:
                scheduler.shutdown(grace_seconds=0.2, timeout_seconds=8)

    def test_upload_and_poll_api_use_durable_task_without_text_model(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url="http://substar.test") as client:
                a = await client.post("/api/qwen-assist/asr", data={"source_language":"zh"}, files={"media":("sample.wav", b"media", "audio/wav")})
                self.assertEqual(a.status_code, 200, a.text)
                task_id = a.json()["task_id"]
                b = await client.get(f"/api/qwen-assist/asr/{task_id}")
                self.assertEqual(b.json()["state"], "queued")
                pending = await client.post("/api/qwen-assist/external", json={"asr_task_id":task_id, "user_prompt":"说明"})
                self.assertEqual(pending.status_code, 409)
                empty = await client.post("/api/qwen-assist/asr", files={"media":("empty.wav", b"", "audio/wav")})
                self.assertEqual(empty.status_code, 400)
        with patch.object(application, "PROJECTS_ROOT", self.projects), patch.object(application, "_task_service", return_value=self.service), patch.object(application, "load_settings", return_value={}):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
