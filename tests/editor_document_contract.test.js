"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {validateDocument} = require("../web/editor_document.js");

function provenance() {
  return {
    kind:"import",
    operation:"build_initial_document",
    actor:"segmentation-worker",
    created_at:"2026-08-17T00:00:00.000Z",
    metadata:{}
  };
}

function documentWithGroupOrigin(origin) {
  return {
    schema_version:"substar.editor-document.v1",
    document_id:"doc_contract",
    properties:{complete:false},
    presentation:{
      upper_punctuation:"remove",
      lower_punctuation:"space",
      display_order:"source_above_target"
    },
    source_tokens:[{token_id:"src_1",index:0,text:"Hello",start:0,end:1}],
    display_tokens:[{
      token_id:"display_1",
      text:"Hello",
      original_text:"Hello",
      state:"active",
      source_token_ids:["src_1"],
      provenance:provenance()
    }],
    groups:[{
      group_id:"group_1",
      origin,
      source_group_ids:["semantic_1"],
      execution_block_ids:["block_1"],
      dirty_flags:[],
      provenance:provenance()
    }],
    cues:[{
      cue_id:"cue_1",
      group_id:"group_1",
      index:0,
      state:"active",
      start:0,
      end:1,
      display_token_ids:["display_1"],
      target:null
    }],
    changes:[]
  };
}

test("canonical segmentation groups are accepted by the editor", () => {
  assert.doesNotThrow(() => validateDocument(documentWithGroupOrigin("segmentation")));
});

test("historical stage labels are rejected by the canonical editor", () => {
  assert.throws(() => validateDocument(documentWithGroupOrigin("stage1")), /origin is unsupported/);
});
