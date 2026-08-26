(function (root, factory) {
  const ordering = root?.EditorCueOrdering
    || (typeof module === "object" && module.exports ? require("./editor_cue_ordering.js") : null);
  const api = factory(ordering);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorDocument = api;
})(typeof globalThis === "object" ? globalThis : this, function (ordering) {
  "use strict";

  if (!ordering?.canonicalCueOrder) throw new Error("Cue ordering contract is required");

  const DOCUMENT_SCHEMA = "substar.editor-document.v1";
  const ACTIVE = "active";
  const DELETED = "deleted";
  const CHANGE_KINDS = new Set(["source", "import", "manual", "ai", "normalization"]);
  const OPERATION_TYPES = new Set([
    "replace", "set_target", "set_cue_time", "set_cue_times", "insert_cue",
    "merge", "delete", "purge_cue", "restore", "insert", "split_cue", "merge_cues",
    "set_cue_speaker", "set_speaker_names", "batch_replace", "set_ai_calibration"
  ]);
  const consumedRevisions = new WeakSet();
  const editorViewCache = new WeakMap();
  const revisionIndexCache = new WeakMap();

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function stableStringify(value) {
    if (value === null || typeof value !== "object") return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
    const keys = Object.keys(value).sort();
    return `{${keys.map(key => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  }

  function stableHash(value) {
    const text = typeof value === "string" ? value : stableStringify(value);
    let hash = 14695981039346656037n;
    const prime = 1099511628211n;
    const mask = 18446744073709551615n;
    for (const char of text) {
      hash ^= BigInt(char.codePointAt(0));
      hash = (hash * prime) & mask;
    }
    return hash.toString(36).padStart(13, "0");
  }

  function requireObject(value, label) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`${label} must be an object`);
    }
    return value;
  }

  function requireText(value, label) {
    if (typeof value !== "string" || !value.trim()) {
      throw new Error(`${label} must be non-empty text`);
    }
    return value;
  }

  function requireArray(value, label) {
    if (!Array.isArray(value)) throw new Error(`${label} must be an array`);
    return value;
  }

  function requireState(value, label) {
    if (value !== ACTIVE && value !== DELETED) {
      throw new Error(`${label} must be active or deleted`);
    }
  }

  function requireTimeRange(start, end, label) {
    if (!Number.isFinite(Number(start)) || !Number.isFinite(Number(end))
      || Number(start) < 0 || Number(end) <= Number(start)) {
      throw new Error(`${label} has an invalid time range`);
    }
  }

  function requireSourceTimeRange(start, end, label) {
    if (!Number.isFinite(Number(start)) || !Number.isFinite(Number(end))
      || Number(start) < 0 || Number(end) < Number(start)) {
      throw new Error(`${label} has an invalid source time range`);
    }
  }

  function assertNoEntityComplete(entity, label) {
    if (Object.prototype.hasOwnProperty.call(entity, "complete")) {
      throw new Error(`${label}.complete is forbidden; use document.properties.complete`);
    }
  }

  function validateProvenance(value, label) {
    const provenance = requireObject(value, label);
    requireText(provenance.kind, `${label}.kind`);
    if (!CHANGE_KINDS.has(provenance.kind)) throw new Error(`${label}.kind is unsupported`);
    requireText(provenance.operation, `${label}.operation`);
    requireText(provenance.actor, `${label}.actor`);
    requireText(provenance.created_at, `${label}.created_at`);
    requireObject(provenance.metadata, `${label}.metadata`);
  }

  function validateTranslationTrack(value, label) {
    const target = requireObject(value, label);
    requireText(target.target_text, `${label}.target_text`);
    if (target.original_text !== null && target.original_text !== undefined) {
      requireText(target.original_text, `${label}.original_text`);
    }
    validateProvenance(target.provenance, `${label}.provenance`);
  }

  function assertUnique(values, label) {
    if (new Set(values).size !== values.length) throw new Error(`duplicate ${label}`);
  }

  function validateDocument(document) {
    requireObject(document, "document");
    if (document.schema_version !== DOCUMENT_SCHEMA) {
      throw new Error(`unsupported schema: ${document.schema_version}`);
    }
    requireText(document.document_id, "document.document_id");
    const properties = requireObject(document.properties, "document.properties");
    if (typeof properties.complete !== "boolean") {
      throw new Error("document.properties.complete must be boolean");
    }
    if (properties.speaker_names !== undefined) requireObject(properties.speaker_names, "document.properties.speaker_names");
    const presentation = requireObject(document.presentation, "document.presentation");
    if (!new Set(["remove", "space"]).has(presentation.upper_punctuation)
      || !new Set(["remove", "space"]).has(presentation.lower_punctuation)) {
      throw new Error("document.presentation punctuation policy is unsupported");
    }
    if (!new Set(["source_above_target", "target_above_source"]).has(presentation.display_order)) {
      throw new Error("document.presentation.display_order is unsupported");
    }
    const sourceTokens = requireArray(document.source_tokens, "document.source_tokens");
    const displayTokens = requireArray(document.display_tokens, "document.display_tokens");
    const cues = requireArray(document.cues, "document.cues");
    const groups = document.groups === undefined
      ? [] : requireArray(document.groups, "document.groups");
    requireArray(document.changes, "document.changes").forEach((item, index) =>
      validateProvenance(item, `document.changes[${index}]`)
    );

    const sourceIds = [];
    let previousSourceIndex = -1;
    let previousSourceStart = -1;
    sourceTokens.forEach((token, index) => {
      requireObject(token, `source_tokens[${index}]`);
      assertNoEntityComplete(token, `source_tokens[${index}]`);
      sourceIds.push(requireText(token.token_id, `source_tokens[${index}].token_id`));
      if (!Number.isInteger(token.index) || token.index < 0 || token.index <= previousSourceIndex) {
        throw new Error("source token indexes must be unique and ordered");
      }
      requireText(token.text, `source_tokens[${index}].text`);
      requireSourceTimeRange(token.start, token.end, `source_tokens[${index}]`);
      if (Number(token.start) < previousSourceStart) throw new Error("source tokens must be time ordered");
      previousSourceIndex = token.index;
      previousSourceStart = Number(token.start);
    });
    assertUnique(sourceIds, "source token id");

    const knownSource = new Set(sourceIds);
    const displayIds = [];
    const lineage = [];
    displayTokens.forEach((token, index) => {
      requireObject(token, `display_tokens[${index}]`);
      assertNoEntityComplete(token, `display_tokens[${index}]`);
      displayIds.push(requireText(token.token_id, `display_tokens[${index}].token_id`));
      requireText(token.text, `display_tokens[${index}].text`);
      requireText(token.original_text, `display_tokens[${index}].original_text`);
      requireState(token.state, `display_tokens[${index}].state`);
      validateProvenance(token.provenance, `display_tokens[${index}].provenance`);
      const ids = requireArray(token.source_token_ids, `display_tokens[${index}].source_token_ids`);
      assertUnique(ids, "source lineage within display token");
      ids.forEach(id => {
        if (!knownSource.has(id)) throw new Error(`unknown source lineage: ${id}`);
        lineage.push(id);
      });
      if (!ids.length && token.provenance.kind !== "manual") {
        throw new Error("only manual display tokens may omit source lineage");
      }
    });
    assertUnique(displayIds, "display token id");
    assertUnique(lineage, "source lineage");
    const lineageSet = new Set(lineage);
    if (lineage.length !== sourceIds.length || sourceIds.some(id => !lineageSet.has(id))) {
      throw new Error("source lineage is incomplete");
    }

    const knownDisplay = new Set(displayIds);
    const groupIds = groups.map((group, index) => {
      requireObject(group, `groups[${index}]`);
      const groupId = requireText(group.group_id, `groups[${index}].group_id`);
      if (!new Set(["segmentation", "manual", "merged"]).has(group.origin)) {
        throw new Error(`groups[${index}].origin is unsupported`);
      }
      validateProvenance(group.provenance, `groups[${index}].provenance`);
      ["source_group_ids", "execution_block_ids", "dirty_flags"].forEach(key =>
        assertUnique(requireArray(group[key] || [], `groups[${index}].${key}`), `group ${key}`)
      );
      return groupId;
    });
    assertUnique(groupIds, "group id");
    const knownGroups = new Set(groupIds);
    const displayById = new Map(displayTokens.map(token => [token.token_id, token]));
    const cueIds = [];
    const cueMembers = [];
    let previousCueIndex = -1;
    let previousCueEnd = -1;
    cues.forEach((cue, index) => {
      requireObject(cue, `cues[${index}]`);
      assertNoEntityComplete(cue, `cues[${index}]`);
      cueIds.push(requireText(cue.cue_id, `cues[${index}].cue_id`));
      if (groups.length) {
        if (!knownGroups.has(cue.group_id)) throw new Error("cue references unknown group");
      } else if (cue.group_id !== undefined && cue.group_id !== null) {
        throw new Error("cue group membership requires document groups");
      }
      if (!Number.isInteger(cue.index) || cue.index < 0 || cue.index <= previousCueIndex) {
        throw new Error("cue indexes must be unique and ordered");
      }
      requireState(cue.state, `cues[${index}].state`);
      requireTimeRange(cue.start, cue.end, `cues[${index}]`);
      const members = requireArray(cue.display_token_ids, `cues[${index}].display_token_ids`);
      if (!members.length) throw new Error("cue must reference at least one display token");
      assertUnique(members, "display token within cue");
      members.forEach(id => {
        if (!knownDisplay.has(id)) throw new Error(`cue references unknown display token: ${id}`);
        cueMembers.push(id);
      });
      if (cue.target !== null && cue.target !== undefined) {
        validateTranslationTrack(cue.target, `cues[${index}].target`);
      }
      if (Object.prototype.hasOwnProperty.call(cue, "translation")) {
        throw new Error("bare cue translation is forbidden; use target TranslationTrack");
      }
      previousCueIndex = cue.index;
      const isManualCue = members.every(id => !(displayById.get(id)?.source_token_ids || []).length);
      if (cue.state === ACTIVE && !isManualCue) {
        if (Number(cue.start) < previousCueEnd) throw new Error("cues must be time ordered and non-overlapping");
        previousCueEnd = Number(cue.end);
      }
    });
    assertUnique(cueIds, "cue id");
    const cueOwners = new Map();
    document.cues.forEach(cue => (cue.display_token_ids || []).forEach(id => {
      if (!cueOwners.has(id)) cueOwners.set(id, []);
      cueOwners.get(id).push(cue);
    }));
    cueOwners.forEach((owners, id) => {
      if (owners.length < 2) return;
      const groups = new Set(owners.map(cue => String(cue.mapping?.source_repeat_group || "")));
      if (groups.has("") || groups.size !== 1) {
        throw new Error(`display token ${id} is repeated without one declared source_repeat_group`);
      }
    });
    const cueMemberSet = new Set(cueMembers);
    if (displayIds.some(id => !cueMemberSet.has(id))) {
      throw new Error("display token cue coverage is incomplete");
    }
    return document;
  }

  function consumeRevision(payload) {
    const source = requireObject(payload, "revision payload");
    if (consumedRevisions.has(source)) return source;
    requireText(source.revision_id, "revision_id");
    requireText(source.document_hash, "document_hash");
    validateDocument(source.document);
    const revision = Object.freeze({
      revision_id:source.revision_id,
      revision_number:Number(source.revision_number),
      parent_revision_id:source.parent_revision_id ?? null,
      created_at:source.created_at,
      document_hash:source.document_hash,
      document:clone(source.document)
    });
    consumedRevisions.add(revision);
    return revision;
  }

  function isReferenceProvenance(provenance) {
    const metadata = provenance?.metadata || {};
    return metadata.reference === true
      || String(metadata.source || "").toLowerCase() === "reference"
      || String(provenance?.operation || "").toLowerCase().includes("reference");
  }

  function provenanceTone(provenance) {
    if (provenance?.kind === "ai") return "light-blue";
    if (isReferenceProvenance(provenance)) return "yellow";
    return "neutral";
  }

  function validationReportIsCurrent(revision, report) {
    return !!report
      && report.document_id === revision.document.document_id
      && report.revision_id === revision.revision_id
      && report.document_hash === revision.document_hash;
  }

  function buildEditorView(revisionPayload, validationReport = null) {
    const revision = consumeRevision(revisionPayload);
    const cached = editorViewCache.get(revision);
    if (cached) {
      if (!validationReport) return cached;
      return {
        ...cached,
        validation:{
          current:validationReportIsCurrent(revision, validationReport),
          report:clone(validationReport)
        }
      };
    }
    const displayById = new Map(revision.document.display_tokens.map(token => [token.token_id, token]));
    const tokenViews = revision.document.display_tokens.map(token => ({
      token_id:token.token_id,
      text:token.text,
      original_text:token.original_text,
      source_token_ids:[...token.source_token_ids],
      provenance:clone(token.provenance),
      state:token.state,
      tone:provenanceTone(token.provenance)
    }));
    const cueViews = ordering.canonicalCueOrder(revision.document.cues).map(cue => ({
      cue_id:cue.cue_id,
      index:cue.index,
      display_token_ids:[...cue.display_token_ids],
      active_display_token_ids:cue.display_token_ids.filter(id => displayById.get(id)?.state === ACTIVE),
      start:cue.start,
      end:cue.end,
      target:cue.target === null ? null : clone(cue.target),
      speaker:cue.speaker,
      state:cue.state,
      group_id:cue.group_id ?? null
    }));
    const view = {
      document_id:revision.document.document_id,
      revision_id:revision.revision_id,
      document_hash:revision.document_hash,
      properties:clone(revision.document.properties),
      presentation:clone(revision.document.presentation),
      changes:clone(revision.document.changes),
      source_tokens:clone(revision.document.source_tokens),
      groups:clone(revision.document.groups || []),
      token_views:tokenViews,
      cue_views:cueViews,
      active_token_ids:tokenViews.filter(token => token.state === ACTIVE).map(token => token.token_id),
      active_cue_ids:cueViews.filter(cue => cue.state === ACTIVE).map(cue => cue.cue_id),
      validation:null
    };
    editorViewCache.set(revision, view);
    if (!validationReport) return view;
    return {
      ...view,
      validation:{
        current:validationReportIsCurrent(revision, validationReport),
        report:clone(validationReport)
      }
    };
  }

  function applyEntityDelta(items, delta, idKey, sortKey = null) {
    const removed = new Set((delta?.remove || []).map(String));
    const upserts = new Map((delta?.upsert || []).map(item => [String(item[idKey]), clone(item)]));
    const next = [];
    items.forEach(item => {
      const id = String(item[idKey]);
      if (removed.has(id)) return;
      next.push(upserts.get(id) || item);
      upserts.delete(id);
    });
    next.push(...upserts.values());
    if (sortKey) next.sort((left, right) => Number(left[sortKey]) - Number(right[sortKey]));
    return next;
  }

  function applyCueDelta(items, delta) {
    const updated = applyOrderedEntityDelta(items, delta, "cue_id");
    return ordering.canonicalCueOrder(updated).map((cue, index) => ({...cue, index}));
  }

  function applyOrderedEntityDelta(items, delta, idKey) {
    const updated = applyEntityDelta(items, delta, idKey);
    const byEntityId = new Map(updated.map(item => [String(item[idKey]), item]));
    // order_splice is calculated against the complete pre-delta order.  Do not
    // remove deleted IDs before applying it: doing so shifts the splice index
    // and can leave the projected entity list malformed after topology edits.
    const order = items.map(item => String(item[idKey]));
    const splice = delta?.order_splice;
    if (splice) {
      order.splice(
        Number(splice.start),
        Number(splice.delete_count),
        ...(splice.insert_ids || []).map(String)
      );
    }
    const filteredOrder = order.filter(id => byEntityId.has(id));
    const known = new Set(filteredOrder);
    byEntityId.forEach((_item, id) => {
      if (!known.has(id)) {
        known.add(id);
        filteredOrder.push(id);
      }
    });
    return filteredOrder.map(id => byEntityId.get(id));
  }

  function applyRevisionDelta(revisionPayload, deltaPayload) {
    const revision = consumeRevision(revisionPayload);
    const delta = requireObject(deltaPayload, "revision delta");
    if (delta.schema_version !== "substar.editor-delta.v1") {
      throw new Error(`unsupported delta schema: ${delta.schema_version}`);
    }
    if (delta.base_revision_id !== revision.revision_id) {
      throw new Error("revision delta is based on a different revision");
    }
    if (delta.document_id !== revision.document.document_id) {
      throw new Error("revision delta belongs to a different document");
    }
    requireText(delta.revision_id, "revision delta revision_id");
    requireText(delta.document_hash, "revision delta document_hash");
    const document = revision.document;
    const appendedChanges = clone(delta.changes_append || []);
    const nextDocument = {
      ...document,
      properties:delta.properties ? clone(delta.properties) : document.properties,
      presentation:delta.presentation ? clone(delta.presentation) : document.presentation,
      source_tokens:applyEntityDelta(document.source_tokens, delta.source_tokens, "token_id", "index"),
      display_tokens:applyOrderedEntityDelta(document.display_tokens, delta.display_tokens, "token_id"),
      cues:applyCueDelta(document.cues, delta.cues),
      changes:delta.changes_replaced ? appendedChanges : [...document.changes, ...appendedChanges]
    };
    const nextGroups = applyEntityDelta(document.groups || [], delta.groups, "group_id");
    if (document.groups !== undefined || nextGroups.length) nextDocument.groups = nextGroups;
    const nextRevision = Object.freeze({
      revision_id:delta.revision_id,
      revision_number:Number(delta.revision_number),
      parent_revision_id:delta.parent_revision_id ?? null,
      created_at:delta.created_at,
      document_hash:delta.document_hash,
      document:nextDocument
    });
    consumedRevisions.add(nextRevision);
    return nextRevision;
  }

  function operationBase(revisionPayload) {
    const revision = consumeRevision(revisionPayload);
    return {
      document_id:revision.document.document_id,
      revision_id:revision.revision_id,
      document_hash:revision.document_hash
    };
  }

  function createOperation(revisionPayload, type, payload, retainedEntities = []) {
    if (!OPERATION_TYPES.has(type)) throw new Error(`unsupported operation: ${type}`);
    const base = operationBase(revisionPayload);
    const operationPayload = clone(payload || {});
    const retained = clone(retainedEntities);
    return {
      operation_id:`op_${stableHash({base, type, payload:operationPayload})}`,
      type,
      base,
      payload:operationPayload,
      retention:{strategy:"state_deleted", entities:retained}
    };
  }

  function revisionIndexes(revisionPayload) {
    const revision = consumeRevision(revisionPayload);
    const cached = revisionIndexCache.get(revision);
    if (cached) return cached;
    const indexes = {
      revision,
      display:new Map(revision.document.display_tokens.map(token => [token.token_id, token])),
      cues:new Map(revision.document.cues.map(cue => [cue.cue_id, cue]))
    };
    revisionIndexCache.set(revision, indexes);
    return indexes;
  }

  function provenanceInput(provenance, operation) {
    const value = clone(provenance || {});
    return {...value, operation:String(value.operation || operation)};
  }

  function replaceOperation(revisionPayload, tokenId, text, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const token = indexes.display.get(String(tokenId));
    if (!token) throw new Error("replace target display token does not exist");
    return createOperation(revisionPayload, "replace", {
      token_id:token.token_id,
      text:String(text),
      original_text:token.text,
      source_token_ids:[...token.source_token_ids],
      provenance:provenanceInput(provenance, "replace")
    });
  }

  function batchReplaceOperation(revisionPayload, replacements, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const prepared = (replacements || []).map(item => {
      const token = indexes.display.get(String(item.token_id));
      if (!token) throw new Error("batch replace target display token does not exist");
      return {
        token_id:token.token_id,
        text:String(item.text || ""),
        expected_text:item.expected_text ?? token.text
      };
    });
    if (!prepared.length) throw new Error("batch replace needs replacements");
    return createOperation(revisionPayload, "batch_replace", {
      replacements:prepared,
      provenance:provenanceInput(provenance, "batch_replace")
    });
  }

  function setAiCalibrationOperation(revisionPayload, tokenIds, action, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const ids = [...new Set((tokenIds || []).map(String))];
    if (!ids.length) throw new Error("AI calibration needs token_ids");
    if (!["cancel", "restore"].includes(String(action))) {
      throw new Error("AI calibration action must be cancel or restore");
    }
    const expectedTexts = {};
    ids.forEach(id => {
      const token = indexes.display.get(id);
      if (!token) throw new Error("AI calibration target does not exist");
      if (token.state !== ACTIVE) throw new Error("AI calibration target must be active");
      expectedTexts[id] = token.text;
    });
    return createOperation(revisionPayload, "set_ai_calibration", {
      token_ids:ids,
      action:String(action),
      expected_texts:expectedTexts,
      provenance:provenanceInput(provenance, "set_ai_calibration")
    }, ids.map(id => indexes.display.get(id)));
  }

  function setTargetOperation(revisionPayload, cueId, targetText, provenance = null, options = {}) {
    const indexes = revisionIndexes(revisionPayload);
    const cue = indexes.cues.get(String(cueId));
    if (!cue) throw new Error("set target cue does not exist");
    return createOperation(revisionPayload, "set_target", {
      cue_id:cue.cue_id,
      target_text:String(targetText || ""),
      original_text:options.original_text ?? cue.target?.original_text ?? cue.target?.target_text ?? "",
      language:options.language ?? cue.target?.language ?? "zh-CN",
      provenance:provenanceInput(provenance, "set_target")
    }, cue.target ? [cue.target] : [cue]);
  }

  function setCueSpeakerOperation(revisionPayload, cueId, speaker, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const cue = indexes.cues.get(String(cueId));
    if (!cue) throw new Error("set speaker cue does not exist");
    if (speaker !== null && !new Set(["speaker_0", "speaker_1", "speaker_2", "speaker_3"]).has(speaker)) {
      throw new Error("speaker must use one of four palette slots");
    }
    return createOperation(revisionPayload, "set_cue_speaker", {
      cue_id:cue.cue_id, speaker, previous_speaker:cue.speaker ?? null,
      provenance:provenanceInput(provenance, "set_cue_speaker")
    }, [cue]);
  }

  function setSpeakerNamesOperation(revisionPayload, speakerNames, provenance = null) {
    const names = {};
    for (let index = 0; index < 4; index += 1) names[`speaker_${index}`] = String(speakerNames?.[`speaker_${index}`] || "").trim();
    return createOperation(revisionPayload, "set_speaker_names", {
      speaker_names:names,
      provenance:provenanceInput(provenance, "set_speaker_names")
    });
  }

  function setCueTimeOperation(revisionPayload, cueId, start, end, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const cue = indexes.cues.get(String(cueId));
    if (!cue) throw new Error("set cue time target does not exist");
    const nextStart = Number(start);
    const nextEnd = Number(end);
    if (!Number.isFinite(nextStart) || !Number.isFinite(nextEnd) || nextStart < 0 || nextEnd <= nextStart) {
      throw new Error("cue time must satisfy 0 <= start < end");
    }
    return createOperation(revisionPayload, "set_cue_time", {
      cue_id:cue.cue_id,
      start:nextStart,
      end:nextEnd,
      previous_start:cue.start,
      previous_end:cue.end,
      provenance:provenanceInput(provenance, "set_cue_time")
    }, [cue]);
  }

  function setCueTimesOperation(revisionPayload, cueChanges, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    if (!Array.isArray(cueChanges) || !cueChanges.length) {
      throw new Error("set cue times needs at least one cue");
    }
    const ids = new Set();
    const changes = cueChanges.map(change => {
      const cueId = String(change?.cue_id || "");
      const cue = indexes.cues.get(cueId);
      if (!cue) throw new Error("set cue times target does not exist");
      if (ids.has(cueId)) throw new Error("set cue times targets must be unique");
      ids.add(cueId);
      const start = Number(change.start);
      const end = Number(change.end);
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) {
        throw new Error("cue time must satisfy 0 <= start < end");
      }
      return {
        cue_id:cueId,
        start,
        end,
        expected_start:cue.start,
        expected_end:cue.end
      };
    });
    const byId = new Map(changes.map(change => [change.cue_id, change]));
    const displayById = indexes.display;
    let previousEnd = -1;
    ordering.canonicalCueOrder(indexes.revision.document.cues).forEach(cue => {
      const value = byId.get(cue.cue_id) || cue;
      const isManualCue = cue.display_token_ids.every(id =>
        !(displayById.get(id)?.source_token_ids || []).length
      );
      if (cue.state !== ACTIVE || isManualCue) return;
      if (value.start < previousEnd - 1e-9) {
        throw new Error("final cue timeline must not overlap and must remain ordered");
      }
      previousEnd = value.end;
    });
    return createOperation(revisionPayload, "set_cue_times", {
      cues:changes,
      provenance:provenanceInput(provenance, "set_cue_times")
    }, changes.map(change => indexes.cues.get(change.cue_id)));
  }

  function insertCueOperation(revisionPayload, start, end, text, provenance = null, options = {}) {
    const nextStart = Number(start);
    const nextEnd = Number(end);
    if (!Number.isFinite(nextStart) || !Number.isFinite(nextEnd) || nextStart < 0 || nextEnd <= nextStart) {
      throw new Error("new cue time must satisfy 0 <= start < end");
    }
    if (!String(text || "").trim()) throw new Error("new cue text cannot be empty");
    return createOperation(revisionPayload, "insert_cue", {
      start:nextStart,
      end:nextEnd,
      text:String(text).trim(),
      speaker:options.speaker ?? null,
      provenance:provenanceInput(provenance, "insert_cue")
    });
  }

  function mergeOperation(revisionPayload, tokenIds, text, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const ids = tokenIds.map(String);
    const tokens = ids.map(id => indexes.display.get(id));
    if (ids.length < 2 || tokens.some(token => !token)) {
      throw new Error("merge needs at least two existing display tokens");
    }
    const cue = indexes.revision.document.cues.find(item =>
      ids.every(id => item.display_token_ids.includes(id))
    );
    if (!cue) throw new Error("merge tokens must belong to one cue");
    const positions = ids.map(id => cue.display_token_ids.indexOf(id));
    if (!positions.every((position, index) => index === 0 || position === positions[index - 1] + 1)) {
      throw new Error("merge tokens must be contiguous and ordered");
    }
    const lineage = tokens.flatMap(token => token.source_token_ids);
    return createOperation(revisionPayload, "merge", {
      cue_id:cue.cue_id,
      token_ids:ids,
      text:String(text),
      original_text:tokens.map(token => token.text).join(" "),
      source_token_ids:lineage,
      provenance:provenanceInput(provenance, "merge")
    }, tokens);
  }

  function deleteOperation(revisionPayload, targets, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const requestedTokenIds = (targets?.token_ids || []).map(String);
    const cueIds = (targets?.cue_ids || []).map(String);
    const cues = cueIds.map(id => indexes.cues.get(id));
    if (!requestedTokenIds.length && !cueIds.length) throw new Error("delete needs token_ids or cue_ids");
    if (cues.some(cue => !cue)) throw new Error("delete target does not exist");
    const tokenIds = [...new Set(requestedTokenIds)];
    const tokens = tokenIds.map(id => indexes.display.get(id));
    if (tokens.some(token => !token)) throw new Error("delete target does not exist");
    return createOperation(revisionPayload, "delete", {
      token_ids:tokenIds,
      cue_ids:cueIds,
      transitions:[
        ...tokenIds.map(token_id => ({entity:"display_token", id:token_id, from:ACTIVE, to:DELETED})),
        ...cueIds.map(cue_id => ({entity:"cue", id:cue_id, from:ACTIVE, to:DELETED}))
      ],
      provenance:provenanceInput(provenance, "delete")
    }, [...tokens, ...cues]);
  }

  function purgeCueOperation(revisionPayload, cueIds, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const ids = [...new Set((cueIds || []).map(String))];
    if (!ids.length) throw new Error("purge cue needs cue_ids");
    const cues = ids.map(id => indexes.cues.get(id));
    if (cues.some(cue => !cue)) throw new Error("purge cue target does not exist");
    if (cues.some(cue => cue.state !== ACTIVE)) {
      throw new Error("only active cues can be permanently deleted");
    }
    const operation = createOperation(revisionPayload, "purge_cue", {
      cue_ids:ids,
      provenance:provenanceInput(provenance, "purge_cue")
    });
    operation.retention = {strategy:"revision_history", entities:[]};
    return operation;
  }

  function restoreOperation(revisionPayload, targets, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const tokenIds = [...new Set((targets?.token_ids || []).map(String))];
    const cueIds = [...new Set((targets?.cue_ids || []).map(String))];
    if (!tokenIds.length && !cueIds.length) throw new Error("restore needs token_ids or cue_ids");
    const tokens = tokenIds.map(id => indexes.display.get(id));
    const cues = cueIds.map(id => indexes.cues.get(id));
    if (tokens.some(token => !token) || cues.some(cue => !cue)) {
      throw new Error("restore target does not exist");
    }
    if (tokens.some(token => token.state !== DELETED) || cues.some(cue => cue.state !== DELETED)) {
      throw new Error("restore targets must be deleted");
    }
    return createOperation(revisionPayload, "restore", {
      token_ids:tokenIds,
      cue_ids:cueIds,
      transitions:[
        ...tokenIds.map(token_id => ({entity:"display_token", id:token_id, from:DELETED, to:ACTIVE})),
        ...cueIds.map(cue_id => ({entity:"cue", id:cue_id, from:DELETED, to:ACTIVE}))
      ],
      provenance:provenanceInput(provenance, "restore")
    }, [...tokens, ...cues]);
  }

  function insertOperation(revisionPayload, cueId, afterTokenId, token, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const cue = indexes.cues.get(String(cueId));
    if (!cue) throw new Error("insert target cue does not exist");
    if (afterTokenId !== null && !cue.display_token_ids.includes(String(afterTokenId))) {
      throw new Error("insert anchor does not belong to cue");
    }
    const sourceTokenIds = (token?.source_token_ids || []).map(String);
    if (sourceTokenIds.length) throw new Error("frontend insert must be manual and have empty source lineage");
    return createOperation(revisionPayload, "insert", {
      cue_id:cue.cue_id,
      after_token_id:afterTokenId === null ? null : String(afterTokenId),
      token:{
        text:String(token?.text || ""),
        original_text:String(token?.original_text || token?.text || ""),
        source_token_ids:[],
        provenance:provenanceInput(provenance, "insert")
      }
    });
  }

  function splitCueOperation(revisionPayload, cueId, afterTokenId, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const cue = indexes.cues.get(String(cueId));
    if (!cue) throw new Error("split target cue does not exist");
    const position = cue.display_token_ids.indexOf(String(afterTokenId));
    if (position < 0 || position >= cue.display_token_ids.length - 1) {
      throw new Error("split boundary must be inside cue");
    }
    const rightCueId = `cue_${stableHash({
      revision_id:indexes.revision.revision_id,
      cue_id:cue.cue_id,
      after_token_id:String(afterTokenId)
    })}`;
    return createOperation(revisionPayload, "split_cue", {
      cue_id:cue.cue_id,
      right_cue_id:rightCueId,
      after_token_id:String(afterTokenId),
      left_display_token_ids:cue.display_token_ids.slice(0, position + 1),
      right_display_token_ids:cue.display_token_ids.slice(position + 1),
      target:cue.target === null ? null : clone(cue.target),
      provenance:provenanceInput(provenance, "split_cue")
    }, [cue]);
  }

  function mergeCuesOperation(revisionPayload, leftCueId, rightCueId, provenance = null) {
    const indexes = revisionIndexes(revisionPayload);
    const cues = ordering.canonicalCueOrder(indexes.revision.document.cues);
    const leftIndex = cues.findIndex(cue => cue.cue_id === String(leftCueId));
    const rightIndex = cues.findIndex(cue => cue.cue_id === String(rightCueId));
    if (leftIndex < 0 || rightIndex !== leftIndex + 1) {
      throw new Error("merge cues must be adjacent and ordered");
    }
    const left = cues[leftIndex];
    const right = cues[rightIndex];
    return createOperation(revisionPayload, "merge_cues", {
      cue_ids:[left.cue_id, right.cue_id],
      display_token_ids:[...left.display_token_ids, ...right.display_token_ids],
      targets:[left.target === null ? null : clone(left.target), right.target === null ? null : clone(right.target)],
      provenance:provenanceInput(provenance, "merge_cues")
    }, [left, right]);
  }

  function findContiguousTokenMatches(tokensInput, queryInput) {
    const tokens = (Array.isArray(tokensInput) ? tokensInput : [])
      .filter(token => token && typeof token.token_id === "string")
      .map(token => ({...token, text:String(token.text || "")}));
    const query = String(queryInput || "").trim();
    if (!tokens.length || !query) return [];
    const spans = [];
    let cursor = 0;
    for (const token of tokens) {
      const start = cursor;
      cursor += token.text.length;
      spans.push({token, start, end:cursor});
    }
    const source = tokens.map(token => token.text).join("");
    const haystack = source.toLocaleLowerCase();
    const needle = query.toLocaleLowerCase();
    const matches = [];
    let searchFrom = 0;
    let index = haystack.indexOf(needle, searchFrom);
    while (index >= 0) {
      const end = index + needle.length;
      const covered = spans.filter(span => span.end > index && span.start < end);
      if (covered.length) {
        const rangeStart = covered[0].start;
        const rangeEnd = covered.at(-1).end;
        matches.push({
          token_ids:covered.map(span => span.token.token_id),
          combined_text:source.slice(rangeStart, rangeEnd),
          start_offset:index - rangeStart,
          end_offset:end - rangeStart
        });
      }
      searchFrom = Math.max(end, index + 1);
      index = haystack.indexOf(needle, searchFrom);
    }
    return matches;
  }

  return Object.freeze({
    DOCUMENT_SCHEMA,
    validateDocument,
    consumeRevision,
    buildEditorView,
    canonicalCueOrder:ordering.canonicalCueOrder,
    applyRevisionDelta,
    validationReportIsCurrent,
    createOperation,
    replaceOperation,
    batchReplaceOperation,
    setAiCalibrationOperation,
    setTargetOperation,
    setCueSpeakerOperation,
    setSpeakerNamesOperation,
    setCueTimeOperation,
    setCueTimesOperation,
    insertCueOperation,
    mergeOperation,
    deleteOperation,
    purgeCueOperation,
    restoreOperation,
    insertOperation,
    splitCueOperation,
    mergeCuesOperation,
    findContiguousTokenMatches
  });
});
