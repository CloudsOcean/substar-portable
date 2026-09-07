# Mixed-language source-text calibration

Inspect every OWN row in the complete time-ordered mixed-language source block. CONTEXT rows are read-only. Return the complete corrected source text for every OWN alias, including unchanged rows.

A row is a fixed display time slot, not necessarily a sentence boundary. First reconstruct sentence and discourse structure across the complete block, including code switches; then project the corrected text back into the same rows without changing boundaries or the language actually spoken in each span.

## Required punctuation reconstruction

- End every confidently complete sentence with the terminal punctuation appropriate to its grammatical language and script on the row containing the actual sentence end.
- Do not add terminal punctuation merely because a display row ends; one sentence may span several rows or switch languages.
- Restore commas, enumeration marks, colons, quotation marks, brackets and other punctuation according to the active grammatical frame.
- When one row ends a sentence and the next starts another, close the first sentence and apply the appropriate script-specific sentence-start convention to the second.
- Before answering, scan the complete block again and verify every inferred sentence beginning and ending.

Fixed-row projection example: `1|The report says` plus `2|鍖椾含浠婂ぉ鍙戝竷娑堟伅` is one sentence, so return `1|The report says` and `2|鍖椾含浠婂ぉ鍙戝竷娑堟伅銆俙 The alias is only a binding address and must not appear inside the corrected text.

## Check every row for

- names, places, organizations, brands, products, titles and acronyms in their contextually authoritative script;
- missing, duplicated or misplaced punctuation;
- high-confidence ASR substitutions, homophones and truncated written forms;
- casing where the active script has case, and script-appropriate conventional forms where it does not;
- numbers, currencies, measurements, dates, units and cross-row terminology consistency;
- accidental transliteration or translation of code-switched material.

## Safety policy

- Preserve meaning, word order, language switches, row boundaries, timing, ownership, speaker identity and translation.
- Spaces in an input row are token-alignment separators, not a request to change the active script's typography. Preserve them exactly unless adjacent fragments must be joined into one certain conventional written form.
- Make the smallest defensible correction. Do not translate, transliterate, paraphrase, summarize, delete emphasis, or normalize one language into another.
- An authoritative glossary or supplied reference outranks model memory and frequency.
- When the intended language, script or lexical correction is uncertain, keep the original wording. The finalizer decides which differences are safe to apply and which require review.
- Join conventional written fragments only inside one row. Never move or merge words across row boundaries.
