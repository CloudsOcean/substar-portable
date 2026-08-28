const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web", "editor.css"), "utf8");

test("tutorial projects expose the green corner and a restart entry beside export", () => {
  assert.match(html, /id="startEditorTutorial"[\s\S]*id="exportMenu"/);
  assert.match(css, /\.project-list button\.tutorial::after[^{]*\{[^}]*#42d79b/s);
  assert.match(js, /button\.classList\.toggle\("tutorial", Boolean\(project\.tutorial_case_id\)\)/);
  assert.match(js, /loadProject\(state\.projectId, \{restoreTranslation:false, resetTutorial:true/);
});

test("first entry points both tutorial cases at the real editor tutorial command", () => {
  assert.match(html, /id="editorTutorialIntro"[\s\S]*id="dismissEditorTutorialIntro"/);
  assert.match(js, /substar\.editor\.tutorial-entry-hint:v1:\$\{caseId\}/);
  assert.match(js, /const buttonLabel = advanced \? "启动进阶教程" : "启动初级教程"/);
  assert.match(js, /点击右上方按钮，开始体验 AI 切分、校准、翻译和审阅/);
  assert.match(js, /点击右上方按钮，开始学习定位、切分、参考稿取舍、时间轴调整和导出/);
  assert.match(js, /#startEditorTutorial"\)\.classList\.add\("tutorial-entry-target"\)/);
  assert.match(js, /#startEditorTutorial"\)\.onclick = restartEditorTutorial/);
  assert.match(css, /#startEditorTutorial\.tutorial-entry-target/);
  assert.match(css, /\.editor-tutorial-intro \{[^}]*background: rgba\(3,5,10,\.76\)/s);
});

test("editor tutorial covers the agreed hands-on workflow", () => {
  for (const text of ["选择与播放", "定位问题字幕", "合并相邻 Cue", "在文字间切分", "判断参考稿差异", "编辑词元", "选择并合并词元", "撤销与重做", "调整时间边界", "放大时间轴", "创建新 Cue", "隐藏、恢复与删除", "查找字幕", "标点处理", "自动吸附", "导出字幕"]) {
    assert.match(js, new RegExp(text));
  }
  assert.match(html, /案例教程 · 1 \/ 19/);
  assert.match(js, /双击“20”进入编辑/);
  assert.match(js, /Ctrl＋左键逐个选择/);
  assert.match(js, /state\.shortcuts\.undo[\s\S]*state\.shortcuts\.redo/);
  assert.match(js, /#snapThreshold"\)\.value = "400"/);
  assert.match(js, /type === "timeline_zoom" && detail\.direction === "in"/);
  assert.match(js, /String\(payload\.text \|\| ""\)\.trim\(\)/);
  assert.match(html, /id="editorTutorialTimelineTarget"/);
  assert.match(html, /id="editorTutorialGhostMouse"/);
  assert.match(css, /\.editor-tutorial-card[^}]*width: min\(460px/);
  assert.match(css, /\.editor-tutorial-intro section \{[^}]*width: min\(360px[^}]*border-radius: 14px/s);
  assert.match(css, /\.editor-tutorial-intro button \{[^}]*border-radius: 8px[^}]*background: #1a1b25/s);
  assert.match(css, /\.editor-tutorial-card footer button \{[^}]*border-radius: 8px/s);
});

test("tutorial keeps the whole task area bright and advances from verified actions", () => {
  assert.match(html, /id="editorTutorialSpotlight" class="editor-tutorial-spotlight"/);
  assert.match(html, /id="editorTutorialFooter"[\s\S]*>继续<\/button>/);
  assert.doesNotMatch(html, /id="editorTutorialPrevious"/);
  assert.match(css, /\.editor-tutorial-shade \{[^}]*pointer-events: auto/s);
  assert.match(css, /\.editor-tutorial-spotlight \{[^}]*pointer-events: none/s);
  assert.match(css, /\.editor-tutorial-layer \{ pointer-events: none; \}/);
  assert.match(js, /observeEditorTutorialOperations\(batch\.operations\)/);
  assert.match(js, /state\.tutorial\.step !== 0/);
  assert.match(js, /filter\(cue => cue\.state === "active"\)/);
  assert.match(js, /new MutationObserver\(\(\) => \{[\s\S]*positionEditorTutorial\(\);/);
  assert.match(js, /tutorialResolver\.resolveBeginnerAnchors/);
  assert.match(js, /anchors\.mergeLeftCue[\s\S]*anchors\.mergedCue[\s\S]*anchors\.threatCue/);
  assert.match(js, /function centerEditorTutorialCue\(cueId\)/);
  assert.match(js, /scheduleEditorTutorialCueCenter\(cueId\)/);
  assert.match(js, /#tokenSelectionMenu:not\(\.hidden\)/);
  assert.match(js, /#exportMenu\[open\] \.export-popover/);
  assert.match(js, /#cueTaskIsland \.cue-history-row/);
  for (const event of ["hard_issue_next", "token_select", "revision_restore", "punctuation_apply", "auto_snap", "export"]) {
    assert.match(js, new RegExp(`observeEditorTutorialEvent\\("${event}"`));
  }
});

test("advanced tutorial demonstrates packaged AI stages and external review without model calls", () => {
  for (const text of ["AI 切分结果已载入", "执行 AI 校准", "查看校准痕迹", "执行 AI 翻译", "查看多对多译文", "打开外部 AI 审阅", "复制审阅内容"]) {
    assert.match(js, new RegExp(text));
  }
  assert.match(js, /isAdvancedTutorial\(\)/);
  assert.match(js, /tutorial\/stages\/\$\{stage\}/);
  assert.match(js, /正在读取内置阶段快照/);
  assert.match(js, /不会请求云端模型/);
  assert.match(js, /state\.tutorial\.step === 3\) \$\("#aiCalibrationMenu"\)\.open = false/);
});
