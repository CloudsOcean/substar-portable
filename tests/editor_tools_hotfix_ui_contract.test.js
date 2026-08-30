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
  assert.deepEqual(tools, ["locator", "search", "punctuation", "auto-snap", "legend"]);
  assert.match(html, /id="autoSnapMenu" class="tool-accordion auto-snap-menu"/);
  assert.match(css, /\.editor-tool-sidebar \.auto-snap-popover \{[^}]*width: 100%;[^}]*min-width: 0;/s);
  assert.match(js, /#detectSpeakers"/);
  assert.doesNotMatch(html, /dubbingSetup|配音设置/);
});

test("hotfix fingerprints every changed editor asset", () => {
  const fingerprints = {
    "editor.css":"20260830-convergence-1",
    "editor.js":"20260830-convergence-1",
    "editor_document.js":"20260826-topology-stable-1",
    "editor_timeline.js":"20260828-shortcuts-1",
    "editor_tutorial.js":"20260826-tutorial-target-1",
  };
  for (const [asset, version] of Object.entries(fingerprints)) {
    assert.match(html, new RegExp(`${asset.replace(".", "\\.")}\\?v=${version}`));
  }
});

test("smart snap and multi-token search producers match their consumers", () => {
  assert.match(js, /autoSnapOnce\(\{[\s\S]*forwardStarts/);
  assert.match(timeline, /function autoSnapPlan\(view, options = \{\}\)/);
  assert.match(documentContract, /function findContiguousTokenMatches/);
  assert.match(js, /contract\.findContiguousTokenMatches\(tokens, query\)/);
});
