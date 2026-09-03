const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "..");

test("editor renders the shared AI task count summary instead of lifecycle stages", () => {
  const html = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
  const js = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");

  assert.match(html, /id="translationTaskSteps"/);
  assert.match(html, /ai_progress_summary\.js/);
  assert.match(js, /renderAiProgress\(\s*task\?\.ai_progress/);
  assert.match(js, /state\.editorAiTask\?\.kind === "calibration"/);
  assert.match(js, /SubstarAiProgressSummary\?\.summarize/);
  assert.match(js, /counter\.textContent = `\$\{Math\.round\(percent\)\}%`/);
  assert.match(js, /message\.classList\.add\("hidden"\)/);
  assert.doesNotMatch(js, /AI_PHASE_ORDER/);
});
