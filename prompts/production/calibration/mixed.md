# Mixed-language source-text calibration

Inspect every OWN Cue in the time-ordered mixed-language source block. CONTEXT Cues are read-only. Return the complete corrected source text for every OWN Cue, including unchanged Cues.

A Cue is a display time slot, not necessarily a sentence boundary. Read across Cue boundaries for context, but preserve the language and script actually spoken in each span.

## Check every Cue for

- names, places, organizations, brands, products, titles and acronyms in their contextually authoritative script;
- missing, duplicated or misplaced light punctuation;
- high-confidence ASR substitutions, homophones and truncated written forms;
- casing where the active script has case, and script-appropriate conventional forms where it does not;
- numbers, currencies, measurements, dates, units and cross-Cue terminology consistency;
- accidental transliteration or translation of code-switched material.

## Safety policy

- Preserve meaning, word order, language switches, Cue boundaries, timing, ownership, speaker identity and translation.
- Make the smallest defensible correction. Do not translate, transliterate, paraphrase or normalize one language into another.
- An authoritative glossary or supplied reference outranks model memory and frequency.
- When the intended language, script or lexical correction is uncertain, keep the original wording. The finalizer decides which differences are safe to apply and which require review.
- Conventional written fragments may be joined only inside one Cue. Never move or merge words across Cue boundaries.

