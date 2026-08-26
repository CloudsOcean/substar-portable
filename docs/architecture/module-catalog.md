# Current module catalog

## Scope and classification

This catalog maps the current implementation at commit `932a5be` at file/module level. It is an audit, not the final target package tree.

Posture labels mean:

- **preserve**: a credible boundary or reusable implementation;
- **refactor**: product behavior remains, but responsibilities must be separated;
- **adapter**: infrastructure/provider boundary that should sit behind an interface;
- **compatibility**: old files/formats/routes may need readers while new writers stop using the name;
- **research**: development/evaluation tooling, not a production runtime component.

## Entry points and application composition

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `launcher.py` | CLI dispatch, launcher mutex, forced instance takeover, backend process, global Windows Job Object and browser launch | Refactor instance supervision |
| `app.py` | FastAPI composition plus settings, glossary, environment, job registry, process orchestration, recovery, workbench API and exports | Split composition from runtime/application services |
| `substar_core/workbench_routes.py` | Page routes, portable bundle import and media relinking; excluded from OpenAPI | Refactor into page and compatibility routers |
| `substar_core/editor_api_v2.py` | Editor HTTP models/routes, project lookup, media/waveform, editing, AI, translation and debug integration | Split HTTP adapter from application services |
| `substar_core/api_testing.py` | FastAPI/TestClient compatibility helper | Preserve as test infrastructure |
| `substar_core/__init__.py` | Package marker | Preserve |

## Runtime, configuration and infrastructure

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `config.py` | Portable/per-user data paths, settings defaults/migration, credential path and project root | Preserve behavior; separate typed settings sections |
| `security.py` | DPAPI-backed credential protection | Adapter; preserve Windows behavior |
| `runtime_instance.py` | Backend mutex, runtime record and HTTP identity probe | Refactor into one instance owner |
| `process_command.py` | Source/frozen executable command construction for backend and worker scripts | Preserve behind process runner |
| `artifacts.py` | Artifact paths plus atomic JSON/text reads and writes | Preserve; narrow authoritative-state use |
| `checkpoint.py` | File-backed progress/checkpoint helper | Compatibility until durable task store replaces lifecycle use |
| `http_client.py` | Shared `requests.Session`, pooling and common request helpers | Preserve as provider adapter foundation |
| `environment_doctor.py` | Inspect/configure FFmpeg, Python/runtime dependencies | Adapter |
| `edition.py` | Edition/build capability metadata | Preserve |
| `model_paths.py` | Resolve packaged/downloaded model paths | Adapter |
| `model_catalog.py` | Provider/model metadata and discovery helpers | Adapter |
| `model_assets.py` | Model asset definitions plus in-memory download threads/status | Keep catalog; move download lifecycle to task runtime |
| `reasoning_capabilities.py` | Discover/probe reasoning controls per provider/model | Adapter |
| `providers.py` | LLM/provider request construction and response parsing | Adapter; route all HTTP through shared client |
| `production_profiles.py` | Named production model/config profiles | Rename to domain configuration when frozen |

## Recognition, media and ingest

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `recognition/contracts.py` | Recognition input/result/provider contracts | Preserve |
| `recognition/registry.py` | Recognition profile registry and adapter selection | Preserve; remove UI knowledge of implementation names |
| `recognition/__init__.py` | Recognition package exports | Preserve |
| `pipeline.py` | FFmpeg preparation, recognition dispatch and ingest artifact production | Refactor as ingest application service |
| `asr_longform.py` | Long-audio ASR chunking/merge helpers | Preserve behind recognition adapter |
| `qwen_backend.py` | Local Qwen ASR/backend integration and subprocess behavior | Adapter; separate process execution |
| `qwen_cloud_asr.py` | Cloud Qwen ASR submission, polling and resumable remote-task checkpoint | Adapter; use common HTTP/task cancellation |
| `media/playback_proxy.py` | Range-capable local media response/proxy behavior | Preserve |
| `media/waveform_cache.py` | Server-side waveform extraction/cache | Preserve |
| `media/__init__.py` | Media package marker/exports | Preserve |

