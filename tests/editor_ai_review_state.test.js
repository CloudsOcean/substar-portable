"use strict";

const assert = require("node:assert/strict");
const {summarize} = require("../web/editor_ai_review_state.js");

assert.deepEqual(summarize(null, "rev_current"), {
  completed:false,
  stale:false,
  issueCount:0,
  status:"等待开始",
  emptyMessage:"开始审阅后，疑似项会显示在这里。",
  buttonLabel:"开始审阅"
});

assert.deepEqual(summarize({
  review_id:"review_1",
  based_on_revision_id:"rev_current",
  issues:[],
  failed_blocks:[]
}, "rev_current"), {
  completed:true,
  stale:false,
  issueCount:0,
  status:"已完成 · 0 项",
  emptyMessage:"审阅完成，未发现疑似项。",
  buttonLabel:"重新审阅"
});

assert.deepEqual(summarize({
  review_id:"review_1",
  based_on_revision_id:"rev_before_translation",
  issues:[],
  failed_blocks:[]
}, "rev_after_translation"), {
  completed:true,
  stale:true,
  issueCount:0,
  status:"已完成 · 0 项 · 版本已变化",
  emptyMessage:"这份审阅已经完成，但结果基于旧版本；请重新审阅当前版本。",
  buttonLabel:"重新审阅"
});

assert.equal(summarize({
  review_id:"review_2",
  based_on_revision_id:"rev_current",
  issues:[{issue_id:"issue_1"}],
  failed_blocks:["block_2"]
}, "rev_current").status, "已完成 · 1 项 · 1 块失败");

assert.deepEqual(summarize({
  review_id:"review_broken",
  based_on_revision_id:"rev_current",
  issues:[],
  failed_blocks:[],
  encoding_error:true
}, "rev_current"), {
  completed:true,
  stale:false,
  issueCount:0,
  status:"历史审阅已损坏",
  emptyMessage:"旧审阅响应存在编码损坏，已停止展示；请重新审阅当前版本。",
  buttonLabel:"重新审阅"
});

assert.equal(summarize(null, "rev_current", true).status, "审阅中");

console.log("editor_ai_review_state: ok");
