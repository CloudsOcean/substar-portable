(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorCueTimeController = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createCueTimeController(options) {
    const contract = options?.contract;
    const getRevision = options?.getRevision;
    const sendOperation = options?.sendOperation;
    const provenance = options?.provenance;
    if (!contract?.setCueTimeOperation || !contract?.setCueTimesOperation) {
      throw new Error("Cue time operation contract is required");
    }
    if (typeof getRevision !== "function" || typeof sendOperation !== "function") {
      throw new Error("Cue time controller requires revision and operation adapters");
    }

    const operationProvenance = name =>
      typeof provenance === "function" ? provenance(name) : null;

    async function commitCueTime(cueId, start, end, operation = "set_cue_time") {
      const revision = getRevision();
      if (!revision) return null;
      return sendOperation(contract.setCueTimeOperation(
        revision, cueId, Number(start), Number(end), operationProvenance(operation)
      ));
    }

    async function commitCueTimes(changes, operation = "set_cue_times") {
      const revision = getRevision();
      if (!revision || !Array.isArray(changes) || !changes.length) return null;
      return sendOperation(contract.setCueTimesOperation(
        revision, changes, operationProvenance(operation)
      ));
    }

    function rangesFromIntent(intent, cues) {
      const cueById = new Map((cues || []).map(cue => [cue.cue_id, cue]));
      const ranges = new Map();
      (intent?.changes || []).forEach(change => {
        const cue = cueById.get(change.cue_id);
        if (!cue || !new Set(["start", "end"]).has(change.edge)) return;
        const current = ranges.get(cue.cue_id) || {
          cue_id:cue.cue_id, start:Number(cue.start), end:Number(cue.end)
        };
        current[change.edge] = Number(change.time);
        ranges.set(cue.cue_id, current);
      });
      return [...ranges.values()];
    }

    async function commitBoundaryIntent(intent, cues) {
      const ranges = rangesFromIntent(intent, cues);
      if (ranges.length === 1) {
        const range = ranges[0];
        return commitCueTime(range.cue_id, range.start, range.end, "timeline_boundary");
      }
      return commitCueTimes(ranges, "timeline_shared_boundary");
    }

    async function commitAutoSnap(intent, cues) {
      const beforeRevisionId = getRevision()?.revision_id || null;
      const revision = await commitCueTimes(
        rangesFromIntent(intent, cues), "auto_snap_once"
      );
      return {revision, beforeRevisionId};
    }

    return {
      commitCueTime,
      commitCueTimes,
      commitBoundaryIntent,
      commitAutoSnap,
      rangesFromIntent
    };
  }

  return {createCueTimeController};
});
