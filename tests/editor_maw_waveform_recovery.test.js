const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../web/editor_maw_waveform_loader.js'),'utf8');
const createWaveformLoader = vm.runInNewContext(source.replace('export function','function')+';createWaveformLoader',
  {AbortController, Promise, Error, setTimeout, clearTimeout});
const settle = async () => {for(let n=0;n<20;n++)await Promise.resolve();};

test('a transient 404 retries and applies the waveform without a page reload', async () => {
  let calls=0, applied;const states=[];
  const loader=createWaveformLoader({retryDelayMs:1,load:async()=>{if(++calls===1)throw Error('Not Found');return 'peaks';},
    apply:value=>{applied=value;},onState:value=>states.push(value.state)});
  const first=loader.start();assert.equal(loader.start(),first);await first;
  assert.equal(calls,2);assert.equal(applied,'peaks');assert.equal(states.at(-1),'ready');assert.ok(states.includes('retrying'));
});

test('persistent failure stops loading and manual retry can recover', async () => {
  let calls=0,fail=true;const states=[];
  const loader=createWaveformLoader({retries:2,retryDelayMs:1,load:async()=>{calls++;if(fail)throw Error('offline');return 'ok';},
    apply:()=>{},onState:value=>states.push(value.state)});
  await loader.start();assert.equal(calls,3);assert.equal(states.at(-1),'error');
  fail=false;await loader.start();assert.equal(calls,4);assert.equal(states.at(-1),'ready');
});

test('hung requests time out, abort, and cannot apply a late response', async () => {
  let signal,late,applied=0;const states=[];
  const loader=createWaveformLoader({timeoutMs:5,retries:0,load:request=>{signal=request.signal;return new Promise(resolve=>late=resolve);},
    apply:()=>applied++,onState:value=>states.push(value.state)});
  await loader.start();assert.equal(signal.aborted,true);assert.equal(states.at(-1),'error');
  late('stale');await settle();assert.equal(applied,0);
});

test('destroying a controller cancels its request without updating the old UI', async () => {
  let signal,late;const states=[];
  const loader=createWaveformLoader({load:request=>{signal=request.signal;return new Promise(resolve=>late=resolve);},
    apply:()=>assert.fail('stale payload'),onState:value=>states.push(value.state)});
  const pending=loader.start();await settle();loader.dispose();await pending;
  assert.equal(signal.aborted,true);late('stale');await settle();assert.deepEqual(states,['loading']);
});

test('invalid payload exits loading with a useful error',async()=>{
  const states=[];const loader=createWaveformLoader({load:async()=>null,apply:()=>false,retries:0,onState:value=>states.push(value)});
  await loader.start();assert.equal(states.at(-1).state,'error');assert.match(states.at(-1).error.message,/格式无效/);
});
