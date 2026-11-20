"use strict";

const assert = require("node:assert/strict");
const {CONTEXT_CUES, build, parseRange, resolveScope} = require("../web/editor_external_review.js");

const cues = Array.from({length:20}, (_, index) => ({
  cue_id:`cue_${index + 1}`,
  index,
  state:"active",
  start:index,
  end:index + 0.8,
  source:`source ${index + 1}`,
  target:{
    target_text:`target ${index + 1}`,
    provenance:{metadata:{}}
  },
  mapping:{
    meaning_unit_id:index === 10 || index === 11 ? "shared_unit" : `unit_${index + 1}`,
    source_evidence_cue_ids:index === 10 || index === 11 ? ["cue_11", "cue_12"] : [`cue_${index + 1}`]
  }
}));

assert.equal(CONTEXT_CUES, 5);
const current = resolveScope(cues, "current", "cue_11", []);
assert.deepEqual(current.cues.map(cue => cue.cue_id), cues.slice(5, 16).map(cue => cue.cue_id));
assert.deepEqual([...current.focusIds], ["cue_11"]);

const selected = resolveScope(cues, "selected", "cue_1", ["cue_8", "cue_10"]);
assert.deepEqual(selected.cues.map(cue => cue.cue_id), cues.slice(2, 15).map(cue => cue.cue_id));
assert.throws(() => resolveScope(cues, "selected", "cue_1", []), /请先/);

assert.deepEqual([...parseRange("12-14；18，20")], [12, 13, 14, 18, 20]);
assert.deepEqual([...parseRange("2; 4、6")], [2, 4, 6]);
assert.throws(() => parseRange("12-8"), /结束编号/);
assert.throws(() => parseRange("十二"), /无法识别/);
const ranged = resolveScope(cues, "range", "cue_1", [], "2，18");
assert.deepEqual([...ranged.focusIds], ["cue_2", "cue_18"]);
assert.deepEqual(
  ranged.cues.map(cue => cue.cue_id),
  [...cues.slice(0, 7), ...cues.slice(12, 20)].map(cue => cue.cue_id)
);
assert.throws(() => resolveScope(cues, "range", "cue_1", [], "50"), /没有可审阅/);

const result = build({
  cues,
  scope:"selected",
  currentCueId:"cue_1",
  selectedCueIds:["cue_11", "cue_12"],
  instruction:"重点核对专名",
  sourceText:cue => cue.source,
  targetText:cue => cue.target.target_text
});
assert.match(result.text, /Cue 是源文的排版与时间单位/);
assert.match(result.text, /允许一对多、多对一和多对多映射/);
assert.match(result.text, /用户补充要求\n重点核对专名/);
assert.match(result.text, /### Cue 11【审阅】/);
assert.match(result.text, /### Cue 6【上下文】/);
assert.match(result.text, /意义组：shared_unit/);
assert.equal(result.focusCount, 2);
assert.equal(result.contextCount, 10);

const rangeResult = build({
  cues,
  scope:"range",
  rangeExpression:"12-14",
  sourceText:cue => cue.source,
  targetText:cue => cue.target.target_text
});
assert.equal(rangeResult.focusCount, 3);
assert.match(rangeResult.text, /### Cue 12【审阅】/);
assert.match(rangeResult.text, /### Cue 7【上下文】/);

const shared = {
  unit_id:"shared_display",
  source_cue_ids:["cue_11", "cue_12"],
  text:"两条 Cue 共用的译文",
  start:10,
  end:11.8
};
const linked = build({
  cues:cues.map(cue => shared.source_cue_ids.includes(cue.cue_id)
    ? {...cue, target_units:[shared]} : cue),
  scope:"current",
  currentCueId:"cue_12",
  sourceText:cue => cue.source,
  targetText:cue => cue.target_units?.[0]?.text || cue.target.target_text
});
assert.equal((linked.text.match(/下行引用：shared_display/g) || []).length, 2);
assert.equal((linked.text.match(/两条 Cue 共用的译文/g) || []).length, 1);
assert.match(linked.text, /显示时段 00:00:10\.000 --> 00:00:11\.800\n源文依据 Cue：11、12/);

console.log("editor_external_review: ok");
