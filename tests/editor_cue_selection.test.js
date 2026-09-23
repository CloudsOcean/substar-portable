const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createSelection}=require('../web/editor_cue_selection.js');
const {dragMode,intersectingTokens}=require('../web/editor_cue_selection.js');
test('drag origin distinguishes source area, cue controls and native editing',()=>{
  const target=(source,button,editing=false)=>({closest(selector){
    if(selector.startsWith('input,'))return editing ? {} : null;
    if(selector==='.source-token-line')return source ? {} : null;
    if(selector==='button')return button ? {matches:()=>button==='cue'} : null;
    return null;
  }});
  assert.equal(dragMode(target(true,'token')),'tokens');
  assert.equal(dragMode(target(true,null)),'tokens');
  assert.equal(dragMode(target(false,'cue')),'cues');
  assert.equal(dragMode(target(false,null)),'cues');
  assert.equal(dragMode(target(false,'delete')),null);
  assert.equal(dragMode(target(true,null,true)),null);
});
test('token marquee selects by rectangle, preserves scroll coordinates and can shrink',()=>{
  const boxes=new Map([['a',{left:20,right:50,top:100,bottom:120}],
    ['b',{left:55,right:80,top:100,bottom:120}],['c',{left:20,right:60,top:200,bottom:220}]]);
  assert.deepEqual(intersectingTokens(boxes,{left:25,right:78,top:105,bottom:115}),['a','b']);
  assert.deepEqual(intersectingTokens(boxes,{left:25,right:78,top:105,bottom:210}),['a','b','c']);
  assert.deepEqual(intersectingTokens(boxes,{left:56,right:78,top:105,bottom:115}),['b']);
});
test('cue range, toggle and additive range preserve the anchor',()=>{
  const s=createSelection(), order=['a','b','c','d','e'];
  s.select('b',order);s.select('d',order,{range:true});assert.deepEqual([...s.ids],['b','c','d']);
  s.select('c',order,{toggle:true});assert.deepEqual([...s.ids],['b','d']);
  s.select('e',order,{range:true,toggle:true});assert.deepEqual(new Set(s.ids),new Set(['b','c','d','e']));
});
test('drag uses full cue order across virtualization and preserves initial additive selection',()=>{
  const s=createSelection(), order=Array.from({length:400},(_,i)=>String(i));
  s.drag('150','310',order,['2']);assert.equal(s.ids.size,162);assert(s.ids.has('250'));
  s.drag('150','151',order,['2']);assert.deepEqual([...s.ids],['2','150','151']);
  s.reconcile(['2','151']);assert.deepEqual([...s.ids],['2','151']);
  s.clear();assert.equal(s.ids.size,0);
});

// Exercise the editor's real event handlers: a pending drag is still a click.
const fs=require('node:fs'), vm=require('node:vm');
const editorSource=fs.readFileSync(require('node:path').join(__dirname,'../web/editor.js'),'utf8');
test('pointerdown preserves focus and playback until a real drag starts',()=>{
  const start=editorSource.indexOf(`  $("#cueList").addEventListener("pointerdown"`);
  const end=editorSource.indexOf("  window.addEventListener('pointermove'",start);
  let handler, prevented=false, followed=true;
  const row={dataset:{cueId:'a'}};
  const list={scrollTop:0,addEventListener(_name,fn){handler=fn;}};
  const ctx={cueCenterGeneration:0,cueDrag:null,suppressCueClick:false,
    state:{followPlayback:true,cueSelection:{ids:new Set()},selectedTokenIds:new Set()},
    window:{EditorCueSelection:{dragMode:()=> 'tokens'}},$:()=>list,stepCueDrag(){},
    suspendPlaybackFollow(){followed=false;}};
  vm.runInNewContext(editorSource.slice(start,end),ctx);
  handler({button:0,pointerId:1,clientX:10,clientY:20,currentTarget:list,
    target:{closest:s=>s==='.cue-row.deleted'?null:row},preventDefault(){prevented=true;}});
  assert.equal(prevented,false);
  assert.equal(ctx.state.followPlayback,true);
  assert.equal(followed,true);
  assert.equal(ctx.cueDrag.moved,false);
});
test('click release does not rerender token controls before the click event',()=>{
  const start=editorSource.indexOf('  function endCueDrag(event)');
  const end=editorSource.indexOf(`  $("#cueList").addEventListener('click'`,start);
  let refreshes=0;
  const ctx={cueDrag:{id:1,startX:10,startY:20,moved:false},suppressCueClick:false,
    $:()=>({classList:{add(){}},hasPointerCapture:()=>false}),
    stepCueDrag(){},cancelAnimationFrame(){},setTimeout(){},refreshTokenSelectionUi(){refreshes++;}};
  vm.runInNewContext(editorSource.slice(start,end),ctx);
  ctx.endCueDrag({type:'pointerup',pointerId:1,clientX:10,clientY:20});
  assert.equal(refreshes,0);
  assert.equal(ctx.suppressCueClick,false);
});
test('cue centering corrects delayed layout but yields to manual scrolling',()=>{
  const start=editorSource.indexOf('  let cueCenterGeneration = 0;');
  const end=editorSource.indexOf('  function activateCue(',start);
  let frames=[], rowPosition=500;
  const list={scrollTop:0,getBoundingClientRect:()=>({top:0,height:400})};
  const row={isConnected:true,getBoundingClientRect:()=>({top:rowPosition-list.scrollTop,height:100})};
  const ctx={state:{activeCueId:'a'},CSS:{escape:x=>x},document:{querySelector:()=>row},
    $:()=>list,requestAnimationFrame:fn=>frames.push(fn)};
  vm.runInNewContext(editorSource.slice(start,end),ctx);
  ctx.centerCueInList({cue_id:'a'});assert.equal(list.scrollTop,350);
  rowPosition=600;frames.shift()();assert.equal(list.scrollTop,450);
  list.scrollTop=250;frames.shift()();assert.equal(list.scrollTop,250);
});
