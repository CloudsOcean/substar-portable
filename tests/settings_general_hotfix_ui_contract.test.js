const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "settings.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "settings.js"), "utf8");

test("general settings use system save-as and retain editor shortcuts without dubbing settings", () => {
  assert.match(html, /data-panel="general"/);
  assert.doesNotMatch(html, /name="default_export_dir"/);
  assert.match(html, /Windows“另存为”窗口/);
  assert.match(html, /name="shortcut_undo"/);
  assert.match(html, /name="shortcut_redo"/);
  assert.match(js, /function shortcutFromEvent/);
  assert.doesNotMatch(html, /dubbing_|配音/);
});

test("hotfix fingerprints the changed settings stylesheet", () => {
  assert.match(html, /settings\.css\?v=20260827-general-merge-1/);
});
