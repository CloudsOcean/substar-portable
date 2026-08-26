from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from substar_core.runtime.supervisor import (
    WorkerAlreadyRunningError,
    WorkerEvent,
    WorkerSupervisor,
)
from substar_core.runtime.worker_protocol import (
    WORKER_COMMAND_SCHEMA,
    WORKER_MESSAGE_SCHEMA,
    WorkerCommand,
    WorkerMessage,
    WorkerMessageType,
    WorkerPaths,
    WorkerProtocolError,
    WorkerTaskType,
    parse_command_line,
    parse_message_line,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "runtime_test_worker.py"


class WorkerProtocolTests(unittest.TestCase):
    def test_frozen_command_and_message_round_trip(self) -> None:
        command = WorkerCommand(
            task_id="task-1",
            attempt=2,
            task_type=WorkerTaskType.SEGMENTATION,
            project_id="project-1",
            input_schema="substar.segmentation-input.v1",
            input={"source": "alignment.json"},
            paths=WorkerPaths(
                project_root="project",
                work_directory="work",
                artifact_directory="artifacts",
            ),
            credential_refs=("llm",),
            deadline_at="2026-08-16T12:00:00+00:00",
            trace_context={"trace_id": "trace-1"},
        )
        raw = command.to_dict()
        self.assertEqual(raw["schema_version"], WORKER_COMMAND_SCHEMA)
        self.assertEqual(parse_command_line(command.to_json_line()), command)

        message = WorkerMessage(
            task_id="task-1",
            attempt=2,
            sequence=1,
            message_type=WorkerMessageType.PROGRESS,
            occurred_at=utc_now(),
            progress=0.25,
            step="segmentation.blocks",
            data={"message": "working"},
        )
        self.assertEqual(message.to_dict()["schema_version"], WORKER_MESSAGE_SCHEMA)
        self.assertEqual(parse_message_line(message.to_json_line()), message)

    def test_unknown_or_extended_message_is_rejected(self) -> None:
        value = WorkerMessage(
            task_id="task-1",
            attempt=1,
            sequence=1,
            message_type=WorkerMessageType.NOTICE,
            occurred_at=utc_now(),
            data={},
        ).to_dict()
        value["unexpected"] = True
        with self.assertRaises(WorkerProtocolError):
            parse_message_line(json.dumps(value))

    def test_schema_types_are_not_coerced(self) -> None:
        command = WorkerCommand(
            task_id="task-1",
            attempt=1,
            task_type=WorkerTaskType.EXPORT,
            input_schema="substar.test.v1",
            input={},
            paths=WorkerPaths(work_directory="work", artifact_directory="artifacts"),
        ).to_dict()
        command["task_id"] = 123
        with self.assertRaises(WorkerProtocolError):
            parse_command_line(json.dumps(command))

        message = WorkerMessage(
            task_id="task-1",
            attempt=1,
            sequence=1,
            message_type=WorkerMessageType.NOTICE,
            occurred_at=utc_now(),
            data={},
        ).to_dict()
        message["occurred_at"] = 123
        with self.assertRaises(WorkerProtocolError):
            parse_message_line(json.dumps(message))

        command["task_id"] = "task-1"
        command["input"] = None
        with self.assertRaises(WorkerProtocolError):
            parse_command_line(json.dumps(command))

        with self.assertRaises(WorkerProtocolError):
            parse_message_line("\n")


class WorkerSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.log_directory = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, task_id: str, attempt: int, mode: str) -> WorkerCommand:
        return WorkerCommand(
            task_id=task_id,
            attempt=attempt,
            task_type=WorkerTaskType.EXPORT,
            input_schema="substar.test-worker-input.v1",
            input={"mode": mode},
            paths=WorkerPaths(
                project_root=None,
                work_directory=str(self.log_directory),
                artifact_directory=str(self.log_directory / "artifacts"),
            ),
        )

    def start(
        self,
        supervisor: WorkerSupervisor,
        task_id: str,
        mode: str,
        *,
        attempt: int = 1,
        timeout_seconds: float | None = None,
        events: list[WorkerEvent] | None = None,
    ):
        return supervisor.start(
            task_id,
            attempt,
            [sys.executable, str(FIXTURE)],
            (events.append if events is not None else None),
            worker_command=self.command(task_id, attempt, mode),
            timeout_seconds=timeout_seconds,
            cwd=ROOT,
        )

    @staticmethod
    def process_running(pid: int) -> bool:
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        stat = Path(f"/proc/{pid}/stat")
        if stat.is_file():
            try:
                return stat.read_text(encoding="utf-8").split()[2] != "Z"
            except (OSError, IndexError):
                pass
        return True

    def test_success_routes_messages_and_persists_stderr(self) -> None:
        events: list[WorkerEvent] = []
        supervisor = WorkerSupervisor(log_directory=self.log_directory)
        handle = self.start(supervisor, "success-task", "success", events=events)
        completion = handle.wait(timeout=5)

        self.assertEqual(completion.status, "succeeded")
        self.assertEqual(completion.result, {"value": 42})
        self.assertFalse(supervisor.running())
        message_types = [
            event.message.message_type
            for event in events
            if event.message is not None
        ]
        self.assertEqual(
            message_types,
            [
                WorkerMessageType.READY,
                WorkerMessageType.PROGRESS,
                WorkerMessageType.RESULT,
            ],
        )
        self.assertIsNotNone(completion.stderr_log_path)
        self.assertIn(
            "fixture diagnostic",
            Path(str(completion.stderr_log_path)).read_text(encoding="utf-8"),
        )

    def test_initial_command_failure_terminates_reaps_and_closes_child(self) -> None:
        spawned = []

        def capture_popen(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            spawned.append(process)
            return process

        supervisor = WorkerSupervisor(
            log_directory=self.log_directory,
            popen_factory=capture_popen,
            poll_interval=0.01,
        )
        with (
            patch.object(
                supervisor, "_write_command", side_effect=BrokenPipeError("closed")
            ),
            self.assertRaises(BrokenPipeError),
        ):
            self.start(supervisor, "failed-start-task", "success")

        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        self.assertTrue(spawned[0].stdin.closed)
        self.assertTrue(spawned[0].stdout.closed)
        self.assertTrue(spawned[0].stderr.closed)
        self.assertFalse(supervisor.running())

    def test_single_process_ownership_and_cooperative_cancel(self) -> None:
        supervisor = WorkerSupervisor(
            log_directory=self.log_directory, poll_interval=0.01
        )
        handle = self.start(supervisor, "cancel-task", "wait_cancel")
        with self.assertRaises(WorkerAlreadyRunningError):
            self.start(supervisor, "other-task", "success")
        self.assertTrue(handle.cancel(grace_seconds=1.0))
        completion = handle.wait(timeout=5)

        self.assertEqual(completion.status, "cancelled")
        self.assertTrue(completion.cancellation_requested)
        self.assertFalse(completion.forced)

    def test_timeout_force_kills_uncooperative_worker(self) -> None:
        supervisor = WorkerSupervisor(
            log_directory=self.log_directory,
            poll_interval=0.01,
            timeout_grace_seconds=0.05,
        )
        handle = self.start(
            supervisor,
            "timeout-task",
            "ignore_cancel",
            timeout_seconds=0.1,
        )
        completion = handle.wait(timeout=8)

        self.assertEqual(completion.status, "timed_out")
        self.assertTrue(completion.timed_out)
        self.assertTrue(completion.forced)
        self.assertFalse(handle.running())

    def test_force_kill_failure_retries_without_losing_process_ownership(self) -> None:
        calls = 0

        def flaky_killer(process, _job) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated kill failure")
            process.kill()
            return True

        supervisor = WorkerSupervisor(
            log_directory=self.log_directory,
            poll_interval=0.01,
            timeout_grace_seconds=0.0,
            tree_killer=flaky_killer,
        )
        handle = self.start(
            supervisor,
            "kill-retry-task",
            "ignore_cancel",
            timeout_seconds=0.05,
        )
        completion = handle.wait(timeout=8)

        self.assertGreaterEqual(calls, 2)
        self.assertEqual(completion.status, "timed_out")
        self.assertFalse(supervisor.running())

    def test_invalid_stdout_is_a_protocol_failure(self) -> None:
        events: list[WorkerEvent] = []
        supervisor = WorkerSupervisor(log_directory=self.log_directory)
        completion = self.start(
            supervisor,
            "invalid-task",
            "invalid_stdout",
            events=events,
        ).wait(timeout=5)

        self.assertEqual(completion.status, "failed")
        self.assertTrue(completion.protocol_errors)
        self.assertTrue(any(event.kind == "protocol_error" for event in events))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object ownership test")
    def test_root_exit_terminates_descendants_before_completion(self) -> None:
        supervisor = WorkerSupervisor(
            log_directory=self.log_directory,
            poll_interval=0.01,
        )
        completion = self.start(
            supervisor,
            "descendant-task",
            "spawn_descendant",
        ).wait(timeout=8)

        self.assertEqual(completion.status, "succeeded")
        child_pid = int(completion.result["child_pid"])
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, child_pid)
        if handle:
            exit_code = ctypes.c_ulong()
            self.assertTrue(
                ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                )
            )
            ctypes.windll.kernel32.CloseHandle(handle)
            self.assertNotEqual(exit_code.value, 259)

    def test_redirected_descendant_is_settled_even_after_reader_eof(self) -> None:
        supervisor = WorkerSupervisor(
            log_directory=self.log_directory,
            poll_interval=0.01,
        )
        completion = self.start(
            supervisor,
            "redirected-descendant-task",
            "spawn_descendant_redirected",
        ).wait(timeout=8)

        self.assertEqual(completion.status, "succeeded")
        child_pid = int(completion.result["child_pid"])
        deadline = time.monotonic() + 2.0
        while self.process_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.process_running(child_pid))

    def test_unconfirmed_residual_tree_cleanup_retains_ownership(self) -> None:
        cleanup_attempted = threading.Event()

        def reject_cleanup(_process, _job) -> bool:
            cleanup_attempted.set()
            return False

        supervisor = WorkerSupervisor(
            log_directory=self.log_directory,
            poll_interval=0.01,
            tree_killer=reject_cleanup,
        )
        handle = self.start(supervisor, "unsettled-tree-task", "success")

        self.assertTrue(cleanup_attempted.wait(3.0))
        with self.assertRaises(TimeoutError):
            handle.wait(timeout=0.05)
        self.assertTrue(supervisor.running())

        supervisor._tree_killer = lambda _process, _job: True
        completion = handle.wait(timeout=3.0)
        self.assertEqual(completion.status, "failed")
        self.assertFalse(supervisor.running())

    def test_exit_callback_is_delivered_once(self) -> None:
        completions = []
        delivered = threading.Event()

        def on_exit(value) -> None:
            completions.append(value)
            delivered.set()

        supervisor = WorkerSupervisor(log_directory=self.log_directory)
        handle = supervisor.start(
            "callback-task",
            1,
            [sys.executable, str(FIXTURE)],
            None,
            on_exit,
            worker_command=self.command("callback-task", 1, "fail"),
            cwd=ROOT,
        )
        completion = handle.wait(timeout=5)
        self.assertTrue(delivered.wait(1))
        self.assertEqual(completion.status, "failed")
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].returncode, 7)

    def test_exit_callback_can_observe_the_published_completion(self) -> None:
        observed = []
        supervisor = WorkerSupervisor(log_directory=self.log_directory)

        def on_exit(value) -> None:
            observed.append(
                supervisor.wait(value.task_id, attempt=value.attempt, timeout=0.1)
            )

        handle = supervisor.start(
            "reentrant-callback-task",
            1,
            [sys.executable, str(FIXTURE)],
            None,
            on_exit,
            worker_command=self.command("reentrant-callback-task", 1, "success"),
            cwd=ROOT,
        )
        completion = handle.wait(timeout=5)

        self.assertEqual(observed, [completion])


if __name__ == "__main__":
    unittest.main()
