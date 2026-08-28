const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const editor = fs.readFileSync(path.join(__dirname, "..", "web", "editor.js"), "utf8");
const split = fs.readFileSync(path.join(__dirname, "..", "web", "split.html"), "utf8");
const settings = fs.readFileSync(path.join(__dirname, "..", "web", "settings.html"), "utf8");
const timeline = fs.readFileSync(path.join(__dirname, "..", "web", "editor_timeline.js"), "utf8");

test("space is reserved only for text editing contexts", () => {
  assert.match(editor, /function isTextEditingContext\(event\)/);
  assert.match(editor, /event\.composedPath\(\)/);
  assert.match(editor, /toggleMediaPlayback\(\);\s*\n\s*\}, true\);/);
  assert.doesNotMatch(editor, /closest\('input, textarea, select, button, a/);
});

test("playback, cue hiding, and timeline zoom read configurable shortcuts", () => {
  assert.match(settings, /name="shortcut_play_pause"/);
  assert.match(settings, /name="shortcut_hide_cue"/);
  assert.match(settings, /name="timeline_zoom_modifier"/);
  assert.match(editor, /state\.shortcuts\.playPause/);
  assert.match(editor, /state\.shortcuts\.hideCue/);
  assert.match(timeline, /options\.zoomModifier/);
  assert.match(timeline, /options\.hideCueShortcut/);
});

test("runtime log labels stages and offers copy", () => {
  assert.match(split, /<summary>任务进度（仅阶段与报错）<\/summary>/);
  assert.match(split, /id="copyRuntimeLog"/);
});
