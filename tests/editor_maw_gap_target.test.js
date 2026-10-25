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
  let committed;const actions=[];
  const start=source.indexOf('function gap('),end=source.indexOf('\n  function word(',start);
  const sandbox={globalThis:{AsrGapRemoveCore:C},h:{getGapState:()=>state,commitGaps:s=>committed=s},reset(){},sep(){},open(){},addMarkerItems(){},item:(label,key,fn)=>actions.push({label,fn})};
  vm.runInNewContext(source.slice(start,end)+';gap(0,0,0);',sandbox);
  actions.find(a=>a.label==='清理空隙').fn();
  assert.equal(C.getGapRemoveDisplayGaps(C.normalizeGapRemoveData(committed).gaps).length,0);
});
