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
  context=vm.createContext({state:{},window:{addEventListener(){}},ordinaryError(){},suspendPlaybackFollow(){},editorAiTaskLocksEditor:()=>false,
    isTopologyOperation:o=>o.type==='split_cue',ensureOperationQueue:()=>queue,
    document:{activeElement:{closest:()=>true,matches:()=>true,blur:()=>context.sendOperation({type:'set_target'})}}});
  vm.runInContext(extract('  async function flushActiveEditorInput()', '  let idleRevisionCheck')+
    extract('  async function sendOperation(operation)', '  function manualProvenance'),context);
  await context.sendOperation({type:'split_cue'});
  assert.deepEqual(order,['set_target','split_cue']);
  assert.equal(context.state.topologyOperationPending,false);
});

test('flushing whole-line blur waits for its structural save acknowledgement',async()=>{
  let release,done=false,c;
  const queue=createOperationQueue({base:()=>({}),sendBatch:()=>new Promise(r=>release=r)});
  const document={activeElement:{closest:()=>true,matches:()=>true,blur(){
    document.activeElement=null;this.editorCommit=c.sendOperation({type:'retime_text'});
  }}};
  c=vm.createContext({state:{},document,window:{addEventListener(){}},ordinaryError(){},suspendPlaybackFollow(){},
    editorAiTaskLocksEditor:()=>false,isTopologyOperation:()=>true,ensureOperationQueue:()=>queue});
  vm.runInContext(extract('  async function flushActiveEditorInput()', '  let idleRevisionCheck')+
    extract('  async function sendOperation(operation)', '  function manualProvenance'),c);
  const flushing=c.flushActiveEditorInput().then(()=>done=true);
  for(let i=0;i<15;i++)await Promise.resolve();
  assert.equal(done,false);assert.equal(typeof release,'function');
  release({});await flushing;queue.destroy();
});

test('a structural command saves the open whole-line edit before changing topology',async()=>{
  const order=[];let c;
  const queue=createOperationQueue({base:()=>({}),sendBatch:async batch=>{
    order.push(...batch.operations.map(op=>op.type));return {};
  }});
  const document={activeElement:{closest:()=>true,matches:()=>true,blur(){
    document.activeElement=null;this.editorCommit=c.sendOperation({type:'retime_text'});
  }}};
  c=vm.createContext({state:{},document,window:{addEventListener(){}},ordinaryError(){},suspendPlaybackFollow(){},
    editorAiTaskLocksEditor:()=>false,isTopologyOperation:()=>true,ensureOperationQueue:()=>queue});
  vm.runInContext(extract('  async function flushActiveEditorInput()', '  let idleRevisionCheck')+
    extract('  async function sendOperation(operation)', '  function manualProvenance'),c);
  await c.sendOperation({type:'split_cue'});
  assert.deepEqual(order,['retime_text','split_cue']);queue.destroy();
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
    contract:{consumeRevision:x=>x},setRevision:r=>c.state.revision=r,loadRevisionHistory:async options=>{
      assert.equal(options.force,true);history++;
    },ordinaryError(){},$:()=>panel});
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
  assert.equal(enqueued,false);assert.ok(!c.state.structuralPreparationPending);
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

test('undo drains pending input before selecting target and serializes rapid clicks',async()=>{
  let release, restores=0;
  const state={revision:{revision_id:'r1'},undoRevisionIds:['r0'],redoRevisionIds:[]};
  const c=vm.createContext({state,editorAiTaskLocksEditor:()=>false,syncRevisionControls(){},ordinaryError(){},
    loadRevisionHistory:async()=>{},
    flushActiveEditorInput:()=>new Promise(resolve=>{release=()=>{
      state.revision={revision_id:'r2'};state.undoRevisionIds.push('r1');resolve();
    };}),
    restoreRevision:async(target,options)=>{restores++;assert.equal(target,'r1');
      assert.deepEqual(Array.from(options.redoRevisionIds),['r2']);}});
  vm.runInContext(extract('  async function undoLatestRevision()', '  async function resetToCheckpoint()'),c);
  const first=c.undoLatestRevision();
  await c.undoLatestRevision();
  assert.equal(state.historyNavigationPending,true);
  release();await first;
  assert.equal(restores,1);assert.equal(state.historyNavigationPending,false);
});

