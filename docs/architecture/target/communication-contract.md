# Frontend, API and worker communication contract

## Principles

1. HTTP routes expose product resources and commands, not internal worker scripts or historical stages.
2. Short reads and transactional editor writes use REST.
3. Long operations return a durable task immediately.
4. Task/project updates use replayable Server-Sent Events (SSE).
5. Every error has one stable envelope.
6. A request ID is accepted/generated and returned on every response.
7. Compatibility routes map to application services; application services never call route functions.

## Canonical REST surface

The exact pagination shape can be extended compatibly, but resource identity and command meaning are frozen.

### Runtime and instance

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/api/runtime/identity` | Live instance/build/port/start-time identity |
| GET | `/api/runtime/health` | Liveness/readiness and migration status |
| POST | `/api/runtime/shutdown` | Authenticated local graceful shutdown request |
| GET | `/api/events` | SSE event stream with replay cursor |

### Projects

| Method | Path | Meaning |
| --- | --- | --- |
| POST | `/api/projects` | Create a stable project and optionally attach media |
| GET | `/api/projects` | List project summaries/capabilities |
| GET | `/api/projects/{project_id}` | Read project metadata and current revision pointer |
| PATCH | `/api/projects/{project_id}` | Change mutable metadata such as display name |
| DELETE | `/api/projects/{project_id}` | Explicit project deletion after active-task check |
| POST | `/api/projects/import` | Import a portable project bundle |
| POST | `/api/projects/{project_id}/media` | Attach/relink media |
| GET | `/api/projects/{project_id}/media` | Range-capable media stream |
| GET | `/api/projects/{project_id}/waveform` | Windowed waveform projection |

Project identity is independent of display name, task identity and directory naming.

### Tasks and workflow commands

| Method | Path | Meaning |
| --- | --- | --- |
| POST | `/api/projects/{project_id}/tasks` | Create a registered project task |
| GET | `/api/tasks` | Query tasks by project/type/state/cursor |
| GET | `/api/tasks/{task_id}` | Read current durable task projection |
| GET | `/api/tasks/{task_id}/events` | Read paginated task event history |
| GET | `/api/tasks/{task_id}/artifacts` | List registered task outputs |
| POST | `/api/tasks/{task_id}/cancel` | Persist cancellation request |
| POST | `/api/tasks/{task_id}/retry` | Retry a failed/interrupted task as a new attempt |

Task creation accepts `Idempotency-Key`. A successful creation returns `202 Accepted` plus a `substar.task.v1` object. Replaying the same key/input returns the same task.

The initial subtitle-creation UI may send one workflow request that transactionally creates a project and a dependency graph. Internally it still produces registered canonical tasks; the browser never reconstructs a workflow from historical status files.

### Editor revisions and operations

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/api/projects/{project_id}/document` | Read current revision/document or delta cursor |
| GET | `/api/projects/{project_id}/revisions` | List revisions/checkpoints |
| GET | `/api/projects/{project_id}/revisions/{revision_id}` | Read one revision |
| POST | `/api/projects/{project_id}/operations` | Apply one or a batch of typed operations |
| POST | `/api/projects/{project_id}/checkpoints` | Create named checkpoint |
| POST | `/api/projects/{project_id}/restore` | Restore a revision as a new revision |
| PUT | `/api/projects/{project_id}/presentation` | Update presentation settings transactionally |
| POST | `/api/projects/{project_id}/validate` | Validate the current document |
| GET | `/api/projects/{project_id}/exports/{track}` | Download a registered/on-demand export |

The current `EditorDocument` operation protocol remains versioned independently. Revision conflicts return HTTP 409 with the canonical error envelope and current revision metadata.

### Settings, providers and glossary

These retain their product responsibilities but move behind focused services. Provider credentials are never returned; the UI receives credential state/capabilities only. Glossary endpoints expose stable project scope by `project_id`, not by mutable name or directory name.

## Request and response rules

- JSON content uses UTF-8 and snake_case field names.
- Timestamps are UTC RFC 3339 strings.
- IDs are opaque strings; clients do not parse meaning from them.
- Lists use explicit cursor pagination where unbounded growth is possible.
- `X-Request-ID` is returned and included in structured logs/events when relevant.
- `Idempotency-Key` is supported on task creation and other retry-prone commands.
- File uploads use multipart only at the HTTP adapter; services receive resolved input handles.
- Secrets are referenced by server-side credential IDs, never embedded in task/event payloads.

## Canonical API error

Every non-success JSON response has this shape:

```json
{
  "schema_version": "substar.api-error.v1",
  "code": "revision_conflict",
  "category": "revision_conflict",
  "message": "The project changed after this edit was prepared.",
  "retryable": true,
  "request_id": "req_...",
  "details": {
    "current_revision_id": "rev_..."
  }
}
```

Status mapping is consistent:

| HTTP status | Use |
| --- | --- |
| 400 | malformed/unsupported command |
| 401/403 | local authorization or provider credential boundary |
| 404 | resource does not exist |
| 409 | revision/idempotency/state conflict |
| 422 | domain input validation failure |
| 429 | local/provider throttling projection |
| 503 | runtime/provider temporarily unavailable |
| 500 | unexpected internal failure |

## SSE event stream

`GET /api/events` returns events from committed `task_events` rows.

Client reconnect may supply either:

- standard `Last-Event-ID` header; or
- `after=<event_id>` for environments where the header is unavailable.

Example frame:

```text
id: 1842
event: task.progress
data: {"schema_version":"substar.task-event.v1",...}
```

Rules:

- `id` is the globally increasing database event cursor;
- events are at-least-once on reconnect, so clients deduplicate by ID;
- ordering is guaranteed by `event_id`, not wall-clock time;
- heartbeats are SSE comments and are not stored as business events;
- if the requested cursor was pruned, the server emits `stream.reset_required` and the client reloads task/project snapshots;
- the stream may include task and project projection events, but all use one envelope;
- high-volume raw logs remain downloadable files rather than SSE history.

Initial event kinds:

```text
task.created
task.started
task.progress
task.waiting
task.cancel_requested
task.cancelled
task.succeeded
task.failed
task.interrupted
task.artifact_registered
project.created
project.updated
project.revision_created
stream.reset_required
```

## Browser client boundaries

The existing pages will share:

```text
ApiClient
├── request IDs
├── JSON/error decoding
├── timeout and AbortSignal
├── idempotency keys
└── compatibility response adapters

TaskClient
├── SSE connection/replay cursor
├── event deduplication
├── current task projection
├── reconnect/backoff
└── low-frequency polling fallback
```

Editor operations continue through their optimistic `DocumentStore` and `OperationQueue`. They do not wait for or pass through the long-task scheduler.

## Worker protocol

The supervisor sends one `substar.worker-command.v1` command through a controlled input channel or file. Worker stdout contains only `substar.worker-message.v1` JSON Lines. Human diagnostics go to stderr.

Message types:

```text
ready
progress
notice
artifact
result
error
cancelled
```

A worker must emit exactly one terminal message (`result`, `error` or `cancelled`) when it can do so. The supervisor still treats exit code, process ownership and result validation as authoritative; a message alone cannot commit task success.

## Compatibility routing

- Existing `/api/v2/*`, `/api/jobs/*` and `/api/workbench/*` paths remain only as temporary HTTP adapters.
- Compatibility handlers translate old request/response names into canonical application commands.
- No new application service imports an old router or status-file model.
- New frontend code calls canonical routes only.
- Removal requires contract tests plus proof that packaged frontend and supported imported projects no longer call the route.
