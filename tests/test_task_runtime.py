from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from substar_core.runtime import (
    IdempotencyConflictError,
    InvalidTaskError,
    RuntimeStore,
    TaskNotFoundError,
    TaskOwnershipError,
    TaskService,
    TaskStateConflictError,
)


class TaskRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.database)
        self.service = TaskService(self.store, "instance-a")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_task(self, **overrides: object) -> dict:
        values = {
            "task_type": "transcription",
            "input_schema": "substar.transcription-input.v1",
            "input_payload": {"media_id": "media-1", "language": "en"},
            "project_id": "project-1",
        }
        values.update(overrides)
        return self.service.create_task(**values)

    def test_migration_creates_frozen_runtime_tables(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(version, 3)
        self.assertEqual(journal.lower(), "wal")
        self.assertTrue(
            {
                "tasks",
                "task_attempts",
                "task_dependencies",
                "task_events",
                "task_artifacts",
            }.issubset(tables)
        )

    def test_legacy_runtime_database_is_rejected(self) -> None:
        legacy = Path(self.temporary.name) / "legacy-runtime.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations VALUES (1, 'initial_task_runtime', 'old');
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    attempt INTEGER NOT NULL
                );
                INSERT INTO tasks VALUES ('legacy-task', 2);
                CREATE TABLE task_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    project_id TEXT,
                    artifact_type TEXT NOT NULL,
                    schema_version TEXT,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                    created_at TEXT NOT NULL,
                    metadata_json TEXT,
                    UNIQUE (task_id, relative_path)
                );
                CREATE INDEX task_artifacts_task_idx
                    ON task_artifacts(task_id, created_at);
                INSERT INTO task_artifacts VALUES (
                    'legacy-artifact', 'legacy-task', 'project-1', 'test_result',
                    'substar.test-result.v1', 'artifacts/result.json',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    42, '2026-08-16T00:00:00Z', NULL
                );
                PRAGMA user_version=1;
                """
            )

        with self.assertRaisesRegex(InvalidTaskError, "legacy runtime database"):
            RuntimeStore(legacy)

    def test_create_and_idempotent_replay_return_schema_projection(self) -> None:
        first = self.create_task(
            idempotency_key="upload-action-1", request_id="req-create-1"
        )
        replay = self.create_task(idempotency_key="upload-action-1")

        self.assertEqual(first, replay)
        self.assertEqual(first["schema_version"], "substar.task.v2")
        self.assertEqual(first["state"], "queued")
        self.assertEqual(first["attempt"], 0)
        self.assertEqual(first["progress"], 0.0)
        self.assertEqual(first["links"]["self"], f"/api/tasks/{first['task_id']}")
        events = self.service.events(task_id=first["task_id"])
        self.assertEqual([event["event_type"] for event in events], ["task.created"])
        self.assertEqual(events[0]["request_id"], "req-create-1")

        with self.assertRaises(IdempotencyConflictError):
            self.create_task(
                idempotency_key="upload-action-1",
                input_payload={"media_id": "different", "language": "en"},
            )

    def test_claim_progress_complete_are_owned_atomic_transitions(self) -> None:
        created = self.create_task()
        claimed = self.service.claim_next({"transcription"}, lease_seconds=30)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["task_id"], created["task_id"])
        self.assertEqual(claimed["state"], "running")
        self.assertEqual(claimed["attempt"], 1)

        progressed = self.service.record_progress(
            created["task_id"],
            1,
            0.4,
            message="Recognition submitted",
            step="recognition_wait",
            wait_reason="provider_processing",
        )
        self.assertEqual(progressed["progress"], 0.4)
        self.assertEqual(progressed["step"], "recognition_wait")

        with self.assertRaises(TaskStateConflictError):
            self.service.record_progress(created["task_id"], 1, 0.3)

        completed = self.service.complete(
            created["task_id"], 1, {"evidence_artifact_id": "artifact-1"}
        )
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(completed["progress"], 1.0)
        self.assertIsNone(completed["links"]["cancel"])
        self.assertEqual(len(self.store.attempt_rows(created["task_id"])), 1)

    def test_payloads_reject_inline_secrets_and_allow_references(self) -> None:
        with self.assertRaises(InvalidTaskError):
            self.create_task(input_payload={"api_key": "must-not-persist"})
        with self.assertRaises(InvalidTaskError):
            self.create_task(input_payload={"provider": {"accessToken": "secret"}})

        created = self.create_task(
            input_payload={"credential_refs": ["provider/openai-default"]}
        )
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        self.assertEqual(claimed["task_id"], created["task_id"])
        with self.assertRaises(InvalidTaskError):
            self.service.complete(created["task_id"], 1, {"access_token": "secret"})
        completed = self.service.complete(created["task_id"], 1, {"ok": True})
        self.assertEqual(completed["state"], "succeeded")

    def test_task_can_enter_exactly_one_repair_phase(self) -> None:
        task = self.create_task()
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        repairing = self.service.enter_repair_phase(task["task_id"], 1)
        self.assertTrue(repairing["repair_phase_entered"])
        self.assertEqual(repairing["phase"], "repair")
        with self.assertRaisesRegex(TaskStateConflictError, "only once"):
            self.service.enter_repair_phase(task["task_id"], 1)

    def test_partial_success_is_terminal_and_attention_bearing(self) -> None:
        task = self.create_task()
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        completed = self.service.complete_with_issues(
            task["task_id"], 1,
            {"needs_attention": True, "problem_cue_ids": ["cue-2"]},
        )
        self.assertEqual(completed["state"], "succeeded_with_issues")
        self.assertTrue(completed["needs_attention"])
        self.assertEqual(completed["result"]["problem_cue_ids"], ["cue-2"])

    def test_finished_task_can_be_dismissed_without_touching_project_identity(self) -> None:
        created = self.create_task(project_id="project-kept")
        with self.assertRaisesRegex(TaskStateConflictError, "cancel it before deletion"):
            self.service.delete_task(created["task_id"])

        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        self.service.complete(claimed["task_id"], claimed["attempt"], {"ok": True})
        self.assertEqual(
            self.service.delete_task(created["task_id"]),
            {"deleted": created["task_id"]},
        )
        with self.assertRaisesRegex(TaskNotFoundError, "task does not exist"):
            self.service.get_task(created["task_id"])

    def test_terminal_error_requires_the_frozen_public_envelope(self) -> None:
        created = self.create_task()
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        with self.assertRaises(InvalidTaskError):
            self.service.fail(created["task_id"], 1, {"code": "unshaped"})
        self.assertEqual(self.service.get_task(created["task_id"])["state"], "running")
        failed = self.service.fail(
            created["task_id"],
            1,
            {
                "schema_version": "substar.api-error.v1",
                "code": "provider_timeout",
                "category": "provider_timeout",
                "message": "Provider timed out.",
                "retryable": True,
                "request_id": "req-terminal-envelope",
                "details": {},
            },
        )
        self.assertEqual(failed["state"], "failed")

    def test_dependencies_block_claim_until_predecessor_succeeds(self) -> None:
        predecessor = self.create_task(
            task_type="transcription", idempotency_key="predecessor"
        )
        dependent = self.create_task(
            task_type="segmentation",
            input_schema="substar.segmentation-input.v1",
            input_payload={"source": "evidence-1"},
            idempotency_key="dependent",
        )
        self.service.add_dependency(dependent["task_id"], predecessor["task_id"])

        self.assertIsNone(self.service.claim_next({"segmentation"}))
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        self.service.complete(claimed["task_id"], claimed["attempt"], {})
        claimed_dependent = self.service.claim_next({"segmentation"})
        assert claimed_dependent is not None
        self.assertEqual(claimed_dependent["task_id"], dependent["task_id"])

    def test_task_creation_can_publish_its_dependency_atomically(self) -> None:
        predecessor = self.create_task(
            task_type="transcription", idempotency_key="atomic-predecessor"
        )
        dependent = self.service.create_task(
            task_type="segmentation",
            input_schema="substar.segmentation-input.v1",
            input_payload={"source": "evidence-atomic"},
            idempotency_key="atomic-dependent",
            depends_on_task_ids=(predecessor["task_id"],),
        )

        self.assertIsNone(self.service.claim_next({"segmentation"}))
        replay = self.service.create_task(
            task_type="segmentation",
            input_schema="substar.segmentation-input.v1",
            input_payload={"source": "evidence-atomic"},
            idempotency_key="atomic-dependent",
            depends_on_task_ids=(predecessor["task_id"],),
        )
        self.assertEqual(replay["task_id"], dependent["task_id"])
        with self.assertRaisesRegex(
            IdempotencyConflictError, "different dependencies"
        ):
            self.service.create_task(
                task_type="segmentation",
                input_schema="substar.segmentation-input.v1",
                input_payload={"source": "evidence-atomic"},
                idempotency_key="atomic-dependent",
            )

    def test_cancel_request_is_persisted_for_queued_and_running_tasks(self) -> None:
        queued = self.create_task(idempotency_key="queued-cancel")
        queued_cancelled = self.service.request_cancel(queued["task_id"])
        self.assertEqual(queued_cancelled["state"], "cancelled")
        queued_events = self.service.events(task_id=queued["task_id"])
        self.assertEqual(
            [event["event_type"] for event in queued_events],
            ["task.created", "task.cancel_requested", "task.cancelled"],
        )

        running = self.create_task(idempotency_key="running-cancel")
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        self.assertEqual(claimed["task_id"], running["task_id"])
        cancelling = self.service.request_cancel(running["task_id"])
        self.assertEqual(cancelling["state"], "cancelling")
        cancelled = self.service.cancelled(running["task_id"], 1)
        self.assertEqual(cancelled["state"], "cancelled")

    def test_retry_increments_attempt_and_preserves_attempt_history(self) -> None:
        task = self.create_task()
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        failed = self.service.fail(
            task["task_id"],
            1,
            {
                "schema_version": "substar.api-error.v1",
                "code": "provider_timeout",
                "category": "provider_timeout",
                "message": "Timed out",
                "retryable": True,
                "request_id": "req-1",
                "details": {},
            },
        )
        self.assertEqual(failed["state"], "failed")
        retried = self.service.retry(task["task_id"])
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(retried["attempt"], 2)
        claimed_again = self.service.claim_next({"transcription"})
        assert claimed_again is not None
        self.assertEqual(claimed_again["attempt"], 2)
        self.assertEqual(
            [row["attempt"] for row in self.store.attempt_rows(task["task_id"])],
            [1, 2],
        )

    def test_event_cursor_and_project_filter_are_globally_ordered(self) -> None:
        first = self.create_task(idempotency_key="cursor-1")
        second = self.create_task(
            idempotency_key="cursor-2", project_id="project-2"
        )
        all_events = self.service.events()
        after_first = self.service.events(after=all_events[0]["event_id"])
        self.assertEqual([event["task_id"] for event in after_first], [second["task_id"]])
        project_events = self.service.events(project_id="project-1")
        self.assertEqual([event["task_id"] for event in project_events], [first["task_id"]])

    def test_artifact_registration_checks_owner_attempt_and_relative_path(self) -> None:
        task = self.create_task()
        claimed = self.service.claim_next({"transcription"})
        assert claimed is not None
        artifact = self.service.record_artifact(
            task["task_id"],
            1,
            artifact_type="recognition_evidence",
            relative_path="artifacts/evidence.json",
            sha256="a" * 64,
            byte_size=42,
            schema_version="substar.recognition-evidence.v1",
        )
        self.assertEqual(artifact["task_id"], task["task_id"])
        self.assertEqual(artifact["attempt"], 1)
        self.assertEqual(self.service.list_artifacts(task["task_id"]), [artifact])
        self.assertIn(
            "task.artifact_registered",
            [event["event_type"] for event in self.service.events(task_id=task["task_id"])],
        )
        with self.assertRaises(InvalidTaskError):
            self.service.record_artifact(
                task["task_id"],
                1,
                artifact_type="recognition_evidence",
                relative_path="artifacts/foreign.json",
                sha256="b" * 64,
                byte_size=1,
                project_id="another-project",
            )

    def test_startup_reconcile_interrupts_previous_owner(self) -> None:
        task = self.create_task()
        claimed = self.service.claim_next({"transcription"}, lease_seconds=3600)
        assert claimed is not None

        replacement = TaskService(self.store, "instance-b")
        with self.assertRaises(TaskOwnershipError):
            replacement.heartbeat(task["task_id"], 1)
        reconciled = replacement.reconcile_startup()
        self.assertEqual([item["task_id"] for item in reconciled], [task["task_id"]])
        self.assertEqual(reconciled[0]["state"], "interrupted")
        retried = replacement.retry(task["task_id"])
        self.assertEqual(retried["attempt"], 2)

        with self.assertRaises(TaskStateConflictError):
            self.service.heartbeat(task["task_id"], 1)


if __name__ == "__main__":
    unittest.main()
