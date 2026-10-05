"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {createOperationQueue} = require("../web/editor_operation_queue.js");

test("one request in flight; retry keeps its batch separate from newly typed edits", async()=>{
  let release; let first=true; let active=0,peak=0;const batches=[];
  const q=createOperationQueue({base:()=>({}),debounceMs:0,preserveUserActions:true,
    sendBatch:async b=>{
      active++;peak=Math.max(peak,active);batches.push(b.operations.map(o=>o.operation_id));
      if(first){first=false;await new Promise(r=>release=r);active--;throw Error('response lost');}
      active--;return {};
    }});
  const a=q.enqueue({operation_id:'a'});const b=q.enqueue({operation_id:'b'});
  const flushing=q.flushNow();
  const c=q.enqueue({operation_id:'c'});
  release();await flushing;await q.flushAndWait();await Promise.all([a,b,c]);
  assert.equal(peak,1);assert.deepEqual(batches,[['a','b'],['a','b'],['c']]);q.destroy();
});

test("changing save delay preserves queued edits and explicit flush bypasses delay", async () => {
  const batches=[];
  const queue=createOperationQueue({base:()=>({}), debounceMs:30000, sendBatch:async b=>{batches.push(b);return {};}});
  const saved=queue.enqueue({operation_id:"delay-edit"});
  assert.equal(batches.length,0);
  queue.configure({debounceMs:10000,maxRetries:0});
  await queue.flushAndWait();
  await saved;
  assert.equal(batches.length,1);
  assert.equal(batches[0].operations[0].operation_id,"delay-edit");
  assert.equal(queue.getState().hasUnsavedChanges,false);
  queue.destroy();
});

test("shortening delay starts automatic saving without losing pending edits", async () => {
  let calls=0;
  const queue=createOperationQueue({base:()=>({}),debounceMs:30000,sendBatch:async()=>{calls++;return {};}});
  const saved=queue.enqueue({operation_id:"auto-edit"});
  queue.configure({debounceMs:0,maxRetries:0});
  await saved;
  assert.equal(calls,1);
  queue.destroy();
});
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
