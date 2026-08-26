"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {manualCueIntent} = require("../web/editor_timeline.js");

function view() {
  return {
    token_views:[
      {token_id:"t1", text:"A"},
      {token_id:"t2", text:"B"},
    ],
    cue_views:[
      {cue_id:"c1", index:0, start:0, end:1, state:"active", display_token_ids:["t1"]},
      {cue_id:"c2", index:1, start:1.08, end:2, state:"active", display_token_ids:["t2"]},
    ],
  };
}

test("manual cue uses the exact click and may be shorter than the minimum", () => {
  const intent = manualCueIntent(view(), 1.04, 1.5, 2);
  assert.equal(intent.type, "create_manual_cue");
  assert.equal(intent.start, 1.04);
  assert.equal(intent.end, 1.08);
});

test("manual cue treats ranges as half-open and selects occupied cue", () => {
  assert.equal(manualCueIntent(view(), 0.5, 1.5, 2).cue_id, "c1");
  assert.equal(manualCueIntent(view(), 1, 1.5, 2).type, "create_manual_cue");
  assert.equal(manualCueIntent(view(), 1.08, 1.5, 2).cue_id, "c2");
});
