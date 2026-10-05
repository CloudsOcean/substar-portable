const fs=require('fs'),{performance}=require('perf_hooks');
const {createDocumentStore}=require('../../web/editor_document_store.js');
const contract=require('../../web/editor_document.js');
const b=JSON.parse(fs.readFileSync(__dirname+'/baseline.json'));
const percent=(v,p)=>[...v].sort((a,b)=>a-b)[Math.floor(v.length*p)];
(async()=>{
  const revision=await(await fetch('http://127.0.0.1:8769/api/projects/'+b.copy)).json();
  const store=createDocumentStore();store.reset(revision);
  const active=revision.document.display_tokens.filter(t=>t.state==='active').slice(500,530);
  const enqueue=[],ack=[],operations=[];
  for(const token of active){
    const op=contract.replaceOperation(revision,token.token_id,token.text+'投影',{kind:'manual',operation:'stress_projection'});
    operations.push(op);const start=performance.now();store.enqueue(op);enqueue.push(performance.now()-start);
  }
  let authoritative=revision;
  const {applyLocalOperation}=require('../../web/editor_document_store.js');
  for(const op of operations){authoritative=applyLocalOperation(authoritative,op);const start=performance.now();store.acknowledge(authoritative,[op.operation_id]);ack.push(performance.now()-start);}
  const start=performance.now();contract.buildEditorView(store.projection());const view=performance.now()-start;
  const result={pending_count:30,enqueue_p95_ms:percent(enqueue,.95),ack_replay_p95_ms:percent(ack,.95),ack_replay_max_ms:Math.max(...ack),ack_replay_total_ms:ack.reduce((a,b)=>a+b,0),build_view_ms:view,final_pending:store.pending().length};
  fs.writeFileSync(__dirname+'/projection-results.json',JSON.stringify(result,null,2));console.log(result);
})().catch(e=>{console.error(e);process.exitCode=1});
