"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {rowRanges,timeLabel,waveformEnvelope,cueHandles,createTimelineController} = require("../web/editor_multiline_timeline.js");
const {movePreview} = require("../web/editor_multiline_timeline.js");

test("overlay movement ignores main-track neighbors while main movement stays constrained", () => {
  const a={cue_id:"a",start:0,end:3}, b={cue_id:"b",start:4,end:6};
  const overlay={cue_id:"o",start:1,end:2,subtitle_track:"overlay"};
  const cues=[a,overlay,b];
  assert.deepEqual(movePreview(cues,overlay,3,10).changes.map(c=>c.time),[4,5]);
  assert.deepEqual(movePreview(cues,a,3,10).changes.map(c=>c.time),[1,4]);
  assert.deepEqual(movePreview(cues,overlay,-3,10).changes.map(c=>c.time),[0,1]);
});

test("multiline rows cover the full media once with no gaps or duplicate seconds", () => {
  for (const duration of [.5, 10, 10.001, 392.12]) {
    for (const seconds of [5,10,15,20]) {
      const rows = rowRanges(duration, seconds);
      assert.equal(rows[0].start, 0);
      assert.equal(rows.at(-1).end, duration);
      rows.forEach((row,i) => {
        assert.ok(row.end > row.start);
        assert.ok(row.end - row.start <= seconds);
        if (i) assert.equal(row.start, rows[i-1].end);
      });
    }
  }
});
test("silence stays empty and the waveform never exceeds its drawing bounds", () => {
  assert.deepEqual(waveformEnvelope([0,0,0,0],4,0,4,0,4),[0,0,0,0]);
  const values=waveformEnvelope([0,.001,.4,1,0],50,0,5,0,5,1,.002);
  assert.equal(values.length,50);
  assert.ok(values.every(value=>Number.isFinite(value)&&value>=0&&value<=1));
  assert.equal(values[0],0);
  assert.equal(values.at(-1),0);
  assert.ok(values.some(value=>value>.5));
});
test("a cue spanning rows has handles only at its real start and end", () => {
  const cue={start:8,end:23};
  const first=cueHandles(cue,{start:0,end:10},10,1000);
  const middle=cueHandles(cue,{start:10,end:20},10,1000);
  const last=cueHandles(cue,{start:20,end:30},10,1000);
  assert.deepEqual([first.start,first.end],[true,false]);
  assert.deepEqual([middle.start,middle.end],[false,false]);
  assert.deepEqual([last.start,last.end],[false,true]);
  assert.equal(first.left,800);
  assert.equal(last.right,300);
});
test("empty media has no waveform rows and minute labels retain milliseconds", () => {
  assert.deepEqual(rowRanges(0), []);
  assert.equal(timeLabel(60), "01:00.000");
  assert.equal(timeLabel(56.98), "00:56.980");
});

