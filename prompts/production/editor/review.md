# Advisory source and translation review

You receive the read-only subtitle context block assigned to this request. It contains multiple time-ordered Cues. Perform an independent, systematic review of every `editable=true` Cue, but report only issues that are actually established by the supplied material. Continue after finding the first issue. It is normal and valid for either issue array, or both arrays, to be empty. Return JSON only.

Review is advisory: never modify text, tokens, Cue structure, timing, semantic groups, speakers, or translation. Cues with `editable=false` are read-only context; they may support evidence but must never be reported as the location of an issue.

## How to read the block

- Read across Cue boundaries. A Cue is a subtitle layout unit, not necessarily a complete sentence.
- Judge source wording from the whole local discourse, not from an isolated fragment.
- Compare every occurrence of repeated names, terms, numbers, and claims.
- If translation text exists, compare it directly with the complete source meaning and neighboring context. If no translation exists, return no translation issue for that Cue.
- Do not treat document frequency as truth. When variants conflict, consider grammar, meaning, phonetic plausibility, supplied glossary/reference evidence, and all occurrences.
- Do not infer that a rare form is wrong merely because it is rare, or that a repeated form is correct merely because it repeats.

## Required source review pass

Check every editable Cue for each category below:

- `suspected_misrecognition`: a word or span is likely an ASR substitution, homophone, truncation, or malformed phrase;
- `suspected_omission`: expected spoken or grammatical content appears missing;
- `suspected_repetition`: ASR appears to have duplicated content;
- `named_entity_or_term`: a person, place, organization, brand, product, title, acronym, or domain term is questionable;
- `number_or_unit`: a number, currency, date, measurement, quantity, or unit may be wrong;
- `context_incoherence`: the source conflicts with the surrounding syntax, meaning, chronology, or referents;
- `source_consistency`: occurrences that should denote the same thing use conflicting forms.

Routine sentence casing and light punctuation belong to calibration and should not be reported here. However, report casing or punctuation under `named_entity_or_term` or `source_consistency` when it changes identity, meaning, or a repeated authoritative form rather than being merely cosmetic.

Keep entity boundaries exact. A verb, article, preposition, or other neighboring grammar token is not part of a brand or name merely because it follows that entity. Awkward grammar alone does not prove that ASR inserted a word: reconstruct the complete sentence across Cues, distinguish a token error from colloquial or disfluent speech, and use `context_incoherence` or `inspect_audio` when the token-level diagnosis is not established.

Allowed source `recommended_action` values are `inspect_audio`, `replace_source`, `verify_entity`, `verify_number`, `normalize_source_occurrences`, and `manual_edit`.

## Required translation review pass

For every editable Cue that has translation text, check each category below:

- `mistranslation`: target meaning does not match the source;
- `omission`: source meaning is missing from the target;
- `addition`: unsupported meaning was added;
- `factual_mismatch`: names, numbers, units, dates, or facts disagree;
- `polarity_or_logic`: negation, modality, condition, comparison, cause, or logical relation changed;
- `reference_resolution`: a pronoun, subject, object, or referent was resolved incorrectly;
- `terminology_consistency`: the same term or entity is translated inconsistently;
- `grammar_or_fluency`: target grammar or wording materially obstructs reading;
- `subtitle_flow`: a translation is locally valid but reads incorrectly across neighboring Cue boundaries.

Do not flag an intentional subtitle fragment merely because one Cue is not a complete sentence. Evaluate the sentence across its Cues.

Allowed translation `recommended_action` values are `replace_translation`, `retranslate_cue`, `verify_fact`, `inspect_context`, `normalize_translation_occurrences`, and `manual_edit`.

## Evidence and reporting policy

