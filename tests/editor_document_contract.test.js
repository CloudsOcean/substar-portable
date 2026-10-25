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
    schema_version:"substar.editor-document.v2",
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

test('revision consumption preserves navigation metadata and edits guard the text actually seen',()=>{
  const {consumeRevision,setTargetOperation}=require('../web/editor_document.js');
  const provenance={operation:'restore_revision',metadata:{undo_revision_ids:[],redo_revision_ids:['future']}};
  const revision=consumeRevision({revision_id:'r',revision_number:2,document_hash:'hash',provenance,
    document:documentWithGroupOrigin('segmentation')});
  assert.deepEqual(revision.provenance,provenance);
  const operation=setTargetOperation(revision,'cue_1','my draft',null,{expected_target_text:'text when editing began'});
  assert.equal(operation.payload.expected_target_text,'text when editing began');
});

test('script projection keeps canonical conflict guards through optimistic edits',()=>{
  const {consumeRevision,replaceOperation,setTargetOperation}=require('../web/editor_document.js');
  const {applyLocalOperation}=require('../web/editor_document_store.js');
  const document=documentWithGroupOrigin('segmentation');
  document.display_tokens[0].text='網絡';document.display_tokens[0].canonical_text='网络';
  document.cues[0].target={target_text:'滑鼠',canonical_target_text:'鼠标',original_text:'鼠标',
    language:'zh-CN',translation_status:'translated',provenance:provenance()};
  const revision=consumeRevision({revision_id:'r',revision_number:1,document_hash:'hash',document});
  const operation=replaceOperation(revision,'display_1','新的網絡');
  assert.equal(operation.payload.expected_text,'网络');
  assert.equal(setTargetOperation(revision,'cue_1','新的滑鼠').payload.expected_target_text,'鼠标');
  const projected=applyLocalOperation(revision,operation);
  assert.equal(replaceOperation(projected,'display_1','下一次编辑').payload.expected_text,'新的網絡');
});

test("canonical segmentation groups are accepted by the editor", () => {
  assert.doesNotThrow(() => validateDocument(documentWithGroupOrigin("segmentation")));
});

test("historical stage labels are rejected by the canonical editor", () => {
  assert.throws(() => validateDocument(documentWithGroupOrigin("stage1")), /origin is unsupported/);
});

test("guaranteed-delivery manual translations may remain blank and editable", () => {
  const document = documentWithGroupOrigin("segmentation");
  document.cues[0].target = {
    target_text:"",
    original_text:"",
    language:"zh-CN",
    translation_status:"manual_required",
    issue_code:"translation_unresolved",
    editable:true,
    provenance:provenance(),
  };
  assert.doesNotThrow(() => validateDocument(document));
});

test("ordinary translated tracks must still contain text", () => {
  const document = documentWithGroupOrigin("segmentation");
  document.cues[0].target = {
    target_text:"",
    original_text:"",
    language:"zh-CN",
    translation_status:"translated",
    issue_code:null,
    editable:true,
    provenance:provenance(),
  };
  assert.throws(() => validateDocument(document), /target_text must be non-empty text/);
});

for (const status of ["translated", "needs_review"]) {
  test(`intentional omitted filler is accepted for ${status} only with explicit metadata`, () => {
    const document = documentWithGroupOrigin("segmentation");
    document.cues[0].target = {target_text:"", language:"zh-CN", translation_status:status,
      issue_code:"review", provenance:{...provenance(),metadata:{omitted_filler:true}}};
    assert.doesNotThrow(() => validateDocument(document));
    document.cues[0].target.provenance.metadata.omitted_filler = "true";
    assert.throws(() => validateDocument(document), /non-empty/);
  });
}
