import test from 'node:test';
import assert from 'node:assert/strict';
import {installSubstarWaveformPaint,softenWaveEnvelope} from '../web/editor_maw_paint.js';

test('waveform fills the row behind subtitle lanes instead of stopping at their upper edge',()=>{
  globalThis.window={devicePixelRatio:1};
  const rectangles=[];
  const points=[];let fills=0;
  const ctx={save(){},restore(){},rect(){},clip(){},closePath(){},fill(){fills++;},quadraticCurveTo:(...args)=>points.push(args),fillRect:(...args)=>rectangles.push(args),beginPath(){},moveTo(){},lineTo(){},stroke(){}};
  let chips=0;
  const wave={settings:{waveformScale:1},_getWaveColors:()=>({rowBg:'#222',rowBorder:'#333'}),activeWaveShape:()=>({peaks:new Int8Array([-127,127,-127,127]),peaksPerSecond:2,peakCount:2}),drawWordChips(){chips++;}};
  installSubstarWaveformPaint(wave);
  const row={querySelectorAll(){throw Error('waveform extent must not depend on subtitle positions');}};
  wave.paintWaveRow(row,ctx,100,200,0,1000,0,100,{withChips:false});
  assert.equal(rectangles.length,1);
  assert.equal(fills,1);
  assert.ok(points.some(p=>p[1]===28));
  assert.ok(points.some(p=>p[1]===194));
  assert.equal(chips,0);
});

test('display softening retains transients and silence without changing source peaks',()=>{
 const input=[0,0,0,1,0,0,0],before=[...input],soft=softenWaveEnvelope(input);
 assert.deepEqual(input,before);
 assert.ok(soft[3]>=.8);
 assert.equal(soft[0],0);assert.equal(soft[1],0);assert.equal(soft[5],0);assert.equal(soft[6],0);
 assert.ok(soft[2]>0&&soft[4]>0);
 assert.deepEqual(softenWaveEnvelope([.4,.4,.4]),[.4,.4,.4]);
});
