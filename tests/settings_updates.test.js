const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function setup(mode, state = 'idle') {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {dataset:{}, textContent:'', hidden:false, disabled:false,
      querySelector:node, addEventListener(){}});
    return nodes.get(selector);
  };
  const calls = [];
  const context = {document:{querySelector:node}, encodeURIComponent, clearTimeout(){}, setTimeout(){},
    fetch:async url => {
      calls.push(url);
      const value = url.includes('/check') ? {available:true, version:'v2.2.8',mode,download_size:1048576,reason:'test',notes:'changes'}
        : url.includes('/identity') ? {instance_id:'test'}
        : {status:state,current_version:'2.2.7',supported:true,downloaded:512,total:1024,mode};
      return {ok:true,json:async()=>value};
    }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/settings_updates.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {node,calls};
}

for (const mode of ['delta','full']) {
  test(`update UI shows ${mode} size and sends explicitly selected mode`, async()=>{
    const {node,calls} = await setup(mode);
    await node('[data-update-check]').onclick();
    assert.match(node('[data-update-status]').textContent,/1\.00 MB/);
    assert.equal(node('[data-update-download]').textContent, mode === 'delta' ? '下载增量更新' : '下载完整更新');
    await node('[data-update-download]').onclick();
    assert.ok(calls.includes('/api/updates/download?mode='+mode));
  });
}

test('verification keeps download and check disabled',async()=>{
  const {node} = await setup('delta','verifying');
  assert.equal(node('[data-update-download]').disabled,true);
  assert.equal(node('[data-update-check]').disabled,true);
  assert.match(node('[data-update-status]').textContent,/正在校验/);
});
