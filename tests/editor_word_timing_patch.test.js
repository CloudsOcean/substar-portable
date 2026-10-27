const test=require('node:test'),assert=require('node:assert/strict');
const M=require('../web/editor_maw_model.js');
test('word timing commits contain only changed words and owning cue, including shared seams',()=>{
 const before=[{id:'a',start:0,end:2000,items:[{id:'w1',start:0,end:1000},{id:'w2',start:1000,end:2000}]}];
 const after=[{...before[0],items:[{id:'w1',start:0,end:1100},{id:'w2',start:1100,end:2000}]},...Array.from({length:1404},(_,i)=>({id:`other${i}`,items:[{id:`x${i}`,start:0,end:1000}]}))];
 const patch=M.wordTimeChanges(before,after);
 assert.deepEqual(patch.cues,[{cue_id:'a',start:0,end:2}]);
 assert.deepEqual(patch.wordTimings,{w1:{start:0,end:1100},w2:{start:1100,end:2000}});
 assert.deepEqual(M.wordTimeChanges(before,before),{cues:[],wordTimings:{}});
});
