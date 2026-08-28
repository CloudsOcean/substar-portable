const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web", "editor.css"), "utf8");

assert.match(html, /id="aiCalibrationMenu"[\s\S]*?id="aiCalibrationInstruction"[\s\S]*?maxlength="4000"/);
assert.match(html, /id="runAiCalibration"[\s\S]*?>执行校准</);
assert.match(html, /id="exportDocument">导出</);
assert.match(js, /instruction\s*\n\s*}\)\s*\n\s*}/);
assert.match(js, /ai-calibration-instruction:\$\{state\.projectId/);
assert.match(js, /state\.editorAiTask\.progress[\s\S]*?renderWorkbenchTask/);
assert.match(js, /ordinaryError\(`已保存：\$\{result\.filename}`, "completed"\)/);
assert.match(css, /\.ordinary-error\.notice/);
assert.match(css, /\.translation-task-panel[\s\S]*?grid-template-areas:[^;]*"head close" "progress close" "steps close" "message close"/);
assert.match(html, /id="translationTaskSteps"/);
assert.match(js, /function renderAiProgress[\s\S]*?repair_completed[\s\S]*?repair_planned/);

console.log("editor_calibration_prompt: ok");
