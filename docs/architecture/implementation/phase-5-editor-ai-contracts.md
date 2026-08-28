# Phase 5: editor AI contracts and exclusive operation boundary

Status: Phase 1 contract freeze. Production behavior is not switched by this
change.

## Product boundary

Semantic grouping materializes an editor-ready document. Calibration,
translation, and review are optional editor operations selected in any order by
the user. They are not pipeline stages and never trigger one another.

Operations are serialized per project:

```text
idle -> queued -> running -> terminal -> idle
                        \-> cancelling -> terminal -> idle
```

`queued`, `running`, and `cancelling` hold the exclusive editor lock. Manual
editing and a second AI operation are rejected until the operation reaches a
terminal state. A running worker cannot transition directly to `cancelled`;
confirmed cancellation must pass through `cancelling`.

## Frozen contracts

| Contract | Schema identifier | Responsibility |
|---|---|---|
| Semantic grouping | `substar.semantic-grouping-result.v1` | Meaning groups, display boundaries, structural exceptions only |
| Calibration | `substar.calibration-result.v1` | Exact source-projection corrections and their policy disposition |
| Translation | `substar.translation-result.v1` | Target text bound to a hash of each source Cue |
| Editor AI task | `substar.editor-ai-task.v1` | Exclusive project lock and AI task lifecycle |

The historical `substar.editor-operation.v1` describes user document editing
commands and will become `substar.document-edit-command.v1` at clean cutover.
The AI task lifecycle uses `substar.editor-ai-task.v1`; neither its module nor
its public types use the ambiguous word `operation`.

## Module boundary

```text
substar_core/editor/
  tasks/contracts.py
  calibration/contracts.py
  translation/contracts.py
  review/contracts.py

substar_core/segmentation/
  semantic_grouping_contract.py
```

Services, repositories, workers, and HTTP adapters will depend inward on these
contracts. Contracts do not import API, storage, provider, or browser code.

## Calibration boundary

The only correction kinds are:

- `set_case`
- `set_punctuation`
- `replace_token`
- `replace_span`

Each action carries exact token anchors, before/after text, evidence,
confidence, the policy disposition (`apply` or `review`), and whether the
change makes an existing translation potentially stale.

Semantic grouping has no correction or calibration field. The JSON Schema
rejects any added `ai_calibrations` property.

## Review boundary

Source and translation review use distinct discriminated taxonomies. Neither
accepts `other`, and a translation issue type is invalid in a source result.

Source issues:

- suspected misrecognition, omission, or repetition;
- named entity or term;
- number or unit;
- context incoherence;
- source consistency.

Translation issues:

- mistranslation, omission, or addition;
- factual mismatch;
- polarity or logical relationship;
- reference resolution;
- terminology consistency;
- grammar or fluency;
- subtitle flow.

Impact, confidence, lifecycle status, and recommended action are independent
fields. Review results remain advisory and do not contain document mutations.

## Clean-cut rule

These contracts introduce no legacy aliases, dual reads, or dual writes. The
production cutover will replace historical names and schemas rather than add a
compatibility path.
