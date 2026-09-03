(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SubstarAiProgressSummary = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function count(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
  }

  function summarize(progress, options = {}) {
    if (!progress || typeof progress !== "object") return null;
    const units = progress.units;
    if (!units || typeof units !== "object") return null;
    const planned = count(units.planned);
    const completed = Math.min(planned || count(units.completed), count(units.completed));
    const repairPlanned = count(units.repair_planned);
    const repairCompleted = Math.min(
      repairPlanned || count(units.repair_completed), count(units.repair_completed),
    );
    const problemCount = count(
      options.problemCount ?? progress.problem_count ?? options.problemCueIds?.length,
    );
    return {
      completed, planned, repairCompleted, repairPlanned, problemCount,
      items: [
        {id:"completed", label:"完成", value:planned ? `${completed}/${planned}` : String(completed), tone:completed >= planned && planned > 0 ? "done" : "active"},
        {id:"repair", label:"修复", value:repairPlanned ? `${repairCompleted}/${repairPlanned}` : "0", tone:repairPlanned && repairCompleted < repairPlanned ? "active" : "done"},
        {id:"review", label:"需人工审核", value:String(problemCount), tone:problemCount ? "warning" : "done"},
      ],
    };
  }

  function format(progress, options = {}) {
    const summary = summarize(progress, options);
    if (!summary) return "";
    const units = progress.units || {};
    const unitLabel = ({
      semantic_group:"个意义组",
      calibration_block:"个校准块",
      segmentation_block:"块",
    })[String(progress.unit_kind || "")] || ({
      translation:"个意义组",
      calibration:"个校准块",
      segmentation:"块",
    })[String(progress.kind || "")] || String(progress.unit_label || options.unitLabel || "").trim();
    const unit = unitLabel ? ` ${unitLabel}` : "";
    const parts = [];
    const repairOnly = progress.phase === "repair"
      && summary.repairPlanned === summary.planned
      && summary.repairCompleted === summary.completed
      && units.accepted == null;
    if (!repairOnly && summary.planned) {
      const primaryLabel = progress.phase === "executing" ? "模型处理" : "完成";
      parts.push(`${primaryLabel} ${summary.completed}/${summary.planned}${unit}`);
    }
    if (summary.repairPlanned) {
      const repairAccepted = count(units.repair_accepted);
      const repairing = progress.phase === "repair"
        && summary.repairCompleted < summary.repairPlanned;
      parts.push(repairing
        ? `修复中 ${summary.repairCompleted}/${summary.repairPlanned}${unit}`
        : `修复 ${units.repair_accepted == null ? summary.repairCompleted : repairAccepted}/${summary.repairPlanned}${unit}`);
    }
    if (summary.problemCount) parts.push(`需人工审核 ${summary.problemCount} 条`);
    return parts.join(" · ");
  }

  return { summarize, format };
});
