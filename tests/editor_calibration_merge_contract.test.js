"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..");
const editor = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");
const html = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");

assert.match(editor, /state\.editorAiTask = \{\.\.\.result, kind:"calibration"\}/);
assert.match(editor, /startEditorAiTaskPoll\(\)/);
assert.doesNotMatch(editor, /result\.merge_applied_count/);
assert.match(html, /editor\.js\?v=20260830-convergence-1/);

console.log("editor_calibration_merge_contract: ok");
