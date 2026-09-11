(function (root) {
  "use strict";
  const FORMAT = "substar.glossary-library.v2";
  const key = value => String(value || "").trim().normalize("NFKC").toLowerCase();
  const uid = () => globalThis.crypto.randomUUID().replaceAll("-", "");
  function parse(text) {
    const value = JSON.parse(text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, ""));
    if (!value || value.schema_version !== FORMAT) throw new Error("词库格式或版本不受支持，请使用导出的 JSON 格式。");
    for (const field of ["collections", "entries", "candidates"]) {
      if (!Array.isArray(value[field])) throw new Error(`缺少 ${field} 数组`);
      if (value[field].some(row => !row || typeof row !== "object" || Array.isArray(row))) throw new Error(`${field} 包含无效条目`);
    }
    const ids = new Set(["global"]);
    for (const c of value.collections) {
      if (typeof c.id !== "string" || !c.id || typeof c.name !== "string" || !c.name.trim()) throw new Error("词库需要名称和 ID");
      if (c.id !== "global" && ids.has(c.id)) throw new Error("词库 ID 重复");
      ids.add(c.id);
      if (!Array.isArray(c.injection_permissions) || c.injection_permissions.some(p => !["asr", "calibration", "translation"].includes(p))) throw new Error("注入权限无效");
    }
    for (const row of [...value.entries, ...value.candidates]) {
      if (typeof row.source !== "string" || !row.source.trim() || row.source.length > 300 || (row.target != null && (typeof row.target !== "string" || row.target.length > 300))) throw new Error("热词不能为空，热词和译文最多 300 字");
      if (row.glossary_id && !ids.has(row.glossary_id)) throw new Error(`所属词库不存在：${row.source}`);
    }
    for (const row of value.candidates) if (!["pending", "approved", "ignored"].includes(row.status)) throw new Error("候选词状态无效");
    return value;
  }
  function merge(local, incoming, { external = false, replace = new Set() } = {}) {
    const library = structuredClone(local);
    library.schema_version = FORMAT;
    library.candidates ||= [];
    const stats = { projects: 0, entries: 0, candidates: 0, filled: 0, skipped: 0 };
    const conflicts = [], projects = [], mapping = new Map([["global", "global"]]);
    for (const c of incoming.collections) {
      if (c.id === "global" || c.kind === "global") continue;
      let existing = library.collections.find(x => x.id === c.id) || library.collections.find(x => key(x.name) === key(c.name));
      if (!existing) {
        existing = { ...c, kind: "project", injection_permissions: external ? [] : c.injection_permissions.slice() };
        library.collections.push(existing); stats.projects++;
      }
      mapping.set(c.id, existing.id);
      projects.push({ name: existing.name, permissions: existing.injection_permissions, merged: existing.id !== c.id });
    }
    const entryKey = r => `${r.glossary_id || "global"}\u0000${key(r.source)}`;
    const entries = new Map(library.entries.map(r => [entryKey(r), r]));
    const ids = new Set([...library.entries, ...library.candidates].map(r => r.id));
    const fresh = r => { if (!r.id || ids.has(r.id)) r.id = uid(); ids.add(r.id); return r; };
    function target(existing, row, id) {
      if (!existing.target && row.target) { existing.target = row.target; stats.filled++; }
      else if (row.target && existing.target !== row.target) {
        conflicts.push({ id, source: row.source, local: existing.target, incoming: row.target });
        if (replace.has(id)) existing.target = row.target;
      } else stats.skipped++;
    }
    if (!external) for (const raw of incoming.entries) {
      const row = { ...raw, source: raw.source.trim(), target: (raw.target || "").trim(), glossary_id: mapping.get(raw.glossary_id || "global") };
      const id = entryKey(row), existing = entries.get(id);
      if (existing) target(existing, row, `entry:${id}`);
      else { library.entries.push(fresh(row)); entries.set(id, row); stats.entries++; }
    }
    const formal = new Set(library.entries.map(r => key(r.source)));
    const candidates = new Map(library.candidates.map(r => [key(r.source), r]));
    for (const raw of [...(external ? incoming.entries : []), ...incoming.candidates]) {
      const row = { ...raw, source: raw.source.trim(), target: (raw.target || "").trim(), glossary_id: mapping.get(raw.glossary_id || "global"), status: external ? "pending" : raw.status };
      const id = key(row.source), existing = candidates.get(id);
      if (existing) { if (existing.status === "pending") target(existing, row, `candidate:${id}`); else stats.skipped++; }
      else if (formal.has(id) && row.status === "pending") stats.skipped++;
      else { library.candidates.push(fresh(row)); candidates.set(id, row); stats.candidates++; }
    }
    return { library, stats, conflicts, projects };
  }
  function serialize(library) { return JSON.stringify({ ...library, schema_version: FORMAT, exported_at: new Date().toISOString() }, null, 2); }
  const api = { parse, merge, serialize, FORMAT };
  if (typeof module !== "undefined") module.exports = api;
  else root.GlossaryTransfer = api;
})(globalThis);
