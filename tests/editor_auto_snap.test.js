const test = require("node:test");
const assert = require("node:assert/strict");

global.SubstarLanguageLayout = {layoutTokens: values => (values || []).join("")};
const timeline = require("../web/editor_timeline.js");

function view() {
  return {
    token_views: [],
    cue_views: [
      {cue_id:"left", index:0, start:0, end:1, state:"active", display_token_ids:[]},
      {cue_id:"right", index:1, start:1.6, end:2.2, state:"active", display_token_ids:[]}
    ]
  };
}

test("combined auto snap applies forward starts before the backward threshold", () => {
  const intent = timeline.autoSnapPlan(view(), {
    forwardStarts:{right:1.45},
    backwardThresholdMs:500
  });

  assert.deepEqual(intent.changes, [
    {cue_id:"left", edge:"end", time:1.45},
    {cue_id:"right", edge:"start", time:1.45}
  ]);
});

test("forward-only smart snap does not fill pure silence", () => {
  const intent = timeline.autoSnapPlan(view(), {
    forwardStarts:{right:1.45},
    backwardThresholdMs:null
  });

  assert.deepEqual(intent.changes, [
    {cue_id:"right", edge:"start", time:1.45}
  ]);
});

test("smart forward snap moves a touching pair as one shared boundary", () => {
  const touching = view();
  touching.cue_views[0].end = 1.6;
  const intent = timeline.autoSnapPlan(touching, {
    forwardStarts:{right:1.45},
    backwardThresholdMs:500
  });

  assert.deepEqual(intent.changes, [
    {cue_id:"left", edge:"end", time:1.45},
    {cue_id:"right", edge:"start", time:1.45}
  ]);
});
