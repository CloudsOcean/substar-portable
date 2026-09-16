const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/editor.js"), "utf8");
const lock = source.slice(source.indexOf("  function editorAiTaskLocksEditor()"), source.indexOf("  async function refreshEditorAiTask()"));
const send = source.slice(source.indexOf("  async function sendOperation(operation)"), source.indexOf("  function manualProvenance(operation)"));

function editor(taskState) {
  const saved = [];
  const errors = [];
  const context = vm.createContext({
    reviewEnabled:true,
    state:{editorAiTask:{kind:"translation", state:taskState}},
    ordinaryError:message => errors.push(message),
    suspendPlaybackFollow:() => {},
    isTopologyOperation:() => false,
    ensureOperationQueue:() => ({enqueue:async operation => {saved.push(operation); return {revision_id:"saved"};}})
  });
  vm.runInContext(lock + send, context);
  return {context, saved, errors};
}

test("annotation mode saves a word edit when the previous translation succeeded", async () => {
  const {context, saved, errors} = editor("succeeded");
  const operation = {type:"replace_token", token_id:"announced", text:"announce"};
  const revision = await context.sendOperation(operation);
  assert.equal(revision.revision_id, "saved");
  assert.deepEqual(saved, [operation]);
  assert.deepEqual(errors.filter(Boolean), []);
});

test("actual pending AI tasks still prevent writes", async () => {
  for (const status of ["queued", "running", "cancelling"]) {
    const {context, saved, errors} = editor(status);
    assert.equal(await context.sendOperation({text:"announce"}), null);
    assert.equal(saved.length, 0);
    assert.match(errors.at(-1), /翻译任务正在排队或执行中/);
  }
});
