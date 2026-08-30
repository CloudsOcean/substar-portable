# Phase 10 convergence acceptance

Date: 2026-08-30 (Asia/Shanghai)

This report records the reproducible checks performed against the portable
production tree after the convergence repair.  The two projects below are
ordinary GUI-created projects under `data/projects` and remain visible in the
Split page's recent-task list.

## Test material

| Asset | SHA-256 | Notes |
| --- | --- | --- |
| `C:\temp\R01_No Music_Map_Animation.mp4` | `9B418A1B33D6FA1A6A8821D331E562CC45543979B848E12BE634A1E486A7A637` | H.264 + AAC, 147.272125 s, 165735220 bytes |
| `C:\temp\参考文稿.docx` | `FB8B57934BFB0947FBA30AF2C0F1D3B27200FD010B3B352A4834722FDA9D686F` | Reference manuscript used in both workflows |

The backend was started through the portable launcher with the bundled
FFmpeg on `PATH` and unrestricted provider network access.

## Project A: reference supplied during creation

- Project ID: `20260830_231306_split_0a585e`
- Workflow: Qwen ASR -> AI segmentation with reference manuscript -> AI
  calibration -> one-to-one translation.
- Creation result: editable project delivered; 53 source Cues.
- Reference result: 12 visible reference change locations after creation.
- Calibration result: `succeeded`, 2/2 blocks accepted, no repair and no
  manual handoff.
- Translation mode frozen in the task: `one_to_one`.
- Translation result: `succeeded`, 53/53 target tracks delivered.
- Primary translation result: 44/53 groups accepted.
- Single repair phase: 9 failed groups planned, 9 processed, 9 accepted, no
  repair-of-repair.
- Final document: 53 Cues and 53 target tracks; no unresolved target in this
  particular live run.
- Provider route: `model_provider:glm`, model `glm-5.3-flash`.
- Request telemetry: requested/effective thinking `enabled`, requested/effective
  reasoning `low`; observed transport-attempt count 1 per recorded request.

## Project B: reference supplied in the editor

- Project ID: `20260830_231322_split_016ac3`
- Workflow: Qwen ASR -> AI segmentation only -> editor reference-manuscript
  injection -> many-to-many translation.
- Creation result: editable project delivered.
- Editor reference result: 64 words marked, similarity 95.9%.
- Translation mode frozen in the task: `many_to_many`.
- Translation result: `succeeded`, 23/23 meaning groups accepted without
  entering repair.
- Final document: 49 Cues and 49 target tracks; no unresolved target in this
  particular live run.
- Provider route: `model_provider:glm`, model `glm-5.3-flash`.

The different denominators (53 one-to-one groups versus 23 many-to-many
groups) prove that the translation selector changes the grouping contract and
is not a display-only option.

## Deterministic failure-path checks

Automated contract tests inject malformed/missing model units and provider
failures without corrupting the retained live projects.  They verify:

- calibration runs one primary phase and at most one repair phase;
- translation repairs only failed groups and preserves accepted groups;
- each failed translation group has at most one repair request;
- an unresolved translation produces a real blank target track with
  `translation_status=manual_required`,
  `issue_code=translation_unresolved`, and `editable=true`;
- the editor renders a target textarea when the target track exists even when
  its text is blank;
- authentication/configuration failures do not enter content repair;
- retryable transport failures use bounded transport retries and do not enter
  content repair after exhaustion;
- GLM forced-thinking routing maps to thinking enabled with Low reasoning;
- one corrupt legacy project is isolated instead of returning HTTP 500 for
  the global editor-task feed.
- legacy revision checksums are verified against the exact reconstructed
  on-disk JSON before backward-compatible schema defaults are applied;
  normalized current documents retain their current runtime hash.

The compatibility path was also exercised against the retained real project
`20260828_234830_split_3a0dc7`.  After a full-permission backend restart, the
live HTTP API loaded revision `rev_3850a37bde124eae45668d42` with 26 Cues and
returned the normalized document hash without an integrity error.  The global
editor-task feed returned independently at the same time.

## Automated gates

- Python: 340 passed.
- Browser/Node contracts: 78 passed.
- Executable system map: passed (`scripts/system_map.py --check`).
- Targeted failure/lifecycle suite: 61 passed.

No release was built from this working tree during this acceptance run.
Versioning, packaging, tag and GitHub Release remain a separate final gate.
