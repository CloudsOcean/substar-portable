# Boundary policy: reconstruct from an unpunctuated word timeline

The request view intentionally hides native ASR sentence markers and trailing structural punctuation. The stored source remains unchanged. Reconstruct clauses, sentences, discourse acts, meaning groups and display Cues from word order, lexical syntax, timing, pauses, speaker changes and discourse continuity.

- First infer coherent clauses and meaning groups across the complete owner block; only then choose display boundaries.
- Do not infer a sentence ending merely from capitalization. Casing is also fallible ASR text evidence.
- Never create a one-word Cue unless it is genuinely an independent reply, interjection, title or credit.
- Keep proper names with an immediately following restrictive or descriptive relative clause when separating them would strand either side.
- Keep determiners with noun heads, prepositions with minimal objects, predicates with required complements, and paired constructions such as `not just ... but also ...` in one meaning group.
- The active hard limit requires multiple Cues inside one meaning group whenever a legal boundary is available; never create a lexical or syntactic fragment merely to meet the limit.
