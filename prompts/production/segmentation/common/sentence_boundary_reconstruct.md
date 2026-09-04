# Native sentence boundary policy: reconstruct from words

Native ASR sentence boundaries are intentionally unavailable. Reconstruct clauses, sentences, discourse acts and display Cues directly from the continuous repaired word-level timeline.

- Infer structure from lexical syntax, punctuation as fallible text evidence, pauses, speaker changes and discourse continuity.
- Do not assume that an ASR punctuation mark proves a sentence ending; cross it when the following words complete a paired construction, unfinished clause, required complement, quotation or other indivisible structure.
- First recover the most coherent clause structure across the whole owner block, then choose display boundaries within it.
- Never imitate likely ASR sentence segmentation from punctuation alone.