test('redo cannot resurrect an abandoned branch after flushing a new edit',async()=>{
  let restores=0;
  const state={revision:{revision_id:'r1'},redoRevisionIds:['r2']};
  const c=vm.createContext({state,editorAiTaskLocksEditor:()=>false,syncRevisionControls(){},ordinaryError(){},
    flushActiveEditorInput:async()=>{state.redoRevisionIds=[];},loadRevisionHistory:async()=>true,restoreRevision:async()=>restores++});
  vm.runInContext(extract('  async function redoLatestRevision()', '  async function resetToCheckpoint()'),c);
  await c.redoLatestRevision();assert.equal(restores,0);assert.equal(state.historyNavigationPending,false);
});

test('checkpoint serializes requests, locks the editor and recovers after a failed flush',async()=>{
  for (const fail of [false,true]) {
    let release,posts=0;
    const panel={inert:false},errors=[];
    const state={revision:{revision_id:'r1'},revisions:[]};
    const c=vm.createContext({state,editorAiTaskLocksEditor:()=>false,
      $:selector=>selector==='#editorWorkbench'?panel:selector==='#checkpointName'?{value:'checkpoint'}:{},
      flushActiveEditorInput:()=>new Promise((resolve,reject)=>{release=()=>fail?reject(Error('offline')):resolve();}),
      loadRevisionHistory:async()=>{},projectPath:()=>'',api:async()=>{posts++;assert.equal(panel.inert,true);return {};},
      setRevision(){},recordCommittedRevision(){},currentProjectBaseName:()=>'',setCheckpointNaming(){},ordinaryError:e=>errors.push(e)});
    vm.runInContext(extract('  async function createCheckpoint()', '  async function undoLatestRevision()'),c);
    const first=c.createCheckpoint();await c.createCheckpoint();
    assert.equal(panel.inert,true);release();await first;
    assert.equal(posts,fail?0:1);assert.equal(panel.inert,false);assert.equal(state.checkpointPending,false);
    assert.equal(errors.length,fail?1:0);
  }
});

test('refresh protection detects unsent input and failed saves without flagging clean fields',()=>{
  let handler,fields=[];
  const state={operationQueue:{getState:()=>({hasUnsavedChanges:false})}};
  const c=vm.createContext({state,document:{querySelectorAll:()=>fields},window:{addEventListener:(_,fn)=>handler=fn}});
  vm.runInContext(extract('  function hasUncommittedEditorInput()', '  let exitingEditor'),c);
  const check=()=>{let blocked=false;handler({preventDefault:()=>blocked=true});return blocked;};
  assert.equal(check(),false);
  for(const field of [
    {value:'new',dataset:{sourceEdit:'cue',originalSource:'old'}},
    {value:'new',dataset:{targetEdit:'cue',rawEditing:'true',originalTarget:'old'}},
    {value:'new',dataset:{originalEdit:'old'}}
  ]) {fields=[field];assert.equal(check(),true);}
  fields=[{value:'display',dataset:{targetEdit:'cue',rawEditing:'false',originalTarget:'raw'}}];
  assert.equal(check(),false);
  state.operationQueue.getState=()=>({hasUnsavedChanges:true});assert.equal(check(),true);
});

test('project switch saves the focused input before replacing the document',async()=>{
  const c=vm.createContext({state:{revision:{revision_id:'old'}},
    flushActiveEditorInput:async()=>{throw Error('not saved');},ordinaryError(){},renderProjectList(){}});
  vm.runInContext(extract('  let projectEpoch = 0;', '  async function loadProjects()'),c);
  await c.loadProject('other');
  assert.equal(c.state.revision.revision_id,'old');
});

