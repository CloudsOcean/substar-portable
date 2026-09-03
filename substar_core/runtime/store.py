from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .model import (
    EVENT_TYPES,
    IdempotencyConflictError,
    InvalidTaskError,
    TaskArtifact,
    TaskEvent,
    TaskNotFoundError,
    TaskOwnershipError,
    TaskRecord,
    TaskState,
    TaskStateConflictError,
    WriterProcessError,
    coerce_state,
    require_transition,
)


RUNTIME_SCHEMA_VERSION = 4


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def lease_deadline(seconds: float) -> str:
    if seconds <= 0:
        raise InvalidTaskError("lease_seconds must be positive")
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=float(seconds)))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_json(value: Mapping[str, Any] | None) -> str:
    try:
        return json.dumps(
            dict(value or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidTaskError("task data must be a finite JSON object") from exc


def _input_hash(
    *,
    task_type: str,
    project_id: str | None,
    parent_task_id: str | None,
    input_schema: str,
    input_json: str,
    expected_revision_id: str | None,
) -> str:
    frozen = canonical_json(
        {
            "task_type": task_type,
            "project_id": project_id,
            "parent_task_id": parent_task_id,
            "input_schema": input_schema,
            "input": json.loads(input_json),
            "expected_revision_id": expected_revision_id,
        }
    )
    return hashlib.sha256(frozen.encode("utf-8")).hexdigest()


class RuntimeStore:
    """Single-writer SQLite authority for durable task lifecycle state.

    A store instance is bound to the process that created it. This prevents an
    inherited worker object from becoming an accidental second database writer.
    Workers report to the API supervisor; only that process uses these methods.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer_pid = os.getpid()
        self.migrate()

    def _assert_writer_process(self) -> None:
        if os.getpid() != self._writer_pid:
            raise WriterProcessError(
                "RuntimeStore may only be used by the API process that created it"
            )

    def _connect(self) -> sqlite3.Connection:
        self._assert_writer_process()
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        self._assert_writer_process()
        with closing(
            sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        ) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > RUNTIME_SCHEMA_VERSION:
                raise InvalidTaskError(
                    f"runtime database schema {current} is newer than supported "
                    f"schema {RUNTIME_SCHEMA_VERSION}"
                )
            if current not in (0, 3, RUNTIME_SCHEMA_VERSION):
                raise InvalidTaskError(
                    "legacy runtime database is unsupported; v2 requires a fresh runtime-v2.sqlite3"
                )
            if current == 0:
                applied_at = utc_now().replace("'", "''")
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        project_id TEXT,
                        parent_task_id TEXT REFERENCES tasks(task_id),
                        task_type TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN (
                            'queued','running','succeeded','succeeded_with_issues','failed',
                            'cancelling','cancelled','interrupted'
                        )),
                        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                        progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
                        progress_message TEXT,
                        progress_payload_json TEXT,
                        step TEXT,
                        phase TEXT,
                        completed_units INTEGER NOT NULL DEFAULT 0 CHECK (completed_units >= 0),
                        total_units INTEGER NOT NULL DEFAULT 0 CHECK (total_units >= 0),
                        repair_phase_entered INTEGER NOT NULL DEFAULT 0 CHECK (repair_phase_entered IN (0,1)),
                        needs_attention INTEGER NOT NULL DEFAULT 0 CHECK (needs_attention IN (0,1)),
                        wait_reason TEXT,
                        input_schema TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        input_hash TEXT NOT NULL,
                        idempotency_key TEXT,
                        idempotency_scope TEXT UNIQUE,
                        expected_revision_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        cancel_requested_at TEXT,
                        owner_instance_id TEXT,
                        lease_expires_at TEXT,
                        result_json TEXT,
                        error_json TEXT,
                        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1)
                    );
                    CREATE TABLE IF NOT EXISTS task_attempts (
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        attempt INTEGER NOT NULL CHECK (attempt >= 1),
                        worker_id TEXT,
                        worker_pid INTEGER,
                        started_at TEXT NOT NULL,
                        heartbeat_at TEXT,
                        finished_at TEXT,
                        exit_code INTEGER,
                        terminal_reason TEXT,
                        work_directory TEXT NOT NULL DEFAULT '',
                        stdout_log TEXT NOT NULL DEFAULT '',
                        stderr_log TEXT NOT NULL DEFAULT '',
                        error_json TEXT,
                        PRIMARY KEY (task_id, attempt)
                    );
                    CREATE TABLE IF NOT EXISTS task_dependencies (
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        condition TEXT NOT NULL DEFAULT 'succeeded' CHECK (condition = 'succeeded'),
                        PRIMARY KEY (task_id, depends_on_task_id),
                        CHECK (task_id <> depends_on_task_id)
                    );
                    CREATE TABLE IF NOT EXISTS task_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
                        project_id TEXT,
                        attempt INTEGER,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        request_id TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS task_artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        project_id TEXT,
                        attempt INTEGER NOT NULL CHECK (attempt >= 1),
                        artifact_type TEXT NOT NULL,
                        schema_version TEXT,
                        relative_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                        created_at TEXT NOT NULL,
                        metadata_json TEXT,
                        UNIQUE (task_id, attempt, relative_path)
                    );
                    CREATE INDEX IF NOT EXISTS tasks_dispatch_idx
                        ON tasks(state, created_at, task_id);
                    CREATE INDEX IF NOT EXISTS tasks_project_idx
                        ON tasks(project_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS tasks_type_state_idx
                        ON tasks(task_type, state, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS tasks_owner_lease_idx
                        ON tasks(owner_instance_id, lease_expires_at)
                        WHERE state IN ('running', 'cancelling');
                    CREATE INDEX IF NOT EXISTS task_events_task_cursor_idx
                        ON task_events(task_id, event_id);
                    CREATE INDEX IF NOT EXISTS task_events_project_cursor_idx
                        ON task_events(project_id, event_id);
                    CREATE INDEX IF NOT EXISTS task_artifacts_task_idx
                        ON task_artifacts(task_id, attempt, created_at);
                    INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                        VALUES ({RUNTIME_SCHEMA_VERSION}, 'structured_ai_progress', '{applied_at}');
                    PRAGMA user_version={RUNTIME_SCHEMA_VERSION};
                    COMMIT;
                    """
                )
            elif current == 3:
                applied_at = utc_now().replace("'", "''")
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    ALTER TABLE tasks ADD COLUMN progress_payload_json TEXT;
                    INSERT INTO schema_migrations(version, name, applied_at)
                        VALUES (4, 'structured_ai_progress', '{applied_at}');
                    PRAGMA user_version=4;
                    COMMIT;
                    """
                )

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        occurred_at: str,
        data: Mapping[str, Any],
        task_id: str | None = None,
        project_id: str | None = None,
        attempt: int | None = None,
        request_id: str | None = None,
    ) -> int:
        if event_type not in EVENT_TYPES:
            raise InvalidTaskError(f"unknown event type: {event_type}")
        cursor = connection.execute(
            "INSERT INTO task_events(task_id, project_id, attempt, event_type, "
            "occurred_at, request_id, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                project_id,
                attempt,
                event_type,
                occurred_at,
                request_id,
                canonical_json(data),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _task_row(
        connection: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"task does not exist: {task_id}")
        return row

    @staticmethod
    def _owned_task(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        allowed_states: Sequence[TaskState],
    ) -> sqlite3.Row:
        row = RuntimeStore._task_row(connection, task_id)
        state = coerce_state(row["state"])
        if state not in set(allowed_states):
            raise TaskStateConflictError(
                f"task {task_id} is {state.value}, expected one of "
                f"{', '.join(item.value for item in allowed_states)}"
            )
        if int(row["attempt"]) != int(attempt):
            raise TaskOwnershipError(
                f"task {task_id} attempt {attempt} is stale; current attempt is {row['attempt']}"
            )
        if str(row["owner_instance_id"] or "") != owner_instance_id:
            raise TaskOwnershipError(f"task {task_id} is owned by another API instance")
        return row

    def create_task(
        self,
        *,
        task_id: str,
        task_type: str,
        input_schema: str,
        input_payload: Mapping[str, Any],
        project_id: str | None,
        parent_task_id: str | None,
        idempotency_key: str | None,
        expected_revision_id: str | None,
        request_id: str | None = None,
        depends_on_task_ids: Sequence[str] = (),
    ) -> tuple[TaskRecord, bool]:
        created_at = utc_now()
        input_json = canonical_json(input_payload)
        digest = _input_hash(
            task_type=task_type,
            project_id=project_id,
            parent_task_id=parent_task_id,
            input_schema=input_schema,
            input_json=input_json,
            expected_revision_id=expected_revision_id,
        )
        scope = (
            f"{project_id or ''}\x1f{task_type}\x1f{idempotency_key}"
            if idempotency_key is not None
            else None
        )
        with self._transaction() as connection:
            if scope is not None:
                existing = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_scope=?", (scope,)
                ).fetchone()
                if existing is not None:
                    if str(existing["input_hash"]) != digest:
                        raise IdempotencyConflictError(
                            "the idempotency key was already used with different input"
                        )
                    existing_dependencies = {
                        str(row["depends_on_task_id"])
                        for row in connection.execute(
                            "SELECT depends_on_task_id FROM task_dependencies "
                            "WHERE task_id=? AND condition='succeeded'",
                            (str(existing["task_id"]),),
                        ).fetchall()
                    }
                    if existing_dependencies != set(depends_on_task_ids):
                        raise IdempotencyConflictError(
                            "the idempotency key was already used with different dependencies"
                        )
                    return TaskRecord.from_row(existing), False
            if parent_task_id is not None:
                self._task_row(connection, parent_task_id)
            connection.execute(
                "INSERT INTO tasks(task_id, project_id, parent_task_id, task_type, state, "
                "attempt, progress, input_schema, input_json, input_hash, idempotency_key, "
                "idempotency_scope, expected_revision_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    project_id,
                    parent_task_id,
                    task_type,
                    input_schema,
                    input_json,
                    digest,
                    idempotency_key,
                    scope,
                    expected_revision_id,
                    created_at,
                    created_at,
                ),
            )
            for dependency_task_id in depends_on_task_ids:
                self._task_row(connection, dependency_task_id)
                try:
                    connection.execute(
                        "INSERT INTO task_dependencies"
                        "(task_id, depends_on_task_id, condition) VALUES (?, ?, 'succeeded')",
                        (task_id, dependency_task_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise InvalidTaskError("invalid task dependency") from exc
            self._event(
                connection,
                event_type="task.created",
                occurred_at=created_at,
                task_id=task_id,
                project_id=project_id,
                attempt=0,
                request_id=request_id,
                data={"task_type": task_type, "state": "queued"},
            )
            return TaskRecord.from_row(self._task_row(connection, task_id)), True

    def find_idempotent_task(
        self,
        *,
        task_type: str,
        input_schema: str,
        input_payload: Mapping[str, Any],
        project_id: str | None,
        parent_task_id: str | None,
        idempotency_key: str,
        expected_revision_id: str | None,
    ) -> TaskRecord | None:
        """Resolve an exact replay without creating state or requiring a handler."""

        input_json = canonical_json(input_payload)
        digest = _input_hash(
            task_type=task_type,
            project_id=project_id,
            parent_task_id=parent_task_id,
            input_schema=input_schema,
            input_json=input_json,
            expected_revision_id=expected_revision_id,
        )
        scope = f"{project_id or ''}\x1f{task_type}\x1f{idempotency_key}"
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_scope=?", (scope,)
            ).fetchone()
        if row is None:
            return None
        if str(row["input_hash"]) != digest:
            raise IdempotencyConflictError(
                "the idempotency key was already used with different input"
            )
        return TaskRecord.from_row(row)

    def get_task(self, task_id: str) -> TaskRecord:
        with closing(self._connect()) as connection:
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def list_tasks(
        self,
        *,
        project_id: str | None = None,
        task_type: str | None = None,
        states: Iterable[str | TaskState] | None = None,
        parent_task_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("project_id=?")
            parameters.append(project_id)
        if task_type is not None:
            clauses.append("task_type=?")
            parameters.append(task_type)
        if parent_task_id is not None:
            clauses.append("parent_task_id=?")
            parameters.append(parent_task_id)
        normalized_states = [coerce_state(item).value for item in (states or [])]
        if normalized_states:
            clauses.append(f"state IN ({','.join('?' for _ in normalized_states)})")
            parameters.extend(normalized_states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM tasks"
                + where
                + " ORDER BY created_at DESC, task_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [TaskRecord.from_row(row) for row in rows]

    def add_dependency(
        self, task_id: str, depends_on_task_id: str, condition: str = "succeeded"
    ) -> None:
        if condition != "succeeded":
            raise InvalidTaskError("only the succeeded dependency condition is supported")
        with self._transaction() as connection:
            self._task_row(connection, task_id)
            self._task_row(connection, depends_on_task_id)
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO task_dependencies"
                    "(task_id, depends_on_task_id, condition) VALUES (?, ?, ?)",
                    (task_id, depends_on_task_id, condition),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidTaskError("invalid task dependency") from exc

    def claim_next(
        self,
        allowed_task_types: Iterable[str],
        owner_instance_id: str,
        lease_seconds: float,
        *,
        worker_id: str | None = None,
        worker_pid: int | None = None,
        work_directory: str = "",
        stdout_log: str = "",
        stderr_log: str = "",
    ) -> TaskRecord | None:
        task_types = sorted(set(allowed_task_types))
        if not task_types:
            return None
        now = utc_now()
        lease = lease_deadline(lease_seconds)
        placeholders = ",".join("?" for _ in task_types)
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT candidate.* FROM tasks AS candidate "
                f"WHERE candidate.state='queued' "
                f"AND candidate.task_type IN ({placeholders}) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_dependencies AS edge "
                "  JOIN tasks AS dependency ON dependency.task_id=edge.depends_on_task_id "
                "  WHERE edge.task_id=candidate.task_id "
                "    AND edge.condition='succeeded' "
                "    AND dependency.state<>'succeeded'"
                ") ORDER BY candidate.created_at, candidate.task_id LIMIT 1",
                task_types,
            ).fetchone()
            if row is None:
                return None
            task = TaskRecord.from_row(row)
            require_transition(task.state, TaskState.RUNNING)
            attempt = max(1, task.attempt)
            existing_attempt = connection.execute(
                "SELECT 1 FROM task_attempts WHERE task_id=? AND attempt=?",
                (task.task_id, attempt),
            ).fetchone()
            if existing_attempt is not None:
                attempt += 1
            connection.execute(
                "INSERT INTO task_attempts(task_id, attempt, worker_id, worker_pid, "
                "started_at, heartbeat_at, work_directory, stdout_log, stderr_log) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    attempt,
                    worker_id,
                    worker_pid,
                    now,
                    now,
                    work_directory,
                    stdout_log,
                    stderr_log,
                ),
            )
            connection.execute(
                "UPDATE tasks SET state='running', attempt=?, progress=0, "
                "progress_message=NULL, progress_payload_json=NULL, step=NULL, phase=NULL, completed_units=0, "
                "total_units=0, repair_phase_entered=0, needs_attention=0, "
                "wait_reason=NULL, updated_at=?, "
                "started_at=?, finished_at=NULL, cancel_requested_at=NULL, "
                "owner_instance_id=?, lease_expires_at=?, result_json=NULL, "
                "error_json=NULL, row_version=row_version+1 WHERE task_id=?",
                (attempt, now, now, owner_instance_id, lease, task.task_id),
            )
            self._event(
                connection,
                event_type="task.started",
                occurred_at=now,
                task_id=task.task_id,
                project_id=task.project_id,
                attempt=attempt,
                data={"state": "running"},
            )
            return TaskRecord.from_row(self._task_row(connection, task.task_id))

    def heartbeat(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        lease_seconds: float,
    ) -> TaskRecord:
        now = utc_now()
        lease = lease_deadline(lease_seconds)
        with self._transaction() as connection:
            self._owned_task(
                connection,
                task_id=task_id,
                attempt=attempt,
                owner_instance_id=owner_instance_id,
                allowed_states=(TaskState.RUNNING, TaskState.CANCELLING),
            )
            connection.execute(
                "UPDATE task_attempts SET heartbeat_at=? WHERE task_id=? AND attempt=?",
                (now, task_id, attempt),
            )
            connection.execute(
                "UPDATE tasks SET lease_expires_at=?, updated_at=?, "
                "row_version=row_version+1 WHERE task_id=?",
                (lease, now, task_id),
            )
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def attach_worker(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        *,
        worker_id: str,
        worker_pid: int,
        work_directory: str,
        stdout_log: str,
        stderr_log: str,
    ) -> TaskRecord:
        """Persist the process identity after a claimed task is successfully spawned."""

        now = utc_now()
        with self._transaction() as connection:
            self._owned_task(
                connection,
                task_id=task_id,
                attempt=attempt,
                owner_instance_id=owner_instance_id,
                allowed_states=(TaskState.RUNNING, TaskState.CANCELLING),
            )
            cursor = connection.execute(
                "UPDATE task_attempts SET worker_id=?, worker_pid=?, heartbeat_at=?, "
                "work_directory=?, stdout_log=?, stderr_log=? "
                "WHERE task_id=? AND attempt=?",
                (
                    worker_id,
                    worker_pid,
                    now,
                    work_directory,
                    stdout_log,
                    stderr_log,
                    task_id,
                    attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskStateConflictError("task attempt is not available for worker attachment")
            connection.execute(
                "UPDATE tasks SET updated_at=?, row_version=row_version+1 WHERE task_id=?",
                (now, task_id),
            )
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def record_progress(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        progress: float,
        *,
        message: str | None = None,
        step: str | None = None,
        wait_reason: str | None = None,
        phase: str | None = None,
        completed_units: int | None = None,
        total_units: int | None = None,
        progress_payload: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> TaskRecord:
        if not 0.0 <= float(progress) <= 1.0:
            raise InvalidTaskError("progress must be between 0.0 and 1.0")
        now = utc_now()
        with self._transaction() as connection:
            row = self._owned_task(
                connection,
                task_id=task_id,
                attempt=attempt,
                owner_instance_id=owner_instance_id,
                allowed_states=(TaskState.RUNNING, TaskState.CANCELLING),
            )
            if float(progress) < float(row["progress"]):
                raise TaskStateConflictError(
                    "progress must be monotonic within one task attempt"
                )
            completed = int(row["completed_units"]) if completed_units is None else int(completed_units)
            total = int(row["total_units"]) if total_units is None else int(total_units)
            payload_json = (
                row["progress_payload_json"]
                if progress_payload is None
                else canonical_json(progress_payload)
            )
            if completed < 0 or total < 0 or (total and completed > total):
                raise InvalidTaskError("unit progress is invalid")
            connection.execute(
                "UPDATE tasks SET progress=?, progress_message=?, progress_payload_json=?, step=?, phase=?, "
                "completed_units=?, total_units=?, wait_reason=?, updated_at=?, "
                "row_version=row_version+1 WHERE task_id=?",
                (float(progress), message, payload_json, step, phase, completed, total, wait_reason, now, task_id),
            )
            event_type = "task.waiting" if wait_reason else "task.progress"
            self._event(
                connection,
                event_type=event_type,
                occurred_at=now,
                task_id=task_id,
                project_id=row["project_id"],
                attempt=attempt,
                request_id=request_id,
                data={
                    "progress": float(progress),
                    "message": message,
                    "step": step,
                    "phase": phase,
                    "completed_units": completed,
                    "total_units": total,
                    "progress_payload": dict(progress_payload) if progress_payload is not None else None,
                    "wait_reason": wait_reason,
                },
            )
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def request_cancel(self, task_id: str, *, request_id: str | None = None) -> TaskRecord:
        now = utc_now()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            task = TaskRecord.from_row(row)
            if task.state is TaskState.CANCELLED:
                return task
            if task.state is TaskState.CANCELLING:
                return task
            if task.state is TaskState.QUEUED:
                require_transition(task.state, TaskState.CANCELLED)
                connection.execute(
                    "UPDATE tasks SET state='cancelled', cancel_requested_at=?, "
                    "updated_at=?, finished_at=?, owner_instance_id=NULL, "
                    "lease_expires_at=NULL, row_version=row_version+1 WHERE task_id=?",
                    (now, now, now, task_id),
                )
                self._event(
                    connection,
                    event_type="task.cancel_requested",
                    occurred_at=now,
                    task_id=task_id,
                    project_id=task.project_id,
                    attempt=task.attempt,
                    request_id=request_id,
                    data={"state": "cancelled", "worker_started": False},
                )
                self._event(
                    connection,
                    event_type="task.cancelled",
                    occurred_at=now,
                    task_id=task_id,
                    project_id=task.project_id,
                    attempt=task.attempt,
                    request_id=request_id,
                    data={"state": "cancelled", "reason": "cancelled_before_dispatch"},
                )
            elif task.state is TaskState.RUNNING:
                require_transition(task.state, TaskState.CANCELLING)
                connection.execute(
                    "UPDATE tasks SET state='cancelling', cancel_requested_at=?, "
                    "updated_at=?, row_version=row_version+1 WHERE task_id=?",
                    (now, now, task_id),
                )
                self._event(
                    connection,
                    event_type="task.cancel_requested",
                    occurred_at=now,
                    task_id=task_id,
                    project_id=task.project_id,
                    attempt=task.attempt,
                    request_id=request_id,
                    data={"state": "cancelling", "worker_started": True},
                )
            else:
                raise TaskStateConflictError(
                    f"cannot cancel a task in {task.state.value} state"
                )
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def retry(self, task_id: str, *, request_id: str | None = None) -> TaskRecord:
        now = utc_now()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            task = TaskRecord.from_row(row)
            require_transition(task.state, TaskState.QUEUED)
            next_attempt = max(1, task.attempt + 1)
            connection.execute(
                "UPDATE tasks SET state='queued', attempt=?, progress=0, "
                "progress_message=NULL, progress_payload_json=NULL, step=NULL, wait_reason=NULL, updated_at=?, "
                "started_at=NULL, finished_at=NULL, cancel_requested_at=NULL, "
                "owner_instance_id=NULL, lease_expires_at=NULL, result_json=NULL, "
                "error_json=NULL, row_version=row_version+1 WHERE task_id=?",
                (next_attempt, now, task_id),
            )
            # The frozen event vocabulary has no task.retried event. A queued
            # retry is represented as a task.progress projection event.
            self._event(
                connection,
                event_type="task.progress",
                occurred_at=now,
                task_id=task_id,
                project_id=task.project_id,
                attempt=next_attempt,
                request_id=request_id,
                data={
                    "state": "queued",
                    "progress": 0.0,
                    "reason": "retry",
                    "previous_state": task.state.value,
                },
            )
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def _finish_owned(
        self,
        *,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        target: TaskState,
        event_type: str,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        exit_code: int | None = None,
        terminal_reason: str | None = None,
        request_id: str | None = None,
    ) -> TaskRecord:
        now = utc_now()
        result_json = canonical_json(result) if result is not None else None
        error_json = canonical_json(error) if error is not None else None
        with self._transaction() as connection:
            row = self._owned_task(
                connection,
                task_id=task_id,
                attempt=attempt,
                owner_instance_id=owner_instance_id,
                allowed_states=(TaskState.RUNNING, TaskState.CANCELLING),
            )
            source = coerce_state(row["state"])
            require_transition(source, target)
            progress = 1.0 if target in (
                TaskState.SUCCEEDED,
                TaskState.SUCCEEDED_WITH_ISSUES,
            ) else float(row["progress"])
            needs_attention = 1 if target is TaskState.SUCCEEDED_WITH_ISSUES else int(row["needs_attention"])
            connection.execute(
                "UPDATE tasks SET state=?, progress=?, updated_at=?, finished_at=?, "
                "owner_instance_id=NULL, lease_expires_at=NULL, result_json=?, "
                "error_json=?, needs_attention=?, row_version=row_version+1 WHERE task_id=?",
                (
                    target.value,
                    progress,
                    now,
                    now,
                    result_json,
                    error_json,
                    needs_attention,
                    task_id,
                ),
            )
            connection.execute(
                "UPDATE task_attempts SET heartbeat_at=?, finished_at=?, exit_code=?, "
                "terminal_reason=?, error_json=? WHERE task_id=? AND attempt=?",
                (
                    now,
                    now,
                    exit_code,
                    terminal_reason or target.value,
                    error_json,
                    task_id,
                    attempt,
                ),
            )
            self._event(
                connection,
                event_type=event_type,
                occurred_at=now,
                task_id=task_id,
                project_id=row["project_id"],
                attempt=attempt,
                request_id=request_id,
                data={
                    "state": target.value,
                    "result": dict(result) if result is not None else None,
                    "error": dict(error) if error is not None else None,
                    "reason": terminal_reason,
                },
            )
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def complete(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        result: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> TaskRecord:
        return self._finish_owned(
            task_id=task_id,
            attempt=attempt,
            owner_instance_id=owner_instance_id,
            target=TaskState.SUCCEEDED,
            event_type="task.succeeded",
            result=result,
            exit_code=0,
            terminal_reason="succeeded",
            request_id=request_id,
        )

    def complete_with_issues(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        result: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> TaskRecord:
        return self._finish_owned(
            task_id=task_id,
            attempt=attempt,
            owner_instance_id=owner_instance_id,
            target=TaskState.SUCCEEDED_WITH_ISSUES,
            event_type="task.succeeded_with_issues",
            result=result,
            exit_code=0,
            terminal_reason="succeeded_with_issues",
            request_id=request_id,
        )

    def enter_repair_phase(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        *,
        request_id: str | None = None,
    ) -> TaskRecord:
        """Atomically enter the only task-wide repair phase."""
        now = utc_now()
        with self._transaction() as connection:
            row = self._owned_task(
                connection,
                task_id=task_id,
                attempt=attempt,
                owner_instance_id=owner_instance_id,
                allowed_states=(TaskState.RUNNING, TaskState.CANCELLING),
            )
            if bool(row["repair_phase_entered"]):
                raise TaskStateConflictError("repair phase may be entered only once")
            connection.execute(
                "UPDATE tasks SET repair_phase_entered=1, phase='repair', updated_at=?, "
                "row_version=row_version+1 WHERE task_id=?",
                (now, task_id),
            )
            self._event(
                connection,
                event_type="task.progress",
                occurred_at=now,
                task_id=task_id,
                project_id=row["project_id"],
                attempt=attempt,
                request_id=request_id,
                data={"phase": "repair", "repair_phase_entered": True},
            )
            return TaskRecord.from_row(self._task_row(connection, task_id))

    def fail(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        error: Mapping[str, Any],
        *,
        exit_code: int | None = None,
        request_id: str | None = None,
    ) -> TaskRecord:
        return self._finish_owned(
            task_id=task_id,
            attempt=attempt,
            owner_instance_id=owner_instance_id,
            target=TaskState.FAILED,
            event_type="task.failed",
            error=error,
            exit_code=exit_code,
            terminal_reason="failed",
            request_id=request_id,
        )

    def interrupted(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        *,
        error: Mapping[str, Any] | None = None,
        reason: str = "worker_interrupted",
        request_id: str | None = None,
    ) -> TaskRecord:
        return self._finish_owned(
            task_id=task_id,
            attempt=attempt,
            owner_instance_id=owner_instance_id,
            target=TaskState.INTERRUPTED,
            event_type="task.interrupted",
            error=error,
            terminal_reason=reason,
            request_id=request_id,
        )

    def cancelled(
        self,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        *,
        reason: str = "cancelled",
        request_id: str | None = None,
    ) -> TaskRecord:
        return self._finish_owned(
            task_id=task_id,
            attempt=attempt,
            owner_instance_id=owner_instance_id,
            target=TaskState.CANCELLED,
            event_type="task.cancelled",
            terminal_reason=reason,
            request_id=request_id,
        )

    def record_artifact(
        self,
        *,
        task_id: str,
        attempt: int,
        owner_instance_id: str,
        artifact_id: str,
        artifact_type: str,
        relative_path: str,
        sha256: str,
        byte_size: int,
        project_id: str | None = None,
        schema_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> TaskArtifact:
        created_at = utc_now()
        metadata_json = canonical_json(metadata) if metadata is not None else None
        with self._transaction() as connection:
            row = self._owned_task(
                connection,
                task_id=task_id,
                attempt=attempt,
                owner_instance_id=owner_instance_id,
                allowed_states=(TaskState.RUNNING, TaskState.CANCELLING),
            )
            if project_id is not None and project_id != row["project_id"]:
                raise InvalidTaskError(
                    "artifact project_id must match the owning task project"
                )
            effective_project_id = row["project_id"]
            try:
                connection.execute(
                    "INSERT INTO task_artifacts(artifact_id, task_id, project_id, attempt, "
                    "artifact_type, schema_version, relative_path, sha256, byte_size, "
                    "created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact_id,
                        task_id,
                        effective_project_id,
                        attempt,
                        artifact_type,
                        schema_version,
                        relative_path,
                        sha256,
                        byte_size,
                        created_at,
                        metadata_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TaskStateConflictError(
                    "artifact id or task-relative path is already registered"
                ) from exc
            self._event(
                connection,
                event_type="task.artifact_registered",
                occurred_at=created_at,
                task_id=task_id,
                project_id=effective_project_id,
                attempt=attempt,
                request_id=request_id,
                data={
                    "artifact_id": artifact_id,
                    "artifact_type": artifact_type,
                    "relative_path": relative_path,
                    "sha256": sha256,
                    "byte_size": byte_size,
                },
            )
            artifact_row = connection.execute(
                "SELECT * FROM task_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            assert artifact_row is not None
            return TaskArtifact.from_row(artifact_row)

    def list_artifacts(
        self, task_id: str, *, attempt: int | None = None
    ) -> list[TaskArtifact]:
        with closing(self._connect()) as connection:
            self._task_row(connection, task_id)
            if attempt is None:
                rows = connection.execute(
                    "SELECT * FROM task_artifacts WHERE task_id=? "
                    "ORDER BY attempt, created_at, artifact_id",
                    (task_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM task_artifacts WHERE task_id=? AND attempt=? "
                    "ORDER BY created_at, artifact_id",
                    (task_id, int(attempt)),
                ).fetchall()
        return [TaskArtifact.from_row(row) for row in rows]

    def delete_task(self, task_id: str) -> None:
        """Delete a non-active runtime projection, never canonical project data."""

        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            state = coerce_state(row["state"])
            if state in {TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING}:
                raise TaskStateConflictError(
                    f"task {task_id} is {state.value}; cancel it before deletion"
                )
            try:
                connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            except sqlite3.IntegrityError as exc:
                raise TaskStateConflictError(
                    f"task {task_id} is still referenced by another task"
                ) from exc

    def events(
        self,
        *,
        after: int = 0,
        task_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskEvent]:
        clauses = ["event_id > ?"]
        parameters: list[Any] = [after]
        if task_id is not None:
            clauses.append("task_id=?")
            parameters.append(task_id)
        if project_id is not None:
            clauses.append("project_id=?")
            parameters.append(project_id)
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM task_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY event_id LIMIT ?",
                parameters,
            ).fetchall()
        return [TaskEvent.from_row(row) for row in rows]

    def reconcile_startup(self, instance_id: str) -> list[TaskRecord]:
        """Interrupt tasks whose recorded owner/lease cannot represent this startup."""

        now = utc_now()
        reconciled: list[TaskRecord] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE state IN ('running','cancelling') "
                "AND (owner_instance_id IS NULL OR owner_instance_id<>? "
                "OR lease_expires_at IS NULL OR lease_expires_at<=?) "
                "ORDER BY created_at, task_id",
                (instance_id, now),
            ).fetchall()
            for row in rows:
                task = TaskRecord.from_row(row)
                require_transition(task.state, TaskState.INTERRUPTED)
                reason = "startup_reconcile_owner_lost"
                connection.execute(
                    "UPDATE tasks SET state='interrupted', updated_at=?, finished_at=?, "
                    "owner_instance_id=NULL, lease_expires_at=NULL, row_version=row_version+1 "
                    "WHERE task_id=?",
                    (now, now, task.task_id),
                )
                if task.attempt > 0:
                    connection.execute(
                        "UPDATE task_attempts SET heartbeat_at=?, finished_at=?, "
                        "terminal_reason=? WHERE task_id=? AND attempt=?",
                        (now, now, reason, task.task_id, task.attempt),
                    )
                self._event(
                    connection,
                    event_type="task.interrupted",
                    occurred_at=now,
                    task_id=task.task_id,
                    project_id=task.project_id,
                    attempt=task.attempt,
                    data={
                        "state": "interrupted",
                        "reason": reason,
                        "previous_owner_instance_id": task.owner_instance_id,
                    },
                )
                reconciled.append(
                    TaskRecord.from_row(self._task_row(connection, task.task_id))
                )
        return reconciled

    def attempt_rows(self, task_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            self._task_row(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]
