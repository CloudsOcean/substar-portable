import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {createWordBoundarySession} from '../web/editor_maw_boundary.js';
const engine=createRequire(import.meta.url)('../web/editor_timeline.js');
const fixture=()=>({id:'cue',start:1000,end:3000,items:[{id:'a',text:"It's",start:1000,end:1500},{id:'b',text:'dynamic',start:1500,end:2200},{id:'c',text:'direct',start:2300,end:3000}]});
function session(segment,extra={}){return createWordBoundarySession({engine,segment,itemIndex:1,edge:'start',selected:new Set(),durationMs:5000,...extra});}
test('word seams use the same shared boundary rules and preserve cue bounds',()=>{
 const segment=fixture(),s=session(segment);assert.equal(s.mode,'shared');s.update(1700);
 assert.equal(segment.items[0].end,1700);assert.equal(segment.items[1].start,1700);
 assert.deepEqual([segment.start,segment.end],[1000,3000]);assert.equal(segment.items[2].start,2300);assert.ok(s.changed());
 s.update(1600);assert.equal(segment.items[0].end,1600);s.restore();assert.deepEqual(segment,fixture());
});
test('selected words and explicit single mode leave the neighbour unchanged',()=>{
 for(const extra of [{selected:new Set([1])},{mode:'single'}]){
   const segment=fixture(),s=session(segment,extra);assert.equal(s.mode,'single');s.update(1700);
   assert.equal(segment.items[0].end,1500);assert.equal(segment.items[1].start,1700);
 }
});
test('outer word edges cannot escape the parent cue',()=>{
 const segment=fixture();session(segment,{itemIndex:0}).update(-500);assert.equal(segment.items[0].start,1000);
 session(segment,{itemIndex:2,edge:'end'}).update(5000);assert.equal(segment.items[2].end,3000);
});
test('separated words default to single and can switch to shared mode',()=>{
 const segment=fixture(),single=session(segment,{itemIndex:2});assert.equal(single.mode,'single');
 const linked=session(segment,{itemIndex:2,mode:'shared'});assert.equal(linked.mode,'shared');linked.update(2400);
 assert.equal(segment.items[1].end,2400);assert.equal(segment.items[2].start,2400);
});
