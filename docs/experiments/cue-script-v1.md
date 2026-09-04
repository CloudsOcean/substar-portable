# Cue Script v1 experiment

## Goal

Test a non-JSON model boundary for semantic segmentation, AI calibration, and
subtitle translation without changing the three tasks' existing scheduler,
cache, retry, validation, progress, revision, or delivery contracts.

The application remains the authority for identities and structure. The model
only sees short, request-local aliases and returns tab-delimited Cue Script.
A deterministic finalizer compiles that text back into each task's frozen
canonical contract.

## Shared wire envelope

Segmentation and calibration use a small `SUBSTAR-CUE-SCRIPT/1` envelope;
translation uses bare C-alias mapping rows. The exact grammar is appended once,
after semantic rules and task configuration.

Aliases are created from request order and exist only for one model call.
Internal project, group, Cue, and token IDs never cross the model boundary.
Context rows can be supplied but are read-only. Duplicate rows cannot steal an
identity; a missing row remains missing and enters the existing repair path.

## Task-specific payloads

- Segmentation returns consecutive Cue aliases and inclusive local word ranges.
  It does not repeat source previews. The finalizer proves exact, ordered coverage of
  every owned word before producing `substar.semantic-grouping-result.v1`.
- Calibration returns the complete corrected text of each owned Cue. The
  finalizer aligns it against the immutable token ledger and emits the existing
  action vocabulary. Case and light punctuation are independent actions; safe
  N:1 written-form merges such as `u s -> U.S.` are supported. Insertions,
  deletions, token splits, strange symbols, and dissimilar lexical rewrites do
  not auto-apply.
- Translation returns target-language text bound to one local Cue alias or a
  consecutive `+` alias range. The finalizer restores canonical group/Cue IDs.
  One-to-one remains one-to-one; many-to-many delivery structures are compiled
  from explicit alias ranges. Valid rows are frozen and only unresolved aliases
  are writable during repair.

## Safety and delivery invariants

1. The model cannot author persistent IDs, timestamps, revisions, or cache keys.
2. The finalizer must validate ownership, alias coverage, ordering, and shape.
3. Transport success is not task success; only finalized canonical output may
   enter the existing validator and guaranteed-delivery pipeline.
4. One repair request receives the complete original block, all block errors,
   and explicit OWN/CONTEXT ownership; only unresolved aliases are returned.
5. Exact system prompt, local-alias request, raw response, finalized response,
   telemetry, and finalizer errors are saved for every call. Exchange filenames
   are immutable and nanosecond-prefixed.
6. Calibration may reuse a token across compatible sequential actions. This is
   required for case plus punctuation and is not an identity conflict.

## Real-API acceptance criteria

- Run the video through normal registered transcription and segmentation, then
  registered editor calibration and one-to-one translation tasks.
- Exercise block concurrency and preserve every raw exchange.
- Report primary/repair call counts, prompt/completion/reasoning/cache tokens,
  summed provider time, task wall time, and effective concurrency.
- Require no empty translated Cue and no unresolved repair group.
- Treat structural success separately from linguistic quality. Semantic
  calibration rewrites must remain review-only even when structurally valid.

The full reproducible runner is `scripts/run_registered_cue_experiment.py`.
Focused translation/cache replay uses
`scripts/rerun_registered_translation_experiment.py`. Output lives under
`data/experiments/<label>/` and includes raw exchanges, selected task artifacts,
a machine-readable report, and a SHA-256 manifest.

The 2026-09-04 real-API results and failure analysis are recorded in
[`cue-script-v1-real-api-audit-2026-09-04.md`](cue-script-v1-real-api-audit-2026-09-04.md).
