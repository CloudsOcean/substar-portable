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
  assert.match(html, /settings\.css\?v=20260831-unsaved-nav-1/);
  assert.match(html, /settings\.js\?v=20260831-unsaved-nav-1/);
});

test("unsaved settings navigation offers save discard and stay actions", () => {
  assert.match(html, /id="unsavedNavigationDialog"/);
  assert.match(html, /data-unsaved-navigation="save"[^>]*>保存并前往/);
  assert.match(html, /data-unsaved-navigation="discard"[^>]*>放弃更改并前往/);
  assert.match(html, /data-unsaved-navigation="stay"[^>]*>留在此页/);
  assert.match(js, /function hasUnsavedPageChanges/);
  assert.match(js, /await saveAllBeforeNavigation\(\)/);
  assert.match(js, /window\.location\.assign\(pendingNavigationUrl\)/);
  assert.match(js, /\.app-header a\[href\]/);
  assert.match(js, /target\.pathname === window\.location\.pathname/);
});
