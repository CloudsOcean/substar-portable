from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from substar_core.runtime import (
    RuntimeStore,
    TaskHandler,
    TaskRegistry,
    TaskScheduler,
    TaskService,
    WorkerLaunch,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "runtime_test_worker.py"


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RuntimeStore(self.root / "runtime.sqlite3")
        self.service = TaskService(self.store, "scheduler-instance")
        self.registry = TaskRegistry()
        self.scheduler: TaskScheduler | None = None

    def tearDown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(grace_seconds=0.1)
        self.temporary.cleanup()

    def register_fixture(self, mode: str) -> None:
        self.registry.register(
            TaskHandler(
                task_type="export",
                prepare=lambda _context: WorkerLaunch(
                    argv=(sys.executable, str(FIXTURE)),
                    cwd=ROOT,
                    worker_input={"mode": mode},
                    timeout_seconds=10.0,
                ),
                finalize=lambda _context, completion: dict(
                    completion.result or {}
                ),
            )
        )

    def start_scheduler(self) -> None:
        self.scheduler = TaskScheduler(
            self.service,
            self.registry,
            self.root / "tasks",
            poll_seconds=0.01,
            lease_seconds=2.0,
            heartbeat_seconds=0.1,
            cancel_grace_seconds=0.1,
        )
        self.scheduler.start()

    def create_task(self) -> dict:
        return self.service.create_task(
            task_type="export",
            input_schema="substar.test-worker-input.v1",
            input_payload={},
            project_id="project-1",
        )

    def wait_for_state(
        self, task_id: str, states: set[str], timeout: float = 8.0
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.service.get_task(task_id)
            if task["state"] in states:
                return task
            time.sleep(0.01)
        self.fail(
            f"task {task_id} did not reach {sorted(states)}; "
            f"last={self.service.get_task(task_id)}"
        )

    def test_dispatch_persists_worker_identity_progress_and_result(self) -> None:
        self.register_fixture("success")
        task = self.create_task()
        self.start_scheduler()

        completed = self.wait_for_state(task["task_id"], {"succeeded"})

        self.assertEqual(completed["result"], {"value": 42})
        self.assertEqual(completed["progress"], 1.0)
        attempt = self.store.attempt_rows(task["task_id"])[0]
        self.assertGreater(int(attempt["worker_pid"]), 0)
        self.assertTrue(Path(attempt["stderr_log"]).is_file())
        event_types = [
            event["event_type"]
            for event in self.service.events(task_id=task["task_id"])
        ]
        self.assertEqual(
            event_types,
            ["task.created", "task.started", "task.progress", "task.succeeded"],
        )

    def test_persisted_cancel_request_reaches_the_worker(self) -> None:
        self.register_fixture("wait_cancel")
        task = self.create_task()
        self.start_scheduler()
        self.wait_for_state(task["task_id"], {"running"})

        self.service.request_cancel(task["task_id"])
        cancelled = self.wait_for_state(task["task_id"], {"cancelled"})

        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIn(
            "task.cancel_requested",
            [
                event["event_type"]
                for event in self.service.events(task_id=task["task_id"])
            ],
        )

    def test_worker_error_text_is_not_persisted_in_the_public_task(self) -> None:
        self.register_fixture("fail_sensitive")
        task = self.create_task()
        self.start_scheduler()

        failed = self.wait_for_state(task["task_id"], {"failed"})
        serialized = str(failed)

        self.assertEqual(failed["error"]["code"], "worker_failed")
        self.assertEqual(failed["error"]["message"], "Worker execution failed.")
        self.assertNotIn("must-not-be-public", serialized)
        self.assertNotIn("C:/private/path", serialized)

    def test_explicit_public_worker_error_is_projected_with_log_tail(self) -> None:
        self.register_fixture("fail_public")
        task = self.create_task()
        self.start_scheduler()

        failed = self.wait_for_state(task["task_id"], {"failed"})

        self.assertIn("unrecognized arguments", failed["error"]["message"])
        self.assertEqual(
            failed["error"]["details"]["algorithm_stderr_tail"],
            "error: unrecognized arguments: segmentation_material.md",
        )

    def test_attach_failure_keeps_worker_owned_until_terminal_cleanup(self) -> None:
        self.register_fixture("ignore_cancel")
        task = self.create_task()
        with patch.object(
            self.service,
            "attach_worker",
            side_effect=RuntimeError("private attach failure detail"),
        ):
            self.start_scheduler()
            failed = self.wait_for_state(task["task_id"], {"failed"})

        self.assertEqual(failed["error"]["code"], "worker_start_failed")
        self.assertNotIn("private attach failure detail", str(failed))
        assert self.scheduler is not None
        deadline = time.monotonic() + 2.0
        while self.scheduler.snapshot()["active"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.scheduler.snapshot()["active"], {})
        self.assertEqual(
            self.scheduler.snapshot()["resources"]["worker"]["in_use"], 0
        )

    def test_shutdown_interrupts_active_work_for_explicit_retry(self) -> None:
        self.register_fixture("ignore_cancel")
        task = self.create_task()
        self.start_scheduler()
        self.wait_for_state(task["task_id"], {"running"})

        self.scheduler.shutdown(grace_seconds=0.05)
        interrupted = self.service.get_task(task["task_id"])

        self.assertEqual(interrupted["state"], "interrupted")
        self.assertIsNotNone(interrupted["links"]["retry"])

    def test_shutdown_during_prepare_cannot_start_a_late_worker(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def prepare(_context):
            entered.set()
            release.wait(5.0)
            return WorkerLaunch(
                argv=(sys.executable, str(FIXTURE)),
                cwd=ROOT,
                worker_input={"mode": "success"},
            )

        self.registry.register(TaskHandler(task_type="export", prepare=prepare))
        task = self.create_task()
        self.start_scheduler()
        self.assertTrue(entered.wait(3.0))

        failures = []

        def stop_scheduler() -> None:
            try:
                assert self.scheduler is not None
                self.scheduler.shutdown(grace_seconds=0.1, timeout_seconds=3.0)
            except Exception as exc:
                failures.append(exc)

        stopper = threading.Thread(target=stop_scheduler)
        stopper.start()
        time.sleep(0.05)
        release.set()
        stopper.join(5.0)

        self.assertFalse(stopper.is_alive())
        self.assertEqual(failures, [])
        interrupted = self.service.get_task(task["task_id"])
        self.assertEqual(interrupted["state"], "interrupted")
        attempt = self.store.attempt_rows(task["task_id"])[0]
        self.assertIsNone(attempt["worker_pid"])

    def test_shutdown_reports_a_scheduler_thread_stuck_in_finalization(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def finalize(_context, completion):
            entered.set()
            release.wait(5.0)
            return dict(completion.result or {})

        self.registry.register(
            TaskHandler(
                task_type="export",
                prepare=lambda _context: WorkerLaunch(
                    argv=(sys.executable, str(FIXTURE)),
                    cwd=ROOT,
                    worker_input={"mode": "success"},
                ),
                finalize=finalize,
            )
        )
        task = self.create_task()
        self.start_scheduler()
        self.assertTrue(entered.wait(5.0))

        assert self.scheduler is not None
        with self.assertRaisesRegex(RuntimeError, "scheduler thread did not stop"):
            self.scheduler.shutdown(grace_seconds=0.0, timeout_seconds=0.2)
        self.assertEqual(self.service.get_task(task["task_id"])["state"], "running")

        release.set()
        completed = self.wait_for_state(task["task_id"], {"succeeded"})
        self.assertEqual(completed["result"], {"value": 42})

    def test_shutdown_finalizes_a_worker_that_already_completed(self) -> None:
        self.register_fixture("success")
        task = self.create_task()
        self.scheduler = TaskScheduler(
            self.service,
            self.registry,
            self.root / "tasks",
            poll_seconds=1.0,
            lease_seconds=2.0,
            heartbeat_seconds=0.1,
        )
        self.scheduler.start()
        self.wait_for_state(task["task_id"], {"running"})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with self.scheduler._lock:
                execution = self.scheduler._executions.get((task["task_id"], 1))
            if execution is not None:
                execution.handle.wait(timeout=5.0)
                break
            time.sleep(0.01)
        else:
            self.fail("scheduler did not publish the worker handle")

        self.scheduler.shutdown(grace_seconds=0.05)
        completed = self.service.get_task(task["task_id"])

        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(completed["result"], {"value": 42})

    def test_named_worker_limit_keeps_excess_work_durably_queued(self) -> None:
        self.register_fixture("wait_cancel")
        first = self.create_task()
        second = self.create_task()
        self.start_scheduler()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            tasks = [
                self.service.get_task(first["task_id"]),
                self.service.get_task(second["task_id"]),
            ]
            running = [task for task in tasks if task["state"] == "running"]
            queued = [task for task in tasks if task["state"] == "queued"]
            if len(running) == len(queued) == 1:
                break
            time.sleep(0.01)
        else:
            self.fail(f"resource limit was not enforced: {tasks}")

        self.service.request_cancel(running[0]["task_id"])
        self.wait_for_state(running[0]["task_id"], {"cancelled"})
        promoted = self.wait_for_state(queued[0]["task_id"], {"running"})
        self.assertEqual(promoted["state"], "running")
        self.service.request_cancel(promoted["task_id"])
        self.wait_for_state(promoted["task_id"], {"cancelled"})

    def test_worker_artifact_is_verified_against_the_actual_attempt_file(self) -> None:
        self.register_fixture("artifact_success")
        task = self.create_task()
        self.start_scheduler()

        completed = self.wait_for_state(task["task_id"], {"succeeded"})
        artifacts = self.service.list_artifacts(task["task_id"])

        self.assertEqual(completed["result"], {"value": 42})
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["relative_path"], "artifacts/result.json")
        actual = (
            self.root
            / "tasks"
            / task["task_id"]
            / "attempts"
            / "1"
            / artifacts[0]["relative_path"]
        )
        self.assertTrue(actual.is_file())

    def test_false_worker_artifact_checksum_fails_the_attempt(self) -> None:
        self.register_fixture("artifact_bad_digest")
        task = self.create_task()
        self.start_scheduler()

        failed = self.wait_for_state(task["task_id"], {"failed"})

        self.assertEqual(failed["error"]["code"], "worker_event_invalid")
        self.assertEqual(self.service.list_artifacts(task["task_id"]), [])

    def test_artifact_changed_after_registration_cannot_commit_success(self) -> None:
        self.register_fixture("artifact_mutated")
        task = self.create_task()
        self.start_scheduler()

        failed = self.wait_for_state(task["task_id"], {"failed"})

        self.assertEqual(failed["error"]["code"], "worker_event_invalid")
        self.assertEqual(len(self.service.list_artifacts(task["task_id"])), 1)

    def test_result_cannot_claim_an_artifact_without_registration_event(self) -> None:
        self.register_fixture("artifact_result_without_event")
        task = self.create_task()
        self.start_scheduler()

        failed = self.wait_for_state(task["task_id"], {"failed"})

        self.assertEqual(failed["error"]["code"], "worker_event_invalid")
        self.assertEqual(self.service.list_artifacts(task["task_id"]), [])

    def test_worker_receives_only_explicit_credential_authority(self) -> None:
        resolved: list[tuple[str, ...]] = []
        self.registry.register(
            TaskHandler(
                task_type="export",
                prepare=lambda _context: WorkerLaunch(
                    argv=(sys.executable, str(FIXTURE)),
                    cwd=ROOT,
                    worker_input={"mode": "credential_scope"},
                    credential_refs=("qwen_cloud",),
                ),
                finalize=lambda _context, completion: dict(
                    completion.result or {}
                ),
            )
        )
        task = self.create_task()
        self.scheduler = TaskScheduler(
            self.service,
            self.registry,
            self.root / "tasks",
            poll_seconds=0.01,
            lease_seconds=2.0,
            heartbeat_seconds=0.1,
            credential_resolver=lambda refs: (
                resolved.append(refs) or {"qwen_cloud": "temporary-secret"}
            ),
        )
        self.scheduler.start()

        completed = self.wait_for_state(task["task_id"], {"succeeded"})

        self.assertEqual(resolved, [("qwen_cloud",)])
        self.assertEqual(
            completed["result"],
            {"granted": True, "unrelated_present": False},
        )
        self.assertNotIn("temporary-secret", str(completed))

    def test_missing_worker_credential_has_a_configuration_error(self) -> None:
        self.registry.register(
            TaskHandler(
                task_type="export",
                prepare=lambda _context: WorkerLaunch(
                    argv=(sys.executable, str(FIXTURE)),
                    cwd=ROOT,
                    worker_input={"mode": "success"},
                    credential_refs=("asr_qwen",),
                ),
                finalize=lambda _context, completion: dict(completion.result or {}),
            )
        )
        task = self.create_task()
        self.scheduler = TaskScheduler(
            self.service,
            self.registry,
            self.root / "tasks",
            poll_seconds=0.01,
            lease_seconds=2.0,
            heartbeat_seconds=0.1,
            credential_resolver=lambda refs: {reference: "" for reference in refs},
        )
        self.scheduler.start()

        failed = self.wait_for_state(task["task_id"], {"failed"})

        self.assertEqual(failed["error"]["code"], "credential_unavailable")
        self.assertEqual(failed["error"]["category"], "configuration")
        self.assertEqual(failed["error"]["details"], {"credential_ref": "asr_qwen"})

    def test_retry_isolates_artifacts_by_attempt(self) -> None:
        self.registry.register(
            TaskHandler(
                task_type="export",
                prepare=lambda context: WorkerLaunch(
                    argv=(sys.executable, str(FIXTURE)),
                    cwd=ROOT,
                    worker_input={
                        "mode": (
                            "artifact_then_error"
                            if int(context.task["attempt"]) == 1
                            else "artifact_success"
                        )
                    },
                ),
                finalize=lambda _context, completion: dict(
                    completion.result or {}
                ),
            )
        )
        task = self.create_task()
        self.start_scheduler()
        self.wait_for_state(task["task_id"], {"failed"})

        retried = self.service.retry(task["task_id"])
        self.assertEqual(retried["attempt"], 2)
        succeeded = self.wait_for_state(task["task_id"], {"succeeded"})
        artifacts = self.service.list_artifacts(task["task_id"])

        self.assertEqual(succeeded["result"], {"value": 42})
        self.assertEqual([artifact["attempt"] for artifact in artifacts], [1, 2])
        self.assertEqual(
            [artifact["relative_path"] for artifact in artifacts],
            ["artifacts/result.json", "artifacts/result.json"],
        )


if __name__ == "__main__":
    unittest.main()
