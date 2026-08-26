(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorCueOrdering = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function compare(left, right, positions) {
    return Number(left.start) - Number(right.start)
      || Number(left.end) - Number(right.end)
      || Number(positions.get(String(left.cue_id))) - Number(positions.get(String(right.cue_id)))
      || String(left.cue_id).localeCompare(String(right.cue_id));
  }

  function canonicalCueOrder(cues) {
    const current = [...(cues || [])];
    const positions = new Map(current.map((cue, index) => [String(cue.cue_id), index]));
    return current
      .sort((left, right) => compare(left, right, positions))
      .map((cue, index) => Number(cue.index) === index ? cue : {...cue, index});
  }

  function cueOrderIds(cues) {
    return canonicalCueOrder(cues).map(cue => String(cue.cue_id));
  }

  function isCanonicalCueOrder(cues) {
    const current = [...(cues || [])];
    const canonical = canonicalCueOrder(current);
    return current.length === canonical.length && current.every((cue, index) =>
      String(cue.cue_id) === String(canonical[index].cue_id)
      && Number(cue.index) === index
    );
  }

  return Object.freeze({canonicalCueOrder, cueOrderIds, isCanonicalCueOrder});
});
