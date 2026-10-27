import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {createBoundarySession,installSubstarBoundaries} from '../web/editor_maw_boundary.js';
const engine=createRequire(import.meta.url)('../web/editor_timeline.js');
const fixture=()=>Array.from({length:1000},(_,i)=>({id:String(i),speaker:'',start:i*2000,end:(i+1)*2000,items:[{id:`w${i}`,start:i*2000,end:(i+1)*2000}]}));
test('cue resizing touches only affected word arrays and restores exact timing',()=>{
 const segments=fixture(),refs=segments.map(s=>s.items);
 const s=createBoundarySession({engine,segments,index:1,edge:'end',selected:null,durationMs:2000000});
 for(let i=0;i<100;i++)s.update(4200+i);
 assert.equal(segments[1].end,4299);assert.equal(segments[2].start,4299);
 assert.equal(segments[1].items[0].end,4299);
 segments.forEach((item,i)=>{if(i!==1&&i!==2)assert.equal(item.items,refs[i]);});
 s.update(4000);assert.equal(s.changed(),false);
 s.restore();assert.deepEqual(segments,fixture());
});
test('preview range includes old and new rows when dragging back',()=>{
 const segments=[{id:'a',speaker:'',start:0,end:1000,items:[]}];
 const s=createBoundarySession({engine,segments,index:0,edge:'end',selected:null,durationMs:10000});
 s.update(7000);s.update(2000);assert.equal(s.previewRange.end,7000);
});
function harness(){
 const callbacks={},frames=new Map();let next=0,previews=0,commits=0;
 const row={dataset:{startMs:'0',endMs:'6000'},getBoundingClientRect:()=>({left:0,top:0,width:600,bottom:100}),append(){}};
 function element(){return {style:{},hidden:false,contains:()=>false,append(){},setAttribute(){},remove(){}};}
 const win={innerWidth:1000,addEventListener:(name,fn)=>{callbacks[name]=fn;},requestAnimationFrame:fn=>{frames.set(++next,fn);return next;},cancelAnimationFrame:id=>frames.delete(id)};
 const doc={defaultView:win,createElement:element};const host={ownerDocument:doc,append(){},addEventListener:(name,fn,opts)=>{if(name==='pointerdown'&&opts.capture)callbacks.down=fn;}};
 const segments=fixture().slice(0,3),block={dataset:{idx:'1',track:'main'},closest:()=>row};
 const handle={classList:{contains:name=>name==='right'},closest:()=>block};
 const target={closest:selector=>selector==='.waveform-cue-handle'?handle:null};
 const wave={appendWordBlocks(){},options:{getSegments:()=>segments,onBeginEdit(){},onCommitEdit(){commits++;},onCancelEdit(){}},durationMs:6000,focusWaveform(){},cancelHoverSeekPreview(){},captureRowGeometry:()=>({}),timeFromPointerUnbounded:e=>e.clientX*10,pane:{setPointerCapture(){},releasePointerCapture(){}},findVisibleWaveformRowForTime:()=>row};
 installSubstarBoundaries({wave,host,signal:new AbortController().signal,getSelected:()=>null,refresh(){},previewRefresh(){previews++;},engine});
 const event=x=>({target,button:0,pointerId:1,clientX:x,clientY:50,preventDefault(){},stopImmediatePropagation(){}});
 return {callbacks,frames,event,segments,get previews(){return previews;},get commits(){return commits;}};
}
test('pointer burst is coalesced, pointerup flushes final time before one commit',()=>{
 const h=harness();h.callbacks.down(h.event(400));
 for(let x=410;x<=440;x++)h.callbacks.pointermove(h.event(x));
 assert.equal(h.frames.size,1);assert.equal(h.previews,0);
 const paint=[...h.frames.values()][0];paint();assert.equal(h.previews,1);assert.equal(h.segments[1].end,4400);
 h.callbacks.pointermove(h.event(450));h.callbacks.pointerup(h.event(460));
 assert.equal(h.segments[1].end,4600);assert.equal(h.commits,1);assert.equal(h.frames.size,0);
});
test('cancelling a queued preview restores timing and prevents late paint',()=>{
 const h=harness();h.callbacks.down(h.event(400));h.callbacks.pointermove(h.event(450));h.callbacks.pointercancel(h.event(450));
 assert.equal(h.segments[1].end,4000);assert.equal(h.frames.size,0);assert.equal(h.commits,0);
});
