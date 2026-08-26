# Phase 3 implementation record: runtime and instance foundation

Status: implemented and regression-tested on 2026-08-16.

## Scope

This phase implements the first migration slice without moving any production
subtitle algorithm. Existing transcription, segmentation, translation,
calibration, review, editor and frontend paths continue to run unchanged.

The real acceptance fixture
`C:/Users/Administrator/Downloads/9_d0JdVfQ-0eY_-E.mp4` is intentionally not
processed by this slice. It becomes the end-to-end cutover gate when
transcription and segmentation are registered on the new runtime.

## Implemented foundation

### Durable task authority

- `data/.substar-workbench/runtime.sqlite3` is the single task-lifecycle
  authority.
- Schema version 2 contains tasks, attempts, dependencies, replayable events
  and registered artifacts. The v1-to-v2 migration preserves existing artifact
  rows while adding attempt ownership.
- Artifact rows carry their owning attempt internally and are unique by
  `(task_id, attempt, relative_path)`. This Phase 3 correction was also added
  to the target runtime table contract so retries can retain prior evidence
  while validating only the current attempt.
- SQLite uses WAL, foreign keys, `synchronous=FULL`, a busy timeout and explicit
  transactions.
- State changes and their event rows commit atomically.
- Task creation supports scoped idempotency keys and rejects key reuse with
  different input. Exact replay is resolved before handler availability, so a
  restart window cannot turn an accepted request into a false 503.
- Inputs, public results, errors and artifact metadata reject inline secret
  fields. Provider credentials remain references rather than SQLite payloads.
- Failed/interrupted task errors must match the frozen
  `substar.api-error.v1` shape before persistence.
- Only the API process may write through a `RuntimeStore`; inherited worker
  processes are rejected by a process-identity guard.
- Claims, leases, heartbeats, monotonic progress, cancellation, retry and
  startup reconciliation implement the frozen seven-state contract.

### Registry, scheduler and resource policy

- `TaskRegistry` permits one handler for each canonical task type.
- `TaskScheduler` claims durable tasks, prepares isolated attempt directories,
  starts supervised workers, persists their process identity, renews leases,
  routes progress/artifact messages and commits terminal outcomes.
- Named resource limits replace ad-hoc task locks. The composition root defines
  bounds for worker, local GPU, media CPU, provider I/O, project writes and
  downloads.
- Dispatch and shutdown share an explicit critical section. A task cannot be
  claimed as `running` and then escape shutdown before its worker handle is
  published.
- An application-requested stop interrupts unfinished work so it is explicitly
  retryable. A worker that had already completed is still finalized instead of
  being incorrectly downgraded to interrupted.
- Active executions are owned by `(task_id, attempt)`, so a late callback from
  an old attempt cannot finalize or release a retried worker.
- Shutdown uses one shared 40-second scheduler deadline, escalates all remaining
  workers concurrently to process-tree termination, and reports a hard failure
  rather than releasing resources while a worker or scheduler thread is live.

No production handler is registered yet. Canonical task creation for an
unregistered type returns `503 task_type_unavailable`; this prevents a hidden
second dispatcher while vertical cutovers are pending.

### Worker boundary

- The supervisor owns one child process and its full process tree.
- The initial stdin record is strict `substar.worker-command.v1` JSONL.
- Cooperative cancellation is strict `substar.worker-control.v1` JSONL.
- Stdout accepts only strict, ordered `substar.worker-message.v1` JSONL.
- Human diagnostics go to a persisted stderr log and never share the protocol
  stream.
- Protocol violations, non-zero exits, timeouts and forced termination become
  structured completions.
- Worker-authored exception/provider strings are kept internal; public task
  errors use fixed messages, and generic progress persists only its numeric
  value until a type-specific handler validator is installed.
- Reader threads only parse and enqueue events; the scheduler thread performs
  SQLite writes in protocol order. Stderr logging failures do not stop pipe
  draining.
- Worker-declared artifacts are contained under the attempt artifact root and
  their actual file size and SHA-256 are verified before registration. Artifact
  project ownership is always derived from the task. Files are verified again
  after the complete process tree exits and immediately before task success.
