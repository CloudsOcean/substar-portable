import test from 'node:test';
import assert from 'node:assert/strict';
import {lightSmoothColumns,installNativeWaveformStyle,suppressLowEnergy} from '../web/editor_maw_native_style.js';
test('light smoothing preserves strong transients, flat sections, timing and raw cache',()=>{
 const raw=new Float32Array([0,0,100,0,0]),copy=raw.slice();
 assert.deepEqual([...lightSmoothColumns(raw)],[0,6.25,87.5,6.25,0]);assert.deepEqual(raw,copy);
 assert.deepEqual([...lightSmoothColumns([30,30,30])],[30,30,30]);
 const column={low:Float32Array.from(raw,v=>-v),high:raw,columnMs:2.5,coverStartMs:0};
 const wave={_getWaveColors:()=>({peak:'#aaa',rowBg:'#222'}),getWaveformColumnEnvelope:()=>column};
 installNativeWaveformStyle(wave);
 const shown=wave.getWaveformColumnEnvelope();assert.equal(shown.columnMs,2.5);assert.equal(shown.coverStartMs,0);
 assert.equal(shown,wave.getWaveformColumnEnvelope());assert.deepEqual(raw,copy);
 assert.equal(wave._getWaveColors().peak,'#69b49c');assert.equal(wave._getWaveColors().rowBg,'#222');
});

test('absolute display mirrors the stronger polarity without changing raw peaks or time mapping',()=>{
 const original={low:new Float32Array([-100,-20,0]),high:new Float32Array([10,80,0]),columnMs:2,coverStartMs:30000};
 const wave={_getWaveColors:()=>({}),getWaveformColumnEnvelope:()=>original};
 installNativeWaveformStyle(wave);const shown=wave.getWaveformColumnEnvelope();
 assert.deepEqual([...shown.high],[98.75,76.25,5]);
 shown.high.forEach((v,i)=>assert.equal(shown.low[i],-v));
 assert.deepEqual([...original.low],[-100,-20,0]);assert.deepEqual([...original.high],[10,80,0]);
 assert.equal(shown.columnMs,2);assert.equal(shown.coverStartMs,30000);
});
test('low energy display suppression defaults off, preserves silence/full scale, and refreshes cached display',()=>{
 for(const v of [0,.1,.5,1])assert.equal(suppressLowEnergy(v,0),v);
 assert.equal(suppressLowEnergy(0,1),0);assert.equal(suppressLowEnergy(1,1),1);
 assert.ok(suppressLowEnergy(.1,.5)<.1);assert.ok(suppressLowEnergy(.1,1)<suppressLowEnergy(.1,.5));
 const original={low:Float32Array.of(-20,-20),high:Float32Array.of(20,20)};let strength=0;
 const wave={_getWaveColors:()=>({}),getWaveformColumnEnvelope:()=>original};
 installNativeWaveformStyle(wave,{getSuppression:()=>strength});const normal=wave.getWaveformColumnEnvelope();strength=.5;
 const reduced=wave.getWaveformColumnEnvelope();assert.ok(reduced.high[0]<normal.high[0]);assert.equal(original.high[0],20);
 strength=0;assert.equal(wave.getWaveformColumnEnvelope().high[0],normal.high[0]);
});
