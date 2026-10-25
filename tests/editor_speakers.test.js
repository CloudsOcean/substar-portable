const test = require("node:test");
const assert = require("node:assert/strict");
const speakers = require("../web/editor_speakers.js");

test("speakers combine recognised and manually named identifiers without a four-slot limit", () => {
  const cues = Array.from({length:100}, (_,i) => ({speaker:`speaker_${i}`}));
  const view = {cue_views:[...cues,{speaker:"speaker_99"},{speaker:null}],properties:{speaker_names:{"guest-A":"Guest"}}};
  const ids = speakers.ids(view);
  assert.equal(ids.length,101);
  assert.ok(ids.indexOf("speaker_2") < ids.indexOf("speaker_10"));
  assert.equal(speakers.nextId(ids),"speaker_100");
  assert.equal(speakers.label("guest-A", view.properties.speaker_names),"Guest");
  assert.equal(speakers.label("speaker_99"),"说话人 100");
});
test("identifier colours survive renaming and list filtering", () => {
  assert.equal(speakers.color("speaker_99"),speakers.color("speaker_99"));
  assert.notEqual(speakers.color("speaker_99"),speakers.color("speaker_100"));
  assert.equal(speakers.nextId(["speaker_0","speaker_2"]),"speaker_1");
  assert.equal(speakers.validId("a\nb"),false);
  assert.equal(speakers.label("__proto__"),"__proto__");
  assert.equal(typeof speakers.color("__proto__"),"string");
});
