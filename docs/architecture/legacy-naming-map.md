# Legacy naming map

## Purpose

The current production code still exposes experiment-era names. This inventory separates business meaning from historical implementation labels. Final canonical names will be frozen in the target-architecture phase.

## Scale at audited commit

Counts include application code, scripts, schemas, prompts and tests, but exclude frozen baseline documentation, public project fixtures, build output and release archives.

| Term | Matches | Files |
| --- | ---: | ---: |
| `stage1` | 919 | 72 |
| `stage2` | 124 | 15 |
| `P2mix` | 171 | 17 |
| `T1mix` | 111 | 28 |
| `experiment` | 95 | 22 |
| `debug_merged` | 19 | 5 |
| `production_one_step` | 21 | 8 |
| `split_branch` | 21 | 4 |
| `_v2` | 183 | 32 |

## Meaning map

| Historical name | Current real meaning | Candidate business name | Migration treatment |
| --- | --- | --- | --- |
| `stage1` | semantic subtitle segmentation and validation | `segmentation` | rename active modules/settings/artifacts through compatibility readers |
| `stage2` | translation, rendering and older delivery checks | split into `translation`, `export`, `quality` | decompose rather than perform a blind rename |
| `stage1_experiment` | current production segmentation workflow/artifact directory | `segmentation` / `segmentation_run` | old directory remains readable; new writer uses canonical name |
| `run_experimental_stage1_from_ingest` | run production segmentation and materialize editor project | `run_segmentation_workflow` | direct active-code rename after contract freeze |
| `P1` | segmentation pre-analysis/protection/seam planning | descriptive planning name or removal | retain only if the production algorithm still executes it |
| `P2mix` | LLM semantic segmentation producing final cue grouping | `semantic_segmentation` | rename prompt registry keys, progress stage and artifacts |
| `P2mix-Repair` | repair invalid segmentation output | `segmentation_repair` | rename together with output schema |
| `P3` / `p3_cut_after` | cue layout and final display cut decisions | `cue_layout` / explicit boundary metadata | migrate serialized lineage explicitly |
| `T1mix` | contextual translation over editor cues/groups | `contextual_translation` | rename prompt keys, progress stage, telemetry and files |
| `production_one_step` | the only supported production segmentation route | no mode field | delete selector when there is only one handler |
| `merged_max_debug` | retired alternate experimental route | none | delete from product path; Git history retains it |
| `split_branch = A` | force the sole supported segmentation behavior | no branch field | delete field and UI label |
| UI `A/B/AB` exports | source/target/bilingual tracks | `source`, `target`, `bilingual` | replace ambiguous letters while keeping legacy URL aliases temporarily |
| `workbench_asr_split` | create editable subtitle project from media | `subtitle_creation` / `media_transcription` | choose one workflow identity in phase 2 |
| `full_pipeline` | ingest followed by segmentation and translation | `subtitle_creation` with requested steps | replace mode branching with explicit workflow input |
| `manual_relay` | legacy/manual exchange and compatibility export | `legacy` adapter or removal | isolate production-required readers; delete unused orchestration |
| `project_v2` | current canonical project store | `project` | version belongs in manifest/schema, not directory/module identity |
| `editor_api_v2` | current editor HTTP adapter | `editor_api` | retain `/api/v2` only as an external compatibility version if required |
| `*_v2` internal modules | several current canonical implementations | unsuffixed domain name | remove suffix as modules move; preserve schema versions explicitly |
| `build_from_split_stages` | editor-document lineage produced by segmentation | `build_from_segmentation` | compatibility-read old revision metadata; write only canonical lineage |
| `stage03A_source_draft.txt` | source subtitle draft artifact | `source_draft.txt` | compatibility-read old artifact |
| `experiment_stage_manifest.json` | segmentation run manifest | `segmentation_manifest.json` | compatibility-read old artifact |
| `stage1_result.json` / `stage1_validation.json` | segmentation result and validation report | `segmentation_result.json` / `segmentation_validation.json` | explicit artifact alias/migration |
| `origin="stage1"` | semantic group produced by segmentation | `origin="segmentation"` | domain-schema migration, not a text replacement |

## Naming rules already agreed

- Product UI uses user-facing concepts: transcription, subtitle segmentation, translation, calibration, review, glossary, dubbing and export.
- Runtime task types use stable domain verbs rather than route nicknames.
- Version numbers are allowed at compatibility boundaries and serialized schemas, not as permanent domain names.
- Old names may exist only in legacy readers, migration tests and frozen fixtures after migration.
- No global text replacement is allowed across persisted project formats; old artifacts require explicit readers/migrations.

## Collision to resolve

The letter `A` currently means both a selected segmentation branch in new changes and the source-track export in existing APIs. `B` means translated track and `AB` means bilingual output. The branch field is redundant and should disappear; export concepts should become explicit track names.
