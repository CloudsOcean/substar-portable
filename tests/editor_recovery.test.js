"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {archive, list, preview, createController, setWorkbenchInert, isWorkbenchInert} = require("../web/editor_recovery.js");
const {createOperationQueue} = require("../web/editor_operation_queue.js");
const fs = require("node:fs");
const vm = require("node:vm");
const editor = fs.readFileSync(require.resolve("../web/editor.js"), "utf8");
const extract = (start,end) => editor.slice(editor.indexOf(start),editor.indexOf(end,editor.indexOf(start)));

test("workbench locks leave the bottom refresh accessible and restore existing inert states",()=>{
  const node=(children=[],inert=false)=>({children,inert,contains(target){return this===target||children.some(child=>child.contains(target));}});
  const refresh=node(), undo=node(), permanentlyLocked=node([],true), editorPane=node();
  const footer=node([undo,refresh]), island=node([permanentlyLocked,footer]), root=node([editorPane,island]);
  setWorkbenchInert(root,refresh,true);
  assert.equal(isWorkbenchInert(root),true);
  for(const ancestor of [root,island,footer,refresh])assert.equal(ancestor.inert,false);
  assert.equal(undo.inert,true);assert.equal(editorPane.inert,true);
  setWorkbenchInert(root,refresh,true);
  setWorkbenchInert(root,refresh,false);
  assert.equal(isWorkbenchInert(root),false);assert.equal(undo.inert,false);assert.equal(editorPane.inert,false);
  assert.equal(permanentlyLocked.inert,true);
});

function memoryStorage() {
  const data = new Map();
  return {get length(){return data.size;}, key:i=>[...data.keys()][i],
    getItem:key=>data.get(key) ?? null, setItem:(key,value)=>data.set(key,value), removeItem:key=>data.delete(key)};
}
const journal = "substar.editor.operations:p:doc";
function fixture(overrides = {}) {
  const storage = memoryStorage(), errors = [], events = [];
  const options = {storage, projectId:()=>"p", capture:()=>({operations:[],inputs:[]}),
    prepare:async()=>{}, verify:async()=>{}, setBusy:value=>events.push(["busy",value]),
    freeze:()=>events.push(["freeze"]), reload:()=>events.push(["reload"]), onError:e=>errors.push(e), ...overrides};
  return {options, storage, errors, events, controller:createController(options)};
}

test("conflicting journal is isolated, and a fresh queue can save new edits",async()=>{
  const storage = memoryStorage();
  const old = createOperationQueue({base:()=>({}), maxRetries:0,
    onJournal:ops=>storage.setItem(journal,JSON.stringify(ops)),
    sendBatch:async()=>{throw Object.assign(Error("conflict"),{status:409});}});
  const failed = assert.rejects(old.enqueue({operation_id:"old"}),/conflict/);
  await assert.rejects(old.flushAndWait()); await failed;
  const f=fixture({storage, capture:()=>({operations:[{operation_id:"old"}],inputs:[]}),
    prepare:()=>old.flushAndWait(),freeze:()=>old.destroy()});
  assert.equal(await f.controller.refresh(),true);
  assert.equal(storage.getItem(journal),null);
  assert.match(list(storage,"p")[0].text,/old/);
  let committed=[];
  const fresh=createOperationQueue({base:()=>({}),sendBatch:async batch=>{committed.push(...batch.operations);return {};}});
  const saved=fresh.enqueue({operation_id:"new"});await fresh.flushAndWait();await saved;
  assert.deepEqual(committed.map(op=>op.operation_id),["new"]);
  assert.equal(fresh.getState().hasUnsavedChanges,false);fresh.destroy();
});

test("a permanently pending save cannot trap recovery, and pre-blur input survives",async()=>{
  let first=true;
  const f=fixture({saveWaitMs:5,prepare:()=>new Promise(()=>{}),
    capture:()=>first?(first=false,{inputs:[{value:"未保存文字"}],operations:[]}):{inputs:[],operations:[]}});
  assert.equal(await f.controller.refresh(),true);
  assert.match(list(f.storage,"p")[0].text,/未保存文字/);
  assert.deepEqual(f.events,[['busy',true],['freeze'],['reload']]);
});