test("dragging inside the end handle preserves the grab offset and moves a shared boundary", t => {
  const globals = ["requestAnimationFrame","cancelAnimationFrame","ResizeObserver"];
  const previous = globals.map(name => Object.getOwnPropertyDescriptor(globalThis,name));
  t.after(() => globals.forEach((name,i) => previous[i]
    ? Object.defineProperty(globalThis,name,previous[i]) : delete globalThis[name]));
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = () => {};
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  const nodes=[];
  const doc={defaultView:{devicePixelRatio:1},addEventListener() {},removeEventListener() {}};
  function element(tag) {
    const node={tag,children:[],dataset:{},style:{},listeners:{},ownerDocument:doc,scrollTop:0,
      clientWidth:1000,clientHeight:400,classList:{add() {},remove() {},toggle() {}},
      append(...children) { this.children.push(...children); },
      prepend(...children) { this.children.unshift(...children); },
      replaceChildren() { this.children=[]; }, setAttribute() {}, remove() {},
      addEventListener(name,handler) { this.listeners[name]=handler; },
      removeEventListener() {},setPointerCapture() {},releasePointerCapture() {},focus() {},
      getBoundingClientRect() { return {left:0,top:0,width:1000,height:152}; }
    };
    nodes.push(node);return node;
  }
  doc.createElement=element;
  const panel=element("section"), track=element("div"), canvas=element("canvas");
  canvas.closest=()=>panel;canvas.parentElement=track;
  let emitted;
  const media={duration:10,currentTime:0,paused:true,addEventListener() {},removeEventListener() {}};
  const selection=require("../web/editor_cue_selection.js").createSelection();
  const controller=createTimelineController({canvas,media,selection,onOperation:(_name,intent)=>{emitted=intent;}});
  controller.setView({cue_views:[
    {cue_id:"a",index:0,start:1,end:3,state:"active",source_text:"a"},
    {cue_id:"b",index:1,start:3,end:5,state:"active",source_text:"b"}
  ]});
  const row=nodes.find(node=>node.className==="multiline-waveform-row");
  const pointer=x=>({button:0,clientX:x,clientY:125,pointerId:1,preventDefault() {}});
  row.listeners.pointerdown(pointer(294));
  row.listeners.pointermove(pointer(314));
  row.listeners.pointerup(pointer(314));
  assert.equal(emitted.mode,"shared");
  assert.deepEqual(emitted.changes,[
    {cue_id:"a",edge:"end",time:3.2},{cue_id:"b",edge:"start",time:3.2}
  ]);
  // Clicking the shared boundary restores the persistent yellow single mode.
  row.listeners.click(pointer(314));
  row.listeners.pointermove(pointer(294));
  row.listeners.pointermove({...pointer(302),clientY:80});
  row.listeners.pointermove({...pointer(330),clientY:44});
  row.listeners.click({...pointer(330),clientY:44});
  row.listeners.pointerdown(pointer(294));
  row.listeners.pointermove(pointer(274));
  row.listeners.pointerup(pointer(274));
  assert.equal(emitted.mode,"single");
  assert.deepEqual(emitted.changes,[{cue_id:"a",edge:"end",time:2.8}]);
  // Consume the browser click following a completed drag, then switch back.
  row.listeners.click(pointer(274));
  row.listeners.click(pointer(300));
  row.listeners.pointerdown(pointer(294));
  row.listeners.pointermove(pointer(314));
  row.listeners.pointerup(pointer(314));
  assert.equal(emitted.mode,"shared");
  assert.equal(emitted.changes.length,2);
  row.listeners.click(pointer(314));
  row.listeners.pointerdown({...pointer(294),shiftKey:true});
  row.listeners.pointermove({...pointer(274),shiftKey:true});
  row.listeners.pointerup({...pointer(274),shiftKey:true});
  assert.equal(emitted.mode,"single");
  assert.equal(emitted.changes.length,1);
  row.listeners.contextmenu(pointer(50));
  assert.equal(emitted.type,"create_manual_cue");
  assert.equal(emitted.start,.5);
  assert.equal(emitted.end,1);
  assert.equal(emitted.overlay,undefined);
  row.listeners.contextmenu(pointer(150));
  assert.equal(emitted.type,"manual_cue_occupied");
  // Modifier clicks share the list selection without starting a move or seeking.
  row.listeners.click(pointer(274)); // consume the last boundary drag click
  const choose=(x,modifiers={})=>{
    const event={...pointer(x),...modifiers};
    row.listeners.pointerdown(event);row.listeners.pointerup(event);row.listeners.click(event);
  };
  choose(150);
  assert.deepEqual([...selection.ids],["a"]);
  const seekTime=media.currentTime;
  choose(450,{ctrlKey:true});
  assert.deepEqual(controller.getState().selected_cue_ids,["a","b"]);
  assert.equal(media.currentTime,seekTime);
  choose(150,{ctrlKey:true});
  assert.deepEqual([...selection.ids],["b"]);
  choose(450,{shiftKey:true});
  assert.deepEqual([...selection.ids],["a","b"]);
  // A list-side change uses the same selection, including its range anchor.
  selection.select("b",["a","b"]);
  controller.redraw();
  assert.deepEqual(controller.getState().selected_cue_ids,["b"]);
  choose(150,{shiftKey:true});
  assert.deepEqual([...selection.ids],["a","b"]);
  nodes.find(node=>node.textContent==="基础").onclick();
  assert.deepEqual(controller.getState().selected_cue_ids,["a","b"]);
  nodes.find(node=>node.textContent==="多行").onclick();
  assert.deepEqual(controller.getState().selected_cue_ids,["a","b"]);
  media.duration=30;
  controller.setView({cue_views:[
    {cue_id:"a",start:1,end:3,state:"active"},
    {cue_id:"b",start:3,end:5,state:"active"},
    {cue_id:"c",start:11,end:14,state:"active"}
  ]});
  const secondRow=nodes.filter(node=>node.className==="multiline-waveform-row").at(-2);
  const crossRow={...pointer(150),shiftKey:true};
  secondRow.listeners.pointerdown(crossRow);secondRow.listeners.pointerup(crossRow);secondRow.listeners.click(crossRow);
  assert.deepEqual(controller.getState().selected_cue_ids,["b","c"]);
  nodes.find(node=>node.textContent==="基础").onclick();
  const basicRow=nodes.filter(node=>node.className==="multiline-waveform-row").at(-2);
  const toggle={...pointer(200),ctrlKey:true};
  basicRow.listeners.pointerdown(toggle);basicRow.listeners.pointerup(toggle);basicRow.listeners.click(toggle);
  assert.deepEqual(controller.getState().selected_cue_ids,["b"]);
  nodes.find(node=>node.textContent==="多行").onclick();
  controller.setView({cue_views:[]});
  const viewport=nodes.find(node=>node.className==="multiline-waveform-viewport");
  nodes.find(node=>node.textContent==="基础").onclick();
  assert.equal(controller.getState().mode,"basic");
  viewport.scrollLeft=1200;
  assert.equal(controller.getState().view_start,12);
  assert.equal(controller.getState().view_end,22);
  controller.revealTime(25,true);
  assert.equal(viewport.scrollLeft,2000);
  nodes.find(node=>node.textContent==="多行").onclick();
  assert.equal(controller.getState().mode,"multiline");
  assert.equal(viewport.scrollLeft,0);
  controller.destroy();
  assert.equal(track.hidden,false);
});

// Narrow transient peaks and silent columns must survive rendering resampling.
test("waveform resampling preserves sharp transients without smoothing adjacent silence", () => {
  assert.deepEqual(waveformEnvelope([0,1,0,.2,0],5,0,5,0,5), [0,1,0,.2,0]);
  assert.deepEqual(waveformEnvelope([0,1,0,.2,0,0],3,0,6,0,6), [1,.2,0]);
  assert.deepEqual(waveformEnvelope([0,1,0],6,0,3,0,3), [0,0,1,1,0,0]);
});

 test("row labels follow token times and preserve untimed text", () => {
   const {rowCueText} = require("../web/editor_multiline_timeline.js");
   const cue = {start:0,end:6,text:"one two",timed_tokens:[{text:"one",start:0,end:1},{text:"two",start:4,end:5}]};
   assert.equal(rowCueText(cue,{start:0,end:2}),"one …");
   assert.equal(rowCueText(cue,{start:2,end:4}),"（间歇）");
   assert.equal(rowCueText(cue,{start:4,end:6}),"… two");
   assert.equal(rowCueText({...cue,timed_tokens:[]},{start:4,end:6}),"续 · one two");
 });
