"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {separatedGeometry} = require("../web/editor_timeline.js");

test("separated timeline reserves distinct waveform and subtitle lanes", () => {
  for (const height of [140, 156, 180, 240]) {
    for (const hasTarget of [false, true]) {
      const g = separatedGeometry(height, hasTarget);
      assert.equal(g.tracks, hasTarget ? 2 : 1);
      assert.ok(g.waveformBottom > 26);
      assert.ok(g.waveformBottom < g.cueTop);
      assert.ok(g.trackHeight >= 20);
      assert.ok(g.cueTop + g.tracks * (g.trackHeight + 4) <= height);
    }
  }
});

test("monolingual layout returns unused target space to waveform", () => {
  assert.ok(separatedGeometry(180, false).waveformBottom > separatedGeometry(180, true).waveformBottom);
});

test("extra panel height preserves composite waveform bounds", () => {
  for (const height of [150, 180, 240]) {
    assert.equal(separatedGeometry(height + 36, false).waveformBottom, height - 10);
    assert.equal(separatedGeometry(height + 69, true).waveformBottom, height - 10);
  }
});


test('rounded boundary edits stay within fractional imported neighbour times',()=>{
  const timeline=require('../web/editor_timeline.js');
  const cues=[{cue_id:'a',start:0,end:1.2344},{cue_id:'b',start:1.2344,end:2.5006},{cue_id:'c',start:2.5006,end:4}];
  for(const requested of [0,1.234,1.2344,1.2345,2.5005,2.5006,10]){
    const start=timeline.previewBoundaryChange(cues,1,'start',requested,'b','single');
    const end=timeline.previewBoundaryChange(cues,1,'end',requested,'b','single');
    assert.ok(start.time>=cues[0].end);
    assert.ok(end.time<=cues[2].start);
    assert.equal(start.changes[0].time,start.time);
    assert.equal(end.changes[0].time,end.time);
  }
});
