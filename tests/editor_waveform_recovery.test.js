const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {createWaveformCache} = require('../web/editor_waveform_cache.js');
const read = name => fs.readFileSync(require.resolve('../web/'+name), 'utf8');
const settle = async () => { for(let i=0;i<12;i++) await Promise.resolve(); };

test('a cleared cache ignores late results and preserves replacement requests',async()=>{
  const cache=createWaveformCache();let oldResolve,newResolve;
  const input={mediaId:'p',start:0,end:10,points:512};
  const old=cache.request({...input,load:()=>new Promise(r=>oldResolve=r)});
  await settle();cache.clear();
  const current=cache.request({...input,load:()=>new Promise(r=>newResolve=r)});
  await settle();oldResolve({peaks:['old']});await old;
  assert.equal(cache.getStats().entries,0);assert.equal(cache.getStats().in_flight,1);
  const duplicate=cache.request({...input,load:()=>{throw Error('duplicate');}});
  newResolve({peaks:['new']});assert.deepEqual(await current,{peaks:['new']});await duplicate;
  assert.equal(cache.getStats().in_flight,0);
});

test('hung waveform requests time out and can be requested again without refresh',async()=>{
  const cache=createWaveformCache({requestTimeoutMs:5});let signal;
  const input={mediaId:'p',start:0,end:10,points:512};
  await assert.rejects(cache.request({...input,load:options=>{signal=options.signal;return new Promise(()=>{});}}),/超时/);
  assert.equal(signal.aborted,true);assert.equal(cache.getStats().in_flight,0);
  assert.deepEqual(await cache.request({...input,load:async()=>({peaks:[.5]})}),{peaks:[.5]});
});

test('failed multiline waveform retries without reload and releases loading state',async()=>{
  const source=read('editor_multiline_timeline.js');
  const code=source.slice(source.indexOf('    function requestPeaks('),source.indexOf('    function drawRow('));
  let attempts=0,redraws=0;const timers=[];
  const c=vm.createContext({generation:1,destroyed:false,waveformRetryTimers:new Set(),
    options:{onWaveformWindow:async()=>{if(++attempts===1)throw Error('404');return {peaks:[.5]};}},
    setTimeout:fn=>{timers.push(fn);return fn;},redraw:()=>redraws++});
  vm.runInContext(code,c);const row={start:0,end:10};
  c.requestPeaks(row,100);await settle();assert.equal(timers.length,1);
  timers.shift()();c.requestPeaks(row,100);await settle();
  assert.deepEqual(row.peaks,[.5]);assert.equal(row.loading,false);assert.equal(attempts,2);
});

test('single timeline waveform retries are bounded and stale failures cannot replace a new request',async()=>{
  const source=read('editor_timeline.js');
  const code=source.slice(source.indexOf('    function requestWaveformWindow('),source.indexOf('    function drawCues('));
  const timers=[];let attempts=0,errors=0;
  const c=vm.createContext({destroyed:false,duration:10,viewStart:0,viewEnd:10,waveform:[],waveformStart:0,waveformEnd:0,
    waveformRequestKey:'',waveformRequestSequence:0,waveformRetryTimer:null,waveformFailureKey:'',waveformFailures:0,
    visibleSpan:()=>10,clamp:(v,a,b)=>Math.min(b,Math.max(a,v)),
    options:{onWaveformWindow:async()=>{attempts++;throw Error('offline');},onWaveformError:()=>errors++},
    setTimeout:fn=>{timers.push(fn);return fn;},clearTimeout(){},draw:()=>c.requestWaveformWindow(500)});
  vm.runInContext(code,c);c.requestWaveformWindow(500);await settle();
  while(timers.length){timers.shift()();await settle();}
  assert.equal(attempts,4);assert.equal(errors,1);
  c.requestWaveformWindow(500);await settle();assert.equal(attempts,4);
});
