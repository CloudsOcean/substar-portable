# Phase 10 v2 convergence acceptance

Date: 2026-08-31 (Asia/Shanghai)

This report records the release-gate checks against the v2 portable production
tree. Both projects are ordinary creation jobs under `data/projects-v2` and are
visible in the Split page's recent-task list. Historical `data/projects` is not
read, migrated or advertised.

## Test material

| Asset | SHA-256 | Notes |
| --- | --- | --- |
| `C:\temp\R01_No Music_Map_Animation.mp4` | `9B418A1B33D6FA1A6A8821D331E562CC45543979B848E12BE634A1E486A7A637` | H.264 + AAC, 147.272125 s, 165735220 bytes |
| `C:\temp\参考文稿.docx` | `FB8B57934BFB0947FBA30AF2C0F1D3B27200FD010B3B352A4834722FDA9D686F` | Reference manuscript used in both workflows |

The source backend was started with the bundled FFmpeg on `PATH`, the canonical
v2 data root and unrestricted provider network access. Localhost GUI automation
was unavailable because the host browser-control policy rejects localhost; the
same multipart/editor HTTP endpoints used by the GUI were exercised directly.

## Project A: reference supplied during creation

- Project ID: `20260831_033108_split_7d0280`.
- Workflow: Qwen ASR -> AI segmentation with reference manuscript -> AI
  calibration -> one-to-one translation.
- Creation result: `awaiting_edit`; 53 source Cues.
- Creation changes include reference projection, insertions and alignment.
- Successful calibration task: `tsk_c024bb1a8b0345db96566ff884b0d39b`.
- Calibration result: `succeeded`; result revision
  `rev_09a596445ee02d5b83536ca8`; no unresolved block.
- Translation task: `tsk_29a599c5eae3433f83f89ee14d8ee663`.
- Translation mode frozen as `one_to_one`.
- Primary result: 42/53 units accepted.
- Single repair phase: 11/11 rejected units processed once and accepted.
- Translation result: `succeeded`; result revision
  `rev_9ff53903b52e72dd30a8c225`; 53/53 editable translations.

## Project B: reference supplied in the editor

- Project ID: `20260831_033128_split_accafd`.
- Workflow: Qwen ASR -> AI segmentation only -> editor reference-manuscript
  injection -> many-to-many translation.
- Creation result: `awaiting_edit`; 49 source Cues.
- Editor reference result: 71 changes applied; similarity 0.957295; result
  revision `rev_f9caf0f86b7e6574f0947430`.
- Translation mode frozen as `many_to_many`.
- First live run accepted 27/27 groups. It exposed an empty repair-stage event
  (`repair_planned=0`) and was used to fix the lifecycle rule: an empty repair
  phase is now skipped and does not consume `repair_phase_entered`.
- Final post-fix task: `tsk_daa3f1eb23bb40e0b00642e922985259`.
- Final post-fix result: `succeeded`; 27/27 primary groups accepted,
  `repair_phase_entered=false`; result revision
  `rev_0f5ea375999ceffbc1ea4a97`; 49/49 editable translations.

The different denominators (53 one-to-one units and 27 many-to-many meaning
groups) prove that the translation selector changes the execution contract and
is not display-only.

## Defects caught by the live gate

The gate stopped release for three real integration defects:

1. Calibration/translation worker stdin inherited the Windows code page while
   the supervisor wrote UTF-8 JSONL. Both workers now explicitly reconfigure
   stdin/stdout/stderr to UTF-8.
2. Calibration correctly committed a revision but published an empty revision
   id because the internal save boundary returned a mapping. The worker now
   uses the canonical revision-id extractor before Runtime finalization.
3. Translation emitted `repair 0/0` even when every primary unit passed. The
   callback now emits repair progress only when repairable units exist.

Each defect has an automated regression test and a post-fix live rerun.

## Deterministic failure-path checks

Automated tests inject invalid/missing blocks without corrupting the retained
live projects. They prove:

- segmentation, calibration and translation run one primary phase and at most
  one repair phase;
- repair receives the rejected output and validator error, and only failed
  units are retried;
- a task with zero repairable units never enters repair;
- successful partial output is retained;
- an unresolved translation materializes `target_text=""`,
  `translation_status="manual_required"`,
  `issue_code="translation_unresolved"`, and `editable=true`;
- authentication/configuration/capability errors do not enter content repair;
- bounded transport retries do not create extra semantic repair attempts;
- GLM forced-thinking routing becomes thinking enabled with Low reasoning;
- non-current project/runtime schemas are rejected rather than migrated.

## Automated gates

- Python: 339 passed after the final lifecycle and old-format rejection
  regressions; the release build reruns the same suite.
- Browser/Node contracts: 78 passed.
- Targeted translation lifecycle: 24 passed.
- Executable system map: passed (`scripts/system_map.py --check`).

Packaging, checksums, cold-start smoke, commit, tag and GitHub Release are the
remaining final gates. Their immutable artifact details are added by the release
commit/build metadata rather than guessed in this report.
