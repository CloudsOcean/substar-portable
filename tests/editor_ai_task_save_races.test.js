"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync(require.resolve("../web/editor.js"), "utf8");
const body = source.slice(source.indexOf("  async function refreshEditorAiTask()"), source.indexOf("  function startEditorAiTaskPoll()"));

function fixture(api) {
  const applied = [], history = [], errors = [];
  const context = vm.createContext({
    projectEpoch:1, state:{projectId:"a",editorAiTask:{task_id:"t",kind:"organize",state:"running"}},
    api, projectPath:()=>`/api/projects/${context.state.projectId}`,
    contract:{consumeRevision:r=>r},
    editorAiTaskLocksEditor:()=>["queued","running","cancelling"].includes(context.state.editorAiTask?.state),
    hasUncommittedEditorInput:()=>!!context.dirty,
    document:{body:{classList:{toggle(){}}}},renderHeader(){},renderWorkbenchTask(){},
    setRevision:r=>applied.push({revision:r,locked:context.editorAiTaskLocksEditor()}),
    recordCommittedRevision:r=>history.push(r), ordinaryError:message=>errors.push(message)
  });
  vm.runInContext(body,context);
  return {context,applied,history,errors};
}
const completed = {task_id:"t",kind:"organize",state:"succeeded"};

test("a failed task-status request never releases the running editor lock",async()=>{
  const {context,applied}=fixture(async()=>{throw Error("offline");});
  await assert.rejects(context.refreshEditorAiTask(),/offline/);
  assert.equal(context.editorAiTaskLocksEditor(),true);
  assert.equal(applied.length,0);
  assert.equal(context.state.editorAiTaskRefresh,null);
});

test("slow task polling has only one request in flight",async()=>{
  let release,calls=0;
  const {context}=fixture(()=>{calls++;return new Promise(r=>release=r);});
  const first=context.refreshEditorAiTask(),second=context.refreshEditorAiTask();
  assert.equal(calls,1);
  release({task_id:"t",kind:"organize",state:"running"});
  await Promise.all([first,second]);
  assert.equal(context.state.editorAiTaskRefresh,null);
});

test("a completed AI version is adopted and added to undo history before unlocking",async()=>{
  const {context,applied,history}=fixture(async path=>path.endsWith("/ai-task")?completed:{revision_id:"ai-result"});
  await context.refreshEditorAiTask();
  assert.equal(applied.length,1);
  assert.equal(applied[0].locked,true);
  assert.equal(history[0].revision_id,"ai-result");
  assert.equal(context.state.revisionHistoryLoaded,false);
  assert.equal(context.editorAiTaskLocksEditor(),false);
});

test("AI completion does not overwrite text typed before snapshot delivery",async()=>{
  let deliver,ready; const started=new Promise(r=>ready=r);
  const {context,applied,history,errors}=fixture(async path=>path.endsWith("/ai-task")?completed:new Promise(r=>{deliver=r;ready();}));
  const polling=context.refreshEditorAiTask();
  await started;
  context.dirty=true;
  deliver({revision_id:"ai-result"});
  await polling;
  assert.equal(applied.length,0);assert.equal(history.length,0);
  assert.match(errors[0],/输入已保留/);
});

test("a late completed snapshot from another project is discarded",async()=>{
  let deliver,ready; const started=new Promise(r=>ready=r);
  const {context,applied}=fixture(async path=>path.endsWith("/ai-task")?completed:new Promise(r=>{deliver=r;ready();}));
  const polling=context.refreshEditorAiTask();
  await started;
  context.projectEpoch=2;context.state.projectId="b";
  context.state.editorAiTask={task_id:"b-task",state:"running"};
  deliver({revision_id:"a-result"});await polling;
  assert.equal(applied.length,0);
  assert.equal(context.state.editorAiTask.task_id,"b-task");
});

test("snapshot read failure keeps the previous task locked for a later retry",async()=>{
  let offline=true;
  const {context,applied}=fixture(async path=>{
    if(path.endsWith("/ai-task"))return completed;
    if(offline)throw Error("snapshot timeout");
    return {revision_id:"ai-result"};
  });
  await assert.rejects(context.refreshEditorAiTask(),/snapshot timeout/);
  assert.equal(context.editorAiTaskLocksEditor(),true);
  offline=false;await context.refreshEditorAiTask();
  assert.equal(applied.length,1);assert.equal(context.editorAiTaskLocksEditor(),false);
});

test("queued and failed local saves are protected when an AI task completes",async()=>{
  const {context,applied}=fixture(async path=>path.endsWith("/ai-task")?completed:{revision_id:"ai-result"});
  context.state.operationQueue={getState:()=>({hasUnsavedChanges:true})};
  await context.refreshEditorAiTask();
  assert.equal(applied.length,0);
});

test("a late poll cannot replace a newly started task with the previous result",async()=>{
  let release;
  const {context,applied}=fixture(()=>new Promise(r=>release=r));
  const polling=context.refreshEditorAiTask();
  context.state.editorAiTask={task_id:"new-task",kind:"punctuation",state:"queued"};
  release(completed);await polling;
  assert.equal(context.state.editorAiTask.task_id,"new-task");
  assert.equal(context.editorAiTaskLocksEditor(),true);
  assert.equal(applied.length,0);
});