## Segmentation and subtitle construction

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `full_pipeline.py` | Ingest/segmentation/translation orchestration and segmentation child-process polling | Refactor; remove experiment route identity |
| `stage1.py` | Older segmentation analysis/decision pipeline | Compatibility/research pending reachability proof |
| `stage1_direct.py` | Direct LLM segmentation route | Compatibility/research pending route consolidation |
| `stage1_optimizer.py` | Existing segmentation optimization/repair | Preserve algorithms; rename by business purpose |
| `stage1_hierarchy.py` | Hierarchical segmentation support | Preserve algorithms; rename |
| `stage1_chunking.py` | Segmentation chunk construction | Preserve; rename |
| `stage1_adaptive_chunking.py` | Adaptive chunk sizing/routing | Preserve; rename |
| `stage1_recovery_v2.py` | Recover/materialize segmentation artifacts into editor project storage | Preserve behavior; replace historical paths with compatibility reader |
| `stage_progress.py` | File-backed stage progress ledger | Compatibility projection; not future task authority |
| `stage_settings.py` | Stage-specific settings extraction | Refactor into typed workflow/provider settings |
| `canonicalization.py` | Normalize tokens/cues and stable data forms | Preserve |
| `punctuation.py` | Punctuation-aware text helpers | Preserve |
| `language_layout.py` | Language/CJK layout and spacing rules | Preserve |
| `policy.py` | Segmentation limits and policy decisions | Preserve; name by owned rules |
| `prompt_registry.py` | Prompt keys/templates/model settings, including historical route keys | Preserve registry concept; migrate keys |
| `contracts/split_result_v2.py` | Serialized segmentation/split result contract | Preserve schema compatibility; canonicalize name/version |
| `contracts/__init__.py` | Contract exports | Preserve |

## Canonical editor domain and persistence

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `domain/editor_document.py` | Source/display tokens, groups, cues, translation, document and revision entities | Strong preserve |
| `domain/__init__.py` | Domain exports | Preserve |
| `document_operations_v2.py` | Typed document operation validation/application and delta behavior | Strong preserve; remove internal suffix during migration |
| `storage/project_store.py` | SQLite WAL revision chain, checksums, snapshots, patches and optimistic concurrency | Strong preserve |
| `storage/__init__.py` | Storage package exports | Preserve |
| `editor/ports/project_repository.py` | Application-facing repository protocol | Strong preserve |
| `editor/infrastructure/sqlite_project_repository.py` | Repository adapter over `ProjectStore` | Strong preserve |
| `editor/application/editing_service.py` | Apply one/batched document operations through repository | Strong preserve |
| `editor/application/revision_service.py` | Revision read/restore application behavior | Preserve |
| `editor/domain/cue_ordering.py` | Canonical cue ordering | Preserve |
| `editor/domain/cue_timing.py` | Cue timing rules | Preserve |
| `editor/domain/groups.py` | Semantic group rules | Preserve |
| `editor/api/editing_endpoints.py` | Focused editing route adapter/service wiring | Preserve direction; absorb relevant large-router routes |
| `editor/protocol/editor_protocol.schema.json` | Serialized browser/backend editing protocol | Preserve explicit protocol versioning |
| `editor/**/__init__.py` | Layer/package exports | Preserve |
| `validation_v2.py` | Document/project validation | Preserve; canonicalize internal name |
| `presentation_v2.py` | Subtitle presentation rules/settings | Preserve; canonicalize internal name |
| `export_v2.py` | Editor/project export functions | Preserve; explicit track names |

