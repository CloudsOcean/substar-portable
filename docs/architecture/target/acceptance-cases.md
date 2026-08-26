# Refactor acceptance cases

## Primary real-world media case

The user-provided video is the canonical end-to-end acceptance input for the refactor.

| Property | Value |
| --- | --- |
| External source | `C:\Users\Administrator\Downloads\9_d0JdVfQ-0eY_-E.mp4` |
| Repository policy | referenced externally; not copied into Git |
| SHA-256 | `E11F63AC3D8D7196F1E36702B524D4C5BC46998209FAA4029271598CC55D28B3` |
| File size | 9,521,125 bytes |
| Container | MP4/QuickTime family |
| Duration | 85.101134 seconds |
| Video | H.264 High, 720 × 1280, 9:16, 30 fps, yuv420p |
| Audio | AAC-LC, 44.1 kHz, stereo, 128 kbps |
| Embedded captions | none |

The input language, glossary and prompt parameters are supplied by the acceptance run; they are not inferred from the filename or frozen in this document.

Before every end-to-end run, the harness verifies the checksum. A missing or changed external file is reported as an unavailable fixture, not as a product regression.

## End-to-end success scenario

```text
create stable project
→ attach/probe media
→ extract canonical audio
→ send transcription language/context/hotwords
→ persist immutable recognition evidence
→ run semantic subtitle segmentation
→ validate and commit initial editor revision
→ open media/waveform/editor
→ edit and commit at least one operation batch
→ translate from a named source revision
→ run calibration and review
→ export source, target and bilingual subtitles
```

Acceptance assertions:

1. The project ID remains stable through rename, retry and restart.
2. Task IDs are distinct from project ID and display name.
3. Every long operation appears in the canonical task API and event stream.
4. Progress remains in `0..1` and uses canonical business steps.
5. Recognition evidence contains sentence/word timing and speaker metadata when returned by the provider.
6. The glossary compilation snapshot proves what was sent to each eligible consumer.
7. Source tokens preserve recognition-evidence lineage and timing.
8. Segmentation does not lose or reorder source evidence and produces a structurally valid editor revision.
9. The editor loads the original portrait video, supports Range playback, waveform windows and cue seeking.
10. Editing produces an optimistic operation followed by one authoritative SQLite revision.
11. Translation is bound to the selected source revision and preserves valid 1:1/1:N/N:1 presentation mapping.
12. Calibration changes only permitted punctuation/case content; review does not mutate the document.
13. Exports are registered artifacts with checksums and valid ordered time ranges.
14. No new artifact/API/UI output exposes historical experiment/stage route names.

## Instance behavior scenarios

### Start while healthy instance exists

1. Start the packaged/source application.
2. Start a long task.
3. Launch Substar again.
4. Assert the original backend PID/start time and task continue unchanged.
5. Assert the existing UI is opened/focused.

### Graceful shutdown

1. Start a cancellable task.
2. Request graceful application shutdown.
3. Assert new dispatch stops.
4. Assert task ownership is reconciled to a durable terminal/interrupted state.
5. Assert no worker/process descendants remain unexpectedly.
6. Restart and assert task history/event cursor remain available.

## Task durability scenarios

### Idempotent submission

- send the same task request and `Idempotency-Key` twice;
- assert one task/attempt/provider submission;
- change the payload with the same key and assert `409 idempotency_conflict`.

### Cancellation

- cancel once while queued and once during provider/worker execution;
- assert `cancel_requested` is durable before signaling;
- assert state is not `cancelled` until process cleanup is verified;
- assert project directory remains present and valid;
- assert repeated cancel is safe and idempotent.

### Backend crash/restart

- terminate the API during transcription provider polling, segmentation and translation in separate runs;
- restart the application;
- assert expired ownership is reconciled to `interrupted` or a safe provider resume;
- retry/resume and assert no duplicate project revision or provider submission.

### Worker crash

- terminate only the task worker;
- assert supervisor detects exit without waiting for stale file polling;
- assert structured failure/interruption and retained logs/artifacts.

### Revision conflict

- start translation/calibration against revision A;
- commit an edit producing revision B before finalization;
- assert the worker result cannot silently overwrite B;
- assert a structured retryable revision conflict and retained candidate artifact.

### SSE replay

- receive several task events and record the cursor;
- disconnect while work continues;
- reconnect with `Last-Event-ID`;
- assert ordered at-least-once delivery and client deduplication;
- test an expired cursor and snapshot reset behavior.

## Frontend preservation scenarios

- Phase 0 page screenshots remain the visual reference.
- Navigation, settings, glossary, create page and editor layout remain recognizable and functional.
- No page owns a distinct error decoder or long-task poll loop after migration.
- Rapid project A → B switching cannot allow A's late response to overwrite B.
- Media playhead, active cue, cue list and timeline remain synchronized.
- Network disconnect does not discard pending editor operations or durable task identity.

## Large-document synthetic case

The 85-second real video is representative of the product workflow but not editor scale. A generated document fixture supplies at least:

```text
10,000 cues
100,000 display tokens
mixed Latin/CJK content
speaker changes
translation tracks
pending operation bursts
```

Required checks:

- playback-time cue lookup is logarithmic/indexed rather than full-array scan per frame;
- the cue-list DOM remains bounded while scrolling the complete project;
- pending operations do not deep-clone/replay the entire document per keystroke;
- operation batching and revision conflict behavior remain correct;
- timeline/waveform rendering stays windowed;
- memory returns to a stable range after navigating away/reloading a project.

Numeric latency/memory budgets will be recorded from the pre-optimization fixture before the frontend performance slice and then frozen as release gates.

## Breaking-version boundary cases

- a canonical project is discovered only by its exact directory ID and `project/manifest.json`;
- a historical project directory or manifest is not scanned, renamed or imported implicitly;
- removed write routes are not registered;
- an unknown canonical schema version fails explicitly without modifying user data;
- source, target and bilingual exports remain reproducible for current-schema projects;
- only the unified purpose-and-provider credential envelope is read.

## Phase gates using this case

| Phase/slice | Required use of the real video |
| --- | --- |
| Runtime foundation | task/process test handler only; video fingerprint/probe may be used |
| Review/calibration | use an existing or baseline editor revision when available |
| Translation | use a frozen revision derived from the case or an equivalent fixture |
| Transcription/segmentation | mandatory full video processing |
| Frontend communication | mandatory media/waveform/task-event interaction |
| Release acceptance | mandatory complete workflow plus at least one cancellation/restart drill |
