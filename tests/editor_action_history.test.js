const test = require('node:test');
const assert = require('node:assert/strict');
const {createOperationQueue} = require('../web/editor_operation_queue.js');

test('different user actions within debounce interval remain separate revisions', async () => {
  const sent = [];
  const queue = createOperationQueue({groupByUserAction:true,debounceMs:1000,base:()=>({}),
    sendBatch:async batch => {sent.push(batch.operations.map(o=>o.operation_id));return {};}});
  const a=queue.enqueue({operation_id:'edit-a'}), b=queue.enqueue({operation_id:'edit-b'});
  await queue.flushAndWait(); await Promise.all([a,b]);
  assert.deepEqual(sent,[['edit-a'],['edit-b']]); queue.destroy();
});

test('explicit suboperations belonging to one user action share one revision', async () => {
  const sent=[];
  const queue=createOperationQueue({groupByUserAction:true,debounceMs:1000,base:()=>({}),
    sendBatch:async batch=>{sent.push(batch.operations.length);return {};}});
  const pending=['a','b'].map(operation_id=>queue.enqueue({operation_id,
    payload:{provenance:{metadata:{user_action_id:'one-action'}}}}));
  await queue.flushAndWait();await Promise.all(pending);
  assert.deepEqual(sent,[2]);queue.destroy();
});
