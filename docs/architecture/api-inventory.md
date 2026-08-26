# Current API inventory

## Snapshot

This inventory describes the HTTP surface frozen at commit `932a5be`. The baseline OpenAPI document contains 61 paths and 67 operations: 32 GET, 29 POST, 3 PUT, 1 PATCH and 2 DELETE operations. FastAPI reports 80 routes when framework, static-file and OpenAPI routes are included.

The API is currently split by implementation location rather than by one explicit application boundary:

| Surface | Current owner | Responsibility |
| --- | --- | --- |
| `/api/v2/*` | `substar_core/editor_api_v2.py` | Editor projects, revisions, operations, media, export, translation and editor AI |
| `/api/settings`, `/api/models`, `/api/environment`, `/api/glossary` | `app.py` | Application configuration, provider discovery, local assets and glossary |
| `/api/jobs`, `/api/workbench/*` | `app.py` | General jobs, subtitle-creation jobs, batches, logs, retry, naming and export |
| Hidden workbench routes and page routes | `substar_core/workbench_routes.py` | Browser pages, portable-bundle import and media relinking |

## Documented operations

### Editor project and revision API

| Method | Path | Current purpose |
| --- | --- | --- |
| GET | `/api/v2/projects` | List editor projects |
| GET | `/api/v2/editor-tasks` | List projected calibration/review task records |
| GET | `/api/v2/projects/{project_id}` | Read the current editor revision |
| GET | `/api/v2/projects/{project_id}/settings` | Read the frozen project settings |
| GET | `/api/v2/projects/{project_id}/media` | Stream project media |
| GET | `/api/v2/projects/{project_id}/waveform` | Read a downsampled waveform window |
| GET | `/api/v2/projects/{project_id}/export/{mode}` | Export an editor track |
| GET | `/api/v2/projects/{project_id}/hotwords/export` | Export project hotwords |
| GET | `/api/v2/projects/{project_id}/revisions` | List revisions |
| GET | `/api/v2/projects/{project_id}/revisions/{revision_id}` | Read one revision |
| PUT | `/api/v2/projects/{project_id}/document` | Replace the document using an expected revision |
| POST | `/api/v2/projects/{project_id}/operations` | Apply one typed editor operation |
| POST | `/api/v2/projects/{project_id}/operation-batches` | Apply a batch of typed operations |
| POST | `/api/v2/projects/{project_id}/batch-replace` | Perform a text replacement batch |
| POST | `/api/v2/projects/{project_id}/checkpoints` | Create a named checkpoint revision |
| POST | `/api/v2/projects/{project_id}/restore` | Restore a previous revision |
| PUT | `/api/v2/projects/{project_id}/presentation` | Update presentation settings |
| POST | `/api/v2/projects/{project_id}/convert-script` | Convert the source script variant |
| POST | `/api/v2/projects/{project_id}/reference-manuscript` | Upload and match a reference manuscript |
| POST | `/api/v2/projects/{project_id}/ai-correct` | Legacy AI correction operation |
| POST | `/api/v2/projects/{project_id}/ai-calibrate` | Run AI calibration and commit accepted changes |
| POST | `/api/v2/projects/{project_id}/ai-review` | Run AI review |
| GET | `/api/v2/projects/{project_id}/ai-review/latest` | Read the latest review result |
| POST | `/api/v2/projects/{project_id}/complete` | Change project completion state |
| POST | `/api/v2/projects/{project_id}/validate` | Validate the current document |
| POST | `/api/v2/projects/{project_id}/translation` | Start translation for a source revision |
| GET | `/api/v2/projects/{project_id}/translation` | Read translation task/result status |

### Experimental debug API still mounted in production

| Method | Path | Current purpose |
| --- | --- | --- |
| GET | `/api/v2/debug/config` | Read debug configuration |
| GET | `/api/v2/projects/{project_id}/debug/tasks` | List project debug tasks |
| POST | `/api/v2/projects/{project_id}/debug/tasks` | Start a debug task |
| GET | `/api/v2/projects/{project_id}/debug/tasks/{task_id}` | Read one debug task |
| POST | `/api/v2/projects/{project_id}/debug/tasks/{task_id}/cancel` | Cancel a debug task |
| POST | `/api/v2/projects/{project_id}/debug/tasks/{task_id}/apply` | Apply debug output to a project |

### Settings, provider, environment and glossary API

