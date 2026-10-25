const test = require('node:test');
const assert = require('node:assert/strict');
require('../web/editor_thumbnails.js');

test('thumbnail requests bind after project selection and reject the previous project response', async () => {
  const previous = {fetch:global.fetch, localStorage:global.localStorage};
  const requests = [];
  global.localStorage = {getItem:()=>null, setItem(){}};
  global.fetch = (url, options) => new Promise(resolve => requests.push({url, options, resolve}));
  const elements = [];
  const element = () => {
    const result = {style:{}, children:[], append(...values){this.children.push(...values);}, setAttribute(){}, remove(){}};
    elements.push(result); return result;
  };
  const doc = {createElement:element, createTextNode:text=>({textContent:text})};
  const control = global.EditorThumbnails.create({doc,settingsPanel:element(),url:'/api/projects//thumbnails',onLayout(){},onPaint(){}});
  try {
    assert.equal(requests.length, 0);
    control.setUrl('/api/projects/first/thumbnails');
    assert.equal(requests[0].url, '/api/projects/first/thumbnails');
    control.setUrl('/api/projects/second/thumbnails');
    assert.equal(requests[0].options.signal.aborted, true);
    requests[1].resolve({ok:true,json:async()=>({state:'idle',requested:false,revision:'second'})});
    await new Promise(setImmediate);
    requests[0].resolve({ok:true,json:async()=>({state:'ready',requested:true,revision:'first',interval:.5})});
    await new Promise(setImmediate);
    assert.equal(control.enabled, false, 'a stale project must not turn on thumbnails in the new project');
    assert.ok(elements.some(el=>el.textContent === '尚未生成画面预览图'));
  } finally {
    control.close();
    global.fetch = previous.fetch; global.localStorage = previous.localStorage;
  }
});
