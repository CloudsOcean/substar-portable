# Cue Script v1 real-API audit — 2026-09-04

## Scope and evidence

The source was the existing 12:22 `vol3 long2` project
`20260904_125956_split_d6097f`. ASR evidence was reused; no ASR request was
made. The configured `glm-5.3-flash` endpoint processed registered Runtime tasks
that appear in Recent Tasks. Exact system prompts, local-alias requests, raw
responses, deterministic finalized results and provider telemetry are retained
under:

- `data/experiments/cue_text_v1_finalizer_audit_20260904/`
- `data/experiments/cue_text_v1_many_to_many_limit_fix_20260904/`
- `data/experiments/cue_text_v1_many_to_many_finalizer_replay_20260904/`

Each archive contains a SHA-256 manifest. The first attempted experiment also
remains as a failed Recent Task with its Windows network-denial error; it was not
counted as a model result.

## Full concurrent run

Projects and task IDs:

| Stage | Project | Task | Result |
|---|---|---|---|
| segmentation | `20260904_151201_cue_text_split_7562e8` | `tsk_a6d1ea9fb1d144aebdbc69a418a90f68` | succeeded, 299 Cues |
| calibration | `20260904_151201_cue_text_cal_7562e8` | `tsk_948c77d481e0449e83ddbfb52ea50a7d` | delivered; 3 review blocks |
| one-to-one translation | `20260904_151201_cue_text_tr1_7562e8` | `tsk_29dbe9dfe3fb46afa8d30f6e2be7f9f4` | succeeded |
| many-to-many translation | `20260904_151201_cue_text_trm_7562e8` | `tsk_0378322fb6434adb8cebdf6dca47735c` | delivered; 2 issue blocks before the final fixes |

| Stage | Provider calls | Repairs | Prompt tokens | Completion tokens | Reasoning tokens | Total tokens | API sum | Wall |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| segmentation | 9 | 0 | 51,289 | 8,118 | 4,779 | 59,407 | 153.396 s | 39.010 s |
| calibration | 8 | 0 | 8,277 | 4,093 | 97 | 12,370 | 59.978 s | 14.966 s |
| one-to-one | 9 | 1 | 12,553 | 2,697 | 13 | 15,250 | 72.435 s | 16.516 s |
| many-to-many | 14 | 6 | 26,931 | 3,249 | 29 | 30,180 | 90.932 s | 24.804 s |
| all | 40 | 7 | 99,050 | 18,157 | 4,918 | 117,207 | 376.741 s | 70.005 s end-to-end |

Prompt tokens were 84.5% of the total. Segmentation accounted for 50.7% of all
tokens, calibration 10.6%, one-to-one 13.0%, and many-to-many 25.7%. The full
run's unique provider-visible system prompt sizes were 6,619 characters for
segmentation, 1,835 for calibration, 1,710/1,994 for one-to-one primary/repair,
and 3,002/2,148 for many-to-many primary/repair.

After segmentation completed, calibration and both translation modes overlapped
in wall time. Their individual walls total 56.286 seconds but the concurrent
phase completed in about 25 seconds plus scheduling overhead. This verifies that
cloud tasks no longer hold the global `project_write` resource.

No verified public split input/output unit price for the exact configured model
was available, so the archive deliberately records cost as unknown rather than
inventing a currency amount. Token counts are exact provider telemetry.

## Calibration audit

Calibration checked 299 Cues in 8 blocks with zero failed or repaired blocks.
The deterministic finalizer produced 201 actions: 197 automatic and four
review-only lexical changes. Those four were `Por -> ¿Por`, `us -> U.S.`,
`sensible -> supposed`, and `chance -> chant`; they occupied three blocks.
Punctuation, case and seven safe span merges were accepted independently, and a
token could participate in compatible sequential actions. “Three review blocks”
therefore describes uncertain lexical proposals, not three structurally failed
blocks.

## Translation failure analysis and fixes

The one-to-one run succeeded. Four complete primary blocks returned exactly one
translation per Cue but omitted/repeated only local labels; frozen-order recovery
restored those labels locally. One remaining target-length block was repaired.
Without that finalizer rule, four unnecessary block repair calls would have been
made.

The first many-to-many run showed three independent causes:

1. the request discussed `hard_limit` but did not transmit the numeric limit;
2. providers emitted recoverable fixed-width forms such as `C026+27译文` and
   flattened fields such as `C007<TAB>C008+C009<TAB>译文`;
3. a repair row mixed an OWN alias with a known CONTEXT alias, so the old
   finalizer discarded the whole row instead of preserving the writable subset.

The protocol now sends `TARGET_LIMIT`/`TARGET_LIMITS` plus `COUNT_RULE`, renders
each length error once as `ACTUAL`, `REQUIRED_MAX`, `ACTION` and `REJECTED`, and
uses OWN/CONTEXT as the only frozen mask. The finalizer normalizes fixed-width
alias typography and ignores known CONTEXT members of a mixed repair row while
never mutating them. Many-to-many output is never positionally guessed.

## Final cache replay

Final project `20260904_152606_cue_text_trm_a9d646`, task
`tsk_c23f6334432e487aae4be3c646e06d1a`, completed with zero problem blocks and
zero validation warnings. It replayed 5 accepted primary blocks and 1 repair
from validated content-addressed raw cache; 3 primary and 2 repair calls were
actually sent to the provider. All cached raw text was re-finalized by the new
code against the cloned project's local ledger.

The replay made 5 paid provider calls, used 9,876 prompt + 1,372 completion =
11,248 total tokens (231 reasoning tokens included in completion telemetry),
summed 48.328 seconds of provider time, and completed in 25.398 seconds. Its
three length-repair blocks all passed; no structure repair or manual review
remained.

## Remaining optimization boundary

Project-local raw caching is safe and effective. A production cross-project
cache is deferred until its key also includes canonical provider identity/base
URL and the store has bounded retention. Sharing only on model name would be an
unsafe cache collision. The larger remaining cost is segmentation prompt/input
volume, followed by many-to-many's language-planning complexity; neither should
be optimized by weakening identity coverage or final validation.