| Method | Path | Current purpose |
| --- | --- | --- |
| GET | `/api/settings` | Read application settings |
| POST | `/api/settings` | Replace application settings |
| POST | `/api/settings/test` | Test a provider connection |
| GET | `/api/runtime/identity` | Read backend instance identity |
| GET | `/api/recognition/profiles` | List recognition profiles |
| GET | `/api/environment/status` | Inspect local runtime dependencies |
| POST | `/api/environment/configure` | Configure the local runtime |
| POST | `/api/model-assets/{asset_id}/download` | Start a model-asset download |
| GET | `/api/model-assets/downloads/{job_id}` | Poll a model-asset download |
| GET | `/api/glossary` | Read the glossary |
| PUT | `/api/glossary` | Replace the glossary |
| GET | `/api/glossary/export-xlsx` | Export glossary XLSX |
| POST | `/api/glossary/import-xlsx` | Import glossary XLSX |
| POST | `/api/models/discover` | Discover provider models |
| POST | `/api/models/reasoning-capabilities` | Resolve cached reasoning capabilities |
| POST | `/api/models/reasoning-probe` | Probe reasoning capabilities |
| GET | `/api/system` | Read system status and paths |
| GET | `/api/prompts` | List prompt registry entries |

### Job and workbench API

| Method | Path | Current purpose |
| --- | --- | --- |
| POST | `/api/jobs` | Create a legacy/general job |
| GET | `/api/jobs` | List jobs and restore file-backed records |
| GET | `/api/jobs/{job_id}` | Read one job |
| POST | `/api/jobs/{job_id}/resume` | Resume an interrupted job |
| GET | `/api/jobs/{job_id}/files/{filename}` | Download a named job artifact |
| POST | `/api/workbench/split-jobs` | Create one subtitle-creation job |
| POST | `/api/workbench/split-batches` | Create a batch of subtitle-creation jobs |
| GET | `/api/workbench/split-batches/{batch_id}` | Read aggregate batch status |
| POST | `/api/workbench/split-jobs/{job_id}/retry` | Retry a failed/interrupted job |
| DELETE | `/api/workbench/split-jobs/{job_id}` | Cancel/delete a job and move its directory to trash |
| PATCH | `/api/workbench/split-jobs/{job_id}/name` | Rename the job/project |
| GET | `/api/workbench/split-jobs/{job_id}/logs` | Read the job log tail |
| DELETE | `/api/workbench/split-jobs/{job_id}/logs` | Clear the job log |
| GET | `/api/workbench/split-jobs/{job_id}/export` | Export the raw split bundle |
| GET | `/api/workbench/split-jobs/{job_id}/export-edited` | Export the edited portable bundle |
| GET | `/api/workbench/split-jobs/{job_id}/subtitles/{mode}` | Export a selected subtitle track |

## Routes excluded from OpenAPI

`substar_core/workbench_routes.py` is included with `include_in_schema=False`, so the frozen OpenAPI is not a complete external-contract record.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/workbench/import-split-bundle` | Import a portable project/split bundle |
| POST | `/api/workbench/projects/{job_name}/media` | Attach or relink project media |
| GET | `/` | Redirect/render the creation page |
| GET | `/split` | Render the creation page |
| GET | `/editor` | Render the editor |
| GET | `/relay` | Render the compatibility relay page |
| GET | `/glossary` | Render the glossary page |
| GET | `/settings` | Render the settings page |

## Contract observations

1. External versioning is inconsistent: editor operations live below `/api/v2`, while related project naming/export lifecycle remains below `/api/workbench`.
2. A project identifier is also treated as a split-job identifier by the frontend, so project identity and execution identity are coupled.
3. Start/status/cancel semantics differ between main jobs, translation, debug work and downloads; calibration and review are synchronous despite exposing task-like status files.
4. Error bodies and conflict behavior are not normalized across surfaces. Each frontend page has its own `fetch` wrapper.
5. The hidden import/relink APIs must be added to the explicit public or compatibility contract before router restructuring.
6. `/api/v2/projects/{project_id}/export/{mode}` and `/subtitles/{mode}` use ambiguous historical mode names that require compatibility aliases during naming migration.
7. There is no event/replay endpoint. All current asynchronous UI updates use polling.

Phase 2 must define the canonical resource and task contracts before any existing endpoint is moved or removed.
