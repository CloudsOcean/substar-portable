const test=require("node:test"),assert=require("node:assert/strict");
const cut=require("../web/editor_cut_timeline.js");
test("preview unions overlapping cuts and respects keep plans",()=>{
  assert.deepEqual(cut.kept({keep:[{start:0,end:6},{start:5,end:10}],cuts:[{start:2,end:4},{start:3,end:7},{start:8,end:10,enabled:false}]},10),[{start:0,end:2},{start:7,end:10}]);
  assert.deepEqual(cut.kept({keep:[],cuts:[]},10),[]);
});
test("non-contiguous word selection does not cut intervening words",()=>{
  const units=Array.from({length:4},(_,i)=>({id:`w${i}`,speaker:"a",start:i,end:i+.5}));
  const ranges=cut.selectionRanges(units,new Set(["w0","w2","w3"]));
  assert.deepEqual(ranges.map(r=>[r.start,r.end]),[[0,.5],[2,3.5]]);
});
test("selection grouping never absorbs a different speaker",()=>{
  const units=[{id:"a",speaker:"a",start:0,end:1},{id:"b",speaker:"b",start:0,end:2}];
  assert.equal(cut.selectionRanges(units,new Set(["a","b"])).length,2);
});
