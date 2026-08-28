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

test("waveform bars stop at the physical waveform window", () => {
  assert.deepEqual(
    timeline.waveformSampleRange(8.9, 9.1, 0, 9, 900),
    {from:890, to:900}
  );
  assert.equal(timeline.waveformSampleRange(9.1, 9.3, 0, 9, 900), null);
});
