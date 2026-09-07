# Source-text calibration

Inspect every OWN row in the complete time-ordered source block. CONTEXT rows are read-only. Return the complete corrected source-language text for every OWN alias, including unchanged rows.

The ASR-derived source may lack all grammatical punctuation and authoritative casing. A row is a fixed display time slot, not necessarily a sentence boundary. First reconstruct the sentence and discourse structure across the complete block; then project the corrected text back into the same rows without changing their boundaries.

## Required punctuation reconstruction

- Every confidently complete sentence must end with `.`, `?`, or `!` on the final word of the row that contains that sentence ending.
- Do not add terminal punctuation merely because a display row ends. A sentence may continue through several rows.
- Restore commas, colons, semicolons, apostrophes, quotation marks, and other punctuation where grammar or discourse requires them.
- When one row ends a sentence and the next starts another, punctuate the first and apply sentence-initial casing to the second.
- Before answering, scan the complete block once more: each inferred sentence beginning must have appropriate casing and each inferred sentence ending must have terminal punctuation.

Example of projection across fixed rows: `1|The talks are over` plus `2|according to officials` is one sentence, so return `1|The talks are over` and `2|according to officials.` The alias is only a binding address and must not appear inside the corrected text.

## Check every row for

- sentence-initial and ordinary casing;
- people, places, organizations, brands, products, titles and acronyms;
- missing, duplicated or misplaced punctuation;
- ASR substitutions, homophones and truncated written forms;
- terminology, numbers, currencies, measurements, dates and units;
- inconsistent forms of the same entity or term across nearby rows.

## Safety policy

- Preserve meaning, word order, row boundaries, timing, ownership, speaker identity and translation.
- Spaces in an input row are token-alignment separators. Preserve them exactly unless adjacent fragments must be joined into one certain conventional written form.
- Make the smallest defensible correction. Do not paraphrase, summarize, delete emphasis, or polish the speaker's style.
- An authoritative glossary or supplied reference outranks model memory and frequency.
- Strong local grammar, context and repeated-document evidence may support an exact correction.
- If an exact lexical correction is uncertain, keep the original wording. The finalizer decides which differences are safe to apply and which require review.
- Conventional written forms may join adjacent fragments only inside one row, such as `u` + `s` to `U.S.`. Never move or merge words across row boundaries.
