const fs=require('fs');
const {performance}=require('perf_hooks');
const contract=require('../../web/editor_document.js');
const {createOperationQueue}=require('../../web/editor_operation_queue.js');
const {applyLocalOperation}=require('../../web/editor_document_store.js');
const baseline=JSON.parse(fs.readFileSync(__dirname+'/baseline.json'));
const url='http://127.0.0.1:8769/api/projects/'+baseline.copy;
const out={project:baseline.copy,phases:[],errors:[]};
let revision,sequence=0;
const runId=Date.now().toString(36);
const base=()=>({document_id:revision.document.document_id,revision_id:revision.revision_id,document_hash:revision.document_hash});
async function request(path='',body){
  const start=performance.now();const response=await fetch(url+path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const raw=await response.text();let data;try{data=JSON.parse(raw)}catch{data={raw:raw.slice(0,300)}};
  if(!response.ok)throw Object.assign(new Error(JSON.stringify(data)),{status:response.status});
  return {data,ms:performance.now()-start,bytes:Buffer.byteLength(raw)};
}
function percentile(values,p){const a=[...values].sort((a,b)=>a-b);return a[Math.min(a.length-1,Math.floor(a.length*p))]||0;}
function operation(token,text,action){return {operation_id:'op_stress_'+runId+'_'+(++sequence),type:'replace',base:base(),payload:{token_id:token.token_id,text,expected_text:token.text,provenance:{kind:'manual',operation:'stress_text_replace',metadata:{user_action_id:action}}}};}
async function phase(name,count,settings={}){
  revision=(await request()).data;
  const active=revision.document.display_tokens.filter(t=>t.state==='active');
  const expected=new Map(),jobs=[],requests=[],cpu=[];
  let peak=0,attempts=0,lost=false;const started=performance.now();
  const queue=createOperationQueue({base,groupByUserAction:true,debounceMs:60,maxRetries:3,
    onStatus:s=>{peak=Math.max(peak,s.pending+s.in_flight+s.failed)},
    onJournal:ops=>{JSON.stringify(ops)},
    async sendBatch(batch){
      attempts++;
      if(settings.offline&&attempts<=4)throw new Error('injected offline');
      if(settings.delay)await new Promise(r=>setTimeout(r,settings.delay));
      const response=await request('/operation-batches',batch);requests.push(response);
      if(settings.lostResponse&&!lost){lost=true;throw new Error('injected lost committed response');}
      const t=performance.now();
      try{revision=contract.applyRevisionDelta(revision,response.data)}catch{revision=(await request()).data}
      cpu.push(performance.now()-t);return response.data;
    }});
  for(let i=0;i<count;i++){
    const token=active[100+i],text=token.text+'测';expected.set(token.token_id,text);
    const op=operation(token,text,settings.group?'one-action':name+'-'+i);
    const t=performance.now();applyLocalOperation(revision,op);cpu.push(performance.now()-t);
    jobs.push(queue.enqueue(op).catch(e=>({error:e.message})));
    if(settings.interval)await new Promise(r=>setTimeout(r,settings.interval));
  }
  let firstFailure=null;
  try{await queue.flushAndWait()}catch(e){firstFailure=e.message;queue.retryFailed();await queue.flushAndWait();}
  await Promise.all(jobs);
  const elapsed=performance.now()-started;
  const saved=(await request()).data;const tokens=new Map(saved.document.display_tokens.map(t=>[t.token_id,t.text]));
  const mismatches=[...expected].filter(([id,text])=>tokens.get(id)!==text);
  const result={name,count,elapsed_ms:elapsed,requests:requests.length,attempts,peak_queue:peak,request_p50_ms:percentile(requests.map(r=>r.ms),.5),request_p95_ms:percentile(requests.map(r=>r.ms),.95),response_bytes:requests.reduce((n,r)=>n+r.bytes,0),projection_delta_p95_ms:percentile(cpu,.95),mismatches:mismatches.length,first_failure:firstFailure,final_state:queue.getState()};
  out.phases.push(result);console.log(JSON.stringify(result));queue.destroy();
}
(async()=>{
  if(process.env.STRESS_EXTRA){await phase('sustained_40_at_5_per_second',40,{interval:200});return;}
  await phase('burst_30_distinct_actions',30);
  await phase('grouped_30_one_action',30,{group:true});
  await phase('slow_save_8',8,{delay:800});
  await phase('lost_committed_response',3,{lostResponse:true});
  await phase('offline_then_retry',3,{offline:true});
  revision=(await request()).data;
  const shared=base(),tokens=revision.document.display_tokens.filter(t=>t.state==='active').slice(200,208);
  const responses=await Promise.all(tokens.map(async(t,i)=>{
    const op=operation(t,t.text+'并发','concurrency-'+i);op.base=shared;
    try{const response=await request('/operation-batches',{batch_id:'stress_parallel_'+i,base:shared,operations:[op]});return {i,status:200,ms:response.ms,text:op.payload.text,id:t.token_id}}
    catch(e){return {i,status:e.status||0,id:t.token_id,error:e.message.slice(0,250)}}
  }));
  const saved=(await request()).data,stored=new Map(saved.document.display_tokens.map(t=>[t.token_id,t.text]));
  out.concurrent={responses,acknowledged_mismatches:responses.filter(r=>r.status===200&&stored.get(r.id)!==r.text).length};
  console.log('concurrent',JSON.stringify(out.concurrent));
})().catch(e=>{out.errors.push(e.stack);console.error(e.stack);process.exitCode=1}).finally(()=>fs.writeFileSync(__dirname+(process.env.STRESS_EXTRA?'/sustained-results.json':'/queue-results.json'),JSON.stringify(out,null,2)));
