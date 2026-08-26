# Canonical naming contract

## Product concepts

New code, APIs, UI labels, task types, event payloads and new artifacts use these names:

| Concept | Canonical identifier | Meaning |
| --- | --- | --- |
| Media transcription | `transcription` | Prepare audio and produce timed recognition evidence |
| Reference matching | `reference_matching` | Match reference text to recognition timing evidence |
| Subtitle segmentation | `segmentation` | Build semantic groups and editable cue layout |
| Contextual translation | `translation` | Produce target-language content for a source revision |
| Presentation mapping | `presentation_mapping` | Map translated semantic units to 1:1, 1:N or N:1 display cues |
| AI calibration | `calibration` | Apply constrained punctuation/case corrections |
| AI review | `review` | Produce non-mutating issues/recommendations |
| Terminology | `glossary` | Stable terms compiled for each consumer |
| Delivery | `export` | Generate source, target or bilingual deliverables |
| Future voice generation | `dubbing` | Revision-bound audio/voice output extension |

## Project and runtime identifiers

| Identifier | Rule |
| --- | --- |
| `project_id` | Stable opaque identity; never changes on rename |
| `display_name` | User-editable project label |
| `task_id` | Stable durable operation identity |
| `attempt` | Monotonic execution number within one task |
| `revision_id` | Immutable editor revision identity |
| `artifact_id` | Registered output identity |
| `event_id` | Globally increasing runtime event replay cursor |
| `provider_id` | Provider adapter identity |
| `credential_ref` | Server-side reference; secret value never serialized publicly |

## Artifact names for new writers

```text
projects/<project_id>/
├── manifest.json
├── input/
│   └── media.<ext>
├── evidence/
│   ├── recognition.json
│   ├── transcript.txt
│   └── reference_match.json
├── project/
│   └── project.sqlite3
├── tasks/<task_id>/attempts/<attempt>/
│   ├── command.json
│   ├── stdout.log
│   ├── stderr.log
│   ├── result.json
│   └── artifacts/
├── derived/
│   ├── translations/<language>/
│   ├── reviews/
│   ├── dubbing/
│   └── exports/
└── cache/
    ├── playback/
    └── waveform/
```

The runtime database registers every durable artifact. Files remain the content store; their presence does not determine task state.

## Forbidden in new canonical code

The following may appear only in compatibility readers, frozen fixtures/tests and historical migration documentation:

```text
stage1
stage2
P1
P2
P2mix
P3
T1mix
experiment
production_one_step
split_branch
merged_max_debug
workbench_asr_split
project_v2
editor_tasks_v2
translation_v2
build_from_split_stages
```

Schema versions remain explicit values such as `substar.task.v1`; implementation filenames and domain names do not carry permanent `_v2` suffixes.

## UI wording

Recommended Chinese labels:

| Canonical concept | UI label |
| --- | --- |
| transcription | 听写 |
| reference_matching | 参考稿匹配 |
| segmentation | 字幕切分 |
| translation | 翻译 |
| calibration | 校准 |
| review | 审阅 |
| glossary | 词库 |
| export | 导出 |
| dubbing | 配音 |

Internal algorithm step names are not shown unless they can be expressed as stable product progress, such as “正在验证字幕切分结果”.

## Compatibility rule

Old serialized names are mapped at the read boundary. Canonical domain objects never preserve old names merely because they were read from an old artifact; migration metadata records the original schema/path separately.
