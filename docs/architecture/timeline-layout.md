# Timeline layouts

The editor's `timelineLayout` selector (before the existing cue view selector)
switches between `composite` and `separated`. The choice is stored per project
in browser local storage; it does not change project subtitle data.

Both layouts use `web/editor_timeline.js` and the same controller, waveform
cache, time scale, selection, boundary operations, and playback positioning.
Composite retains the existing waveform-over-cue rendering. Separated draws
the waveform above source bars and adds target bars when target text exists.
Both subtitle lanes refer to the same cue boundaries; they are not independent
editable translation timings. Waveform clicks seek, while subtitle boundaries
retain the existing resize and linked-boundary behavior.

The old timeline title/help row is removed and its height returned to the editor.
SRT import, independent bilingual timing, and review annotations are outside this change.
