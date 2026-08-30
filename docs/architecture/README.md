# Substar architecture record

This directory is the durable source of truth for the Substar backend refactor. It is updated at phase boundaries so decisions do not depend on chat history.

## Current executable authority

- [`system-map.json`](system-map.json) is the machine-readable authority for every production module, contract, caller/consumer relationship, recovery path, test and release boundary.
- [`system-map.md`](system-map.md) is the generated human-readable module map, including browser API callers, backend APIs, Scheduler/Worker/Finalizer chains, provider connectors, editor services, settings, glossary, export, launcher and packaging.
- [`system-map.mmd`](system-map.mmd) is the generated whole-system Mermaid graph.

Regenerate with `python scripts/system_map.py`. CI and the Windows release build run `python scripts/system_map.py --check`; they fail when production files are unowned, symbols/tests are missing, contracts lack producers or consumers, Worker results lack a Finalizer, or generated views are stale.

## Phase 0 — frozen baseline

- [Baseline report](baseline/README.md)
- [Frozen OpenAPI](baseline/openapi.json)
- [UI screenshots](baseline/ui)

## Phase 1 — current system audit

- [Current system map](current-system-map.md)
- [Current data flow](current-data-flow.md)
- [API inventory](api-inventory.md)
- [Task and state inventory](task-state-inventory.md)
- [Frontend module map](frontend-module-map.md)
- [Module catalog](module-catalog.md)
- [Legacy naming map](legacy-naming-map.md)

## Phase 2 — frozen target architecture

- [Target architecture index](target/README.md)
- [Target architecture](target/target-architecture.md)
- [Target module map](target/target-module-map.md)
- [Task runtime contract](target/task-runtime-contract.md)
- [Communication contract](target/communication-contract.md)
- [Data ownership](target/data-ownership.md)
- [Canonical naming](target/canonical-naming.md)
- [Migration strategy](target/migration-strategy.md)
- [Acceptance cases](target/acceptance-cases.md)

## Status

- [Current implemented module architecture](implementation/current-module-architecture.md)
- [Phase 5 segmentation cutover](implementation/phase-5-segmentation-cutover.md)
- [Phase 9 release acceptance](implementation/phase-9-release-acceptance.md)
- [Phase 10 convergence repair plan](implementation/phase-10-convergence-repair-plan.md)
- [Phase 10 convergence acceptance](implementation/phase-10-convergence-acceptance.md)

- Audited source commit: `932a5be`
- Phase 0: complete
- Phase 1: complete (current-system audit)
- Phase 2: complete (target architecture and contracts)
- Phases 3-9: implemented and release-accepted
- Implementation records are kept separate from the frozen current/target records:
  [Phase 3 runtime and instance foundation](implementation/phase-3-runtime-foundation.md).
