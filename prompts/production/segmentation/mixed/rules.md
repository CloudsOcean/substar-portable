# Mixed-language source boundary policy

- Preserve every language switch and every source token exactly; boundary planning must never translate, transliterate or normalize either side of a switch.
- Apply the syntactic dependency rules of the active local language to each span. Do not strand particles, articles, auxiliaries, negation, prepositions, case markers, endings or required complements.
- Keep mixed-script proper names, abbreviations, model names, URLs, numbers and units intact.
- A language switch alone is not automatically a Cue boundary. Split there only when it is also a natural information, discourse, speaker or timing boundary.
- Prefer completed clauses, information steps, independent reactions, speaker changes and strong pauses.
- If the current language is uncertain, choose the boundary that preserves the longest clearly dependent construction and leaves neither side as a function-word fragment.
- Enforce the active mixed-language `hard_limit` as a rejection ceiling, not a preferred length or fill target.

