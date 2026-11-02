(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorRecovery = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const workbenchLocks = new WeakMap();
  function isWorkbenchInert(workbench) {
    return workbenchLocks.has(workbench) || workbench.inert;
  }
  function setWorkbenchInert(workbench, refreshButton, locked) {
    const previous = workbenchLocks.get(workbench);
    if (previous) {
      previous.forEach(([node, inert]) => { node.inert = inert; });
      workbenchLocks.delete(workbench);
    }
    workbench.inert = false;
    if (!locked) return;
    if (!refreshButton || !workbench.contains(refreshButton)) {
      workbench.inert = true;
      return;
    }
    const saved = [];
    function lockChildren(parent) {
      for (const child of parent.children) {
        if (child === refreshButton) continue;
        if (child.contains(refreshButton)) lockChildren(child);
        else { saved.push([child, child.inert]); child.inert = true; }
      }
    }
    lockChildren(workbench);
    workbenchLocks.set(workbench, saved);
  }

  function keys(storage, prefix) {
    const result = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (key?.startsWith(prefix)) result.push(key);
    }
    return result;
  }

  async function bounded(work, milliseconds, message) {
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(work),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), milliseconds); })
      ]);
    } finally { clearTimeout(timer); }
  }

  function archive(storage, projectId, snapshots) {
    const journalKeys = keys(storage, `substar.editor.operations:${projectId}:`);
    // Preserve raw journals too: even an unreadable journal must not be silently lost.
    const journals = journalKeys.map(key => ({key, value:storage.getItem(key)}));
    const dirty = journals.some(item => item.value && item.value !== "[]")
      || snapshots.some(item => item.operations?.length || item.inputs?.length);
    let key = null;
    if (dirty) {
      key = `substar.editor.conflict-copy:${projectId}:${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const text = JSON.stringify({schema_version:"substar.editor-recovery.v2", project_id:projectId,
        created_at:new Date().toISOString(), snapshots, journals});
      storage.setItem(key, text);
      if (storage.getItem(key) !== text) throw new Error("恢复草稿写入校验失败");
    }
    // Only detach failed operations after a verified durable copy exists.
    journalKeys.forEach(key => storage.removeItem(key));
    return key;
  }

  function list(storage, projectId) {
    return keys(storage, `substar.editor.conflict-copy:${projectId}:`).sort().reverse()
      .map(key => ({key, text:storage.getItem(key)}));
  }

  function preview(raw) {
    let copy;
    try { copy = JSON.parse(raw); } catch (_) { return raw; }
    const snapshots = copy.snapshots || [copy];
    const lines = [];
    snapshots.forEach((snapshot, index) => {
      lines.push(`—— 刷新前快照 ${index + 1} ——`);
      for (const input of snapshot.inputs || []) {
        lines.push(`尚未提交的输入 · ${input.label || "字幕"} · ${input.cue_id || ""}`, input.value === "" ? "（空文本）" : input.value, "");
      }
      const document = snapshot.revision?.document;
      const tokens = new Map((document?.display_tokens || []).map(token => [token.token_id, token.text]));
      for (const cue of document?.cues || []) {
        lines.push(`${cue.cue_id}  ${cue.start} – ${cue.end}${cue.state === "deleted" ? "（已隐藏）" : ""}`,
          cue.source_text ?? (cue.display_token_ids || []).map(id => tokens.get(id) || "").join(" "));
        if (cue.target) lines.push(cue.target.target_text || "");
        lines.push("");
      }
    });
    return lines.join("\n").trim() || raw;
  }

  function createController(options) {
    let busy = false;
    return {
      async refresh() {
        if (busy) return false;
        const projectId = options.projectId();
        if (!projectId) return false;
        busy = true;
        let frozen = false;
        let busyShown = false;
        try {
          // Capture before blur/flush can replace a field or its original text.
          const before = JSON.parse(JSON.stringify(options.capture()));
          options.pause?.();
          busyShown = true;
          options.setBusy(true);
          try { await bounded(() => options.prepare(), options.saveWaitMs ?? 1500, "保存尚未完成"); }
          catch (_) { /* The unresolved input will be isolated in a recovery draft. */ }
          await bounded(() => options.verify(projectId), options.verifyWaitMs ?? 10000, "连接检查超时，请确认 Substar 正在运行后重试");
          if (projectId !== options.projectId()) throw new Error("项目已切换，请在当前项目重新刷新");
          const after = JSON.parse(JSON.stringify(options.capture()));
          const draftKey = archive(options.storage, projectId, [before, after]);
          options.freeze();
          frozen = true;
          options.reload(projectId, {draftKey});
          return true;
        } catch (error) {
          options.onError(error);
          return false;
        } finally {
          busy = false;
          if (!frozen) {
            options.resume?.();
            if (busyShown) options.setBusy(false);
          }
        }
      }
    };
  }

  return {archive, list, preview, createController, setWorkbenchInert, isWorkbenchInert};
});
