const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let activeFilter = "all";
let dirty = false;

const typeLabels = {
  person: "人名",
  organization: "机构",
  place: "地名",
  program: "节目",
  product: "产品／品牌",
  technical: "专业术语",
  other: "其他",
};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      message = (await response.json()).detail || message;
    } catch (_) {}
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

function makeEntry(entry = {}) {
  const card = $("#entryTemplate").content.firstElementChild.cloneNode(true);
  card.dataset.id = entry.id || crypto.randomUUID().replaceAll("-", "");
  const field = (name) => $(`[data-field="${name}"]`, card);
  field("enabled").checked = entry.enabled !== false;
  field("source").value = entry.source || "";
  field("standard_source").value = entry.standard_source || "";
  field("target").value = entry.target || "";
  field("aliases").value = (entry.aliases || []).join(", ");
  field("type").value = entry.type || "other";
  field("scope").value = entry.scope || "global";
  field("project").value = entry.project || "";
  field("hotword_weight").value = entry.hotword_weight || 4;
  field("notes").value = entry.notes || "";
  field("case_sensitive").checked = Boolean(entry.case_sensitive);
  field("do_not_translate").checked = Boolean(entry.do_not_translate);

  const updateMeta = () => {
    $(".type-chip", card).textContent =
      typeLabels[field("type").value] || "其他";
    $(".project-field", card).classList.toggle(
      "hidden",
      field("scope").value !== "project",
    );
    applyFilter();
    updateCounts();
  };
  card.addEventListener("input", () => {
    markDirty();
    updateMeta();
  });
  $(".expand-entry", card).addEventListener("click", () =>
    card.classList.toggle("open"),
  );
  $(".delete-entry", card).addEventListener("click", () => {
    card.remove();
    markDirty("已删除词条，保存后生效");
    applyFilter();
    updateCounts();
  });
  updateMeta();
  return card;
}

function addEntry(entry = {}, focus = true) {
  const card = makeEntry(entry);
  $("#entryList").prepend(card);
  $("#emptyState").hidden = true;
  if (focus) {
    card.classList.add("open");
    $('[data-field="source"]', card).focus();
    markDirty("新增词条尚未保存");
  }
  updateCounts();
  return card;
}

function collectEntries() {
  return $$(".entry-card")
    .map((card) => {
      const field = (name) => $(`[data-field="${name}"]`, card);
      return {
        id: card.dataset.id,
        source: field("source").value.trim(),
        standard_source: field("standard_source").value.trim(),
        target: field("target").value.trim(),
        type: field("type").value,
        case_sensitive: field("case_sensitive").checked,
        do_not_translate: field("do_not_translate").checked,
        hotword_weight: Number(field("hotword_weight").value || 4),
        aliases: field("aliases")
          .value.split(/[,，;；\n]+/)
          .map((value) => value.trim())
          .filter(Boolean),
        notes: field("notes").value.trim(),
        scope: field("scope").value,
        project:
          field("scope").value === "project"
            ? field("project").value.trim()
            : "",
        enabled: field("enabled").checked,
      };
    })
    .filter((entry) => entry.source);
}

function applyFilter() {
  const query = $("#searchInput").value.trim().toLocaleLowerCase();
  $$(".entry-card").forEach((card) => {
    const scope = $('[data-field="scope"]', card).value;
    const values = [
      "source",
      "standard_source",
      "target",
      "aliases",
      "project",
      "notes",
    ]
      .map((name) => $(`[data-field="${name}"]`, card)?.value || "")
      .join(" ")
      .toLocaleLowerCase();
    card.classList.toggle(
      "filtered",
      (activeFilter !== "all" && scope !== activeFilter) ||
        (query && !values.includes(query)),
    );
  });
}

function updateCounts() {
  const entries = collectEntries();
  $("#allCount").textContent = entries.length;
  $("#enabledCount").textContent = entries.filter(
    (item) => item.enabled,
  ).length;
  $("#projectCount").textContent = entries.filter(
    (item) => item.scope === "project",
  ).length;
  $("#emptyState").hidden = $$(".entry-card").length > 0;
}

async function loadGlossary() {
  try {
    const result = await api("/api/glossary");
    $("#entryList").innerHTML = "";
    (result.entries || [])
      .slice()
      .reverse()
      .forEach((entry) => addEntry(entry, false));
    dirty = false;
    setHeader(`已载入 ${result.entries?.length || 0} 条`, "saved");
    $("#formMessage").textContent = "词库尚未修改";
    updateCounts();
    applyFilter();
  } catch (error) {
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
      body: JSON.stringify({ entries: collectEntries() }),
    });
    $("#entryList").innerHTML = "";
    (result.entries || [])
      .slice()
      .reverse()
      .forEach((entry) => addEntry(entry, false));
    dirty = false;
    setHeader(`已保存 ${result.entries.length} 条`, "saved");
    $("#formMessage").textContent = `已保存 ${result.entries.length} 条术语`;
    updateCounts();
    applyFilter();
  } catch (error) {
    $("#formMessage").textContent = error.message;
    $("#formMessage").className = "error";
    setHeader("保存失败", "error");
  } finally {
    button.disabled = false;
    button.textContent = "保存词库";
  }
}

