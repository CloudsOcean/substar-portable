import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {installPcmWaveformPaint,reducePcmSpikes} from '../web/editor_maw_pcm_paint.js';
const require=createRequire(import.meta.url);
globalThis.EditorMultilineTimeline=require('../web/editor_multiline_timeline.js');
globalThis.window={devicePixelRatio:1};
function setup(loadWindow){
 const controller=new AbortController(),rects=[];
 const paths=[];let fills=0;
 const ctx={save(){},restore(){},rect(){},clip(){},closePath(){},fill(){fills++;},quadraticCurveTo:(...v)=>paths.push(v),fillRect:(...v)=>rects.push(v),beginPath(){},moveTo(){},lineTo(){},stroke(){}};
 let native=0,renders=0;
 const wave={durationMs:2000,settings:{mode:'multi',waveformScale:1},paintWaveRow(){native++;},_getWaveColors:()=>({rowBg:'#222',rowBorder:'#333'}),drawWordChips(){},render(){renders++;}};
 let source='pcm';installPcmWaveformPaint(wave,{loadWindow,getSource:()=>source,signal:controller.signal});
 return {wave,ctx,rects,paths,fills:()=>fills,controller,source:value=>source=value,native:()=>native,renders:()=>renders};
}
test('uses original PCM window and lightly reduced display spikes, deduplicating row loads',async()=>{
 let requests=[];const payload={window_start:0,window_end:2,peaks:[.25,1,.5,0]};
 const h=setup(async request=>{requests.push(request);return payload;});
 h.wave.paintWaveRow({},h.ctx,4,180,0,2000,0,4);h.wave.paintWaveRow({},h.ctx,4,180,0,2000,0,4);
 await new Promise(r=>setImmediate(r));
 assert.deepEqual(requests,[{start:0,end:2,points:4096}]);assert.equal(h.renders(),1);
 h.rects.length=0;h.wave.paintWaveRow({},h.ctx,4,180,0,2000,0,4);
 assert.equal(h.rects.length,1);assert.ok(h.fills()>0);assert.ok(h.paths.length>0);assert.ok(h.paths.every(p=>p[1]>=2&&p[1]<=178&&p[3]>=2&&p[3]<=178));
 assert.deepEqual(payload.peaks,[.25,1,.5,0]);
 h.source('reapeaks');h.wave.paintWaveRow({},h.ctx,4,180,0,2000,0,4);assert.equal(h.native(),1);
});
test('late PCM response cannot redraw a destroyed editor',async()=>{
 let complete;const h=setup(()=>new Promise(r=>complete=r));
 h.wave.paintWaveRow({},h.ctx,4,180,0,2000,0,4);await Promise.resolve();h.controller.abort();
 complete({peaks:[1]});await new Promise(r=>setImmediate(r));assert.equal(h.renders(),0);
});

test('smoothing rounds isolated spikes, preserves flat envelopes and leaves source data intact',()=>{
 const source=[0,0,0,0,0,1,0,0,0,0,0],before=[...source],smooth=reducePcmSpikes(source);
 assert.deepEqual(source,before);assert.ok(smooth[5]>0&&smooth[5]<=.5);
 assert.ok(smooth[4]>0&&smooth[6]>0);assert.equal(smooth[0],0);assert.equal(smooth[10],0);
 assert.deepEqual(smooth,[...smooth].reverse());
 assert.deepEqual(reducePcmSpikes([1,1,1,1,1]),[1,1,1,1,1]);
 assert.deepEqual(reducePcmSpikes([0,0,0]),[0,0,0]);
});
