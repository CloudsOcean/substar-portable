# Data ownership and authority

## Core rule

Every mutable concern has exactly one durable authority. Other files, API payloads and in-memory objects are projections, caches or registered artifacts and must be rebuildable or explicitly versioned.

## Authority matrix

| Concern | Durable authority | Projections/caches | Forbidden authority |
| --- | --- | --- | --- |
| Runtime instance | live process identity + backend mutex | runtime discovery record | port ownership alone, stale `runtime.json` |
| Task lifecycle | `runtime.sqlite3: tasks/task_attempts` | frontend task store, compatibility status JSON | daemon-thread memory, artifact existence |
| Task events | `runtime.sqlite3: task_events` | SSE connection/browser cursor | polling timestamps |
| Task outputs | artifact file plus `task_artifacts` registration/checksum | download response, export list | unregistered file discovery as success |
| Project identity | project `manifest.json` | runtime project catalog/index | directory name or display name |
| Current subtitle document | latest valid revision in project SQLite | browser projected revision, export files | initial segmentation JSON |
| Editor history | project SQLite revision chain | named checkpoint list | browser local storage alone |
| Raw recognition evidence | immutable canonical recognition artifact | transcript text/TSV views | rewritten working alignment |
| Reference matching | versioned match artifact referencing raw evidence | effective transcript projection | destructive replacement of raw evidence |
| Translation applied to editor | project SQLite revision | translation run result and SRT | task status JSON |
| Review result | registered review artifact bound to revision | latest-review pointer | browser local storage |
| Settings | versioned atomic settings store | form state, frozen task input | page local storage for backend settings |
| Credentials | DPAPI-protected credential store | connected/capability projection | task payload, log or event |
| Glossary | versioned glossary store keyed by stable IDs/project IDs | compiled consumer snapshots | project display name matching |
| Prompt used by task | registered prompt snapshot/version in task artifacts | prompt registry current version | mutable registry value after task creation |
| Waveform/playback data | disposable cache derived from media fingerprint | browser LRU | project/task completion state |

## Runtime database

Location: selected application-data directory, for example:

```text
data/.substar-workbench/runtime.sqlite3
```

It contains task/attempt/dependency/event/artifact records and a reconstructable project catalog. SQLite settings use WAL, foreign keys, busy timeout and explicit migrations. Only the API/runtime process writes it; worker processes communicate through the supervisor.

The project catalog is an index of project manifests. If it is lost, manifests can rebuild it. Task history is not reconstructed from project artifacts.

## Project manifest

The manifest is small, portable and stable:

```text
schema_version
project_id
display_name
created_at
updated_at
media_reference
project_store_version
origin/import metadata
```

Changing `display_name` does not rename the stable project identity. Filesystem-safe directories are based on `project_id` only.

## Recognition evidence

Provider output is normalized once into a canonical, immutable recognition-evidence schema containing:

- media fingerprint and timing metadata;
- language request and detected/result language;
- provider/model metadata;
- sentence/chunk timing;
- word/token timing;
- speaker identity/confidence where available;
- glossary/hotword compilation audit;
- source provider response artifact reference.

Reference matching creates a separate artifact that maps normalized manuscript content to evidence token IDs/timing. It never overwrites the raw normalized evidence. Segmentation consumes a deterministic effective transcript view made from these two artifacts.

## Editor document

The existing source/display separation remains:

```text
Recognition evidence
→ immutable SourceToken lineage
→ editable DisplayToken
→ DisplayCue and SemanticGroup
→ revision-bound TranslationTrack/presentation
```

Segmentation output is a candidate until application finalization validates it and writes the initial project revision. After that point, the segmentation result remains an audit artifact and the SQLite revision is authoritative.

## Derived content

| Derived content | Source binding | Final authority after success |
| --- | --- | --- |
| Translation | expected source revision + target language + glossary/prompt versions | newly committed project revision |
| Calibration | expected revision + calibration policy/prompt | newly committed project revision |
| Review | immutable source revision + review request | registered review artifact |
| Export | requested revision + track/presentation mode | registered export artifact |
| Future dubbing | revision + language/voice/profile | registered audio/alignment artifact; never implicit editor text authority |

If a mutating result cannot be committed because the expected revision changed, the task does not report success. Its candidate artifacts remain available for audit or explicit rebase/retry.

## Settings and credentials

The existing portable/per-user selection and DPAPI behavior are retained behind store ports.

- settings writes are versioned and atomic;
- task input freezes relevant non-secret settings and records their schema/version;
- secrets enter a worker through an ephemeral channel/environment assembled by the supervisor;
- secret values are redacted before logging and never stored in task input/result/events;
- provider capability discovery is cache/projection data, not credential authority.

## Glossary compilation

Glossary entries use stable entry IDs and optional stable `project_id` scope. Before each consumer runs, a compiler creates and registers a versioned snapshot:

```text
asr_context
segmentation_constraints
translation_terms
calibration_protections
review_checks
dubbing_pronunciations
```

The snapshot hash enters task input/result metadata so a run is reproducible. A project rename cannot change glossary matching.

## Atomicity rules

- task state transition and corresponding event commit together;
- artifact registration happens only after checksum/path validation;
- project revision commit uses expected revision and checksum;
- task success follows required project/artifact commit, never precedes it;
- project deletion checks active task ownership transactionally before recoverable trash movement;
- import writes to a temporary project directory, validates manifest/store, then atomically exposes it.

## Caches and cleanup

`cache/`, temporary worker files and provider download scratch data are disposable. Cleanup is versioned policy executed as an explicit maintenance action. Immutable evidence, current project revisions, registered final outputs and task audit metadata are protected from cache cleanup.
