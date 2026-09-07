"use strict";
const fs=require('fs'),vm=require('vm'),{execFileSync}=require('child_process');
const current=require('../web/editor_document_store.js');
const originalSource=execFileSync('git',['show','fc902d5:web/editor_document_store.js'],{encoding:'utf8'});
const context={module:{exports:{}},require:()=>require('../web/editor_cue_ordering.js'),console};
vm.runInNewContext(originalSource,context);
function fixture(count){
 const tokens=Array.from({length:count*8},(_,i)=>({token_id:'t'+i,text:'word'+i,original_text:'word'+i,source_token_ids:['s'+i],state:'active',provenance:{kind:'source',operation:'fixture',metadata:{}}}));
 return {revision_id:'rev',document:{document_id:'bench',source_tokens:tokens.map((t,i)=>({token_id:'s'+i,text:t.text,index:i,start:i*.1,end:i*.1+.08})),display_tokens:tokens,cues:Array.from({length:count},(_,i)=>({cue_id:'cue'+i,index:i,display_token_ids:tokens.slice(i*8,i*8+8).map(t=>t.token_id),start:i*.8,end:(i+1)*.8,target:null})),properties:{},changes:[]}};
}
const rows=[];
for(const size of [500,2000,10000]){
 const rev=fixture(size);const row={cues:size,tokens:size*8};
 for(const [name,api] of [['baseline',context.module.exports],['fixed',current]]){
  const store=api.createDocumentStore();store.reset(rev);
  const samples=[];
  for(let i=0;i<5;i++){
   const t=performance.now();store.enqueue({operation_id:'op'+i,type:'replace',payload:{token_id:'t'+i,text:'changed'+i}});samples.push(performance.now()-t);
  }
  row[name+'_first_ms']=Number(samples[0].toFixed(2));row[name+'_fifth_ms']=Number(samples[4].toFixed(2));
 }
 rows.push(row);
}
fs.mkdirSync('data/audit-validation/acceptance',{recursive:true});
fs.writeFileSync('data/audit-validation/acceptance/frontend-benchmark.json',JSON.stringify(rows,null,2));
console.log(JSON.stringify(rows));
