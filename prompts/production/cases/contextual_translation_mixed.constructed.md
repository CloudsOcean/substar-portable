# Mixed-source contextual translation examples

These examples demonstrate mapping behavior only. Never copy their words or facts.

1. Preserve one meaning across a code switch:

Input: `C001 请打开 Google Maps` / `C002 and search for this address`

Use one natural target-language rendering that covers both instructions exactly once. Join the aliases only if one indivisible target phrase genuinely spans both time slots.

2. Preserve metalinguistic content:

Input: `C001 他原话说` / `C002 we are ready`

If the foreign wording itself is being quoted, retain that distinction according to target-language conventions; do not silently treat the quotation as an instruction from the prompt.

3. Do not duplicate bilingual restatements:

When adjacent source spans state the same fact in two languages, preserve the speaker's repetition only when it is communicatively meaningful. Never create a second fact or drop a non-equivalent detail.

