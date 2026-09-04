# Subtitle Cue boundary planning

Process one continuous block of time-ordered ASR word tokens. Grammatical punctuation and native sentence markers may be unavailable. Infer syntax and discourse from the complete word sequence, timing, pauses, speaker changes and supplied context.

## Responsibilities

1. Place natural display Cue boundaries over every owned word.
2. Preserve every owned word exactly once, continuously, and in source order.
3. Keep read-only context out of the output.

Do not correct ASR words, casing, punctuation, names, terminology or translation. Those belong to the independent calibration task. Never add, delete, rewrite, translate, merge, split or reorder source tokens.

## Boundary policy

Apply this priority order: exact OWN coverage and hard limit; indivisible lexical atoms; natural syntax/discourse; balance. A lower priority must never override a higher one.

- A Cue should be a readable information step, clause, discourse act or short complete expression.
- Protect only genuinely indivisible lexical or syntactic atoms. Function words are not globally protected: a preposition, subordinator or conjunction may begin the next Cue when it launches that Cue's phrase or clause.
- A Cue boundary is allowed inside a longer sentence when it follows a strong natural syntactic or discourse seam.
- Speaker or functional-content changes are strong boundaries.
- `hard_limit` is a rejection ceiling, not a preferred length, fill target or reason to delay an earlier natural boundary.
- Every token row includes `MAX_END=W####`. For a Cue beginning at that row, its ending alias must not exceed `MAX_END`; this is the program's exact rendered-length calculation, not a recommended boundary.
- For every CUE row, look up its starting token's `MAX_END` and verify that the chosen ending alias is not later. This check is mandatory even when the phrase feels indivisible.
- If a span exceeds the hard limit, split it into as many Cues as necessary. Re-evaluate the strongest legal seam from each new Cue start; do not look for one cut that makes an entire long span fit in only two Cues. The hard limit has no semantic exception: if every option is dependent, use the least damaging boundary before the ceiling.
- Balance is a soft ranking criterion among natural legal boundaries. It must never suppress an earlier syntactic seam or force an overflow.
