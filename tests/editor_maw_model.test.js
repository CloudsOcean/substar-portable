const test=require('node:test'),assert=require('node:assert/strict');
const M=require('../web/editor_maw_model.js');
test('MAW projection preserves source, translations, speakers and manual cue identity',()=>{
 const view={cue_views:[{cue_id:'a',start:.123,end:1.234,speaker:'s1',state:'active',display_token_ids:['w'],target:{target_text:'译文'}},{cue_id:'manual',start:.5,end:2,source_text:'人工',display_token_ids:[],state:'active',speaker:'s2'}],token_views:[{token_id:'w',text:'Hello',start:.2,end:.8}]};
 const previous=JSON.stringify(view),items=M.segments(view);assert.equal(items[0].translation,'译文');assert.equal(items[0].start,123);assert.equal(items[1].manual,true);assert.equal(items[1].speaker,'s2');items[0].items[0].start=999;assert.equal(JSON.stringify(view),previous);
});
test('legacy cuts migrate from seconds without losing disabled ranges',()=>assert.deepEqual(M.legacyGaps({active_plan_id:'p',plans:[{id:'p',cuts:[{start:.2,end:1.1,enabled:false}]}]}),[{start:200,end:1100,removed:false}]));
test('gap provenance and export projection stay associated with the selected plan',()=>{
 const original={markers:[{id:'m',start:1}],plans:[{id:'one',cuts:[]},{id:'two',cuts:[]}],active_plan_id:'two'};
 const gap={gaps:[{start:1420,end:1920,removed:true}],provenance:{sources:{audio_gate:[]},manual_overrides:[]}};
 const saved=M.withGapState(original,gap);assert.equal(saved.plans[0].cuts.length,0);assert.deepEqual(saved.plans[1].cuts[0],{id:'maw-gap-0',start:1.42,end:1.92,enabled:true,source:'maw'});assert.deepEqual(saved.plans[1].gap_remove,gap);assert.equal(original.plans[1].cuts.length,0);
});
test('editing one overlapping speaker produces only its cue change',()=>{
 const before=[{id:'a',start:0,end:1000},{id:'b',start:500,end:1500}],after=M.clone(before);after[0].start=100;after[0].end=1100;assert.deepEqual(M.timeChanges(before,after),[{cue_id:'a',start:.1,end:1.1}]);
});
test('marker metadata and integer millisecond positions round trip',()=>{
 const markers=[{id:'m',start:1.234,end:2.345,color:'#00a2c7',review:{status:'pending',reason:'检查'}}];assert.deepEqual(M.storedMarkers(M.markers({markers})),markers);
});

test('range anchor moves independently and clamps without moving either boundary',()=>{
 const original={id:'m',start:1000,end:3000,point:2000};
 assert.deepEqual(M.markerDrag(original,'move',2500,10000),{...original,point:2500});
 assert.deepEqual(M.markerDrag(original,'move',9000,10000),{...original,point:3000});
 assert.deepEqual(M.markerDrag(original,'move',0,10000),{...original,point:1000});
 assert.equal(original.point,2000);
});
test('both point handles create a range and boundaries do not displace its anchor',()=>{
 const p={id:'m',start:2000};
 assert.deepEqual(M.markerDrag(p,'resize-start',1000,10000),{...p,start:1000,end:2000,point:2000});
 assert.deepEqual(M.markerDrag(p,'resize-end',3000,10000),{...p,end:3000,point:2000});
 const r={...p,start:1000,end:3000,point:2000};
 assert.deepEqual(M.markerDrag(r,'resize-start',2500,10000),{...r,start:2000});
 assert.deepEqual(M.markerDrag(r,'resize-end',1500,10000),{...r,end:2000});
});
test('anchor survives normalization and millisecond storage round trip',async()=>{
 const {createUtilsModule}=await import('../web/vendor/maw/shared/utils/markers.js');
 const saved={id:'m',start:1,end:3,point:2.123,name:'',note:'',color:'#3e63dd'};
 const normalized=createUtilsModule().normalizeMarkers(M.markers({markers:[saved]}));
 assert.deepEqual(M.storedMarkers(normalized),[saved]);
 assert.equal(createUtilsModule().normalizeMarkers([{id:'old',start:1000,end:3000}])[0].point,1000);
});

test('waveform drag uses anchor time for preview while preserving range',async()=>{
 const {createWaveformModule}=await import('../web/vendor/maw/editor/media/waveform/markers.js');
 const methods=createWaveformModule({clamp:(v,a,b)=>Math.max(a,Math.min(b,v)),roundMs:Math.round});
 const oldWindow=global.window;global.window={SubstarMawModel:M};
 try {
  const drag={mode:'move',original:{id:'m',start:1000,end:3000,point:2000},pointerStartMs:2000};
  const host={durationMs:10000,timeFromPointerUnbounded:()=>2500};
  methods.applyMarkerDragTime.value.call(host,drag,{});
  assert.deepEqual(drag.pending,{id:'m',start:1000,end:3000,point:2500});
  let time;methods.refreshMarkerDragPointerLine.value.call({markerDrag:drag,showDragPointerLineAtTime:v=>time=v});
  assert.equal(time,2500);
 } finally {global.window=oldWindow;}
});

test('collapsed marker moves freely and fully retracted boundaries become a point',()=>{
 assert.deepEqual(M.markerDrag({id:'m',start:8781,end:8782,point:8781},'move',9500,10000),{id:'m',start:9500});
 assert.deepEqual(M.markerDrag({id:'m',start:1000,end:2000,point:2000},'resize-start',2000,10000),{id:'m',start:2000});
 assert.deepEqual(M.markerDrag({id:'m',start:2000,end:3000,point:2000},'resize-end',2000,10000),{id:'m',start:2000});
});
