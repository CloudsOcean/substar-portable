const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(
  path.join(__dirname, "..", "web", "split.js"),
  "utf8",
);
const styles = fs.readFileSync(
  path.join(__dirname, "..", "web", "split.css"),
  "utf8",
);

test("split UI stops presenting cached active jobs as live after backend loss", () => {
  assert.match(source, /runtimeConnected:\s*true/);
  assert.match(source, /state\.runtimeConnected\s*=\s*false/);
  assert.match(source, /后端已断开/);
  assert.match(source, /disconnectedActive/);
  assert.match(source, /systemNode.dataset.runtimeDisconnected = "true"/);
});

test("task cards retain error copying without a duplicate runtime panel", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "web", "split.html"), "utf8");
  assert.doesNotMatch(html, /id="(?:runtimeLog|statusPill|copyRuntimeLog)"/);
  assert.doesNotMatch(source, /renderRuntimeLog|renderTaskProgress|runtimeJobId|#statusPill/);
  assert.match(source, /copyText\(errorText, "报错已复制"\)/);
});

test("creation calibration and translation retain independent task cards", () => {
  assert.doesNotMatch(source, /coalescePipelineJobs|project_pipeline/);
  assert.match(source, /id:`editor:\$\{task\.project_id\}:\$\{task\.task_id\}`/);
  assert.match(source, /ai_progress:task\.ai_progress \|\| null/);
  assert.match(source, /job\.ai_progress\?\.progress \?\? job\.progress/);
  assert.match(source, /SubstarAiProgressSummary\?\.format/);
  assert.doesNotMatch(styles, /\.queue-ai-counts/);
});
