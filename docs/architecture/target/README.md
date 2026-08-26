# Target architecture record

This directory freezes the Phase 2 architecture boundary for the Substar refactor. Later implementation phases may refine internal code, but must not silently change these public concepts, state authorities or compatibility rules.

## Decisions

- [Target architecture](target-architecture.md)
- [Target module map](target-module-map.md)
- [Task runtime contract](task-runtime-contract.md)
- [Communication contract](communication-contract.md)
- [Data ownership](data-ownership.md)
- [Canonical naming](canonical-naming.md)
- [Migration strategy](migration-strategy.md)
- [Acceptance cases](acceptance-cases.md)

## Machine-readable contracts

- [Task schema](contracts/task.schema.json)
- [Task event schema](contracts/task-event.schema.json)
- [Worker command schema](contracts/worker-command.schema.json)
- [Worker message schema](contracts/worker-message.schema.json)
- [API error schema](contracts/api-error.schema.json)
- [Transcription request schema](contracts/transcription-request.schema.json)
- [Recognition evidence schema](contracts/recognition-evidence.schema.json)
- [Provider submission audit schema](contracts/provider-submission-audit.schema.json)
- [Transcription result schema](contracts/transcription-result.schema.json)
- [Segmentation request schema](contracts/segmentation-request.schema.json)
- [Segmentation candidate schema](contracts/segmentation-candidate.schema.json)
- [Segmentation result schema](contracts/segmentation-result.schema.json)

## Frozen principles

1. One local modular monolith, not distributed microservices.
2. One durable task runtime for all long-running work.
3. REST for commands/queries and revision edits; replayable SSE for task events.
4. Supervised worker processes for long-running or cancellable work.
5. Per-project SQLite remains the authority for subtitle revisions.
6. Runtime SQLite is the authority for task lifecycle and event history.
7. Artifacts are registered outputs, never inferred task state.
8. Existing UI and editor-domain behavior are preserved behind new adapters.
9. New code uses business names only; historical names stay inside compatibility readers/tests.
10. Migration proceeds by vertical production cutovers, without a user-visible old/new route selector.

## Status

- Based on current-system audit commit: `6016cde`
- Phase 2 contract status: frozen
- Phase 3 implementation: unified runtime and instance supervision complete
- Phase 4 implementation: canonical transcription cutover complete
- Phase 5 implementation: canonical segmentation and initial editor-document cutover complete
- Phase 6 implementation: translation, calibration and review duties separated
- Phase 7 implementation: exclusive editor AI task locking complete
- Phase 8 implementation: production naming and storage cutover complete
- Phase 9 implementation: release acceptance complete
