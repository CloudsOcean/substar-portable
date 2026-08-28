const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const editorJs = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");
const editorHtml = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
const editorCss = fs.readFileSync(path.join(root, "web", "editor.css"), "utf8");

test("reference legend distinguishes applied, inserted, suggested, and retained ASR", () => {
  for (const marker of [
    "tone-reference-applied",
    "tone-reference-inserted",
    "tone-reference-inserted-deleted",
    "tone-reference-suggested",
    "tone-reference-retained"
  ]) {
    assert.match(editorHtml, new RegExp(marker));
  }
  for (const marker of [
    "reference-applied",
    "reference-inserted",
    "reference-inserted-deleted",
    "reference-suggested",
    "reference-asr-retained"
  ]) {
    assert.match(editorCss, new RegExp(`\\.${marker}\\b`));
    assert.match(editorJs, new RegExp(marker));
  }
});

test("all cue boundary controls use the shared right-click contract", () => {
  assert.match(editorJs, /dataset\.boundaryControl\s*=\s*""/);
  assert.match(editorJs, /dataset\.boundaryAction\s*=\s*"split"/);
  assert.match(editorJs, /dataset\.boundaryAction\s*=\s*"merge-next"/);
  assert.match(editorJs, /addEventListener\("contextmenu"/);
  assert.match(editorJs, /closest\("\[data-boundary-control\]"\)/);
  assert.match(editorJs, /document\.createElement\(cue\.state === "active" \? "button" : "span"\)/);
  assert.match(editorJs, /setAttribute\("aria-label", "右键在此处切分 Cue"\)/);
  assert.match(editorCss, /\.virtual-boundary \{[^}]*width: \.48em;[^}]*margin: 0 -\.24em;/);
  assert.doesNotMatch(editorJs, /scheduleConnectorAction|connectorClickTimer/);
  assert.doesNotMatch(editorHtml, /右键间隙切分 · 右键紫点合并/);
  assert.match(editorHtml, />视图\s+<select id="cueSplitView"/);
  assert.doesNotMatch(editorHtml, /<summary>行长<\/summary>/);
  assert.match(editorHtml, /id="taskInfoMenu"[^>]*class="[^"]*editor-command-menu[^\"]*task-info-menu/);
  assert.match(editorHtml, /<summary id="taskInfo"[^>]*>任务信息<\/summary>/);
  assert.match(editorHtml, /id="taskInfoForm"[^>]*class="[^"]*editor-command-popover[^\"]*task-info-popover/);
  assert.doesNotMatch(editorHtml, /id="taskInfoDialog"|task-info-dialog/);
  assert.match(editorHtml, /id="taskInfoSourceLimit"/);
  assert.match(editorHtml, /id="taskInfoTargetLimit"/);
  assert.match(editorJs, /projectPath\("\/task-info"\)/);
  assert.match(editorHtml, /<option value="virtual" selected>光标<\/option>/);
  assert.match(editorHtml, /<option value="auxiliary">辅助点<\/option>/);
});

test("editor chrome exposes the bounded project list and concise usage guide", () => {
  assert.match(editorHtml, /id="projectList"[^>]*role="listbox"/);
  assert.match(editorCss, /\.project-list \{[^}]*max-height: 390px;[^}]*overflow-y: auto;/s);
  assert.match(editorHtml, /左键词元：选择并编辑/);
  assert.match(editorHtml, /右键字词间隙或辅助点：切分/);
  assert.doesNotMatch(editorHtml, /id="documentSummary"/);
  assert.doesNotMatch(editorHtml, /id="refreshDocument"/);
  assert.match(editorHtml, /撤销<\/button><button[^>]+>恢复<\/button><button[^>]+>暂存<\/button><button[^>]+>重置<\/button><button[^>]+>刷新<\/button>/);
});

test("regional Chinese conversion choices remain explicit", () => {
  assert.match(editorHtml, /data-script-target="traditional_tw"/);
  assert.match(editorHtml, /繁体中文（台湾）/);
  assert.match(editorHtml, /data-script-target="traditional_hk"/);
  assert.match(editorHtml, /繁体中文（香港）/);
});

test("AI and manuscript commands follow the requested editor order", () => {
  assert.match(
    editorHtml,
    /AI 翻译[\s\S]*AI 校准[\s\S]*AI 审阅[\s\S]*参考文稿[\s\S]*繁简转换[\s\S]*说话人设置/,
  );
  assert.match(editorJs, /translationMenuSummary"\)\.textContent = running \? "AI 翻译中…" : "AI 翻译"/);
});

test("external AI review is read-only and the retired generation exchange is absent", () => {
  assert.match(editorHtml, /id="aiReviewMenu"/);
  assert.match(editorHtml, /id="copyExternalReview"/);
  assert.match(editorHtml, /id="downloadExternalReview"/);
  assert.match(editorJs, /externalReview\.build/);
  assert.doesNotMatch(editorHtml, /data-exchange-export="external-ai-generation"/);
  assert.doesNotMatch(editorJs, /external-ai-generation-checkpoint|\/external-ai-generation/);
});
