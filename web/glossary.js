const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let collections = [{ id: "global", name: "未分配", kind: "global" }];
let activeGlossaryId = "all";
let dirty = false;
let candidates = [];
let pendingDeleteCollectionId = "";

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function setHeader(text, state = "") {
  const node = $("#saveState");
  node.textContent = text;
  node.className = `header-state ${state}`.trim();
}

function markDirty(message = "词库有未保存修改") {
  dirty = true;
  setHeader("有未保存修改");
  $("#formMessage").textContent = message;
}

function collectionOptions(selectedId) {
  return collections.map((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.id === "global" ? "未分配" : item.name;
    option.selected = item.id === selectedId;
    return option;
  });
}

function makeEntry(entry = {}) {
  const card = $("#entryTemplate").content.firstElementChild.cloneNode(true);
  card.dataset.id = entry.id || crypto.randomUUID().replaceAll("-", "");
  card._entry = { ...entry };
  const destination = entry.glossary_id || (activeGlossaryId === "all" ? "global" : activeGlossaryId);
  const field = (name) => $(`[data-field="${name}"]`, card);
  field("enabled").checked = entry.enabled !== false;
  field("source").value = entry.source || "";
  field("target").value = entry.target || "";
  field("glossary_id").replaceChildren(...collectionOptions(destination));
  card.addEventListener("input", () => { markDirty(); applyFilter(); updateCounts(); });
  card.addEventListener("change", () => { markDirty(); applyFilter(); updateCounts(); });
  $(".delete-entry", card).addEventListener("click", () => {
    card.remove();
    markDirty("已删除词条，保存后生效");
    applyFilter();
    updateCounts();
  });
  return card;
}

function addEntry(entry = {}, focus = true) {
  const card = makeEntry(entry);
  $("#entryList").prepend(card);
  $("#emptyState").hidden = true;
  if (focus) {
    $('[data-field="source"]', card).focus();
    markDirty("新增词条尚未保存");
  }
  updateCounts();
  return card;
}

function collectEntries() {
  return $$(".entry-card").map((card) => {
    const field = (name) => $(`[data-field="${name}"]`, card);
    const glossaryId = field("glossary_id").value || "global";
    return {
      ...card._entry,
      id: card.dataset.id,
      source: field("source").value.trim(),
      target: field("target").value.trim(),
      glossary_id: glossaryId,
      scope: glossaryId === "global" ? "global" : "project",
      project: "",
      enabled: field("enabled").checked,
    };
  }).filter((entry) => entry.source);
}

function activateGlossary(id) {
  activeGlossaryId = id;
  $$('[data-glossary]').forEach((node) => node.classList.toggle("active", node.dataset.glossary === id));
  const item = collections.find((value) => value.id === id);
  $("#contentTitle").textContent = id === "all" ? "全部词条" : item?.name || "项目词库";
  renderPermissions();
  $("#candidateDestination").replaceChildren(...collectionOptions(id === "all" ? "global" : id));
  applyFilter();
}

function renderCollections() {
  const list = $("#collectionList");
  list.innerHTML = "";
  collections.filter((item) => item.kind === "project").forEach((item) => {
    const row = document.createElement("div");
    row.className = "collection-row";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "asset-sidebar-item";
    button.dataset.glossary = item.id;
    button.classList.toggle("active", item.id === activeGlossaryId);
    button.innerHTML = `<span class="asset-sidebar-icon"><svg class="ui-icon"><use href="/assets/ui-icons.svg#align"></use></svg></span><span class="asset-sidebar-copy"><b></b></span><i class="asset-sidebar-meta"></i>`;
    $("b", button).textContent = item.name;
    $("i", button).textContent = collectEntries().filter((entry) => entry.glossary_id === item.id).length;
    button.addEventListener("click", () => activateGlossary(item.id));
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "collection-delete";
    deleteButton.setAttribute("aria-label", `删除项目词库 ${item.name}`);
    deleteButton.title = "删除项目词库";
    deleteButton.innerHTML = `<svg class="ui-icon"><use href="/assets/ui-icons.svg#trash"></use></svg>`;
    deleteButton.addEventListener("click", () => requestDeleteCollection(item.id));
    row.append(button, deleteButton);
    list.append(row);
  });
  $("#projectCount").textContent = collections.filter((item) => item.kind === "project").length;
}

