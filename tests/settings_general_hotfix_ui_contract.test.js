const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "settings.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "settings.js"), "utf8");

test("general settings retain editor shortcuts without obsolete export or dubbing settings", () => {
  assert.match(html, /data-panel="general"/);
  assert.doesNotMatch(html, /name="default_export_dir"/);
  assert.doesNotMatch(html, /Windows“另存为”窗口/);
  assert.match(html, /name="shortcut_undo"/);
  assert.match(html, /name="shortcut_redo"/);
  assert.match(html, /name="shortcut_play_pause"/);
  assert.match(html, /name="shortcut_hide_cue"/);
  assert.match(html, /name="timeline_zoom_modifier"/);
  assert.match(js, /function shortcutFromEvent/);
  assert.doesNotMatch(html, /dubbing_|配音/);
});

test("hotfix fingerprints the changed settings stylesheet", () => {
  assert.match(html, /settings\.css\?v=20260828-model-controls-1/);
});
