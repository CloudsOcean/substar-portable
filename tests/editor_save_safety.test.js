const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {createOperationQueue} = require('../web/editor_operation_queue.js');
const {createDocumentStore} = require('../web/editor_document_store.js');
const source = fs.readFileSync(require.resolve('../web/editor.js'), 'utf8');
const extract = (a,b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));

test('projection failure does not leave an unqueued ghost edit',()=>{
  const store=createDocumentStore({applyLocalOperation:()=>{throw Error('invalid');}});
  store.reset({revision_id:'base'});
  assert.throws(()=>store.enqueue({operation_id:'bad'}));
  assert.equal(store.pending().length,0);
  assert.equal(store.projection().revision_id,'base');
});

test('topology waits for blur edit to save before acquiring lock', async () => {
  const order=[];
  let context;
  const queue = createOperationQueue({base:()=>({}),sendBatch:async b=>{order.push(...b.operations.map(o=>o.type));return {};}});
  context=vm.createContext({state:{},ordinaryError(){},suspendPlaybackFollow(){},editorAiTaskLocksEditor:()=>false,
    isTopologyOperation:o=>o.type==='split_cue',ensureOperationQueue:()=>queue,
    document:{activeElement:{closest:()=>true,matches:()=>true,blur:()=>context.sendOperation({type:'set_target'})}}});
  vm.runInContext(extract('  async function flushActiveEditorInput()', '  let idleRevisionCheck')+
    extract('  async function sendOperation(operation)', '  function manualProvenance'),context);
  await context.sendOperation({type:'split_cue'});
  assert.deepEqual(order,['set_target','split_cue']);
  assert.equal(context.state.topologyOperationPending,false);
});

test('dangling rejection keeps the entire batch in recovery journal',async()=>{
  const context=vm.createContext({error:{status:422},isDanglingEntityOperation:()=>true});
  const block=extract('          if (isDanglingEntityOperation(error)) {','          if (error.status !== 409)');
  let journal=[];
  const q=createOperationQueue({base:()=>({}),sendBatch:async()=>vm.runInContext(block,context),onJournal:ops=>journal=ops});
  const a=q.enqueue({operation_id:'valid'}).catch(()=>{}),b=q.enqueue({operation_id:'stale'}).catch(()=>{});
  await q.flushNow(); await Promise.all([a,b]);
  assert.equal(q.getState().failed,2);
  assert.deepEqual(journal.map(o=>o.operation_id),['valid','stale']);
  q.destroy();
});

test('restore stops at refreshed history when server version changed',async()=>{
  let posts=0,history=0;
  const panel={inert:false};
  const c=vm.createContext({state:{revision:{revision_id:'old'},undoRevisionIds:[],redoRevisionIds:[]},
    flushActiveEditorInput:async()=>{},api:async(path,options)=>{if(options)posts++;return {revision_id:'new'};},projectPath:()=>'',
    contract:{consumeRevision:x=>x},setRevision:r=>c.state.revision=r,loadRevisionHistory:async()=>history++,ordinaryError(){},$:()=>panel});
  vm.runInContext(extract('  async function restoreRevision(', '  async function createCheckpoint()'),c);
  await c.restoreRevision('historical');
  assert.equal(posts,0);assert.equal(history,1);assert.equal(panel.inert,false);
  assert.equal(c.state.restorePreparationPending,false);
});

test('failed input save prevents structural operation',async()=>{
  let enqueued=false;
  const c=vm.createContext({state:{},ordinaryError(){},suspendPlaybackFollow(){},editorAiTaskLocksEditor:()=>false,
    isTopologyOperation:()=>true,flushActiveEditorInput:async()=>{throw Error('conflict');},
    ensureOperationQueue:()=>({enqueue:()=>enqueued=true})});
  vm.runInContext(extract('  async function sendOperation(operation)', '  function manualProvenance'),c);
  await c.sendOperation({type:'split_cue'});
  assert.equal(enqueued,false);assert.equal(c.state.structuralPreparationPending,false);
});

test('idle synchronization preserves drafts and refreshes only a clean editor',async()=>{
  let editing=true,fetches=0,updates=0;
  const c=vm.createContext({state:{revision:{revision_id:'old'},projectId:'p'},
    document:{activeElement:{closest:()=>editing},addEventListener(){}},window:{addEventListener(){}},
    editorAiTaskLocksEditor:()=>false,api:async()=>{fetches++;return {revision_id:'new'};},projectPath:()=>'',
    contract:{consumeRevision:x=>x},setRevision:r=>{updates++;c.state.revision=r;},loadRevisionHistory:async()=>{},console});
  vm.runInContext(extract('  let idleRevisionCheck', '  async function restoreRevision('),c);
  await c.syncIdleRevision();assert.equal(fetches,0);
  editing=false;c.state.operationPending=true;
  await c.syncIdleRevision();assert.equal(fetches,0);
  c.state.operationPending=false;
  await c.syncIdleRevision();assert.equal(updates,1);assert.equal(c.state.revision.revision_id,'new');
});
