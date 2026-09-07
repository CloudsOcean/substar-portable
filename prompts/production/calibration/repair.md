# One-pass calibration block repair

The complete original execution block remains in the request. In this single repair pass, fix every OWN alias that the primary response omitted or that the program could not bind. CONTEXT rows are frozen accepted context.

- Cover every OWN alias in this repair request exactly once.
- Return the complete corrected source-language row, including unchanged text.
- Address all PROGRAM VALIDATION errors for the block in the same response.
- Preserve row boundaries, order, alignment separators, timing, meaning and language.
- Do not repeat CONTEXT rows and do not return aliases outside this repair scope.
