const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const adapter=fs.readFileSync(require.resolve('../web/editor_maw_timeline.js'),'utf8');
const input=fs.readFileSync(require.resolve('../web/vendor/maw/editor/media/waveform/input.js'),'utf8');
const factory=vm.runInNewContext(input.replace('export function createWaveformModule','function createWaveformModule')+';createWaveformModule');
const methods=factory({clamp:(n,min,max)=>Math.max(min,Math.min(max,n))});
for(const dragPreview of [false,true])test(`waveform ${dragPreview?'drag preview':'click'} seeks in seconds`,()=>{
  let actual;
  const callback=vm.runInNewContext('('+adapter.match(/seek:([^\n]+),togglePlayback,/)[1]+')',{seek:seconds=>actual=seconds});
  const ctx={options:{seek:callback},pointerTimeMs:()=>12500,updatePlayback(){},durationMs:30000,isMultiMode:()=>false};
  methods.seekFromPointer.value.call(ctx,{}, {},false,null,dragPreview,dragPreview);
  assert.equal(actual,12.5);
});