- Windows worker startup requires successful kill-on-close Job assignment.
  Root-process exit triggers residual-tree termination before reader EOF and
  completion publication, so inherited pipes cannot hide orphan descendants.
- Windows workers use a Job Object where available, with verified process-tree
  termination as fallback.

### Canonical task communication

The application now exposes:

- `POST /api/projects/{project_id}/tasks`
- `GET /api/tasks`
- `GET /api/tasks/{task_id}`
- `POST /api/tasks/{task_id}/cancel`
- `POST /api/tasks/{task_id}/retry`
- `GET /api/tasks/{task_id}/events`
- `GET /api/tasks/{task_id}/artifacts`
- `GET /api/events`
- `GET /api/runtime/health`
- `POST /api/runtime/shutdown`

Task API errors use the frozen `substar.api-error.v1` envelope, including
framework-level body validation failures. Every task response carries an
`X-Request-ID`. SSE reads only committed event rows and supports both
`Last-Event-ID` and `after=<event_id>` replay cursors; the task-event endpoint
is the polling fallback. SSE filters are validated before the 200 response is
started. Event rows are not pruned in schema v1; `stream.reset_required` must
be implemented together with any future bounded-retention policy.

### Instance policy

- A second launcher opens the existing instance only when its application
  marker, instance identity, build ID and installation root match.
- A different build/installation is reported as a conflict and is never
  silently terminated.
- A stale record is removed only when every recorded owner is definitely gone
  or is a different process; an unknown process state is not treated as stale.
- `--stop` first sends an instance-bound graceful shutdown request.
- Successful shutdown means the identity endpoint and the recorded processes
  have both exited. A closed socket alone is not considered completion.
- Forced fallback is available only after repeated strict validation of
  instance ID, PID, process creation time, executable, installation root and
  Job Object name.
- Runtime discovery records are namespaced by a hash of the installation root,
  preventing one portable installation's dead record from blocking another.
- PID creation timestamps are mandatory for Windows force-stop decisions, and
  launcher initialization failures terminate and reap any child already born.
- The launcher derives its 55-second graceful exit budget from the same runtime
  policy module, leaving 15 seconds after the scheduler's 40-second deadline
  for Uvicorn lifespan and final persistence cleanup before a verified force
  fallback.
- Closing the original launcher console remains the documented OS Job Object
  fallback for abrupt console termination.

## Verification

- Durable task runtime tests cover migrations, idempotency, dependencies,
  ownership, progress, cancellation, retry, canonical errors, secret
  rejection, artifacts, event cursors and startup reconciliation.
- Worker tests cover strict protocol parsing, success, failure, cooperative
  cancellation, timeout/force-kill, stderr persistence and callback delivery.
- Scheduler tests cover success, persisted PID/progress/result, cancellation,
  verified artifact files, false checksums, resource bounds, shutdown
  interruption and completion-versus-shutdown ordering.
- API tests cover create/replay, canonical errors, polling history, malformed
  requests and SSE replay.
- Launcher tests cover same-build reuse, build/install conflicts, safe stale
  detection, graceful stop, closed-socket/process-exit distinction and strict
  force-stop validation.
- Runtime objects are validated against every frozen JSON Schema.
- The shutdown scheduler suite was repeated ten consecutive times after the
  claim/publish race fix.
- The complete Python regression suite passes: 93 tests, including strict
  `ResourceWarning` handling.
- The preserved editor frontend regression suite passes: 4 Node tests.
- `python launcher.py --smoke-import` passes.

## Exit boundary

This phase establishes one reliable control plane but deliberately leaves the
old production task mechanisms in place until their individual vertical
cutovers. The next phase must register exactly one production task type at a
time and remove its previous dispatcher only after characterization and
recovery tests pass. Before the first production handler is registered, its
type-specific input, worker-event, result/artifact and startup-reconciliation
validators must be defined; the runtime intentionally does not guess business
schemas in this foundation phase.
