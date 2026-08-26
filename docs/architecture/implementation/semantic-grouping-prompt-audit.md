# Semantic grouping prompt audit

Status: discussion draft. This audit does not change production prompt semantics.

## Decisions already fixed

- The human-confirmed task snapshot is authoritative. A configured source
  language and its hard limit must not be overridden by content detection.
  Detection is only permitted when the user explicitly selects `Auto`.
- The canonical operation name is `semantic_grouping`; its repair operation is
  `semantic_grouping_repair`.
- The refactored runtime will not carry `P2mix` aliases, dual reads, or dual
  writes. If retained user data needs conversion, it is upgraded once at the
  project migration boundary. Incomplete legacy tasks are recreated.

## What the current production call actually uses

The current one-step segmentation call still uses the original layered prompt
framework:

1. common grouping/layout instructions;
2. a language-specific boundary policy;
3. constructed language examples;
4. a sentence-boundary evidence policy;
5. a compiled glossary appended at call time.

The task snapshot selects the language variant and hard limit. The request also
contains immutable global token indexes, timing, sentence-boundary evidence,
speaker identity, block ownership, and neighbouring context. The program then
validates continuous coverage and line-break indexes.

This framework is structurally sound. The Phase 5 refactor reused it; it did not
replace it with a newly optimized semantic prompt.

## Keep

- Common rules, language rules, examples, boundary-evidence policy, and glossary
  remain separate prompt fragments.
- Source tokens and global indexes remain immutable.
- Every owned token must be covered exactly once, continuously, and in order.
- JSON-only output and deterministic program validation remain mandatory.
- Meaning groups and display cue boundaries remain distinct concepts.
- Speaker changes, language grammar, glossary atoms, timing, and pauses remain
  evidence used by the model rather than post-hoc text rewriting.
- The confirmed task snapshot remains the sole authority for language profile
  and character limit.

## P0 findings

### 1. The prompt combines two product responsibilities

The current prompt asks one model response to produce:

- semantic groups;
- display cue boundaries;
- optional ASR lexical calibration.

The third responsibility belongs to the independent calibration workflow. It
adds output branches, validation paths, and failure modes to the operation that
must reliably create an editable subtitle document.

Recommendation: `semantic_grouping` returns only groups, cue boundaries, and
structural exceptions. ASR lexical corrections are proposed by the later
`calibration` operation and never affect whether grouping succeeds.

### 2. The English hard-limit rule is contradictory

The English rule currently says to split "before or at the nearest legal
boundary after the limit". That cannot define a deterministic preference and
can encourage an over-limit cue even when a legal earlier boundary exists.

Replace it with one exact rule:

> Choose the latest legal boundary whose projected cue length is within the
> hard limit. If none exists after the previous cue boundary, choose the first
> following legal boundary, preserve all source tokens, and emit a structured
> indivisible-overflow exception.

The same rule should be expressed equivalently in every language fragment.

### 3. The output contract is prose, not one canonical schema

The prompt embeds a small example, while production validation accepts multiple
historical schema names and loosely shaped exceptions. This makes the model and
validator negotiate an implicit contract.

Recommendation: introduce exactly one `substar.semantic-grouping-result.v1`
schema. Validate it before semantic validation. `exceptions` should use a fixed
shape and finite codes, for example:

```json
{
  "code": "indivisible_overflow",
  "alignment_start": 120,
  "alignment_end": 126,
  "detail": "proper_name"
}
```

The prompt should contain a compact schema summary; the repository schema is the
authority.

### 4. Context ownership needs an explicit negative rule

The prompt says that every `owner=true` token must be covered, but it does not
plainly say what must happen to neighbouring `owner=false` rows.

Add: context rows may inform the first and last boundary, but must never appear
in returned groups, cue boundaries, corrections, or exceptions owned by this
block.

### 5. Meaning groups and display cues are easy to conflate

The examples explain the distinction, but the output field
`line_breaks_after` is nested inside `meaning_groups` without concise invariants.

Add these explicit invariants:

- a meaning group is one semantic/discourse unit;
- a meaning group may contain one or more display cues;
- every group end is a cue end;
- an internal cue end is not automatically a meaning-group end;
- a display cue may be grammatically dependent on an adjacent cue in the same
  meaning group when the hard limit requires it.

### 6. Repair is allowed to rewrite too much

The repair request includes the rejected output and validator feedback, but the
prompt only says to repair the split structure. It does not require unchanged
valid groups to remain byte-for-byte stable.

Recommendation: identify failing group indexes in structured validator output.
The repair call may replace only those groups and must echo all other groups
unchanged. If the validator cannot localize the failure, rerun the complete
block explicitly rather than pretending it is a local repair.

## P1 findings

### Language-rule parity

English and Chinese rules are substantially more detailed than Japanese and
Korean. Expand Japanese and Korean coverage for particles/endings, auxiliary
constructions, counters and units, proper names, quotations, conjunctions,
speaker changes, and the exact hard-limit rule. The goal is equivalent
constraints, not word-for-word translations.

### Examples do not demonstrate the actual JSON contract

The current English examples explain boundaries with slash notation. They are
useful linguistically but do not teach ownership, indexes, multiple cues in one
group, or structured exceptions.

Keep a small set of linguistic counterexamples, then add three to five compact
index-based input/output cases per language covering:

- multiple display cues inside one meaning group;
- speaker change creating a new meaning group;
- context rows excluded from output;
- glossary/proper-name atomicity;
- an indivisible hard-limit overflow.

### Boundary modes are duplicated as registry keys

The registry currently multiplies grouping and repair keys across reference,
reconstruct, and unpunctuated modes. Use one `semantic_grouping` key and append
one selected boundary-evidence fragment from an explicit request enum. Use one
`semantic_grouping_repair` key. This removes a combinatorial registry without
introducing runtime compatibility aliases.

### Prompt size is not the main problem

The static prompt fragments are modest. Most request volume comes from the ASR
token rows. Reducing instructions for token count would sacrifice clarity for
little gain. Optimize contract precision and responsibility boundaries first.

## Proposed canonical prompt package

```text
prompts/production/semantic_grouping/
  core.md
  repair.md
  languages/
    en.md
    zh.md
    ja.md
    ko.md
  boundary_evidence/
    reference.md
    reconstruct.md
    unpunctuated.md
  examples/
    en.json
    zh.json
    ja.json
    ko.json
schemas/
  semantic-grouping-result.v1.schema.json
```

The new package contains no `P1`, `P2mix`, `stage1`, `experiment`, or merged
P2/P3 terminology.

## Recommended implementation order

1. Freeze golden input/output cases from representative English, Chinese,
   Japanese, Korean, mixed-language, and speaker-change material.
2. Introduce the single result schema and make validator errors structured.
3. Remove calibration from semantic grouping.
4. Rewrite the common contract and hard-limit language; expand Japanese and
   Korean rules.
5. Replace slash-only examples with compact index-based examples.
6. Rename the operation, prompt package, events, settings, artifacts, and
   provenance together in one clean cut.
7. Run the golden set and the real video acceptance case; compare coverage,
   hard-limit violations, repair rate, cue count, and manual boundary quality.

No production prompt wording should be switched until the golden comparison is
reviewed, because prompt quality is a behavioural product change rather than a
mechanical refactor.
