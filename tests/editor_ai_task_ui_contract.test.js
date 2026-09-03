const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const editorHtml = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
const editorCss = fs.readFileSync(path.join(root, "web", "editor.css"), "utf8");
const editorJs = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");

test("cursor view preserves exact AI calibration highlighting", () => {
  assert.match(
    editorCss,
    /\.source-token-line\.virtual-line \.display-token\.tone-light-blue\s*\{[^}]*background:/s,
  );
  const rule = editorCss.match(
    /\.source-token-line\.virtual-line \.display-token\.tone-light-blue\s*\{([^}]*)\}/s,
  )?.[1] || "";
  assert.doesNotMatch(rule, /box-shadow|border-bottom|text-decoration/);
});

test("task island close control cancels the active editor AI task", () => {
  assert.match(editorHtml, /id="dismissTaskPanel"[^>]*title="取消当前任务"/);
  assert.match(editorJs, /async function cancelOrDismissTaskPanel\(\)/);
  assert.match(editorJs, /api\(projectPath\("\/ai-task"\), \{method:"DELETE"\}\)/);
  assert.match(editorJs, /#dismissTaskPanel"\)\.onclick = cancelOrDismissTaskPanel/);
});

test("exclusive task polling cannot overwrite translation's detailed task status", () => {
  assert.match(
    editorJs,
    /const genericTaskOwnsPanel = state\.editorAiTask\?\.kind !== "translation";/,
  );
  assert.match(
    editorJs,
    /state\.editorAiTask\?\.state === "succeeded_with_issues"/,
  );
  assert.match(editorJs, /state\.editorAiTask\.display_error/);
  assert.match(editorJs, /已等待 \$\{Math\.round\(elapsedSeconds\)\} 秒/);
});

test("an active calibration or translation task dims every editor-writing command menu", () => {
  assert.match(editorHtml, /id="scriptProjectionMenu" class="editor-command-menu"/);
  assert.match(editorCss, /\.editor-command-menu\.disabled\s*\{[^}]*opacity:\s*\.36;[^}]*pointer-events:\s*none;/s);
  for (const pair of [
    '["#translationMenu", "#translationMenuSummary"]',
    '["#aiCalibrationMenu", "#aiCalibrate"]',
    '["#scriptProjectionMenu", "#scriptProjectionSummary"]',
  ]) assert.ok(editorJs.includes(pair));
  assert.match(editorJs, /const editingCommandDisabled = !revision \|\| locked;/);
});

test("blank manual translation tracks still render editable rows", () => {
  assert.match(editorJs, /const hasTarget = Boolean\(cue\.target\);/);
  assert.doesNotMatch(
    editorJs,
    /const hasTarget = Boolean\(String\(cue\.target\?\.target_text \|\| ""\)\.trim\(\)\);/,
  );
});
