# Source-text calibration

Inspect every OWN Cue in the time-ordered source-text block. CONTEXT Cues are read-only. Return the complete corrected source-language text for every OWN Cue, including unchanged Cues.

The ASR-derived source may lack grammatical punctuation and authoritative casing. Read across Cue boundaries: a Cue is a display slot, not necessarily a sentence boundary.

## Check every Cue for

- sentence-initial and ordinary casing;
- people, places, organizations, brands, products, titles and acronyms;
- missing, duplicated or misplaced light punctuation;
- ASR substitutions, homophones and truncated written forms;
- terminology, numbers, currencies, measurements, dates and units;
- inconsistent forms of the same entity or term across nearby Cues.

## Safety policy

- Preserve meaning, word order, Cue boundaries, timing, ownership, speaker identity and translation.
- Make the smallest defensible correction. Do not paraphrase or polish the speaker's style.
- An authoritative glossary or supplied reference outranks model memory and frequency.
- Strong local grammar, context and repeated-document evidence may support an exact correction.
- If an exact lexical correction is uncertain, keep the original wording. The finalizer, not the model, decides which differences are safe to apply and which require review.
- Conventional written forms may join adjacent fragments inside one Cue, such as `u` + `s` to `U.S.`. Never move or merge words across Cue boundaries.
