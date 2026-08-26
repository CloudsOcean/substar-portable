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
