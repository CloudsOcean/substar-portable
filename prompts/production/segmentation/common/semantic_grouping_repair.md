# Block-wide Cue boundary patch

The complete original execution block is present. Repair every OWN range and every ERROR in this single response. Accepted Cues are marked CONTEXT/FROZEN and are read-only.

- Cover every OWN word exactly once, continuously and in order.
- Keep every CONTEXT word read-only and out of the result.
- Correct all missing, overlapping, invalid or over-limit OWN ranges identified by the program at once.
- Apply the same natural-boundary and hard-limit rules as the primary task.
- Treat each token row's `MAX_END` as the authoritative legal ceiling for a Cue beginning on that row. Choose the strongest semantic boundary at or before it.
- Audit every returned CUE row mechanically before answering: its END must be no later than the starting row's `MAX_END`.
- Long rejected spans may require three or more Cues. Plan them sequentially: choose a natural END no later than the current start row's `MAX_END`, then repeat from the next token until the OWN block is covered.
- Re-plan the complete OWN block. Do not preserve a previous boundary merely because it was structurally valid.
- Do not correct or rewrite source text.
- Do not return any CONTEXT/FROZEN range.
