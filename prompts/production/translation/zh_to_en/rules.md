# Chinese to English

- Produce concise, idiomatic broadcast English rather than word-for-word Chinese syntax.
- Recover an explicit English subject only when grammar requires it and context identifies it; never invent an actor.
- Infer tense, aspect, singular/plural and articles conservatively from time expressions and context. Do not add unsupported certainty.
- Convert Chinese topic-comment, serial-verb, `把/被`, long attributive and omitted-link structures into natural English clause order.
- Make a logical connector explicit only when the Chinese relation is clear; preserve negation scope, condition, contrast and causality.
- Preserve every name, number, currency, unit, date and comparison exactly once.
- Allocate one natural English stream across consecutive Cues. Fragments are allowed only when the next Cue immediately completes them; avoid stranded articles, prepositions, auxiliaries and infinitival `to`.
- English `hard_limit` includes spaces and punctuation when `count_rule` says so; every target Cue must remain within it.
