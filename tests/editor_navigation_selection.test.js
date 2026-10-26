const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('web/editor.js','utf8');
const activate=source.slice(source.indexOf('  function activateCue('),source.indexOf('  function selectCue(',source.indexOf('  function activateCue(')));
function setup(){
 const ids=new Set(['old']),state={cueSelection:{get ids(){return ids;},clear(){ids.clear();}},activeCueId:'old'};
 const context={state,currentCue:()=>null,activeMedia:()=>({}),refreshCueSelection(){},renderPreview(){},document:{activeElement:null},$:()=>null};
 vm.runInNewContext(activate+'; this.activate=activateCue;',context);
 return {state,activate:context.activate};
}
test('ordinary navigation clears the old cue selection',()=>{
 const x=setup();x.activate('new',{explicitNavigation:true});assert.equal(x.state.cueSelection.ids.size,0);assert.equal(x.state.activeCueId,'new');
});
test('explicit selection gestures and playback keep a deliberate selection',()=>{
 const x=setup();x.activate('new',{explicitNavigation:true,preserveCueSelection:true});assert.deepEqual([...x.state.cueSelection.ids],['old']);
 x.state.cueSelection.ids.add('second');
 x.activate('playback');assert.deepEqual([...x.state.cueSelection.ids],['old','second']);
});

test('playback moving to another cue clears a stale single selection',()=>{
 const x=setup();x.activate('playback');assert.equal(x.state.cueSelection.ids.size,0);assert.equal(x.state.activeCueId,'playback');
});
