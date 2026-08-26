# Semantic grouping repair

Repair only the structure rejected by the program. Return JSON only.

- Treat `program_validation_error` as authoritative.
- `accepted_groups_frozen` contains groups that already passed structural validation. Copy those ranges and their Cue boundaries exactly; repair only uncovered or rejected ranges. Never redesign a successful group.
- When it identifies an over-limit Cue by alignment range, inspect the supplied
  owned tokens in that exact range and add the minimum necessary legal
  `line_breaks_after` indexes inside it. Do not merely return the rejected
  boundaries again.
- Preserve every owned token, exact indexes, source order, and result binding.
- Context rows are read-only and must not enter the result.
- Make meaning groups continuous and complete.
- End every group with its own final `line_breaks_after` index.
- Apply the authoritative hard limit to every display Cue.
- Do not correct or rewrite source text.
- Do not add calibration, canonicalization, translation, or commentary fields.

Return only `substar.semantic-grouping-result.v1` JSON.
