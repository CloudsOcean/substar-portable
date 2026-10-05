const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const read = name => fs.readFileSync(path.join(__dirname, '..', 'web', name), 'utf8');
const split = read('split.js');
const requests = split.slice(split.indexOf('  function errorMessage('), split.indexOf('  function toast('));

function pollHarness(fetch) {
  let expire, cleared = 0;
  const context = vm.createContext({fetch, AbortController,
    setTimeout(fn) { expire = fn; return 1; },
    clearTimeout() { cleared++; },
  });
  vm.runInContext(requests, context);
  return {poll:context.pollApi, expire:() => expire(), cleared:() => cleared};
}

test('a stalled task request is aborted and a later refresh succeeds', async () => {
  let first = true;
  const harness = pollHarness((_url, {signal}) => {
    if (!first) return Promise.resolve({ok:true, json:async () => ({projects:[]})});
    first = false;
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  });
  const pending = harness.poll('/api/projects');
  harness.expire();
  await assert.rejects(pending, error => error.code === 'POLL_TIMEOUT');
  assert.equal(harness.cleared(), 1);
  assert.deepEqual(await harness.poll('/api/projects'), {projects:[]});
  assert.equal(harness.cleared(), 2);
});

test('timeout also covers response body loading, not just headers', async () => {
  const harness = pollHarness(async (_url, {signal}) => ({ok:true,
    json:() => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')))),
  }));
  const pending = harness.poll('/api/projects');
  await new Promise(resolve => setImmediate(resolve));
  harness.expire();
  await assert.rejects(pending, error => error.code === 'POLL_TIMEOUT');
});

test('play cancellation is quiet, genuine playback errors remain visible', async () => {
  const editor = read('editor.js');
  const fn = editor.slice(editor.indexOf('  async function toggleMediaPlayback('), editor.indexOf('  function isTextEditingContext('));
  const errors = [];
  let failure = Object.assign(new Error('paused while starting'), {name:'AbortError'});
  const media = {paused:true, play:async () => {throw failure;}};
  const context = vm.createContext({activeMedia:() => media, ordinaryError:error => errors.push(error)});
  vm.runInContext(fn, context);
  await context.toggleMediaPlayback();
  assert.equal(errors.length, 0);
  failure = new Error('decode failed');
  await context.toggleMediaPlayback();
  assert.match(errors[0], /decode failed/);
});