test("recovery pauses queued saves and never submits failed or pending edits",async()=>{
  const storage=memoryStorage();let sends=0;
  const queue=createOperationQueue({base:()=>({}),debounceMs:60000,
    onJournal:ops=>storage.setItem(journal,JSON.stringify(ops)),
    sendBatch:async()=>{sends++;return {};}});
  queue.enqueue({operation_id:'uncommitted'});
  const f=fixture({storage,pause:()=>queue.pause(),prepare:()=>queue.waitForInFlight(),
    freeze:()=>queue.destroy()});
  assert.equal(await f.controller.refresh(),true);
  assert.equal(sends,0);assert.equal(storage.getItem(journal),null);
  assert.match(list(storage,'p')[0].text,/uncommitted/);
});

test("failed recovery resumes the original save queue",async()=>{
  const events=[];
  const f=fixture({pause:()=>events.push('pause'),resume:()=>events.push('resume'),
    verify:async()=>{throw Error('offline');}});
  assert.equal(await f.controller.refresh(),false);
  assert.deepEqual(events,['pause','resume']);
});

test("recovery waits for an active save but archives the next edit without sending it",async()=>{
  const storage=memoryStorage();let release,sends=0;
  const queue=createOperationQueue({base:()=>({}),debounceMs:60000,maxBatchSize:1,
    onJournal:ops=>storage.setItem(journal,JSON.stringify(ops)),
    sendBatch:()=>{sends++;return new Promise(r=>release=r);}});
  const first=queue.enqueue({operation_id:'committing'});const sending=queue.flushNow();
  queue.enqueue({operation_id:'waiting'});
  const f=fixture({storage,pause:()=>queue.pause(),prepare:()=>queue.waitForInFlight(),
    freeze:()=>queue.destroy()});
  const recovery=f.controller.refresh();
  release({});await first;await sending;
  assert.equal(await recovery,true);assert.equal(sends,1);
  assert.match(list(storage,'p')[0].text,/waiting/);
  assert.equal(storage.getItem(journal),null);
});

test("storage quota failure leaves the queue journal intact and does not reload",async()=>{
  const f=fixture();f.storage.setItem(journal,'[{"operation_id":"keep"}]');
  f.storage.setItem=()=>{throw Error("quota");};
  assert.equal(await f.controller.refresh(),false);
  assert.match(f.storage.getItem(journal),/keep/);
  assert.equal(f.events.some(e=>e[0]==="freeze"||e[0]==="reload"),false);
  assert.deepEqual(f.events.at(-1),['busy',false]);
});

test("silent storage failure is detected before detaching operations",()=>{
  const storage=memoryStorage();storage.setItem(journal,'[1]');storage.setItem=()=>{};
  assert.throws(()=>archive(storage,"p",[]),/校验失败/);
  assert.equal(storage.getItem(journal),'[1]');
});

test("offline or stalled status checks preserve inputs and allow another refresh",async()=>{
  for(const verify of [async()=>{throw Error("offline");},()=>new Promise(()=>{})]) {
    const f=fixture({verify,verifyWaitMs:5});f.storage.setItem(journal,'[1]');
    assert.equal(await f.controller.refresh(),false);
    assert.equal(f.storage.getItem(journal),'[1]');
    assert.equal(f.events.some(e=>e[0]==="reload"),false);
    f.options.verify=async()=>{};
    assert.equal(await f.controller.refresh(),true);
  }
});

test("repeated clicks share one recovery attempt",async()=>{
  let release;
  const f=fixture({verify:()=>new Promise(r=>release=r)});
  const first=f.controller.refresh();
  assert.equal(await f.controller.refresh(),false);
  while(!release)await new Promise(r=>setImmediate(r));
  release();assert.equal(await first,true);
  assert.equal(f.events.filter(e=>e[0]==="reload").length,1);
});

test("project changes during recovery cannot detach another project's journal",async()=>{
  let project="p";
  const f=fixture({projectId:()=>project,verify:async()=>{project="q";}});
  f.storage.setItem(journal,'[1]');
  assert.equal(await f.controller.refresh(),false);
  assert.equal(f.storage.getItem(journal),'[1]');
});

