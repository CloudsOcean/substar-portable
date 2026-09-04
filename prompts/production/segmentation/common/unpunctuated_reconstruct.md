# Boundary policy: reconstruct from an unpunctuated word timeline

The request view intentionally hides native ASR sentence markers and trailing structural punctuation. The stored source remains unchanged. Reconstruct clauses, sentences, discourse acts and display Cues from word order, lexical syntax, timing, pauses, speaker changes and discourse continuity.

- First infer coherent clauses across the complete owner block; only then choose display boundaries.
- Do not infer a sentence ending merely from capitalization. Casing is also fallible ASR text evidence.
- Never create a one-word Cue unless it is genuinely an independent reply, interjection, title or credit.
- Keep proper names with an immediately following restrictive or descriptive relative clause when separating them would strand either side.
- Keep determiners with noun heads, prepositions with minimal objects, predicates with required complements, and paired constructions such as `not just ... but also ...` readable across adjacent Cues.
- When a longer thought exceeds the active hard limit, use multiple Cues at legal natural seams; never create a lexical or syntactic fragment merely to meet the limit.
