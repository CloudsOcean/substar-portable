const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web", "editor.css"), "utf8");

assert.match(html, /id="aiCalibrationMenu"[\s\S]*?AI标点[\s\S]*?id="aiCalibrationInstruction" hidden/);
assert.match(html, /id="runAiCalibration"[\s\S]*?>执行标点</);
assert.match(html, /id="organizeOptimizeBoundaries" type="checkbox">优化请求边界/);
assert.doesNotMatch(html, /id="organizeOptimizeBoundaries"[^>]*checked/);
assert.match(js, /runSubtitleTextTask\("punctuation"\)/);
assert.match(js, /runSubtitleTextTask\("organize"\)/);
assert.match(html, /id="exportDocument">导出</);
assert.match(js, /instruction\s*\n\s*}\)\s*\n\s*}/);
assert.match(js, /ai-calibration-instruction:\$\{state\.projectId/);
assert.match(js, /state\.editorAiTask\.progress[\s\S]*?renderWorkbenchTask/);
assert.match(js, /ordinaryError\(`已保存：\$\{result\.filename}`, "completed"\)/);
assert.match(css, /\.ordinary-error\.notice/);
assert.match(css, /\.translation-task-panel[\s\S]*?grid-template-areas:[^;]*"head close" "progress close" "steps close" "message close"/);
assert.match(html, /id="translationTaskSteps"/);
assert.match(html, /ai_progress_summary\.js/);
assert.match(js, /function renderAiProgress[\s\S]*?SubstarAiProgressSummary\?\.summarize/);

console.log("editor_calibration_prompt: ok");
