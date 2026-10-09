const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('web/editor.js', 'utf8');
const start = source.indexOf('  $("#undoAutoMerge").onclick = async () => {');
const end = source.indexOf('\n  };', start) + 5;
function setup(overrides = {}, restored = {}) {
  const state = {projectId:'p',revision:{revision_id:'after'},autoMergeUndo:{projectId:'p',before:'before',after:'after'},...overrides};
  const nodes = {'#undoAutoMerge':{disabled:false},'#autoMergeStatus':{textContent:''}};
  const calls = [];
  vm.runInNewContext(source.slice(start,end), {state,$:id=>nodes[id],restoreRevision:async id=>{calls.push(id);return restored;},renderHeader:()=>{}});
  return {state,nodes,calls,run:nodes['#undoAutoMerge'].onclick};
}
test('merge undo restores only its recorded previous revision', async()=>{const h=setup();await h.run();assert.deepEqual(h.calls,['before']);assert.equal(h.state.autoMergeUndo,null);});
test('merge undo cannot discard a later edit or affect a different project', async()=>{for(const override of [{revision:{revision_id:'later'}},{projectId:'other'}]){const h=setup(override);await h.run();assert.deepEqual(h.calls,[]);}});
test('failed merge undo retains its recovery entry', async()=>{const h=setup({},null);await h.run();assert.equal(h.state.autoMergeUndo.before,'before');});
