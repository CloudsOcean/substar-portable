"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {createOperationQueue} = require("../web/editor_operation_queue.js");
test("flushing a failed edit reports its original cause and retains it for retry", async () => {
  let reject = true;
  const queue = createOperationQueue({base:()=>({revision_id:"r1"}), maxRetries:0,
    sendBatch:async () => { if (reject) throw Object.assign(new Error("original save rejection"),{status:400}); return {}; }});
  const saving = queue.enqueue({operation_id:"op1"});
  const rejected = assert.rejects(saving,/original save rejection/);
  await assert.rejects(queue.flushAndWait(),/original save rejection/);
  await rejected;
  assert.equal(queue.getState().failed,1);
  reject=false;
  queue.retryFailed();
  await queue.flushAndWait();
  assert.equal(queue.getState().hasUnsavedChanges,false);
  queue.destroy();
});
