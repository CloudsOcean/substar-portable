(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SubstarExternalReview = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CONTEXT_CUES = 5;

  function activeCues(cues) {
    return (cues || []).filter(cue => cue?.state === "active");
  }

  function parseRange(expression) {
    const input = String(expression || "").trim();
    if (!input) throw new Error("请输入要审阅的 Cue 编号或区间");
    const normalized = input
      .replace(/[，、；]/g, ",")
      .replace(/[–—－~～]/g, "-")
      .replace(/\s*-\s*/g, "-");
    const parts = normalized.split(/[\s,;]+/).filter(Boolean);
    const numbers = new Set();
    for (const part of parts) {
      const match = part.match(/^(\d+)(?:-(\d+))?$/);
      if (!match) throw new Error(`无法识别范围“${part}”`);
      const start = Number(match[1]);
      const end = Number(match[2] || match[1]);
      if (start < 1 || end < 1) throw new Error("Cue 编号必须从 1 开始");
      if (end < start) throw new Error(`范围“${part}”的结束编号不能小于开始编号`);
      if (end - start > 100000) throw new Error(`范围“${part}”过大`);
      for (let number = start; number <= end; number += 1) numbers.add(number);
    }
    return numbers;
  }

  function resolveScope(cues, scope, currentCueId, selectedCueIds, rangeExpression) {
    const active = activeCues(cues);
    if (!active.length) throw new Error("当前工程没有可审阅的 Cue");
    if (scope === "full") return {cues:active, focusIds:new Set(active.map(cue => cue.cue_id))};

    const requestedNumbers = scope === "range" ? parseRange(rangeExpression) : null;
    const wanted = scope === "range"
      ? new Set(active.filter(cue => requestedNumbers.has(Number(cue.index) + 1)).map(cue => cue.cue_id))
      : scope === "selected"
      ? new Set(selectedCueIds || [])
      : new Set(currentCueId ? [currentCueId] : []);
    const focusPositions = active.flatMap((cue, index) => wanted.has(cue.cue_id) ? [index] : []);
    if (!focusPositions.length) {
      if (scope === "range") throw new Error("指定范围内没有可审阅的 Cue");
      throw new Error(scope === "selected" ? "请先在编辑区选择要审阅的字幕" : "请先选择当前 Cue");
    }
    const includedPositions = new Set();
    focusPositions.forEach(position => {
      const start = Math.max(0, position - CONTEXT_CUES);
      const end = Math.min(active.length, position + CONTEXT_CUES + 1);
      for (let index = start; index < end; index += 1) includedPositions.add(index);
    });
    return {
      cues:active.filter((_, index) => includedPositions.has(index)),
      focusIds:new Set(active.filter(cue => wanted.has(cue.cue_id)).map(cue => cue.cue_id))
    };
  }

  function clock(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const remainder = (value % 60).toFixed(3).padStart(6, "0");
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${remainder}`;
  }

  function build(options) {
    const resolved = resolveScope(
      options.cues, options.scope, options.currentCueId, options.selectedCueIds, options.rangeExpression
    );
    const instruction = String(options.instruction || "").trim();
    const units = new Map();
    const cuesById = new Map((options.cues || []).map(cue => [cue.cue_id, cue]));
    const lines = [
      "# 字幕审阅任务",
      "",
      "请审阅下面的字幕。Cue 是源文的排版与时间单位，上下行允许一对多、多对一和多对多映射。独立下行以译文单元为准。若多条 Cue 引用同一个单元，它在所列时段只显示一次。判断漏译、错位和重复时，请比对整个单元的源文依据、译文和显示时段。共享引用本身无需拆分。只对【审阅】Cue 或其关联的译文单元报告有依据的问题。不要改写时间码或 Cue 编号。",
      "相邻且同属一个意义组的原生 Cue 可以各自保留一条相同的下行文字，作为一段连续显示。请按整个意义组审阅。不得仅凭文字相同就报重复，也不要建议跨 Cue 合并字幕块。若认为确有错位或重复，请指出实际的文本或时段证据。",
      instruction ? `\n## 用户补充要求\n${instruction}` : "",
      "",
      "## 字幕内容"
    ].filter(line => line !== "");
    resolved.cues.forEach(cue => {
      const focus = resolved.focusIds.has(cue.cue_id);
      const source = String(options.sourceText?.(cue) || "").trim();
      const target = String(options.targetText?.(cue) || "").trim();
      const references = (cue.target_units || []).filter(unit => unit?.unit_id);
      const targetMetadata = cue.target?.provenance?.metadata || {};
      const mapping = cue.mapping || {};
      const meaningUnit = mapping.meaning_unit_id || targetMetadata.meaning_unit_id || "";
      const evidence = mapping.source_evidence_cue_ids
        || targetMetadata.source_evidence_cue_ids || [];
      lines.push(
        "",
        `### Cue ${Number(cue.index) + 1}【${focus ? "审阅" : "上下文"}】 ${clock(cue.start)} --> ${clock(cue.end)}`,
        `上行：${source || "（空）"}`
      );
      if (references.length) {
        lines.push(`下行引用：${references.map(unit => unit.unit_id).join("、")}`);
        for (const unit of references) units.set(unit.unit_id, unit);
        return;
      }
      lines.push(`下行：${target || "（空）"}`);
      if (meaningUnit) lines.push(`意义组：${meaningUnit}`);
      if (evidence.length) lines.push(`源文依据 Cue：${evidence.join(", ")}`);
    });
    if (units.size) {
      const includedCueIds = new Set(resolved.cues.map(cue => cue.cue_id));
      lines.push("", "## 独立译文单元（每个单元仅列一次）");
      for (const unit of units.values()) {
        const evidence = (unit.source_cue_ids || []).map(id => cuesById.get(id)).filter(Boolean);
        const cueNumbers = evidence.map(cue => Number(cue.index) + 1).join("、");
        lines.push(
          "",
          `### 译文单元 ${unit.unit_id}｜显示时段 ${clock(unit.start)} --> ${clock(unit.end)}`,
          `源文依据 Cue：${cueNumbers || "未提供"}`,
          `下行：${unit.text || "（空）"}`
        );
        for (const cue of evidence) {
          if (!includedCueIds.has(cue.cue_id)) {
            const text = String(options.sourceText?.(cue) || "").trim();
            lines.push(`Cue ${Number(cue.index) + 1}【上下文】上行：${text || "（空）"}`);
          }
        }
      }
    }
    lines.push("", "请按 Cue 编号列出确有依据的问题、理由和建议；涉及独立译文时注明单元 ID。同一单元的问题只报告一次。没有问题时明确说明。", "");
    return {
      text:lines.join("\n"),
      focusNumbers:resolved.cues.filter(c=>resolved.focusIds.has(c.cue_id)).map(c=>Number(c.index)+1),
      cueCount:resolved.cues.length,
      focusCount:resolved.focusIds.size,
      contextCount:resolved.cues.length - resolved.focusIds.size
    };
  }

  return {CONTEXT_CUES, build, parseRange, resolveScope};
});
