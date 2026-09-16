# Subtitle import and shared review

## Implemented architecture

- `subtitle_project_import.py`: explicit single, two-file, bilingual-line and
  delimiter-based bilingual-inline previews. Never infer bilingual tracks from
  ordinary wrapping. Ambiguous splits block import. Unique exact-time pairs are
  suggested; unmatched and overlapping entries retain their independent timing.
- `build_subtitle_document`: creates real token-free `DisplayCue.source_text`
  records, not sentence-sized fake tokens. Source-only and target-only entries
  preserve unmatched track timings. Domain serialization, ProjectStore and SRT
  export support these records. Existing token documents serialize unchanged.
- `review_notes.py`: cue/track/token anchors, original text snapshots, explicit
  reply/resolve/reopen, changed-text and missing-anchor projections.
- `review_store.py`: atomic `review/notes.json` sidecar with optimistic version
  checks. Existing package traversal includes the review directory.
- `http_api.py`: GET/POST review notes; checks subtitle revision and review version.
- Editor review is initialized: the button before the timeline selector enables
  read-only review, while playback and selection remain available. Opening review
  waits for queued edits. The locator supports open/all filtering, previous/next,
  replies, resolve/reopen and explicit relocation with preserved anchor history.

## User-facing workflow

1. Select `基于已有字幕生成`. Media and subtitle files are required. Explicit
   format/order/separator controls precede a mandatory preview confirmation.
2. `subtitle_api.py` prepares waveform audio without ASR or AI calls and registers
   the completed ProjectStore. Native media references reuse the existing media
   link service; browser uploads remain managed media.
3. Both tracks are ordinary text fields. Source edits use `set_source_text` in
   the shared operation queue, revision CAS and undo history. Token-only commands
   and AI operations are disabled for imported projects.
4. The same timeline controller supports both layouts; imported projects default
   to separated lanes. Independent overlapping track timing is preserved.
5. Review supports tokens, whole cues and Unicode text selections. Changed anchors
   are flagged rather than silently rebound. Notes and subtitle import provenance
   travel in project packages, not ordinary SRT; a text review list is exportable.

## Verification

Automated coverage includes all import forms, ambiguity, independent timing,
domain roundtrip, real FFmpeg creation, waveform retrieval, editing, review CAS,
Unicode anchors and package roundtrip. Isolated browser checks cover source edits
across reload, review creation, resolve/reopen, changed-text indication and the
separated timeline. Existing token-project regression tests remain enabled.

No existing user project was replaced or converted during foundation work.
