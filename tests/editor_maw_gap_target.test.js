const test=require('node:test');
const assert=require('node:assert/strict');
require('../web/vendor/maw/shared/gap-remove-core.js');
const C=globalThis.AsrGapRemoveCore;
test('clearing one moved visible gap removes every underlying provenance fragment',()=>{
  const before=C.normalizeGapRemoveData({gaps:[{start:1000,end:2000,removed:true},{start:4000,end:4500,removed:true}]});
  const moved=C.moveGapRemoveProvenance(before.provenance,before.gaps,0,500,10000,before.gaps);
  const state=C.normalizeGapRemoveData({...before,...moved});
  const visible=C.getGapRemoveDisplayGaps(state.gaps);
  assert.equal(state.gaps.length,3);assert.equal(visible.length,2);
  const target=visible[0];assert.equal(target.start,1500);assert.equal(target.end,2500);
  const provenance=C.removeGapRemoveProvenanceRange(state.provenance,target.start,target.end,state.gaps);
  const after=C.normalizeGapRemoveData({...state,provenance});
  assert.deepEqual(C.getGapRemoveDisplayGaps(after.gaps).map(g=>[g.start,g.end]),[[4000,4500]]);
  // Undo restores the exact moved block, not its pre-drag position.
  assert.deepEqual(C.getGapRemoveDisplayGaps(state.gaps).map(g=>[g.start,g.end]),[[1500,2500],[4000,4500]]);
});
test('gap menus resolve the same projected index that MAW renders',()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const source=fs.readFileSync(require.resolve('../web/editor_maw_menus.js'),'utf8');
  const before=C.normalizeGapRemoveData({gaps:[{start:1000,end:2000,removed:true}]});
  const state=C.normalizeGapRemoveData({...before,...C.moveGapRemoveProvenance(before.provenance,before.gaps,0,500,10000,before.gaps)});
  let committed,heard;const actions=[];
  const start=source.indexOf('function gap('),end=source.indexOf('\n  function word(',start);
  const sandbox={options:{onRetranscribeGap:(a,b)=>heard=[a,b]},globalThis:{AsrGapRemoveCore:C},h:{getGapState:()=>state,commitGaps:s=>committed=s},reset(){},sep(){},open(){},addMarkerItems(){},item:(label,key,fn)=>actions.push({label,fn})};
  vm.runInNewContext(source.slice(start,end)+';gap(0,0,0);',sandbox);
  actions.find(a=>a.label==='重新听写此片段').fn();
  assert.deepEqual(heard,[1.5,2.5]);
  actions.find(a=>a.label==='清理空隙').fn();
  assert.equal(C.getGapRemoveDisplayGaps(C.normalizeGapRemoveData(committed).gaps).length,0);
});

const plain = state => C.gapRangesFromProvenance(state.provenance).map(g=>[g.start,g.end,g.removed]);
const resized = (state,index,edge,time) => C.normalizeGapRemoveData({...state,...C.resizeGapRemoveProvenanceBoundary(state.provenance,state.gaps,index,edge,time)});
const cleared = (state,start,end) => C.normalizeGapRemoveData({...state,provenance:C.removeGapRemoveProvenanceRange(state.provenance,start,end,state.gaps)});

test('clear replays after repeated edge edits, survives reload and preserves unrelated restored gaps',()=>{
  let state=C.normalizeGapRemoveData({gaps:[{start:1000,end:1400,removed:true},{start:8000,end:9000,removed:false}]});
  state=resized(state,0,'end',6000);
  state=resized(state,0,'start',2200);
  const before=JSON.stringify(state);
  const after=cleared(state,2200,6000);
  assert.deepEqual(plain(after),[[8000,9000,false]]);
  assert.deepEqual(plain(C.normalizeGapRemoveData(JSON.parse(JSON.stringify(after)))),[[8000,9000,false]]);
  assert.equal(JSON.stringify(state),before); // undo snapshot remains untouched
  const recreated=C.normalizeGapRemoveData({...after,provenance:C.appendGapRemoveManualOverrides(after.provenance,[{start:2200,end:6000,removed:true}])});
  assert.deepEqual(plain(resized(recreated,0,'end',5000)),[[2200,5000,true],[8000,9000,false]]);
});

test('partial clear of moved gap preserves remaining target and never resurrects its base',()=>{
  const initial=C.normalizeGapRemoveData({gaps:[{start:1000,end:3000,removed:true}]});
  const moved=C.normalizeGapRemoveData({...initial,...C.moveGapRemoveProvenance(initial.provenance,initial.gaps,0,4000,20000)});
  const after=cleared(moved,5500,6500);
  assert.deepEqual(plain(after),[[5000,5500,true],[6500,7000,true]]);
  assert.deepEqual(plain(resized(after,0,'end',5700)),[[5000,5700,true],[6500,7000,true]]);
  assert.deepEqual(plain(cleared(after,5000,7000)),[]);
});

test('clear restored and adjacent ranges without changing other source records',()=>{
  const initial=C.normalizeGapRemoveData({provenance:{sources:{audio_gate:[{start:1000,end:5000}]},manual_overrides:[{start:2000,end:3000,removed:false}]}});
  const after=cleared(initial,2000,3000);
  assert.deepEqual(plain(after),[[1000,2000,true],[3000,5000,true]]);
  assert.deepEqual(after.provenance.sources,initial.provenance.sources);
  assert.deepEqual(plain(cleared(after,3000,5000)),[[1000,2000,true]]);
});
