# Semantic grouping and subtitle cue layout

Process one continuous block of time-ordered ASR word tokens. Grammatical punctuation and ASR sentence-boundary markers have intentionally been removed; infer syntax and discourse directly from the complete word sequence and timing. Return JSON only.

## Responsibilities

1. Recover coherent semantic and discourse groups from the immutable source tokens.
2. Place one or more display Cue boundaries inside each semantic group when required by the active hard limit.
3. Preserve every owned token exactly once, continuously, and in source order.

Do not correct ASR words, casing, punctuation, names, terminology, or translation. Text correction belongs to the independent editor calibration task.

## Ownership

- `owner=true` rows are the complete output domain and must be covered exactly once.
- `owner=false` rows are read-only context. They may inform the first and last boundary but must never appear in a returned group, Cue boundary, or exception span.
- Never add, delete, rewrite, translate, merge, split, or reorder source tokens.

## Meaning groups and display Cues

- A meaning group is one semantic or discourse unit.
- A meaning group may contain one or more display Cues.
- Every meaning-group end is also a Cue end.
- An internal Cue end is not automatically a meaning-group end.
- Adjacent Cues in the same meaning group may remain grammatically dependent when the hard limit requires a display split.
- `line_breaks_after` must be strictly increasing, remain inside the group, and end at `alignment_end`.

## Hard length contract

The request contains the authoritative human-confirmed source language, `hard_limit`, and `count_rule`. Count every retained Unicode character after display whitespace normalization, including letters, CJK characters, digits, punctuation, symbols, and required spaces.

`hard_limit` is only a rejection ceiling. It is not a preferred length, a fill target, a compactness objective, or a reason to delay an earlier natural information boundary. First recover the best semantic and discourse structure without trying to use the available character budget. Then place display boundaries where a viewer can most naturally absorb the next information step. A Cue may be substantially shorter than the limit.

When one otherwise coherent unit exceeds the limit, add internal Cue boundaries at its strongest natural syntactic or discourse seams. Preserve the most intelligible incremental reading on both sides; do not choose a later boundary merely because more characters still fit. If no legal internal boundary exists, preserve the indivisible span and emit `indivisible_overflow`. Never hide source material to satisfy the limit.

## Result contract

Echo `result_binding.input_fingerprint`, `result_binding.block_id`, and `result_binding.ownership` exactly. Return exactly:

```json
{
  "schema_version": "substar.semantic-grouping-result.v1",
  "input_fingerprint": "64 lowercase hex characters",
  "block_id": "c0001",
  "ownership": {"alignment_start": 0, "alignment_end": 9},
  "meaning_groups": [
    {"alignment_start": 0, "alignment_end": 9, "line_breaks_after": [4, 9]}
  ],
  "exceptions": []
}
```

Allowed exception codes are `indivisible_overflow`, `source_timing_conflict`, and `speaker_boundary_conflict`. Every exception must contain `code`, `alignment_start`, `alignment_end`, and a short `detail`.

Do not return analysis, chain of thought, or prose outside the JSON object.
