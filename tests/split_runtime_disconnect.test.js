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
  assert.match(source, /页面已停止把缓存状态显示为正在运行/);
});

test("runtime log wraps long messages without a horizontal scrollbar", () => {
  assert.match(styles, /\.runtime-log-lines \{[^}]*overflow-x: hidden/);
  assert.match(styles, /\.runtime-log-lines pre \{[^}]*white-space: pre-wrap/);
  assert.match(styles, /\.runtime-log-lines pre \{[^}]*overflow-wrap: anywhere/);
  assert.doesNotMatch(styles, /\.runtime-log-lines pre \{[^}]*min-width: max-content/);
});

test("creation calibration and translation retain independent task cards", () => {
  assert.doesNotMatch(source, /coalescePipelineJobs|project_pipeline/);
  assert.match(source, /id:`editor:\$\{task\.project_id\}:\$\{task\.task_id\}`/);
  assert.match(source, /ai_progress:task\.ai_progress \|\| null/);
  assert.match(source, /job\.ai_progress\?\.progress \?\? job\.progress/);
  assert.match(source, /SubstarAiProgressSummary\?\.format/);
  assert.doesNotMatch(styles, /\.queue-ai-counts/);
});
