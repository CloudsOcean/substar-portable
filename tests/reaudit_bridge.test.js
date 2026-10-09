const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs"),os=require("node:os"),path=require("node:path");
const {spawnSync}=require("node:child_process");
const contract=require("../web/editor_document.js");
const {createDocumentStore}=require("../web/editor_document_store.js");
const {createOperationQueue}=require("../web/editor_operation_queue.js");
const bundled=path.resolve(__dirname,"../runtime/python/python.exe");
const python=process.env.SUBSTAR_TEST_PYTHON || (fs.existsSync(bundled)?bundled:"python");

test("real factory, store and refreshed journal reconcile an ACK-lost commit",async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),"substar-bridge-"));
  const bridge=(mode,batch)=>{
    const processResult=spawnSync(python,[path.join(__dirname,"reaudit_bridge.py")],{input:JSON.stringify({root,mode,batch}),encoding:"utf8"});
    assert.equal(processResult.status,0,processResult.stderr);
    return JSON.parse(processResult.stdout);
  };
  const baseOf=rev=>({document_id:rev.document.document_id,revision_id:rev.revision_id,document_hash:rev.document_hash});
  let queue,recovered;
  try {
    const initial=contract.consumeRevision(bridge("init").revision);
    const store=createDocumentStore();store.reset(initial);
    const id=initial.document.display_tokens[0].token_id;
    const provenance={kind:"manual",operation:"inline_replace"};
    let journal=[];let loseAck;
    const responseLost=new Promise((_,reject)=>{loseAck=reject;});
    queue=createOperationQueue({debounceMs:100000,maxRetries:0,base:()=>baseOf(store.acknowledged()),onJournal:ops=>{journal=JSON.parse(JSON.stringify(ops));},sendBatch:async batch=>{bridge("commit",batch);return responseLost;}});
    const a=contract.replaceOperation(store.projection(),id,"Word0",provenance);
    store.enqueue(a);queue.enqueue(a).catch(()=>{});const flight=queue.flushNow();
    const b=contract.replaceOperation(store.projection(),id,"WORD0",provenance);
    assert.equal(b.payload.expected_text,"Word0");
    store.enqueue(b);queue.enqueue(b).catch(()=>{});
    assert.equal(journal.length,2);
    const persisted=bridge("load");assert.equal(persisted.count,2);
    const refreshed=createDocumentStore();refreshed.reset(contract.consumeRevision(persisted.revision));refreshed.restore(journal);
    recovered=createOperationQueue({initialOperations:journal,debounceMs:100000,base:()=>baseOf(refreshed.acknowledged()),sendBatch:async batch=>{
      const result=bridge("commit",batch);
      refreshed.acknowledge(contract.consumeRevision(result.revision),batch.operations.map(op=>op.operation_id));return result;
    }});
    await recovered.flushAndWait();
    assert.equal(refreshed.projection().document.display_tokens[0].text,"WORD0");
    assert.equal(recovered.getState().hasUnsavedChanges,false);
    assert.equal(bridge("load").count,3);
    loseAck(Object.assign(new Error("response lost"),{status:400}));await flight;
  } finally {
    queue?.destroy();recovered?.destroy();
    fs.rmSync(root,{recursive:true,force:true});
  }
});
