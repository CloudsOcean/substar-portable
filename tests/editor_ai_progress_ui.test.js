const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "..");

test("editor renders counted AI task stages with the short repair label", () => {
  const html = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
  const js = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");

  assert.match(html, /id="translationTaskSteps"/);
  assert.match(js, /renderAiProgress\(\s*task\?\.ai_progress/);
  assert.match(js, /state\.editorAiTask\?\.kind === "calibration"/);
  assert.doesNotMatch(js, /Fallback 修复/);
});
