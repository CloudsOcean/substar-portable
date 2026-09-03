(function (root, factory) {
  const ordering = root?.EditorCueOrdering
    || (typeof module === "object" && module.exports ? require("./editor_cue_ordering.js") : null);
  const api = factory(ordering);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorDocumentStore = api;
})(typeof globalThis === "object" ? globalThis : this, function (ordering) {
  "use strict";

  if (!ordering?.canonicalCueOrder) throw new Error("Cue ordering contract is required");

  const clone = value => JSON.parse(JSON.stringify(value));

  function localProvenance(operation) {
    const value = clone(operation.payload?.provenance || {});
    return {
      kind:value.kind || "manual",
      operation:value.operation || operation.type,
      actor:value.actor || "editor",
      created_at:value.created_at || new Date().toISOString(),
      metadata:{...(value.metadata || {}), operation_id:operation.operation_id, optimistic:true}
    };
  }

  function sourceBounds(document, displayIds) {
    const displayById = new Map(document.display_tokens.map(token => [token.token_id, token]));
    const sourceById = new Map(document.source_tokens.map(token => [token.token_id, token]));
    const lineage = displayIds.flatMap(id => displayById.get(id)?.source_token_ids || [])
      .map(id => sourceById.get(id)).filter(Boolean);
    if (!lineage.length) return null;
    return [
      Math.min(...lineage.map(token => Number(token.start))),
      Math.max(...lineage.map(token => Number(token.end)))
    ];
  }

  function translationJoinSeparator(left, right) {
    if (!left || !right || /\s$/.test(left) || /^\s/.test(right)) return "";
    if (/^[，。！？；：、,.!?;:)\]}】》」』]/.test(right)) return "";
    if (/[，。！？；：、]$/.test(left)) return "";
    const leftAsciiWord = /[A-Za-z0-9]$/.test(left);
    const rightAsciiWord = /^[A-Za-z0-9]/.test(right);
    if (/[,\.!?;:]$/.test(left) && rightAsciiWord) return " ";
    if (leftAsciiWord && rightAsciiWord) return " ";
    if (/[A-Za-z]$/.test(left) && !/^[\x00-\x7F]/.test(right)) return " ";
    if (!/[\x00-\x7F]$/.test(left) && /^[A-Za-z]/.test(right)) return " ";
    return "";
  }

  function joinTranslationText(leftInput, rightInput) {
    const left = String(leftInput || "").trimEnd();
    const right = String(rightInput || "").trimStart();
    return `${left}${translationJoinSeparator(left, right)}${right}`;
  }

  function applyLocalOperation(revision, operation) {
    const next = clone(revision);
    const document = next.document;
    const payload = operation.payload || {};
    const tokenById = new Map(document.display_tokens.map(token => [token.token_id, token]));
    const cueById = new Map(document.cues.map(cue => [cue.cue_id, cue]));
    const provenance = localProvenance(operation);
    switch (operation.type) {
      case "replace": {
        const token = tokenById.get(String(payload.token_id));
        if (token) {
          token.text = String(payload.text || token.text);
          token.provenance = provenance;
        }
        break;
      }
      case "batch_replace":
        (payload.replacements || []).forEach(item => {
          const token = tokenById.get(String(item.token_id));
          if (token) {
            token.text = String(item.text || token.text);
            token.provenance = provenance;
          }
        });
        break;
      case "set_target": {
        const cue = cueById.get(String(payload.cue_id));
        if (cue) {
          const text = String(payload.target_text || "").trim();
          cue.target = text ? {
            target_text:text,
            original_text:String(payload.original_text || cue.target?.original_text || text),
            language:String(payload.language || cue.target?.language || "zh-CN"),
            translation_status:"translated",
            issue_code:null,
            editable:true,
            provenance
          } : null;
        }
        break;
      }
      case "set_cue_speaker": {
        const cue = cueById.get(String(payload.cue_id));
        if (cue) cue.speaker = payload.speaker ?? null;
        break;
      }
      case "set_speaker_names":
        document.properties.speaker_names = {...(payload.speaker_names || {})};
        break;
      case "set_cue_time": {
        const cue = cueById.get(String(payload.cue_id));
        if (cue) {
          cue.start = Number(payload.start);
          cue.end = Number(payload.end);
        }
        break;
      }
      case "set_cue_times":
        (payload.cues || []).forEach(item => {
          const cue = cueById.get(String(item.cue_id));
          if (cue) {
            cue.start = Number(item.start);
            cue.end = Number(item.end);
          }
        });
        break;
      case "split_cue": {
        const cue = cueById.get(String(payload.cue_id));
        const position = cue?.display_token_ids.indexOf(String(payload.after_token_id)) ?? -1;
        if (!cue || position < 0 || position >= cue.display_token_ids.length - 1) break;
        const originalEnd = Number(cue.end);
        const leftIds = cue.display_token_ids.slice(0, position + 1);
        const rightIds = cue.display_token_ids.slice(position + 1);
        const leftBounds = sourceBounds(document, leftIds);
        const rightBounds = sourceBounds(document, rightIds);
        let boundary = leftBounds && rightBounds
          ? (leftBounds[1] + rightBounds[0]) / 2
          : (Number(cue.start) + originalEnd) / 2;
        boundary = Math.min(Math.max(boundary, Number(cue.start) + 0.001), originalEnd - 0.001);
        const copiedTarget = cue.target ? {
          ...clone(cue.target),
          provenance:{...provenance, operation:"split_cue_translation_copy"}
        } : null;
        cue.display_token_ids = leftIds;
        cue.end = boundary;
        cue.target = copiedTarget;
        const right = {
          ...clone(cue),
          cue_id:String(payload.right_cue_id),
          index:Number(cue.index) + 1,
          display_token_ids:rightIds,
          start:boundary,
          end:originalEnd,
          target:copiedTarget ? clone(copiedTarget) : null
        };
        const cueIndex = document.cues.findIndex(item => item.cue_id === cue.cue_id);
        document.cues.splice(cueIndex + 1, 0, right);
        break;
      }
      case "merge_cues": {
        const ids = (payload.cue_ids || []).map(String);
        const left = cueById.get(ids[0]);
        const right = cueById.get(ids[1]);
        const leftIndex = document.cues.findIndex(cue => cue.cue_id === ids[0]);
        if (!left || !right || document.cues[leftIndex + 1]?.cue_id !== right.cue_id) break;
        const targets = [left.target, right.target].filter(Boolean);
        let target = null;
        if (targets.length === 1) target = clone(targets[0]);
        else if (targets.length === 2) {
          const leftGroup = targets[0].provenance?.metadata?.translation_copy_group;
          const copied = leftGroup
            && leftGroup === targets[1].provenance?.metadata?.translation_copy_group
            && targets[0].target_text === targets[1].target_text
            && targets[0].original_text === targets[1].original_text
            && targets[0].language === targets[1].language;
          target = copied ? clone(targets[0]) : {
            ...clone(targets[0]),
            target_text:joinTranslationText(targets[0].target_text, targets[1].target_text),
            original_text:joinTranslationText(
              targets[0].original_text || targets[0].target_text,
              targets[1].original_text || targets[1].target_text
            ),
            provenance
          };
        }
        left.display_token_ids = [...left.display_token_ids, ...right.display_token_ids];
        left.end = Number(right.end);
        left.target = target;
        left.speaker = left.speaker === right.speaker ? left.speaker : null;
        document.cues.splice(leftIndex + 1, 1);
        break;
      }
      case "delete":
      case "restore": {
        const state = operation.type === "delete" ? "deleted" : "active";
        new Set((payload.token_ids || []).map(String)).forEach(id => {
          const token = tokenById.get(id);
          if (token) token.state = state;
        });
        new Set((payload.cue_ids || []).map(String)).forEach(id => {
          const cue = cueById.get(id);
          if (cue) cue.state = state;
        });
        break;
      }
      default:
        // Topology-changing operations are projected by their focused editor control
        // until the authoritative delta supplies server-stable entity IDs.
        return next;
    }
    document.cues = ordering.canonicalCueOrder(document.cues);
    document.changes.push(provenance);
    return next;
  }

  function createDocumentStore(options = {}) {
    let acknowledged = null;
    let projected = null;
    let pending = [];
    let activeCueId = null;
    let selectedCueId = null;
    let selectedTokenIds = [];
    const reducer = options.applyLocalOperation || applyLocalOperation;

    function replay() {
      projected = acknowledged ? pending.reduce(reducer, clone(acknowledged)) : null;
      options.onChange?.(snapshot());
      return projected;
    }

    function snapshot() {
      return {
        acknowledged,
        projected,
        pending:[...pending],
        activeCueId,
        selectedCueId,
        selectedTokenIds:[...selectedTokenIds]
      };
    }

    return Object.freeze({
      reset(revision) {
        acknowledged = revision;
        pending = [];
        return replay();
      },
      replaceAcknowledged(revision) {
        acknowledged = revision;
        return replay();
      },
      enqueue(operation) {
        if (!pending.some(item => item.operation_id === operation.operation_id)) {
          pending.push(operation);
        }
        return replay();
      },
      restore(operations) {
        operations.forEach(operation => {
          if (!pending.some(item => item.operation_id === operation.operation_id)) {
            pending.push(operation);
          }
        });
        return replay();
      },
      acknowledge(revision, operationIds) {
        const ids = new Set((operationIds || []).map(String));
        acknowledged = revision;
        pending = pending.filter(operation => !ids.has(operation.operation_id));
        return replay();
      },
      discard(operationIds) {
        const ids = new Set((operationIds || []).map(String));
        pending = pending.filter(operation => !ids.has(operation.operation_id));
        return replay();
      },
      setSelection(value = {}) {
        if (Object.hasOwn(value, "activeCueId")) activeCueId = value.activeCueId;
        if (Object.hasOwn(value, "selectedCueId")) selectedCueId = value.selectedCueId;
        if (Object.hasOwn(value, "selectedTokenIds")) selectedTokenIds = [...value.selectedTokenIds];
        options.onChange?.(snapshot());
      },
      acknowledged:() => acknowledged,
      projection:() => projected,
      pending:() => [...pending],
      snapshot
    });
  }

  return Object.freeze({createDocumentStore, applyLocalOperation});
});