function applyFilter() {
  const query = $("#searchInput").value.trim().toLocaleLowerCase();
  $$(".entry-card").forEach((card) => {
    const destination = $('[data-field="glossary_id"]', card).value;
    const text = `${$('[data-field="source"]', card).value} ${$('[data-field="target"]', card).value} ${(card._entry.aliases || []).join(" ")}`.toLocaleLowerCase();
    card.classList.toggle("filtered", (activeGlossaryId !== "all" && destination !== activeGlossaryId) || (query && !text.includes(query)));
  });
  $("#emptyState").hidden = $$(".entry-card:not(.filtered)").length > 0;
}

function updateCounts() {
  const entries = collectEntries();
  $("#allCount").textContent = entries.length;
  $("#enabledCount").textContent = entries.filter((item) => item.enabled).length;
  renderCollections();
}

function renderLibrary(value) {
  candidates = value.candidates || [];
  collections = Array.isArray(value.collections) && value.collections.length ? value.collections : collections;
  $("#entryList").innerHTML = "";
  (value.entries || []).slice().reverse().forEach((entry) => addEntry(entry, false));
  renderCollections();
  activateGlossary(collections.some((item) => item.id === activeGlossaryId) ? activeGlossaryId : "all");
  updateCounts();
  renderCandidates();
}

async function loadGlossary() {
  try {
    const result = await api("/api/glossary");
    if (!result.collections?.every(c => Array.isArray(c.injection_permissions))) throw new Error("服务仍是旧版本，请重启 Substar 后使用词库注入与候选池。");
    renderLibrary(result);
    dirty = false;
    setHeader(`已载入 ${result.entries?.length || 0} 条`, "saved");
    $("#formMessage").textContent = "词库尚未修改";
  } catch (error) {
    $("#saveButton").disabled = true;
    setHeader("读取失败", "error");
    $("#formMessage").textContent = error.message;
    $("#formMessage").className = "error";
  }
}

async function saveGlossary() {
  const button = $("#saveButton");
  button.disabled = true;
  button.textContent = "保存中…";
  $("#formMessage").className = "";
  try {
    const result = await api("/api/glossary", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ collections, entries: collectEntries() }),
    });
    renderLibrary(result);
    dirty = false;
    setHeader(`已保存 ${result.entries.length} 条`, "saved");
    $("#formMessage").textContent = `已保存 ${result.entries.length} 条术语`;
    return true;
  } catch (error) {
    $("#formMessage").textContent = error.message;
    $("#formMessage").className = "error";
    setHeader("保存失败", "error");
    return false;
  } finally {
    button.disabled = false;
    button.textContent = "保存词库";
  }
}

async function importGlossary(event) {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (!file.name.toLowerCase().endsWith(".xlsx")) throw new Error("请导入热词表 Excel 文件（.xlsx）");
    const form = new FormData();
    form.append("file", file);
    const value = await api("/api/glossary/import-xlsx", { method: "POST", body: form });
    const entries = value.entries || [];
    const destination = activeGlossaryId === "all" ? "global" : activeGlossaryId;
    entries.slice().reverse().forEach((entry) => addEntry({ ...entry, glossary_id: destination }, false));
    markDirty(`已导入 ${entries.length} 条，保存后生效`);
    applyFilter();
  } catch (error) {
    $("#formMessage").textContent = error.message;
    $("#formMessage").className = "error";
  }
  event.target.value = "";
}

async function exportGlossary() {
  try {
    await window.SubstarSystemSaveAs.saveUrl(window.SubstarSystemSaveAs.glossarySpec("xlsx", "/api/glossary/export-xlsx"));
  } catch (error) {
    $("#formMessage").textContent = `导出失败：${error.message}`;
    $("#formMessage").className = "error";
  }
}

