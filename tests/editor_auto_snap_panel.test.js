const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../web/editor.js'), 'utf8');
const start = source.indexOf('$("#applyAutoSnap").onclick =');
const end = source.indexOf('$("#snapThreshold").oninput', start);

for (const layout of ['experimental', 'classic']) {
  test(`successful snap preserves the ${layout} panel behavior`, async () => {
    const nodes = {
      '#applyAutoSnap': {}, '#autoSnapStatus': {}, '#autoSnapMenu': {open: true},
      '#snapThreshold': {value: 500}, '#forwardSnapPreRoll': {value: 0},
      '#forwardSnapSensitivity': {value: 50},
    };
    const context = {
      $: selector => nodes[selector],
      document: {documentElement: {dataset: {editorLayout: layout}},
        querySelector: selector => ({value: selector.includes('forward') ? 'none' : 'threshold'})},
      state: {revision: {revision_id: 'before'}, operationPending: false,
        timelineController: {autoSnapOnce: () => ({count: 1})}},
      applyTimelineAutoSnap: async () => ({revision_id: 'after'}),
      observeEditorTutorialEvent() {}, renderHeader() {},
      ordinaryError(message) {throw new Error(message);},
    };
    vm.runInNewContext(source.slice(start, end), context);
    await nodes['#applyAutoSnap'].onclick();
    assert.match(nodes['#autoSnapStatus'].textContent, /已调整 1/);
    assert.equal(nodes['#autoSnapMenu'].open, layout === 'experimental');
  });
}
