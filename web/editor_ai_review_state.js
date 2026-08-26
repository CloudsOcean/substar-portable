(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorAiReviewState = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function summarize(review, currentRevisionId, running = false) {
    const issues = Array.isArray(review?.issues) ? review.issues : [];
    const failedBlocks = Array.isArray(review?.failed_blocks) ? review.failed_blocks : [];
    const completed = Boolean(String(review?.review_id || "").trim());
    const basedOn = String(review?.based_on_revision_id || "").trim();
    const current = String(currentRevisionId || "").trim();
    const stale = completed && Boolean(basedOn && current && basedOn !== current);

    if (running) {
      return {
        completed,
        stale,
        issueCount:issues.length,
        status:"审阅中",
        emptyMessage:"正在审阅当前文稿…",
        buttonLabel:"正在审阅…"
      };
    }
    if (!completed) {
      return {
        completed:false,
        stale:false,
        issueCount:0,
        status:"等待开始",
        emptyMessage:"开始审阅后，疑似项会显示在这里。",
        buttonLabel:"开始审阅"
      };
    }

    const suffixes = [];
    if (failedBlocks.length) suffixes.push(`${failedBlocks.length} 块失败`);
    if (stale) suffixes.push("版本已变化");
    const status = [`已完成 · ${issues.length} 项`, ...suffixes].join(" · ");
    let emptyMessage = "审阅完成，未发现疑似项。";
    if (failedBlocks.length) {
      emptyMessage = `审阅已完成，但有 ${failedBlocks.length} 个执行块失败；未生成疑似项。`;
    }
    if (stale) {
      emptyMessage = "这份审阅已经完成，但结果基于旧版本；请重新审阅当前版本。";
    }
    return {
      completed:true,
      stale,
      issueCount:issues.length,
      status,
      emptyMessage,
      buttonLabel:"重新审阅"
    };
  }

  return Object.freeze({summarize});
});
