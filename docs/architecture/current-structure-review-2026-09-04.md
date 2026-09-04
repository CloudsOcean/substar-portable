# Substar structure review — 2026-09-04

## Finding

The project is now a modular local monolith with one durable task runtime and a
strong immutable document boundary. The main architectural risk is no longer
multiple task systems. It is concentration inside several orchestration and UI
files, plus a few internal legacy translation names that survive behind the new
Cue-owned text protocol.

## Boundaries worth preserving

- `runtime/service.py`, `runtime/scheduler.py`, `runtime/store.py` and worker
  protocol own every task lifecycle. Segmentation, calibration and translation
  are peers in this container.
- `ProjectStore` is the only editable-document authority. Expected revision and
  content hashes protect final publication.
- `cue_script.py` owns the provider-visible C/W alias grammar and deterministic
  finalizers. Persistent project, Cue and token IDs do not leave the process.
- The prompt registry composes semantic policy; one output contract is appended
  last by code. Empty optional sections such as an empty glossary are omitted.
- Raw model responses are retained and re-finalized against the current local
  ledger. A provider response is not a document mutation.

## Current size and concentration signals

| File | Approximate lines | Responsibility pressure |
|---|---:|---|
| `web/editor.js` | 4,569 | editor shell, polling, commands and several panels |
| `substar_core/editor/http_api.py` | 3,822 | router plus calibration application service seams |
| `app.py` | 2,971 | composition root plus project-creation facade |
| `scripts/run_semantic_segmentation.py` | 1,857 | planning, provider exchange, validation, repair and artifact assembly |
| `substar_core/editor/translation/contextual.py` | 1,418 | planning, cache/exchange, finalization, repair and materialization |
| `substar_core/runtime/scheduler.py` | 871 | task scheduling and resource ownership |
| `substar_core/cue_script.py` | 783 | three text grammars and deterministic finalizers |

These are maintainability signals, not proof of incorrect behavior. The current
Python module graph remains acyclic and the domain/storage boundaries are clear.

## Prompt injection shape

The intended provider request is:

1. shared semantic policy;
2. task or translation-mode policy;
3. direction and a non-empty glossary only when applicable;
4. exactly one authoritative output contract appended last;
5. one execution block containing local aliases, OWN/CONTEXT flags and source
   text;
6. the exact target limit and counting rule; and, on repair only, the same
   complete block plus one compact copy of every program validation error.

`OWN`/`CONTEXT` on each input row is the frozen mask; a second alias list would
only duplicate it. The provider does not need persistent IDs, legacy
meaning-group objects, accepted output from other blocks, repeated examples,
raw rejected envelopes or an empty glossary. Exact system prompt, request, raw
response, finalized result, usage and timing are archived for audit.

## Cache and repair assessment

The current project-local cache is content-addressed by the exact system prompt,
user request, model policy and wire schema. Cached raw text is safe to reuse
within the project because it is always re-finalized against the current ledger;
invalid output is never saved. It already prevents paying for primary calls
again after a worker/finalization failure.

Cross-project cache sharing is intentionally not enabled yet. Before doing so,
the key must also bind the canonical provider identity/base URL and the shared
store needs bounded retention. Otherwise two providers with the same model name
could incorrectly share text. This is an optimization phase, not a correctness
fix.

One-to-one translation now has a narrow local recovery: when an all-OWN block
returns exactly one non-empty row per Cue, output order is exact and no unique
alias contradicts that order, omitted or repeated local labels are restored
without a repair call. Many-to-many is never rebound by position. All remaining
repair is block-wide and receives all errors for that block once.

The repair finalizer also normalizes fixed-width short join aliases and flattened
tab-separated alias fragments. If a row contains both known OWN and known
CONTEXT aliases, only the OWN subset is retained and the frozen CONTEXT subset
is audited and ignored; persistent or unknown identities are never guessed.

## Concurrency finding

Calibration and translation previously held the scheduler's global
`project_write` resource during the entire cloud inference task. That serialized
otherwise independent projects. They now claim only `worker` and `provider_io`;
ProjectStore protects the short final publication. The editor still prevents two
exclusive AI operations on the same project and a revision conflict fails rather
than overwriting newer work.

## Refactoring order

1. Extract calibration orchestration from `editor/http_api.py` into a dedicated
   application service; leave the router as validation and transport only.
2. Split `contextual.py` into block planning, exchange/cache adapter,
   translation compiler and document materializer modules.
3. Split `run_semantic_segmentation.py` into orchestration, Cue Script adapter,
   candidate validator and artifact writer.
4. Split `web/editor.js` by feature controller while preserving the shared
   document store and progress projection.
5. Introduce a shared `ModelTextExchangeRecorder`; only then consider a bounded,
   provider-bound cross-project raw-response cache.
6. Remove internal `meaning_unit`/`meaning_group` compatibility names after the
   stored translation-result contract is intentionally versioned. Do not mix
   that storage migration into provider-protocol work.

## Evidence

The registered experiment archive under `data/experiments/` contains exact
provider exchanges, task snapshots, published artifacts, token/timing summaries
and a SHA-256 manifest. The canonical map's registered tests cover runtime
resource policy, block repair, finalizer binding and prompt composition.