function createCollection(event) {
  event.preventDefault();
  const name = $("#collectionName").value.trim();
  const error = $("#collectionNameError");
  error.textContent = "";
  if (!name) { error.textContent = "请输入词库名称"; $("#collectionName").focus(); return; }
  if (collections.some((item) => item.name.toLocaleLowerCase() === name.toLocaleLowerCase())) {
    error.textContent = "已有同名词库，请换一个名称";
    $("#collectionName").focus();
    return;
  }
  const item = { id: `glossary_${crypto.randomUUID().replaceAll("-", "")}`, name, kind: "project", injection_permissions: $$("#collectionPermissions input:checked").map(input => input.value) };
  collections.push(item);
  $("#collectionDialog").close();
  $("#collectionName").value = "";
  markDirty(`已新建“${name}”，保存后生效`);
  renderCollections();
  activateGlossary(item.id);
}

function requestDeleteCollection(id) {
  const item = collections.find((value) => value.id === id && value.kind === "project");
  if (!item) return;
  pendingDeleteCollectionId = id;
  const count = collectEntries().filter((entry) => entry.glossary_id === id).length;
  $("#deleteCollectionDescription").textContent = `“${item.name}”包含 ${count} 条术语。`;
  $("#deleteCollectionDialog").showModal();
}

function deleteCollection(event) {
  event.preventDefault();
  const id = pendingDeleteCollectionId;
  const item = collections.find((value) => value.id === id);
  if (!item) return $("#deleteCollectionDialog").close();
  $$(".entry-card").forEach((card) => {
    if ($('[data-field="glossary_id"]', card).value === id) card.remove();
  });
  collections = collections.filter((value) => value.id !== id);
  pendingDeleteCollectionId = "";
  $("#deleteCollectionDialog").close();
  if (activeGlossaryId === id) activeGlossaryId = "all";
  markDirty(`已删除“${item.name}”，保存后生效`);
  renderCollections();
  activateGlossary(activeGlossaryId);
  updateCounts();
}

function renderPermissions() {
  const panel = $("#glossaryPermissions");
  panel.replaceChildren();
  panel.hidden = !collections.some(c => c.kind === "project" && (activeGlossaryId === "all" || c.id === activeGlossaryId));
  for (const collection of collections.filter(c => c.kind === "project" && (activeGlossaryId === "all" || c.id === activeGlossaryId))) {
    const row = document.createElement("div"); row.className = "permission-row";
    const title = document.createElement("strong"); title.textContent = collection.name; row.append(title);
    for (const [stage, label] of [["asr","Qwen 听写"],["calibration","AI 校准"],["translation","翻译"]]) {
      const field = document.createElement("label"), check = document.createElement("input"); check.type = "checkbox";
      check.checked = (collection.injection_permissions || []).includes(stage);
      check.addEventListener("change", () => {const permissions = new Set(collection.injection_permissions || []); check.checked ? permissions.add(stage) : permissions.delete(stage); collection.injection_permissions = [...permissions]; markDirty();});
      field.append(check, document.createTextNode(label)); row.append(field);
    }
    panel.append(row);
  }
}
function renderCandidates() {
  $("#candidateDestination").replaceChildren(...collectionOptions(activeGlossaryId === "all" ? "global" : activeGlossaryId));
  const pending = candidates.filter(c => c.status === "pending");
  $("#candidateCount").textContent = pending.length;
  $("#candidateSelectAll").checked = false;
  $("#candidateList").replaceChildren();
  for (const candidate of pending) {
    const row = document.createElement("label"); row.className = "candidate-row";
    const check = document.createElement("input"); check.type = "checkbox"; check.value = candidate.id;
    const word = document.createElement("strong"); word.textContent = candidate.source;
    const target = document.createElement("span"); target.textContent = candidate.target || "—";
    const origin = document.createElement("small"); origin.textContent = (candidate.sources || []).map(s => ({manual:"手动填写",ai_direct:"AI 生成",ai_asr:"听写生成",external:"外部模型",asr_generated:"听写生成"}[s.kind] || s.kind) + (s.project ? ` · ${s.project}` : "")).join("；");
    row.append(check, word, target, origin); $("#candidateList").append(row);
  }
  $("#candidateMessage").textContent = pending.length ? "审核后才参与词库注入。" : "暂无待审核热词";
}
async function reviewCandidates(action) {
  const ids = $$("#candidateList input:checked").map(n=>n.value);
  const destination = $("#candidateDestination").value;
  if (!ids.length) { $("#candidateMessage").textContent = "请先选择候选词"; return; }
  try {
    if (dirty && !await saveGlossary()) return;
    const result = await api("/api/glossary/candidates/review", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ids, action, glossary_id:destination})});
    renderLibrary(result); setHeader("已保存", "saved");
  } catch(e) { $("#candidateMessage").textContent = e.message; }
}
$("#candidateSelectAll").addEventListener("change", e=>$$("#candidateList input").forEach(n=>n.checked=e.target.checked));
$("#approveCandidates").addEventListener("click", ()=>reviewCandidates("approve"));
$("#ignoreCandidates").addEventListener("click", ()=>reviewCandidates("ignore"));

