# Mixed-language source boundary policy

## Mandatory boundaries

- Split confirmed speaker, track, title, credit and independent-response changes.
- Prefer complete sentence ends, strong pauses and clear discourse restarts.
- In any long span, choose the strongest legal boundary before the active ceiling.

## Protected local atoms

- Preserve every source token and language switch exactly; never translate, transliterate or normalize during boundary planning.
- Apply the dependency rules of the active local language to each span. Do not split particles from their phrases, articles or determiners from noun heads, auxiliaries or negation from predicates, prepositions from minimal objects, case markers or endings from their hosts, or predicates from required complements.
- Keep mixed-script proper names, abbreviations, model names, URLs, numbers, units, dates and ranges intact.
- A language switch alone is not a boundary. Split there only when it is also a natural information, syntax, discourse, speaker or timing seam.

Actively consider completed clauses, information steps, independent parallel clauses, reactions, explanations, examples and natural pauses. A connector may begin the next Cue when it launches a complete phrase or clause, but it must not be stranded by itself or at the previous Cue's end. Inspect both sides of every boundary and avoid a function-word fragment in either language. If the language is uncertain, choose the boundary that preserves the clearest local dependency. Use balance only to rank equally natural choices.

Every Cue must obey the active `hard_limit` and the start token's `MAX_END`. The ceiling is a rejection limit, not a fill target, and has no semantic exception. If the ideal construction would overflow, move earlier to the least damaging legal seam. Never drop, rewrite, reorder or overflow source tokens.