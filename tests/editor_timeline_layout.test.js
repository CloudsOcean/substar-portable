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