function quoteCsv(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}
async function exportGlossary(format) {
  const saveAs = window.SubstarSystemSaveAs;
  if (format === "xlsx") {
    $(".export-menu").open = false;
    try {
      await saveAs.saveUrl(saveAs.glossarySpec("xlsx", "/api/glossary/export-xlsx"));
    } catch (error) {
      $("#formMessage").textContent = `导出失败：${error.message}`;
      $("#formMessage").className = "error";
    }
    return;
  }
  const entries = collectEntries();
  let text;
  let type;
  if (format === "csv") {
    const fields = [
      "source",
      "standard_source",
      "target",
      "aliases",
      "type",
      "scope",
      "project",
      "case_sensitive",
      "do_not_translate",
      "enabled",
      "hotword_weight",
      "notes",
    ];
    text = [
      fields.join(","),
      ...entries.map((entry) =>
        fields
          .map((field) =>
            quoteCsv(
              field === "aliases" ? entry.aliases.join(";") : entry[field],
            ),
          )
          .join(","),
      ),
    ].join("\r\n");
    type = "text/csv;charset=utf-8";
  } else {
    text = JSON.stringify({ entries }, null, 2);
    type = "application/json";
  }
  const blob = new Blob(["\ufeff", text], { type });
  $(".export-menu").open = false;
  try {
    await saveAs.saveBlob(saveAs.glossarySpec(format), blob);
  } catch (error) {
    $("#formMessage").textContent = `导出失败：${error.message}`;
    $("#formMessage").className = "error";
  }
}

function parseCsv(text) {
  const rows = [];
  let row = [],
    cell = "",
    quoted = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        cell += '"';
        index++;
      } else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n") {
      row.push(cell.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      cell = "";
    } else cell += char;
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  if (!rows.length) return [];
  const headers = rows
    .shift()
    .map((value) => value.trim().replace(/^\ufeff/, ""));
  return rows
    .filter((row) => row.some(Boolean))
    .map((row) =>
      Object.fromEntries(
        headers.map((header, index) => [header, row[index] || ""]),
      ),
    )
    .map((item) => ({
      ...item,
      aliases: (item.aliases || "").split(/[;；]/).filter(Boolean),
      case_sensitive: /^(true|1|yes)$/i.test(item.case_sensitive),
      do_not_translate: /^(true|1|yes)$/i.test(item.do_not_translate),
      enabled: !item.enabled || /^(true|1|yes)$/i.test(item.enabled),
    }));
}

async function importGlossary(event) {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const isXlsx = file.name.toLowerCase().endsWith(".xlsx");
    if (!isXlsx) throw new Error("请导入热词表 Excel 文件（.xlsx）");
    const form = new FormData();
    form.append("file", file);
    const value = await api("/api/glossary/import-xlsx", { method: "POST", body: form });
    const entries = Array.isArray(value) ? value : value.entries;
    if (!Array.isArray(entries))
      throw new Error("导入文件必须包含 entries 数组");
    entries
      .slice()
      .reverse()
      .forEach((entry) => addEntry(entry, false));
    markDirty(`已导入 ${entries.length} 条，保存后生效`);
    applyFilter();
    updateCounts();
  } catch (error) {
    $("#formMessage").textContent = error.message;
    $("#formMessage").className = "error";
  }
  event.target.value = "";
}

$("#addEntry").addEventListener("click", () => addEntry());
$("#addEntryTop").addEventListener("click", () => addEntry());
$("#emptyState button").addEventListener("click", () => addEntry());
$("#searchInput").addEventListener("input", applyFilter);
$$(".filter-tabs button").forEach((button) =>
  button.addEventListener("click", () => {
    $$(".filter-tabs button").forEach((item) =>
      item.classList.remove("active"),
    );
    button.classList.add("active");
    activeFilter = button.dataset.filter;
    applyFilter();
  }),
);
$("#importButton").addEventListener("click", () => $("#importFile").click());
$("#importFile").addEventListener("change", importGlossary);
$$("[data-export]").forEach((button) =>
  button.addEventListener("click", () => exportGlossary(button.dataset.export)),
);
$("#saveButton").addEventListener("click", saveGlossary);
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
loadGlossary();