$("#addEntry").addEventListener("click", () => addEntry());
$("#addEntryTop").addEventListener("click", () => addEntry());
$("#emptyState button").addEventListener("click", () => addEntry());
$("#searchInput").addEventListener("input", applyFilter);
$$('[data-glossary]').forEach((button) => button.addEventListener("click", () => activateGlossary(button.dataset.glossary)));
$("#addCollection").addEventListener("click", () => {
  $("#collectionName").value = "";
  $("#collectionNameError").textContent = "";
  $$("#collectionPermissions input").forEach(input => { input.checked = false; });
  $("#collectionDialog").showModal();
  $("#collectionName").focus();
});
$("#confirmCollection").addEventListener("click", createCollection);
$("#confirmDeleteCollection").addEventListener("click", deleteCollection);
$("#deleteCollectionDialog").addEventListener("close", () => { pendingDeleteCollectionId = ""; });
$("#importButton").addEventListener("click", () => $("#importFile").click());
$("#importFile").addEventListener("change", importGlossary);
$$('[data-export]').forEach((button) => button.addEventListener("click", exportGlossary));
$("#saveButton").addEventListener("click", saveGlossary);
window.addEventListener("beforeunload", (event) => { if (dirty && !navigationInProgress) { event.preventDefault(); event.returnValue = ""; } });
let navigationInProgress = false;
let pendingNavigationUrl = "";
let navigationSaving = false;
const navigationDialog = $("#unsavedNavigationDialog");
document.addEventListener("click", (event) => {
  const link = event.target.closest(".app-header a[href]");
  if (!link || navigationInProgress || !dirty || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target === "_blank") return;
  const target = new URL(link.href, window.location.href);
  if (target.origin !== window.location.origin || (target.pathname === window.location.pathname && target.search === window.location.search)) return;
  event.preventDefault();
  pendingNavigationUrl = target.href;
  $("#unsavedNavigationStatus").textContent = "";
  navigationDialog.showModal();
}, true);
navigationDialog.addEventListener("cancel", event => { if (navigationSaving) event.preventDefault(); });
navigationDialog.addEventListener("close", () => { if (!navigationInProgress) pendingNavigationUrl = ""; });
navigationDialog.addEventListener("click", async (event) => {
  const action = event.target.closest("[data-unsaved-navigation]")?.dataset.unsavedNavigation;
  if (!action || navigationSaving) return;
  if (action === "stay") { navigationDialog.close(); return; }
  if (action === "save") {
    navigationSaving = true;
    const buttons = $$("[data-unsaved-navigation]", navigationDialog);
    buttons.forEach(button => { button.disabled = true; });
    $("#unsavedNavigationStatus").textContent = "正在保存…";
    const saved = await saveGlossary();
    navigationSaving = false;
    buttons.forEach(button => { button.disabled = false; });
    if (!saved) { $("#unsavedNavigationStatus").textContent = $("#formMessage").textContent || "保存失败，请重试"; return; }
  }
  if (pendingNavigationUrl) { navigationInProgress = true; window.location.assign(pendingNavigationUrl); }
});
loadGlossary();
