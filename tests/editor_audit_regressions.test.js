"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const {createOperationQueue} = require("../web/editor_operation_queue.js");
const {createDocumentStore} = require("../web/editor_document_store.js");

test("retry backoff is observed and export waits for ACK", async () => {
  const times=[];
  const queue=createOperationQueue({debounceMs:0,base:()=>({revision_id:"saved"}),sendBatch:async()=>{
    times.push(performance.now());
    if(times.length<3)throw Object.assign(new Error("temporary"),{status:503});
    return {};
  }});
  const pending=queue.enqueue({operation_id:"one",type:"replace",payload:{}});
  const base=await queue.flushAndWait();
  await pending;
  assert.equal(base.revision_id,"saved");
  assert.ok(times[1]-times[0]>=130);
  assert.ok(times[2]-times[1]>=280);
  assert.equal(queue.getState().hasUnsavedChanges,false);
  queue.destroy();
});

test("a conflict is retained, blocks dependent saves and prevents export", async () => {
  let sends=0;
  let journal=[];
  const queue=createOperationQueue({maxBatchSize:1,debounceMs:0,base:()=>({}),onJournal:value=>{journal=value;},sendBatch:async()=>{
    sends++;throw Object.assign(new Error("conflict"),{status:409});
  }});
  const first=queue.enqueue({operation_id:"one"}).catch(()=>{});
  const second=queue.enqueue({operation_id:"two"}).catch(()=>{});
  await assert.rejects(queue.flushAndWait(),/未保存/);
  await first;
  assert.equal(sends,1);
  assert.equal(queue.getState().failed,1);
  assert.equal(queue.getState().hasUnsavedChanges,true);
  assert.equal(queue.getState().pending,1);
  assert.deepEqual(journal.map(operation=>operation.operation_id),['one','two']);
  assert.equal(queue.discardUnsaved().length,2);
  await second;
  queue.destroy();
});

test("optimistic replacement shares immutable entities and applies new intent once",()=>{
  const source={token_id:"source",text:"word",start:0,end:1};
  const cue={cue_id:"cue",index:0,display_token_ids:["a","b"],start:0,end:1};
  const revision={document:{source_tokens:[source],display_tokens:[{token_id:"a",text:"a"},{token_id:"b",text:"b"}],cues:[cue],changes:[],properties:{}}};
  let reductions=0;
  const {applyLocalOperation}=require("../web/editor_document_store.js");
  const store=createDocumentStore({applyLocalOperation:(value,op)=>{reductions++;return applyLocalOperation(value,op);}});
  store.reset(revision);
  for(let i=0;i<5;i++)store.enqueue({operation_id:String(i),type:"replace",payload:{token_id:"a",text:String(i)}});
  assert.equal(reductions,5);
  assert.equal(store.projection().document.source_tokens,revision.document.source_tokens);
  assert.equal(store.projection().document.cues[0],cue);
  assert.equal(store.projection().document.display_tokens[1],revision.document.display_tokens[1]);
  assert.equal(revision.document.display_tokens[0].text,"a");
});

test("source edits mark the optimistic translation for review without mutating the saved track",()=>{
  const {applyLocalOperation}=require("../web/editor_document_store.js");
  const target={target_text:"译文",translation_status:"translated",issue_code:null};
  const saved={document:{source_tokens:[],display_tokens:[{token_id:"a",text:"old",state:"active"}],cues:[{cue_id:"cue",display_token_ids:["a"],state:"active",target}],properties:{},changes:[]}};
  const projected=applyLocalOperation(saved,{operation_id:"edit",type:"replace",payload:{token_id:"a",text:"new"}});
  assert.equal(projected.document.cues[0].target.translation_status,"needs_review");
  assert.equal(target.translation_status,"translated");
});

test("late project A load cannot overwrite project B session",async()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const source=fs.readFileSync(require.resolve('../web/editor.js'),'utf8');
  const code=source.slice(source.indexOf('  let projectEpoch = 0;'),source.indexOf('  async function loadProjects()'));
  const requests=[],adopted=[],errors=[];
  const state={projectId:'',projects:[],operationQueue:null};
  const elements=new Map();
  const context={state,URL,localStorage:{getItem:()=>null},window:{location:{href:'http://localhost/editor'}},history:{replaceState:()=>{}},
    $:selector=>{if(!elements.has(selector))elements.set(selector,{});return elements.get(selector);},
    projectPath:(suffix='')=>`/api/projects/${state.projectId}${suffix}`,
    api:path=>new Promise(resolve=>requests.push({path,resolve})),
    ordinaryError:value=>errors.push(value),
    setRevision:revision=>{state.revision=revision;adopted.push(revision.revision_id);},
    refreshEditorAiTask:async()=>null,
    refreshTranslationTask:async()=>null,
    loadRevisionHistory:async()=>{},
  };
  for(const name of ['renderHeader','renderProjectList','stopTranslationPoll','renderTranslationTask','hideEditorTutorialIntro','applySubtitlePolicy','configureTranslationLanguageDefaults','configureMedia','seedRevisionMetadata','ensureOperationQueue','setMediaMessage','loadProjectMedia','startEditorAiTaskPoll','followTranslationTask','maybeShowTutorialIntro'])context[name]=()=>{};
  vm.createContext(context);vm.runInContext(code,context);
  const a=context.loadProject('A');
  const b=context.loadProject('B');
  const resolve=id=>requests.filter(item=>item.path.startsWith(`/api/projects/${id}`)).forEach(item=>item.resolve({revision_id:id,document:{changes:[]}}));
  resolve('B');await b;
  resolve('A');await a;
  assert.equal(state.projectId,'B');
  assert.equal(state.revision.revision_id,'B');
  assert.deepEqual(adopted,['B']);
  assert.equal(errors.filter(Boolean).length,0);
});
