const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../web/editor.js"), "utf8");
const documentContract = require("../web/editor_document.js");

test("blank source replacement uses reversible delete operations", () => {
  assert.match(source, /manualProvenance\("search_delete"\)/);
  assert.match(source, /manualProvenance\("search_delete_all"\)/);
  assert.doesNotMatch(source, /请填写替换文字/);
});

test("single-token search keeps token-covering matches such as here within here.", () => {
  assert.match(source, /contract\.findContiguousTokenMatches\(tokens, query\)/);
  assert.match(source, /\[\\p\{L\}\\p\{N\}\]\/u\.test\(unmatchedEdges\)/);
  assert.deepEqual(
    documentContract.findContiguousTokenMatches(
      [{token_id:"token-here", text:"here."}], "here"
    ).map(match => match.token_ids),
    [["token-here"]]
  );
});

test("failed AI task card exposes model retry and settings actions", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/editor.html"), "utf8");
  assert.match(html, /id="failedTaskModel"/);
  assert.match(html, /id="retryFailedTask"/);
  assert.match(source, /window\.location\.href = "\/settings#api"/);
});

test("search tools expose a scoped undo immediately left of replace all", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/editor.html"), "utf8");
  const undo = html.indexOf('id="toolUndoReplace"');
  const replaceAll = html.indexOf('id="toolReplaceAll"');
  assert.ok(undo >= 0 && replaceAll > undo);
  assert.match(source, /state\.searchReplaceUndo\.after !== revision\?\.revision_id/);
  assert.match(source, /restoreRevision\(entry\.before\)/);
});
