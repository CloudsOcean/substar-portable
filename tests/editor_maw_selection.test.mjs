import test from 'node:test';
import assert from 'node:assert/strict';
import {createWordSelection,selectionRuns} from '../web/editor_maw_selection.js';
import {createMarkerBoundarySnapper} from '../web/editor_maw_gap_snap.js';
test('word selection supports cross-cue Shift ranges and Ctrl toggles independently',()=>{
 const order=['a1','a2','b1','b2','c1'],words=createWordSelection(),cues=new Set(['cueA']);
 words.select('a2',order);words.select('b2',order,{shiftKey:true});assert.deepEqual([...words.keys],['a2','b1','b2']);
 words.select('b1',order,{ctrlKey:true});words.select('c1',order,{ctrlKey:true});assert.deepEqual([...words.keys],['a2','b2','c1']);
 assert.deepEqual([...cues],['cueA']);cues.add('cueC');assert.equal(words.keys.size,3);
});
test('selection runs merge adjacent items across pauses but preserve unselected breaks',()=>{
 const entries=[{key:'a',start:0,end:10},{key:'b',start:50,end:60},{key:'c',start:60,end:70},{key:'d',start:70,end:80}];
 assert.deepEqual(selectionRuns(entries,new Set(['a','b','d'])),[{start:0,end:60},{start:70,end:80}]);
 assert.deepEqual(selectionRuns(entries,new Set()),[]);
});
test('marker snap switches granularity and respects distance and Alt bypass',()=>{
 let words=false;const segments=[{start:100,end:1000,items:[{start:150,end:400},{start:450,end:900}]}];
 const snap=createMarkerBoundarySnapper(()=>segments,()=>words);
 assert.equal(snap(130),100);words=true;assert.equal(snap(130),150);assert.equal(snap(700),700);assert.equal(snap(130,{event:{altKey:true}}),130);
 segments[0].items[0].start=170;assert.equal(snap(165),170);
});
