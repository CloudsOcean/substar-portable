const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const editor = fs.readFileSync(path.join(__dirname, "..", "web", "editor.js"), "utf8");
const split = fs.readFileSync(path.join(__dirname, "..", "web", "split.html"), "utf8");
const settings = fs.readFileSync(path.join(__dirname, "..", "web", "settings.html"), "utf8");
require("../web/editor_shortcuts.js");
const shortcuts = globalThis.SubstarShortcuts;
const shortcutSource = fs.readFileSync(path.join(__dirname, "..", "web", "editor_shortcuts.js"), "utf8");
const mawTimeline = fs.readFileSync(path.join(__dirname, "..", "web", "editor_maw_timeline.js"), "utf8");
const timeline = fs.readFileSync(path.join(__dirname, "..", "web", "editor_timeline.js"), "utf8");

test("space is reserved only for text editing contexts", () => {
  assert.match(shortcutSource, /function inInput\(event, \{textOnly = false\}/);
  assert.match(shortcutSource, /event\.composedPath\(\)/);
  assert.match(editor, /action === "play_pause"\) return toggleMediaPlayback\(\);/);
  assert.doesNotMatch(editor, /closest\('input, textarea, select, button, a/);
});

test("playback, cue hiding, and timeline zoom read configurable shortcuts", () => {
  assert.equal(shortcuts.bindings({shortcut_play_pause:"Backspace"}).play_pause, "Backspace");
  assert.equal(shortcuts.bindings({shortcut_hide_cue:"Space"}).hide_cue, "Space");
  assert.match(settings, /name="timeline_zoom_modifier"/);
  assert.match(editor, /shortcutBindings/);
  assert.match(shortcutSource, /"hide_cue", "隐藏字幕", "Backspace"/);
  assert.match(timeline, /options\.zoomModifier/);
  assert.match(mawTimeline, /action === "hide_cue"/);
});

test("task cards replace the standalone runtime log", () => {
  assert.doesNotMatch(split, /id="runtimeLog"|id="copyRuntimeLog"/);
  const code = fs.readFileSync(path.join(__dirname, "..", "web", "split.js"), "utf8");
  assert.match(code, /copyText\(lines\.join\("\\n"\),/);
});
