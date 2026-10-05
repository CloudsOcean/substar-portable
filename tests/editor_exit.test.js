const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../web/editor.js'), 'utf8');
const exitCode = source.slice(source.indexOf('  let exitingEditor = false;'), source.indexOf('  let idleRevisionCheck'));

function setup(flush) {
  const events = [], errors = [];
  let timeout;
  const context = vm.createContext({state:{revision:{}}, flushActiveEditorInput:flush,
    activeMedia:()=>({pause:()=>events.push('pause')}), window:{location:{assign:url=>events.push(url)}},
    ordinaryError:message=>errors.push(message), setTimeout:callback=>{timeout=callback;return 1;},clearTimeout(){}});
  vm.runInContext(exitCode,context);
  const link = {href:'/split',textContent:'退出'};
  const event = {currentTarget:link,button:0,preventDefault:()=>events.push('prevent')};
  return {context,events,errors,link,event,timeout:()=>timeout()};
}

test('exit waits for save confirmation before navigating', async()=>{
  let acknowledge;
  const pending = new Promise(resolve=>acknowledge=resolve);
  const s=setup(()=>pending);
  const exit=s.context.exitEditor(s.event);
  assert.deepEqual(s.events,['prevent']);
  await s.context.exitEditor(s.event);
  acknowledge(); await exit;
  assert.deepEqual(s.events,['prevent','prevent','pause','/split']);
  assert.equal(s.link.textContent,'退出');
});

test('save failure retains editor and releases exit button',async()=>{
  const s=setup(async()=>{throw Error('conflict');});
  await s.context.exitEditor(s.event);
  assert.deepEqual(s.events,['prevent']);
  assert.match(s.errors[0],/conflict/);
  assert.equal(s.link.textContent,'退出');
});

test('slow save times out without navigating or discarding the save',async()=>{
  const s=setup(()=>new Promise(()=>{}));
  const exit=s.context.exitEditor(s.event);
  s.timeout(); await exit;
  assert.deepEqual(s.events,['prevent']);
  assert.equal(s.link.textContent,'退出');
  assert.match(s.errors[0],/保存响应较慢/);
});

test('modified link click keeps native browser behavior',async()=>{
  const s=setup(async()=>{throw Error('should not save');});
  await s.context.exitEditor({...s.event,ctrlKey:true});
  assert.deepEqual(s.events,[]);
});

test('production workspace works without experimental URL suffix',()=>{
  const script=fs.readFileSync(require.resolve('../web/editor_experimental.js'),'utf8');
  const initialization=script.slice(script.indexOf('  // This workspace'),script.indexOf('  function installWorkspaceLayouts'));
  for (const url of ['http://localhost/editor?project=example','http://localhost/editor?project=example&layout=experimental']) {
    let rewritten;
    const context=vm.createContext({URL,document:{documentElement:{dataset:{}}},location:{href:url},history:{replaceState:(_,__,target)=>rewritten=String(target)}});
    vm.runInContext(initialization,context);
    assert.equal(context.document.title,'Substar · 字幕编辑器');
    assert.equal(context.document.documentElement.dataset.editorLayout,'experimental');
    if(url.includes('layout='))assert.equal(rewritten,'http://localhost/editor?project=example');
  }
});
