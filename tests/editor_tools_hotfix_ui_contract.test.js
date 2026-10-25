const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
const css = fs.readFileSync(path.join(root, "web", "editor.css"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");
const timeline = fs.readFileSync(path.join(root, "web", "editor_timeline.js"), "utf8");
const documentContract = fs.readFileSync(path.join(root, "web", "editor_document.js"), "utf8");

test("release keeps the non-dubbing editor tool contract", () => {
  const tools = [...html.matchAll(/data-tool-panel="([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(tools, ["locator", "search", "punctuation", "auto-snap", "merge", "legend"]);
  assert.match(html, /id="autoSnapMenu" class="tool-accordion auto-snap-menu"/);
  assert.match(css, /\.editor-tool-sidebar \.auto-snap-popover \{[^}]*width: 100%;[^}]*min-width: 0;/s);
  assert.match(js, /#detectSpeakers"/);
  assert.doesNotMatch(html, /dubbingSetup|配音设置/);
});

test("hotfix fingerprints every changed editor asset", () => {
  const boot=fs.readFileSync(path.join(root,"web","editor_maw_boot.js"),"utf8");
  for (const asset of ["editor.css","editor.js","editor_document.js","editor_timeline.js","editor_maw_boot.js"]) {
    assert.match(html+boot, new RegExp(asset.replaceAll(".","\\.")+"\\?v=[a-zA-Z0-9_-]+"));
  }
});

test("command and history actions share hover feedback", () => {
  assert.match(css, /\.editor-command-bar button:hover:not\(:disabled\)/);
  assert.match(css, /\.editor-command-menu:not\(\.disabled\) > summary:hover/);
  assert.match(css, /\.editor-command-link:hover/);
  assert.match(css, /\.cue-history-row button:hover:not\(:disabled\)/);
  assert.match(css, /\.cue-history-row button:disabled \{[^}]*opacity: \.36;[^}]*cursor: not-allowed;/s);
});

test("smart snap and multi-token search producers match their consumers", () => {
  assert.match(html, /id="forwardSnapPreRoll"[^>]*value="0"/);
  assert.match(js, /preRollStored === null \? 0 : Number\(preRollStored\)/);
  assert.match(js, /autoSnapOnce\(\{[\s\S]*forwardStarts/);
  assert.match(timeline, /function autoSnapPlan\(view, options = \{\}\)/);
  assert.match(documentContract, /function findContiguousTokenMatches/);
  assert.match(js, /contract\.findContiguousTokenMatches\(tokens, query\)/);
});

