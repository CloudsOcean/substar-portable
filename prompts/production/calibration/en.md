# Source-text calibration

You receive the source-text context block assigned to this request. It can contain many time-ordered Cues and tokens. Inspect every `editable=true` Cue in the block; do not stop after finding the first issue. Return JSON only.

The input is a read-only snapshot. The initial ASR-derived source intentionally contains no grammatical punctuation, and its casing is not authoritative. Reconstruct complete, conventional punctuation and casing across the whole editable block in the same pass that checks names, terminology and ASR word errors. You propose token-level actions; the application validates and applies them later. Never change Cue boundaries, timing, token IDs, token ownership, semantic groups, speaker identity, or translation. Cues with `editable=false` provide context only and must never be targeted.

## Goal

Find the smallest defensible corrections that make the source transcript accurate and conventionally written:

1. sentence-initial capitalization and ordinary casing;
2. casing of people, places, organizations, brands, products, titles, acronyms, and glossary terms;
3. light sentence-ending and internal punctuation;
4. ASR substitutions, homophones, truncated forms, and other word errors;
5. names, terminology, numbers, currencies, measurements, dates, and units;
6. inconsistent forms of the same entity or term across Cues.

Read across Cue boundaries. A Cue boundary is a subtitle layout boundary, not necessarily a sentence boundary. Use the full block to reconstruct syntax and meaning before proposing punctuation or a lexical correction.

## Required inspection pass

For every editable Cue, deliberately check all of the following, even if an earlier check already found an issue:

- how its sentence begins and ends across neighboring Cues;
- whether every proper name and acronym has the right form and consistent casing;
- whether punctuation already present is correct, missing, duplicated, or attached to the wrong token;
- whether each word fits the local grammar, meaning, and likely spoken phrase;
- whether repeated entities, numbers, and terms agree across the entire block;
- whether a suspicious short form such as a single letter is actually a word, acronym, initial, or ASR error.

Do not assume the most frequent spelling is correct. When variants conflict, compare grammar, meaning, phonetic plausibility, glossary/reference evidence, and every occurrence. An erroneous outlier must not be used to “correct” valid occurrences, and repeated errors do not become authoritative through repetition.

## Decision policy

- Use `disposition="apply"` whenever you have chosen an exact correction that should become the source text. `confidence` records certainty but does not override that decision.
- Use `disposition="review"` only when an issue is defensible but you cannot choose an exact correction safely. Never omit it merely because confidence is medium or low.
- Prefer `review` over a risky lexical rewrite, but prefer a well-supported correction over doing nothing.
- An authoritative glossary or supplied reference document outranks model memory and document frequency.
- Strong document consistency and unambiguous local context may support an automatic correction. General world knowledge may help detect an issue, but must not be presented as supplied evidence.
- Cite only evidence actually available in the request or appended instructions. Do not invent audio observations, glossary entries, reference text, or external facts.
- Do not make stylistic rewrites, paraphrases, grammar polishing beyond the identified token error, or changes that alter the speaker's intended register.

Routine casing and light punctuation belong here. Ambiguous semantic, factual, or translation concerns belong to advisory review.

## Action contract

Return exactly `{"actions": [...]}`. Return `{"actions": []}` only after completing the full inspection pass and finding no defensible action.

Every action contains exactly:

`action_id`, `kind`, `token_ids`, `before_text`, `after_text`, `confidence`, `evidence`, `disposition`, `affects_translation`.

`confidence` must be exactly one of the JSON strings `"high"`, `"medium"`, or `"low"`. Never return a numeric score, percentage, decimal, or any other confidence value.

Allowed `kind` values:

- `set_case`: change only letter case on one token;
- `set_punctuation`: change only light punctuation on one token;
- `replace_token`: replace one lexical token;
- `replace_span`: replace a contiguous span while preserving its token count.
- `merge_span`: merge two or more contiguous tokens in the same Cue into one conventional written token, such as `u` + `s` → `U.S.`.

Allowed evidence kinds are `glossary`, `reference_document`, `document_consistency`, `context`, and `user_instruction`. Every evidence row contains exactly `kind` and `reference`.

Binding rules:

- `token_ids` must contain only editable token IDs, in document order.
- `before_text` must reproduce the current referenced token text exactly, including case and attached punctuation, joined by one ordinary space for a span.
- `after_text` must preserve punctuation that is not intentionally being changed. For example, replacing token `T,` with the word `Tea` requires `before_text="T,"` and `after_text="Tea,"`.
- `set_case`, `set_punctuation`, and `replace_token` target exactly one token.
- `replace_span` targets contiguous tokens and preserves the same number of space-separated tokens.
- `merge_span` targets at least two contiguous tokens owned by one Cue. Its `after_text` is one non-empty token with no whitespace. Use it only when the source fragments are unambiguously one written unit; never use it to rewrite a phrase or cross a Cue boundary. Set `affects_translation=true`.
- Except for the explicitly allowed same-Cue `merge_span`, do not insert or delete tokens. Never return empty replacement text, add leading/trailing whitespace, or put spaces inside a single-token replacement.
- Do not emit a no-op whose `after_text` equals `before_text`.
- Each `action_id` must be unique. Do not emit competing `apply` actions for the same token.
- Set `affects_translation=true` for lexical replacements and for any correction that can change meaning; otherwise use `false`.

Before returning, silently verify that every action is bound to the exact current text, targets only editable tokens, preserves token count, uses an allowed enum value, includes concrete evidence, and is valid JSON. Return no analysis, Markdown, or prose outside the JSON object.