test('checkpoints preserve logical undo and redo after history is reloaded',()=>{
  const metadata=new Map([
    ['r0',{revision_id:'r0'}],
    ['r1',{revision_id:'r1',parent_revision_id:'r0',provenance:{operation:'operation_batch'}}],
    ['c1',{revision_id:'c1',parent_revision_id:'r1',provenance:{operation:'checkpoint'}}],
    ['undo',{revision_id:'undo',parent_revision_id:'c1',provenance:{operation:'restore_revision',
      metadata:{undo_revision_ids:[],redo_revision_ids:['c1']}}}],
    ['c2',{revision_id:'c2',parent_revision_id:'undo',provenance:{operation:'checkpoint'}}]
  ]);
  const c=vm.createContext({});
  vm.runInContext(extract('  function logicalUndoStack(', '  function seedRevisionMetadata('),c);
  assert.deepEqual(Array.from(c.logicalUndoStack('c1',metadata)),['r0']);
  assert.deepEqual(Array.from(c.logicalUndoStack('c2',metadata)),[]);
  assert.deepEqual(Array.from(c.logicalRedoStack('c2',metadata)),['c1']);
});

test('committing a checkpoint does not add an empty undo or discard redo',()=>{
  const state={revisions:[{revision_id:'r1',is_latest:true}],undoRevisionIds:['r0'],redoRevisionIds:['r2']};
  const c=vm.createContext({state,revisionMetadata:r=>r,renderRevisionMenu(){},syncRevisionControls(){}});
  vm.runInContext(extract('  function recordCommittedRevision(', '  async function loadRevisionHistory('),c);
  c.recordCommittedRevision({revision_id:'checkpoint',parent_revision_id:'r1',provenance:{operation:'checkpoint'}});
  assert.deepEqual(state.undoRevisionIds,['r0']);assert.deepEqual(state.redoRevisionIds,['r2']);
});

test('history fetched before an edit cannot replace newly committed history',async()=>{
  let resolve;
  const state={projectId:'p',revision:{revision_id:'r1'},revisions:[{revision_id:'r2'}],undoRevisionIds:['r1']};
  const c=vm.createContext({state,projectPath:()=>'',api:()=>new Promise(r=>resolve=r)});
  vm.runInContext(extract('  async function loadRevisionHistory(', '  async function flushActiveEditorInput()'),c);
  const loading=c.loadRevisionHistory();state.revision={revision_id:'r2'};
  resolve({revisions:[{revision_id:'r1'}]});await loading;
  assert.equal(state.revisions[0].revision_id,'r2');assert.equal(state.revisionHistoryLoaded,false);
  assert.equal(state.revisionHistoryLoading,false);
});

test('concurrent history readers wait for one response instead of using seed history',async()=>{
  let resolve,calls=0;
  const state={projectId:'p',revision:{revision_id:'r2'},revisions:[],undoRevisionIds:[],redoRevisionIds:[]};
  const c=vm.createContext({state,projectPath:()=>'',api:()=>{calls++;return new Promise(r=>resolve=r);},
    logicalUndoStack:()=>['r0','r1'],logicalRedoStack:()=>[],renderRevisionMenu(){},syncRevisionControls(){},ordinaryError(){}});
  vm.runInContext(extract('  async function loadRevisionHistory(', '  async function flushActiveEditorInput()'),c);
  const first=c.loadRevisionHistory();let done=false;
  const second=c.loadRevisionHistory().then(result=>{done=true;return result;});
  await Promise.resolve();assert.equal(done,false);assert.equal(calls,1);
  resolve({revisions:[{revision_id:'r2',parent_revision_id:'r1'}]});
  assert.equal(await first,true);assert.equal(await second,true);assert.deepEqual(state.undoRevisionIds,['r0','r1']);
});

test('long histories avoid recursion overflow and fit the server navigation limit',()=>{
  const metadata=new Map();
  for(let i=0;i<15000;i++)metadata.set(String(i),{revision_id:String(i),parent_revision_id:i?String(i-1):null});
  const c=vm.createContext({});
  vm.runInContext(extract('  function logicalUndoStack(', '  function seedRevisionMetadata('),c);
  const ids=c.logicalUndoStack('14999',metadata);
  assert.equal(ids.length,1000);assert.equal(ids.at(-1),'14998');assert.equal(ids[0],'13999');
});