## Translation, calibration, review and terminology

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `translation_service_v2.py` | File-backed translation status, daemon thread, worker process and limited recovery | Replace lifecycle with shared task runtime; preserve application intent |
| `translation_input_v2.py` | Build translation input from editor revision | Preserve; canonicalize name |
| `translation_atoms.py` | Translation unit construction/mapping | Preserve |
| `translation_context.py` | Context window/group construction | Preserve |
| `translation_editor.py` | Apply translation output to editor revision | Preserve |
| `translation_t1mix.py` | Current contextual translation and 1:1/1:N/N:1 presentation mapping; imports private experimental grouping helpers | Preserve algorithm; extract shared mapping domain and rename historical route |
| `stage2.py` | Older translation/quality/delivery orchestration | Decompose into translation, quality and export; retire unreachable routes |
| `glossary.py` | Global/project terminology store plus prompt/hotword projections; active consumer integration is incomplete | Preserve; add an explicit compiler for each ASR/LLM consumer |
| `glossary_xlsx.py` | Glossary spreadsheet import/export | Adapter |
| `chinese_script.py` | Simplified/traditional conversion | Preserve utility |
| `manuscript_matching.py` | Reference manuscript parsing/alignment/matching | Preserve domain service |
| `manuscript_naming.py` | Reference manuscript/project naming helpers | Preserve |

Calibration and review currently live mostly as functions inside `editor_api_v2.py`; they do not yet have independent application modules. Their model-call algorithms should be extracted from HTTP before their execution moves to the common task runtime.

## Portability and compatibility

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `split_bundle.py` | Sanitize, export and import portable project bundles | Preserve as compatibility/portability adapter |
| `subtitle_exports.py` | Generate source/target/bilingual subtitle files | Preserve; replace ambiguous A/B/AB names at API boundary |
| `manual_relay.py` | Manual exchange/legacy workflow orchestration | Isolate; retain only proven compatibility behavior |
| `relay_profile.py` | Relay/provider compatibility profile | Isolate or merge into provider configuration |
| `experimental/merged_max_debug.py` | Alternate debug task runtime and experimental route | Remove from production composition; retain research history if needed |
| `experimental/__init__.py` | Experimental package marker | Remove from product dependency graph |

## Active worker and production-entry scripts

| Script | Current caller/use | Posture |
| --- | --- | --- |
| `scripts/run_ingest_worker.py` | Main job ingest subprocess protocol | Preserve behavior; register as ingest task handler |
| `scripts/run_stage1_experiment.py` | Current production segmentation subprocess | Rename/move to canonical segmentation worker after contract freeze |
| `scripts/run_production_translation.py` | Translation subprocess | Preserve behavior; register as translation task handler |
| `scripts/import_jianying_srt.py` | Optional legacy/Jianying alignment workflow | Compatibility adapter |
| `scripts/export_stage1_source_srt.py` | Historical source-track export | Replace caller with canonical export service; keep migration tool if needed |
| `scripts/run_full_pipeline.py` | CLI full-pipeline entry | Rebind to canonical application workflow or retire |
| `scripts/run_media_pipeline.py` | CLI media/ASR pipeline entry | Rebind to ingest application service |

## Research, evaluation and migration scripts

The remaining `scripts/` files are not all runtime dependencies. They fall into four explicit support groups and must not be imported by the production composition root:

