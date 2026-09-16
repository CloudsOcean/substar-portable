const {test} = require('node:test');
const assert = require('node:assert/strict');
const saveAs = require('../web/system_save_as.js');
test('generated exports choose a destination before generating and writing', async () => {
  const events = [];
  const result = await saveAs.saveBlob({suggestedName:'material.txt'}, async () => {
    events.push('generate'); return new Blob(['material']);
  }, {picker:async () => {events.push('picker'); return {name:'material.txt',createWritable:async () => ({write:async blob => events.push(await blob.text()),close:async () => events.push('close')})};}});
  assert.equal(result.cancelled,false);
  assert.deepEqual(events,['picker','generate','material','close']);
});
test('cancelled destination never generates material or resolves package URL', async () => {
  const picker = async () => {throw Object.assign(new Error('cancel'),{name:'AbortError'});};
  const unexpected = async () => {throw new Error('must not run');};
  assert.deepEqual(await saveAs.saveBlob({},unexpected,{picker}),{cancelled:true});
  assert.deepEqual(await saveAs.saveUrl({url:unexpected},{picker,fetch:unexpected}),{cancelled:true});
});
