# Migration strategy

## Decision

Migration uses vertical production cutovers inside one repository. The new control plane is built alongside the current implementation only long enough to move and verify each behavior. There is no user-visible experiment/branch selector and no permanent dual runtime.

## Invariants throughout migration

- the current frontend remains usable;
- only projects created by the new canonical schema are supported;
- current editor revisions are never rewritten destructively;
- every production cutover has contract and recovery tests;
- one task type has exactly one production dispatcher after its cutover;
- old files, routes and settings are rejected rather than adapted;
- algorithms move only after characterization tests capture their current behavior;
- no production package contains a historical-project compatibility reader.

## Implementation slices

### Slice 1 — runtime and instance foundation

Build without migrating business algorithms:

- runtime SQLite schema/migrations;
- task state model, transitions, events and store;
- handler registry and scheduler;
- worker protocol/supervisor/process-tree cancellation;
- canonical task/events/runtime API;
- SSE replay and bounded polling recovery tests;
- instance behavior: second launch opens existing backend; graceful stop/restart;
- one low-risk `model_download` or test worker proves dispatch/cancel/recovery.

Exit gate: kill/restart/cancel/idempotency tests pass and non-current runtime databases are rejected.

### Slice 2 — review and calibration

- extract model-call and validation behavior out of `editor_api_v2.py`;
- register `calibration`; keep review external;
- remove their HTTP-request-bound execution;
- bind every task to an expected revision;
- preserve calibration as a validated revision commit;
- expose only canonical editor task projections.

Exit gate: network disconnect/restart does not lose task visibility; revision conflicts are deterministic.

### Slice 3 — translation

- extract presentation mapping from the experimental module;
- characterize 1:1, 1:N and N:1 behavior;
- move provider calls behind the gateway;
- register translation worker/finalizer;
- replace translation status JSON as lifecycle authority;
- reject historical translation artifacts rather than reading them.

Exit gate: identical source fixture produces contract-equivalent document/export, and interruption can resume or retry without duplicate revision commits.

### Slice 4 — transcription and segmentation

- normalize the recognition adapter contract;
- connect compiled glossary/hotword input;
- register transcription worker with resumable remote-provider metadata;
- extract active segmentation symbols from mixed historical files;
- preserve strict validation, one repair phase and deterministic partial delivery;
- register segmentation worker and initial-document finalizer;
- make the subtitle-creation workflow create a durable dependency graph.

Exit gate: the real acceptance video completes from upload through editable project using only canonical tasks/artifacts.

Implementation status: transcription completed in Phase 4; segmentation,
atomic task-graph publication and the initial editor-document finalizer
completed in Phase 5.

### Slice 5 — frontend communication

- add shared `ApiClient` and `TaskClient`;
- consume canonical project/task APIs and SSE;
- remove page-level task-status reconstruction and overlapping poll loops;
- split the editor coordinator at project/session/playback/render boundaries;
- fix rapid project-switch response ordering;
- optimize cue time index, projected document updates and bounded list virtualization;
- keep HTML/CSS/theme interaction unchanged.

Exit gate: UI snapshots remain consistent, task reconnection works and the 10,000-cue synthetic performance checks pass.

### Slice 6 — legacy removal and naming cleanup

- delete historical artifact/settings readers;
- migrate current prompt/task/artifact writers to canonical names;
- reject unknown or historical project schemas at the project boundary;
- remove production imports of experiment modules;
- remove unreachable routers/workflow branches and duplicated status stores;
- retain CLI/offline research scripts outside the product dependency graph.

Exit gate: new-schema fixtures open/export correctly, historical schemas fail explicitly, and forbidden historical names do not appear in canonical package/API/UI code.

### Slice 7 — release acceptance

- full real-video workflow;
- provider failure, cancellation, forced process exit and application restart;
- concurrent edit/revision conflict tests;
- import/export and portable packaging;
- performance and resource-limit tests;
- final recovery drill and release bundle smoke test.

## Deliberate breaking-version boundary

- A project must contain the canonical `project/manifest.json` and SQLite revision store.
- Historical directory names, relay files and experiment artifacts are not scanned.
- Removed HTTP routes are not registered and return the framework's normal 404/405 response.
- Settings accept only the current allowlisted keys.
- Credentials load only the unified purpose-and-provider envelope.
- User data is never deleted automatically; unsupported folders simply remain outside the new application's project catalog.

## Cutover method

Each slice follows:

```text
characterize current behavior
→ implement canonical contract behind tests
→ run old/new fixture comparison
→ switch the sole production composition binding
→ verify old shapes are rejected
→ remove the old dispatcher, status writer and reader
```

Temporary selection exists only inside tests/composition during development. It is not persisted as `route`, `branch`, `experiment` or a user setting.

## Rollback

- commits remain small and phase/slice scoped;
- schema upgrades inside the canonical generation are forward-only;
- a failed implementation cutover is reverted at the composition binding, not by mutating project data backward;
- task/runtime migrations include explicit version checks and startup refusal on unknown newer schemas.

## Definition of complete

The refactor is complete when:

- one task runtime owns all long-work lifecycle;
- no production long operation executes inside an HTTP request;
- second launch never silently kills a healthy instance;
- frontend uses one API client and one task event client;
- project revisions and recognition evidence have single declared authorities;
- production packages contain no dependency on `experimental` modules;
- new artifacts and UI expose no historical route/stage names;
- historical project formats and routes are absent from the production dependency graph;
- the real acceptance video passes the end-to-end and recovery scenarios.
