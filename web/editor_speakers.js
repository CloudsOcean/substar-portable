(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorSpeakers = api;
})(typeof globalThis === "object" ? globalThis : this, function() {
  "use strict";
  const validId = value => typeof value === "string" && value.trim().length > 0
    && value.length <= 120 && !/[\x00-\x1f]/.test(value);
  function ids(view) {
    return [...new Set([
      ...Object.keys(view?.properties?.speaker_names || {}),
      ...(view?.cue_views || view?.cues || []).map(cue => cue.speaker)
    ].filter(validId))].sort((a,b) => a.localeCompare(b, "en", {numeric:true}));
  }
  function nextId(values) {
    const used = new Set(values);
    let index = 0;
    while (used.has(`speaker_${index}`)) index++;
    return `speaker_${index}`;
  }
  function label(id, names = {}) {
    if (!id) return "未分配说话人";
    const match = /^speaker_(\d+)$/.exec(id);
    return (Object.hasOwn(names, id) && names[id]) || (match ? `说话人 ${Number(match[1]) + 1}` : id);
  }
  function color(id) {
    if (!id) return "#8996a5";
    const legacy = {speaker_0:"#5596ef",speaker_1:"#e568a8",speaker_2:"#46bda7",speaker_3:"#e5a452"};
    if (Object.hasOwn(legacy, id)) return legacy[id];
    let hash = 0;
    for (const char of id) hash = (Math.imul(hash,31) + char.codePointAt(0)) >>> 0;
    return `hsl(${hash * 137.508 % 360} 58% 58%)`;
  }
  return {validId, ids, nextId, label, color};
});
