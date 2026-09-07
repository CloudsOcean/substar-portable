# English source boundary policy

## Must split

- confirmed speaker or track changes;
- independent replies, reactions, titles, credits and unrelated functional centres;
- complete sentence endings, strong silence and clear topic restarts when available.

## Forbidden boundaries

- inside words, contractions, hyphenated words, URLs, decimals, model names or abbreviations;
- inside glossary terms, proper names, programme and organisation names;
- between a number and its unit, currency, percentage, date or range;
- between a determiner/possessive and its minimal noun head;
- between negation and its governed predicate;
- between modal/auxiliary/copular material and the required predicate;
- between infinitival `to` and its verb;
- between a preposition and its minimal object once that prepositional phrase has begun;
- between a relative/subordinate marker and the minimal clause core it launches;
- inside comparisons, phrasal verbs, fixed expressions or atomic list items;
- before or after a stranded function word. `and/or/but/nor` may begin the following Cue but must not hang at the end of the previous Cue.

Place a function word by its actual syntactic role, not by its spelling alone:

- complementizer `that`, relative `that/which`, and subordinators such as `because/if/although` normally travel with the clause they launch;
- demonstrative `that` stays with its noun, while pronominal `that` may complete the preceding predicate;
- a preposition stays with its minimal object, an infinitival `to` stays with its verb, and a phrasal-verb particle stays with its verb;
- a genuinely stranded preposition at the grammatical end of a clause stays with that clause.

## Positive boundary candidates

Actively consider these seams instead of waiting for sentence-final punctuation:

- before a prepositional phrase, keeping the preposition with its object: `the policy | in the United States`, not `the policy in | the United States`;
- before an infinitive phrase, keeping `to` with its verb: `we need a plan | to reduce the cost`;
- before a relative clause, keeping its marker with the clause: `the proposal | that Congress rejected`;
- before a subordinate or adverbial clause: `we can proceed | if the vote passes`;
- before a coordinating conjunction that launches a new clause or information step: `the talks failed | and the market fell`;
- after a completed subject-predicate centre or required complement, before optional modifiers, examples, explanations or parallel additions;
- at a natural hesitation, pause or discourse restart when both sides remain intelligible.

These are candidates, not mandatory cuts. Do not split a lexicalized phrasal verb (`give up`, `carry out`), a verb from a required prepositional complement (`rely on`, `look at`), or any other minimal atom. A relative or subordinate clause is not globally glued to its antecedent or matrix clause: keep the clause internally intact, but allow the boundary immediately before its marker.


Prefer sentence ends, completed information steps, complete subject-predicate centres, parallel-item seams and natural pauses. Keep short subject-predicate pairs, required complements and modifier-head units together when possible. Judge both edges of every proposed boundary: the left Cue should not stop at a misleading partial parse, and the right Cue should begin with a recognizable continuation. Among equally natural candidates, prefer a reasonably balanced pair, but balance is only a tie-breaker. When every legal choice remains dependent, choose the one that is easiest to understand incrementally.

## Review the complete Cue sequence before returning it

- Do not separate a short subject from its predicate when they fit together and there is no speaker change or strong pause. Likewise, keep short coordinated predicates and an introductory phrase with its following short clause together when they fit. Before returning, review each adjacent pair: if they form one coherent clause or phrase within `MAX_END`, remove the unnecessary boundary in your own output. Keep real sentence endings and distinct discourse acts separate. For example, use `The regional director has announced`, not `The regional director / has announced`; use `She says the crew is trained and ready to leave`, not `She says the crew / is trained / and ready to leave`.
- A legal phrase boundary is optional. Keep a short introductory phrase with its time modifier, and short coordinated predicates together, when the combined Cue fits and there is no strong pause or discourse change. Do not isolate a connector such as `and likewise` from the phrase it introduces.
- Inspect Cues lasting less than roughly one second and very short dependent fragments. These are review signals, not minimum-duration or minimum-word-count rules. Keep independent replies, interjections and genuine sentence endings; otherwise try joining the fragment to its grammatical host, or moving a neighboring boundary so every Cue still obeys `MAX_END`.
- Resolve sentence transitions before applying local function-word rules. In an unpunctuated sequence, a trailing preposition may complete the preceding predicate while the next pronoun starts a new sentence. Read the following predicate before attaching that pronoun to the preposition. Never create a Cue that combines the end of one sentence with the beginning of the next.
- For `... has been agreed to this would ...` or `... was signed off on this will ...`, test the full parse: `agreed to` / `signed off on` completes the preceding passive predicate; `this would` / `this will` starts the next subject-predicate pair. Keep the terminal preposition with the first sentence and start the next Cue at `this`. Do not group a terminal preposition with the next sentence's subject merely because a preposition usually takes an object.
- During overflow repair, retain a leading connector with enough of the phrase it introduces to be intelligible. For example, if `and likewise the future of a resilient and flourishing city` exceeds the cap, use `and likewise the future / of a resilient and flourishing city`, not `and likewise / the future of a resilient and flourishing city`. Review the entire rejected range before choosing its replacement cuts.
- Check the next Cue as well as the current one before accepting a cut. If a later Cue becomes an isolated object, connector or sentence tail, reconsider adjacent boundaries rather than accepting the first locally legal cut. Preserve source words exactly; infer boundaries without correcting grammar or inventing punctuation.

Every Cue must obey the active `hard_limit`; it is a rejection ceiling, never a preferred length or fill target. Natural information progression decides where to cut before the ceiling, but it cannot override the ceiling. If an indivisible-looking construction itself would overflow, use the least damaging internal boundary and preserve every source token. Never lose source material.
