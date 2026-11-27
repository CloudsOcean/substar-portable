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
  assert.match(html, /id="shortcutRowTemplate"/);
  assert.match(js, /shortcuts\.actions/);
  assert.match(js, /input\.name = "shortcut_" \+ id/);
  require("../web/editor_shortcuts.js");
  const {serverActions} = globalThis.SubstarShortcuts;
  for (const action of ["undo","redo","play_pause","hide_cue"]) assert.ok(serverActions.has(action));
  assert.match(html, /name="timeline_zoom_modifier"/);
  assert.match(js, /shortcuts\.fromEvent/);
  assert.doesNotMatch(html, /dubbing_|配音/);
});

test("hotfix fingerprints the changed settings stylesheet", () => {
  assert.match(html, /settings\.css\?v=[^"]+/);
  assert.match(html, /settings\.js\?v=[^"]+/);
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
