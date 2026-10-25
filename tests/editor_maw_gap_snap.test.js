const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const load=path=>import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(require.resolve(path),'utf8')).toString('base64'));
const api=load('../web/editor_maw_gap_snap.js');
const cues=[{start:1000,end:3000,items:[{start:1000,end:1603},{start:1702,end:3000}]},{start:4000,end:5000,disabled:true}];
test('sentence mode ignores internal word boundaries and snaps only near cue edges',async()=>{
 const {gapSnapCandidates,snapGapBoundary}=await api;
 const edges=gapSnapCandidates(cues,false);
 assert.deepEqual(edges,[1000,3000]);
 assert.equal(snapGapBoundary(1640,edges),1640);
 assert.equal(snapGapBoundary(1030,edges),1000);
 assert.equal(snapGapBoundary(2970,edges),3000);
});
test('word mode includes exact word starts and ends, excluding hidden cues',async()=>{
 const {gapSnapCandidates,snapGapBoundary}=await api;
 const edges=gapSnapCandidates(cues,true);
 assert.deepEqual(edges,[1000,1603,1702,3000]);
 assert.equal(snapGapBoundary(1640,edges),1603);
 assert.equal(snapGapBoundary(1730,edges),1702);
 assert.equal(snapGapBoundary(4010,edges),4010);
});
test('Alt bypasses snapping and points outside the threshold remain free',async()=>{
 const {snapGapBoundary}=await api;
 assert.equal(snapGapBoundary(1030,[1000],80,true),1030);
 assert.equal(snapGapBoundary(1200,[1000],80),1200);
 assert.equal(snapGapBoundary(1000,[]),1000);
});
test('toggling word visibility refreshes candidates without resetting source data',async()=>{
 const {createGapBoundarySnapper}=await api;let words=false;
 const snap=createGapBoundarySnapper(()=>cues,()=>words);
 assert.equal(snap(1640),1640);words=true;assert.equal(snap(1640),1603);
 assert.equal(snap(1640,{event:{altKey:true}}),1640);
 words=false;assert.equal(snap(1640),1640);
});
test('snap radius follows screen scale and is capped at 120 milliseconds',async()=>{
 const {createGapBoundarySnapper}=await api;
 const snap=createGapBoundarySnapper(()=>cues,()=>false);
 assert.equal(snap(1030,{row:{dataset:{startMs:0,endMs:1000},getBoundingClientRect:()=>({width:1000})}}),1030);
 assert.equal(snap(1110,{row:{dataset:{startMs:0,endMs:20000},getBoundingClientRect:()=>({width:100})}}),1000);
 assert.equal(snap(1130,{row:{dataset:{startMs:0,endMs:20000},getBoundingClientRect:()=>({width:100})}}),1130);
});
test('native range commit retains exact snapped word times after grid rounding',async()=>{
 const {createWaveformModule}=await load('../web/vendor/maw/editor/media/waveform/gap-drag.js');
 const {createGapBoundarySnapper}=await api;
 const instance={};Object.defineProperties(instance,createWaveformModule({ROUND_MS:10,roundMs:n=>Math.round(n/10)*10,clamp:(n,a,b)=>Math.max(a,Math.min(b,n))}));
 const row={releasePointerCapture(){}};let result;
 instance.options={snapGapBoundary:createGapBoundarySnapper(()=>cues,()=>true),applyGapRange:(...args)=>result=args};
 instance.clearGapRangePreviews=()=>{};
 instance.gapRangeDrag={pointerId:1,row,moved:true,startMs:1603,endMs:3000,removed:true};
 const previous=globalThis.window;globalThis.window={removeEventListener(){}};
 try{instance.endGapRangeDrag({pointerId:1,type:'pointerup',altKey:false});}finally{globalThis.window=previous;}
 assert.deepEqual(result,[1603,3000,true]);
});

test('Alt used to start a left-drag gap remains a creation gesture, not a snap bypass',async()=>{
 const {createGapBoundarySnapper}=await api;
 const snap=createGapBoundarySnapper(()=>cues,()=>true);
 assert.equal(snap(1640,{event:{altKey:true},altIsGesture:true}),1603);
 assert.equal(snap(1640,{event:{altKey:true},altIsGesture:false}),1640);
});
