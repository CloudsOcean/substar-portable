# Subtitle Cue boundary planning

Process one continuous block of time-ordered ASR word tokens. Grammatical punctuation and native sentence markers may be unavailable. Infer syntax and discourse from the complete word sequence, timing, pauses, speaker changes and supplied context.

## Responsibilities

1. Place natural display Cue boundaries over every owned word.
2. Preserve every owned word exactly once, continuously, and in source order.
3. Keep read-only context out of the output.

Do not correct ASR words, casing, punctuation, names, terminology or translation. Those belong to the independent calibration task. Never add, delete, rewrite, translate, merge, split or reorder source tokens.

## Boundary policy

- A Cue should be a readable information step, clause, discourse act or short complete expression.
- Do not strand determiners, prepositions, conjunctions, auxiliaries, negation, noun heads, required complements, names, numbers or fixed expressions.
- A Cue boundary is allowed inside a longer sentence when it follows a strong natural syntactic or discourse seam.
- Speaker or functional-content changes are strong boundaries.
- `hard_limit` is a rejection ceiling, not a preferred length, fill target or reason to delay an earlier natural boundary.
- If a span exceeds the hard limit, split it at the strongest legal seam. If no legal seam exists, preserve the indivisible span for explicit review; never hide source material.
