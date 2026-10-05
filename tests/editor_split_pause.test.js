const test = require("node:test");
const assert = require("node:assert/strict");
const {applyLocalOperation} = require("../web/editor_document_store.js");

function split(deleted = false) {
  const document = {
    source_tokens:[{token_id:"s1",start:0,end:1},{token_id:"s2",start:1.2,end:2},
      {token_id:"s3",start:2.2,end:3}],
    display_tokens:[{token_id:"t1",state:"active",source_token_ids:["s1"]},
      {token_id:"t2",state:deleted?"deleted":"active",source_token_ids:["s2"]},
      {token_id:"t3",state:"active",source_token_ids:["s3"]}],
    cues:[{cue_id:"cue_left",index:0,start:0,end:3,display_token_ids:["t1","t2","t3"],target:null}],
    changes:[],groups:[]
  };
  return applyLocalOperation({document}, {type:"split_cue",operation_id:"split",
    payload:{cue_id:"cue_left",after_token_id:deleted?"t2":"t1",right_cue_id:"cue_right"}}).document;
}
test("optimistic split preserves silence", () => {
  const d=split(); assert.equal(d.cues[0].end,1); assert.equal(d.cues[1].start,1.2);
});
test("optimistic split excludes deleted timings", () => {
  const d=split(true); assert.equal(d.cues[0].end,1); assert.equal(d.cues[1].start,2.2);
});