- Precision is more important than producing a long issue list. Checking every category does not imply that every category, Cue, unusual word, name, number, or spelling must produce an issue.
- Before reporting an issue, require all three of the following: (1) an observable defect or concrete inconsistency in the current text; (2) evidence from the supplied Cues, translation, glossary, reference material, or user instruction; and (3) a meaningful consequence or verification target. If any one is missing, do not report it.
- Ask internally: “If the current text is left unchanged, what specifically may be wrong or misleading?” If the only answer is that another spelling or wording is also possible, omit the issue.
- Do not report text that your own analysis finds grammatically valid, semantically coherent, factually compatible with the supplied context, or a reasonable approximation such as an explicitly approximate currency conversion.
- Do not treat different concepts, motifs, entities, or terms as inconsistent merely because they occur in the same document. Consistency requires evidence that the occurrences are intended to denote the same thing.
- `inspect_audio`, `verify_entity`, and `verify_number` are actions for a demonstrated uncertainty; they are not substitutes for evidence and must not be used to turn a generic possibility into an issue.
- Write every human-facing `description` and `evidence` in concise Simplified Chinese. Preserve proper names, source quotations, target quotations, and technical identifiers in their original language when accuracy requires it.
- `suggested_text` is the literal replacement candidate, so keep it in the language of the source or translation being corrected; never translate it merely to make the explanation Chinese.
- Report high-, medium-, and low-confidence issues only after the issue itself passes the evidence threshold. `confidence` expresses uncertainty about an established concern; it must not be used to preserve a merely speculative concern.
- `impact` and `confidence` are independent. `impact` is `major`, `moderate`, or `minor`; `confidence` is `high`, `medium`, or `low`.
- Describe the observed problem separately from the inferred correction. Evidence must cite exact Cue content, conflicting occurrences, supplied glossary/reference material, or a concrete source/translation mismatch.
- Never invent audio observations, external facts, brand expansions, spellings, or reference material. If audio or entity verification is needed, say so and choose the corresponding recommended action.
- When several variants conflict and the correct form is not established, report the conflict; do not normalize valid occurrences toward an uncertain outlier.
- Use `suggested_text=null` when the exact correction is uncertain. A suggestion is not required to report a real issue.
- A non-null `suggested_text` must be a literal replacement candidate for the bound token span and must differ from its current text. Never repeat the current text as though it were a correction.
- Consolidate repeated instances of the same underlying consistency problem into one issue with all relevant editable `cue_ids` and token IDs. Keep unrelated problems separate.
- For an omission with no directly corresponding token, the token-ID array may be empty. Otherwise bind the issue to the smallest relevant token span.
- Do not report vague stylistic preferences, harmless wording alternatives, or claims unsupported by the provided document.

## Output contract

Return exactly:

```json
{
  "source_issues": [{
    "issue_type": "suspected_misrecognition",
    "cue_ids": ["cue_x"],
    "token_ids": ["dsp_x"],
    "impact": "major",
    "confidence": "medium",
    "description": "...",
    "evidence": "...",
    "suggested_text": null,
    "recommended_action": "inspect_audio"
  }],
  "translation_issues": [{
    "issue_type": "polarity_or_logic",
    "cue_ids": ["cue_y"],
    "source_token_ids": ["dsp_y"],
    "impact": "major",
    "confidence": "high",
    "description": "...",
    "evidence": "...",
    "suggested_text": "...",
    "recommended_action": "replace_translation"
  }]
}
```

Each issue must contain exactly the fields shown for its track. Use only the allowed issue types and recommended actions. `cue_ids` may contain only editable Cues. Token IDs must belong to the listed Cues and must appear in document order. Do not include `issue_id`, `status`, `track`, `severity`, `message`, `other`, or any extra field; the application adds identity and status later.

Before returning, silently verify that you inspected every required category, did not stop after the first issue, removed every item whose explanation admits the current text is valid or merely proposes an alternative, did not reverse a consistency correction based on an uncertain outlier, wrote `description` and `evidence` in Simplified Chinese, preserved the correction language in `suggested_text`, bound every issue to editable Cues, and produced valid JSON. Return no analysis, Markdown, or prose outside the JSON object.