test('full snapshot adoption cannot discard pending local operations',()=>{
  const saved={revision_id:'saved'};
  const state={revision:saved,documentStore:{pending:()=>[{operation_id:'unsaved'}]}};
  const c=vm.createContext({state});
  vm.runInContext(extract('  function setRevision(', '  function scheduleAuthoritativeResync('),c);
  assert.throws(()=>c.setRevision({revision_id:'late response'}),/本地编辑/);
  assert.equal(state.revision,saved);assert.equal(state.documentStore.pending().length,1);
});

test('document writes wait for saved input, exclude competing writes and unlock on failure',async()=>{
  let release,writeCalled=false;
  const state={},panel={inert:false};
  const c=vm.createContext({state,$:()=>panel,flushActiveEditorInput:()=>new Promise(r=>release=r)});
  vm.runInContext(extract('  async function withSavedDocument(', '  function hasUncommittedEditorInput()'),c);
  const first=c.withSavedDocument(async()=>{writeCalled=true;assert.equal(state.documentWriteLocked,true);throw Error('server error');});
  const rejected=assert.rejects(first,/server error/);
  assert.equal(writeCalled,false);assert.equal(panel.inert,true);
  await assert.rejects(c.withSavedDocument(async()=>assert.fail()),/稍候/);
  release();await rejected;
  assert.equal(state.documentWritePending,false);assert.equal(state.documentWriteLocked,false);assert.equal(panel.inert,false);
});

test('receipt-confirmed retry after undo clears the journal and preserves redo',async()=>{
  const document={document_id:'doc',changes:[],source_tokens:[],display_tokens:[],cues:[],properties:{}};
  const initial={revision_id:'r0',revision_number:1,document_hash:'hash',document};
  const provenance={operation:'restore_revision',metadata:{undo_revision_ids:[],redo_revision_ids:['r1']}};
  const latest={...initial,revision_id:'r2',revision_number:3,parent_revision_id:'r1',provenance};
  const op={operation_id:'saved-but-response-lost',type:'set_target',payload:{}};
  const store=createDocumentStore({applyLocalOperation:r=>r});store.reset(initial);store.enqueue(op);
  const state={projectId:'p',revision:initial,documentStore:store,undoRevisionIds:[],redoRevisionIds:[],
    revisions:[{...initial,is_latest:true}],taskInfo:{autosave_retry_count:0}};
  const c=vm.createContext({state,projectEpoch:1,localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
    window:{EditorOperationQueue:{createOperationQueue}},console,
    contract:{consumeRevision:r=>r,applyRevisionDelta:()=>{throw Error('base mismatch');}},
    api:async(_path,options)=>options?.method==='POST'?{acknowledged_operation_ids:[op.operation_id],action_revisions:[
      {revision_id:'r1',revision_number:2,parent_revision_id:'r0',provenance:{operation:'operation_batch'}}]}:latest,
    setRevision:r=>state.revision=r,adoptRevisionWithoutRender:r=>state.revision=r,visibleRevisionSignature:()=>'',
    renderRevisionMenu(){},syncRevisionControls(){},renderHeader(){},observeEditorTutorialOperations(){},
    ordinaryError:message=>{if(message)assert.fail(message);},$:()=>({classList:{toggle(){}}})});
  vm.runInContext(extract('  function revisionMetadata(', '  function syncRevisionControls()')+
    extract('  function recordCommittedRevision(', '  async function loadRevisionHistory(')+
    extract('  function appliedOperationIds(', '  async function sendOperation(operation)'),c);
  const q=c.ensureOperationQueue(),saving=q.enqueue(op);await q.flushAndWait();await saving;
  assert.equal(q.getState().hasUnsavedChanges,false);assert.equal(store.pending().length,0);
  assert.deepEqual(Array.from(state.redoRevisionIds),['r1']);assert.equal(state.revision.revision_id,'r2');q.destroy();
});
