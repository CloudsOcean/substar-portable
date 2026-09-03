"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../web/editor.js"), "utf8");

assert.match(
  source,
  /function navigateEntries[\s\S]*?selectCue\(entry\.cue_id, true, true\);/,
  "locator navigation must seek and center the matching Cue"
);
assert.match(
  source,
  /function selectCue\(cueId, seek = false, scroll = false\)[\s\S]*?activateCue\(cueId, \{seek, scroll,/,
  "selectCue must forward explicit list scrolling to activateCue"
);

console.log("editor_locator_navigation: ok");
