const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../web/editor_maw_timeline.js'),'utf8');
function fixture(){
  const saves=[],messages=[],reads=[];let refreshes=0;
  const c=vm.createContext({M:{clone:structuredClone},pending:0,queue:Promise.resolve(),dead:false,
    timeline:{position:0},view:{position:0},host:{dataset:{}},
    options:{onTimelineChange(snapshot,flags){return new Promise((resolve,reject)=>saves.push({snapshot,flags,resolve,reject}));}},
    show:t=>messages.push(t),readView:v=>reads.push(v),refreshControls(){},refresh:()=>refreshes++});
  vm.runInContext(source.slice(source.indexOf('  let timelineWrites='),source.indexOf('  function commitMarkers')),c);
  return {c,saves,messages,reads,refreshes:()=>refreshes};
}
test('consecutive gap drafts stage immediately and older saves never overwrite newer positions',async()=>{
  const f=fixture();
  const a=f.c.persist({position:1},{gapOnly:true});
  f.c.timeline={position:2};f.c.view={position:2};
  const b=f.c.persist(f.c.timeline,{gapOnly:true});
  assert.equal(f.saves.length,2);
  f.c.timeline.position=3;
  assert.equal(f.saves[1].snapshot.position,2);
  f.saves[0].resolve();await a;
  assert.equal(f.c.timeline.position,3);assert.equal(f.reads.length,0);
  assert.equal(f.c.pending,1);
  f.saves[1].resolve();await b;
  assert.equal(f.c.pending,0);assert.equal(f.c.host.dataset.saving,'false');
  assert.equal(f.refreshes(),0);assert.equal(f.reads[0].position,2);
});
test('failed gap saves retain the local draft and settle pending state without rollback',async()=>{
  const f=fixture();f.c.timeline={position:7};
  const saving=f.c.persist(f.c.timeline,{gapOnly:true});
  f.saves[0].reject(Error('offline'));
  await assert.rejects(saving,/offline/);
  assert.equal(f.c.timeline.position,7);assert.equal(f.reads.length,0);
  assert.equal(f.c.pending,0);assert.match(f.messages.at(-1),/修改已保留/);
});
test('synchronous submission failures release saving state',async()=>{
  const f=fixture();f.c.options.onTimelineChange=()=>{throw Error('locked');};
  await assert.rejects(f.c.persist({position:4},{gapOnly:true}),/locked/);
  assert.equal(f.c.pending,0);assert.equal(f.c.host.dataset.saving,'false');
});
test('successful retry reconciles a failed draft and clears its failure state',async()=>{
  const f=fixture();f.c.wave={refreshGapOverlay(){}};f.c.renderGapPanel=()=>{};
  const saving=f.c.persist({position:4},{gapOnly:true});
  f.saves[0].reject(Error('offline'));await assert.rejects(saving);
  const start=source.indexOf('markSaved(next){'),end=source.indexOf(',setView(next,',start);
  vm.runInContext('function '+source.slice(start,end),f.c);
  f.c.markSaved({position:4});
  assert.equal(f.reads.at(-1).position,4);assert.equal(f.messages.at(-1),'');
  assert.equal(vm.runInContext('timelineWriteFailed',f.c),false);
});