test("raw damaged journals and prior drafts survive repeated recovery",()=>{
  const storage=memoryStorage();storage.setItem(journal,'{bad-json');
  storage.setItem('substar.editor.operations:q:doc','[2]');
  archive(storage,"p",[]);
  archive(storage,"p",[{inputs:[{value:"第二次输入"}]}]);
  assert.equal(list(storage,"p").length,2);
  assert.ok(list(storage,"p").some(copy=>copy.text.includes('{bad-json')));
  assert.equal(storage.getItem('substar.editor.operations:q:doc'),'[2]');
});

test("clean recovery does not create an empty draft",()=>{
  const storage=memoryStorage();storage.setItem(journal,'[]');
  archive(storage,"p",[{operations:[],inputs:[]}]);
  assert.equal(list(storage,"p").length,0);assert.equal(storage.getItem(journal),null);
});

test("draft preview exposes unsubmitted text and subtitle text for copying",()=>{
  const text=preview(JSON.stringify({snapshots:[{inputs:[{cue_id:"c",value:"尚未提交",label:"原文"}],
    revision:{document:{display_tokens:[{token_id:"t",text:"原文"}],cues:[{
      cue_id:"c",start:0,end:1,display_token_ids:["t"],target:{target_text:"译文"}}]}}}]}));
  assert.match(text,/尚未提交/);assert.match(text,/原文/);assert.match(text,/译文/);
});

test("task unlock reenables subtitle fields and history controls without replacing draft text",()=>{
  let locked=true, historyUpdates=0;
  const fields=[{dataset:{sourceEdit:'c'},value:'draft',disabled:true},
    {dataset:{targetEdit:'deleted'},value:'retained',disabled:true}];
  const buttons=['c','text','overlay'].map(id=>({disabled:true,closest:()=>({dataset:{cueId:id}})}));
  const node=()=>({classList:{toggle(){}},dataset:{},setAttribute(){},disabled:false});
  const state={view:{properties:{},cue_views:[]},revision:{document:{properties:{}}},projects:[],revisions:[],
    indexes:{cueById:new Map([['c',{state:'active'}],['deleted',{state:'deleted'}],
      ['text',{source_text:'text'}],['overlay',{source_text:'overlay',subtitle_track:'overlay'}]])}};
  const context=vm.createContext({state,document:{querySelectorAll:selector=>selector.includes('data-cue-action')?buttons:fields},$:()=>node(),
    editorAiTaskLocksEditor:()=>locked,currentProjectBaseName:()=>'',syncCueViewSwitch(){},
    syncRevisionControls:()=>historyUpdates++});
  vm.runInContext(extract('  function renderHeader()', '  function isReferenceToken'),context);
  context.renderHeader();assert.equal(fields[0].disabled,true);
  assert.ok(buttons.every(button=>button.disabled));
  locked=false;context.renderHeader();
  assert.equal(fields[0].disabled,false);assert.equal(fields[0].value,'draft');
  assert.equal(fields[1].disabled,true);assert.equal(historyUpdates,2);
  assert.deepEqual(buttons.map(button=>button.disabled),[false,true,false]);
});

test("a truncated successful API response cannot be treated as task completion or saved data",async()=>{
  const context=vm.createContext({projectEpoch:1,fetch:async()=>({ok:true,
    headers:{get:()=>"application/json"},json:async()=>{throw Error('truncated');}})});
  vm.runInContext(extract('  async function api(', '  function shortcutFromEvent'),context);
  await assert.rejects(context.api('/api/projects/p/ai-task'),error=>error.code==='invalid_response' && error.status===502);
});

test("capturing a recovery snapshot preserves empty deletions, source and translation drafts",()=>{
  const fields=[{value:'',dataset:{sourceEdit:'source',originalSource:'old'}},
    {value:'new translation',dataset:{targetEdit:'target',rawEditing:'true',originalTarget:'old'}},
    {value:'projected',dataset:{targetEdit:'clean',rawEditing:'false',originalTarget:'raw'}}];
  for(const field of fields)field.getAttribute=()=>null;
  const context=vm.createContext({state:{revision:null},document:{querySelectorAll:()=>fields}});
  vm.runInContext(extract('  function captureRecoverySnapshot()', '  function syncRecoveryDraftButton'),context);
  const snapshot=context.captureRecoverySnapshot();
  assert.equal(snapshot.inputs.length,2);assert.equal(snapshot.inputs[0].value,'');
  assert.equal(snapshot.inputs[1].value,'new translation');
});
