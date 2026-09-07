# English segmentation prompt review

The request-block planner remains programmatic. Subtitle boundaries within each
block are model-authored. At most one semantic repair is allowed. If lengths
still overflow, delivery preserves the latest structurally complete model
response and marks the affected ranges for review. If the repair is malformed,
the structurally complete primary response is retained. A block placeholder
remains the last resort when neither response has valid coverage.

The prompt now distinguishes optional phrase seams from necessary boundaries,
asks the model to review short dependent Cues and adjacent pairs, and clarifies
terminal prepositions before a new sentence. Repair examples discourage isolated
connectors. No local merging, re-cutting, or duration threshold was introduced.

## Video comparison

Source: `Weixin Videos2026-08-02_194048_181.mp4`. Reused the original frozen ASR
material (174 tokens), GLM-5.3-flash with thinking enabled, temperature 0,
55-character limit, 90-second request-block target. The final experiment used
one model call and no semantic repair, and produced a candidate without replacing
the user's current editor project.

| Measurement | Original | Revised |
|---|---:|---:|
| Cues | 28 | 27 |
| Cues shorter than 1 second | 5 | 0 |
| Over-limit Cues | 0 | 0 |
| Covered source tokens | 174 | 174 |

Improved: `Writing on Truth Social a short time ago`, the short coordinated
predicate `is locked and loaded and ready to go`, `to hold off any attack`, and
the sentence transition `agreed to / this would ...`.

Remaining: the model still splits `in that the parameters of a deal / has been
agreed to` despite the combined span fitting. Earlier prompt experiments also
varied in quality (25 and 30 Cues). This is a single-video comparison, not an
estimate of general accuracy or a guarantee of consistently optimal segmentation.

Local evidence: `data/audit-validation/acceptance/prompt-comparison.json`,
`prompt-original.srt`, `prompt-revised.srt`, and `prompt-v4/` with prompt hashes,
raw model response, model-call telemetry, validation and candidate document.

Validation: 482 Python tests passed, including regression cases for a still
over-limit repair, malformed repair and valid repair. Each case asserts one
repair call even when a larger retry count is passed, and exact retention of
the chosen model boundaries. The system map check also passed.