| Group | Modules | Treatment |
| --- | --- | --- |
| Segmentation experiments | `run_atomic_stage1.py`, `run_direct_segmentation_api.py`, `run_global_planner_ab.py`, `run_stage03a.py`, `run_stage1_boundary_audit.py`, `run_stage1_hierarchical.py`, `run_stage1_local_candidates.py`, `run_stage1_pipeline.py`, `optimize_existing_stage1.py`, `run_p4_on_frozen_sol_route.py`, `replay_frozen_a3.py` | Research tooling; canonical naming is optional unless retained |
| Evaluation/audit | `audit_cue_order.py`, `audit_delivery.py`, `audit_prompt_leakage.py`, `audit_three_routes.py`, `compare_boundary_experiments.py`, `evaluate_character_boundary_similarity.py`, `evaluate_speaker_metadata_effect.py`, `evaluate_srt_with_mask.py`, `evaluate_stage1_boundaries.py`, `evaluate_stage1_quality.py`, `run_stage2_quality_review.py`, `run_stage2_risk_review.py` | Keep outside product runtime |
| Dataset/material preparation | `build_3dspeaker_experiment_material.py`, `build_company_corpus.py`, `build_stage1_holdout.py`, `prepare_sentence_boundary_ab.py`, `prepare_sol_blind_package.py`, `prepare_sol_direct_segmentation_package.py`, `convert_reference_srt_to_stage03a.py`, `attach_speaker_diarization.py` | Offline research/data tooling |
| Repair/import/rebuild | `compact_stage1_plan.py`, `enforce_stage1_hard_limits.py`, `import_reviewed_source_draft.py`, `import_sol_blind_stage1.py`, `import_sol_direct_cuts.py`, `inspect_project_result.py`, `merge_stage1_local_results.py`, `normalize_stage1_source_draft.py`, `rebuild_ingest_master.py`, `rebuild_with_reused_translations.py`, `reflow_stage1_plan.py`, `repair_global_stage1_plan.py`, `repair_srt_track_limits.py` | Explicit operator/migration tools; keep format compatibility |
| Translation experiments | `run_stage2_pipeline.py`, `run_translation_stages.py`, `test_t1mix_presentation.py` | Research/legacy CLI |
| Packaging/release | `build_windows_icon.py`, `build-windows.ps1`, `generate_release_checksums.py` | Preserve release tooling |
| Manual integration | `run_flash_map_pro_editor.py`, `submit_mapping_test_job.py` | External/manual tooling; isolate from product runtime |

## Frontend file catalog

| Module | Current responsibility | Posture |
| --- | --- | --- |
| `split.html`, `split.css`, `split.js` | Create/task page UI and controller | Preserve UI; refactor controller/API/status mapping |
| `settings.html`, `settings.css`, `settings.js` | Settings UI and auto-save controller | Preserve UI; add typed adapter |
| `glossary.html`, `glossary.css`, `glossary.js` | Glossary UI and whole-list persistence | Preserve at current scale |
| `editor_v2.html`, `editor_v2.css` | Editor layout and visual system | Preserve |
| `editor_v2.js` | Editor application coordinator | Split into session, repository/client, playback and render coordination |
| `editor_document_v2.js` | Browser-side editor protocol/domain operations | Strong preserve; optimize implementation and naming adapters |
| `editor_v2_document_store.js` | Optimistic projected document state | Preserve interface; replace full cloning |
| `editor_v2_operation_queue.js` | Batched operation transport/retry | Strong preserve |
| `editor_v2_timeline.js` | Canvas timeline | Strong preserve |
| `editor_v2_waveform_cache.js` | Windowed waveform LRU/dedup/cancel | Preserve |
| `editor_v2_cue_list_view.js` | Incremental cue window | Replace with bounded virtualization behind same role |
| `editor_v2_cue_time_controller.js` | Timing-operation coordinator | Preserve |
| `editor_v2_cue_ordering.js` | Cue sort contract | Preserve |
| `editor_v2_language.js` | Language/layout helpers | Preserve |
| `shell.css`, `styles.css` | Shared application/page styling | Preserve/freeze during backend migration |
| `theme/*` | Theme tokens, personalization and vendored variables | Preserve |
| `ui-icons.svg`, `substar-logo.svg` | Shared assets | Preserve |

## Schemas and prompts

The `schemas/stage1_*` and `schemas/stage2_*` files are serialized LLM/output contracts, while prompt-registry entries and prompt files select them. They are part of data compatibility even when their filenames are historical. New canonical schemas require explicit versioned readers/migrations; a global rename would break old project artifacts and cached model responses.

## Dependency findings

- The internal Python import graph is acyclic.
- Highest current fan-out is `editor_api_v2.py` (20 internal modules), followed by `full_pipeline.py` (10).
- Highest fan-in includes `artifacts`, the editor `domain` package and `config`, indicating useful shared foundations but also broad file/config coupling.
- Production subprocess creation is spread across launcher, app orchestration, ingest/ASR, segmentation, translation, media/environment helpers and experiments rather than one process-supervision adapter.
- Direct provider HTTP calls still bypass `http_client.py` in several modules.

The catalog supports a bounded refactor: preserve the editor domain/store, media/timeline behavior and most provider algorithms; replace the control plane, composition boundaries and historical routing names.
