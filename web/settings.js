const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const form = $("#settingsForm");
let settings = null;
let dirty = false;
let editRevision = 0;
let autoSaveTimer = null;
let saveQueue = Promise.resolve();
const sensitiveAutoSaveFields = new Set(["api_key", "alignment_api_key", "translation_api_key"]);
let recognitionProfiles = [];
let runtimeIdentity = null;
let edition = "standard";
let selectedModelProvider = "deepseek";
let savedModelProvider = "deepseek";
let modelProviderCatalog = [];
let modelProviderDrafts = {};
let preserveConnectedStateOnProviderSwitch = false;
let reasoningCapabilityCache = new Map();
let connectionModelCapability = null;
let reasoningRefreshTimer = null;
let connectionModelValue = "";
const inheritedModelStages = new Set();
const explicitlyConfiguredThinkingStages = new Set();
const explicitlyConfiguredEffortStages = new Set();
let promptCatalogData = null;
let promptCatalogPromise = null;
let selectedPromptCategory = "segmentation";
let selectedPromptComponent = null;
let loadedPromptText = "";
const connectionStateKey = "substar.settings.connected-providers.v1";
const shortcutDefaults = {
  undo: "Ctrl+Z", redo: "Ctrl+Y", play_pause: "Space", hide_cue: "Backspace",
};
const singleKeyShortcutCommands = new Set(["play_pause", "hide_cue"]);
const providerModelCatalogs = new Map();
let connectedProviders = (() => {
  try {
    const value = JSON.parse(localStorage.getItem(connectionStateKey) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (_) {
    return {};
  }
})();

function saveConnectedProviders() {
  try {
    localStorage.setItem(connectionStateKey, JSON.stringify(connectedProviders));
  } catch (_) {
    // The labels can still update for the current page if storage is unavailable.
  }
}

function shortcutFromEvent(event, { allowSingleKey = false } = {}) {
  if (["Control", "Shift", "Alt", "Meta"].includes(event.key)) return "";
  if (!event.ctrlKey && !event.altKey && !event.metaKey && !allowSingleKey) return "";
  const parts = [];
  if (event.ctrlKey) parts.push("Ctrl");
  if (event.altKey) parts.push("Alt");
  if (event.shiftKey) parts.push("Shift");
  if (event.metaKey) parts.push("Meta");
  const key = event.key.length === 1 ? event.key.toUpperCase() : event.key;
  parts.push(key === " " ? "Space" : key);
  return parts.join("+");
}

function setShortcutMessage(text, error = false) {
  const message = $("#shortcutMessage");
  if (!message) return;
  message.textContent = text;
  message.classList.toggle("error", error);
}

function assignShortcut(input, value) {
  const collision = $$('[data-shortcut-input]').find((other) =>
    other !== input && String(other.value || "").toLowerCase() === value.toLowerCase()
  );
  if (collision) {
    setShortcutMessage(`快捷键 ${value} 已分配给另一个命令。`, true);
    return;
  }
  input.value = value;
  input.dispatchEvent(new Event("input", {bubbles:true}));
  setShortcutMessage(`已设置为 ${value}，正在保存。`);
}

function setProviderConnected(provider, connected) {
  if (connected) connectedProviders[provider] = Date.now();
  else delete connectedProviders[provider];
  saveConnectedProviders();
}

function syncProviderItemStates() {
  $$('[data-model-provider]').forEach((button) => {
    const provider = button.dataset.modelProvider;
    const verified = Boolean(connectedProviders[`model:${provider}`]);
    const configured = Boolean(settings?.model_provider_key_set?.[provider]);
    const active = provider === selectedModelProvider;
    button.classList.toggle("configured", configured);
    const state = $(".provider-item-state", button);
    if (state) state.textContent = configured
      ? (active ? "正在使用" : "已配置")
      : (verified ? "已验证" : "待配置");
  });
  $$('[data-engine-profile]').forEach((button) => {
    const active = button.classList.contains("active");
    const configured = Boolean(connectedProviders[`engine:${button.dataset.engineProfile}`]) || (
      button.dataset.engineProfile === "qwen_cloud" &&
      Boolean(settings?.api_key_set) &&
      !form.elements.api_key.value.trim()
    );
    button.classList.toggle("configured", configured);
    const state = $(".provider-item-state", button);
    if (state) state.textContent = configured ? (active ? "正在使用" : "已配置") : "待配置";
  });
}

const modelProviderNames = {};

const engineProviderNames = {
  qwen_cloud: "Qwen 云端 · 文件听写",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>\"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#039;",
  })[character]);
}

function inferModelProvider(baseUrl) {
  const value = String(baseUrl || "").toLowerCase();
  if (value.includes("deepseek.com")) return "deepseek";
  if (value.includes("bigmodel.cn")) return "glm";
  if (value.includes("openai.azure.com") || value.includes("/deployments/")) return "azure_openai";
  if (value.includes("api.openai.com")) return "openai";
  if (value.includes("deerapi.com")) return "deerapi";
  if (value.includes("generativelanguage.googleapis.com")) return "gemini";
  if (value.includes("siliconflow")) return "siliconflow";
  if (value.includes("dashscope") || value.includes("aliyuncs")) return "qwen";
  return "custom";
}

function providerDefinition(providerId) {
  return modelProviderCatalog.find((item) => item.id === providerId) || null;
}

function renderModelProviderRail() {
  const container = $("#modelProviderList");
  if (!container) return;
  container.innerHTML = modelProviderCatalog.map((provider) => `
    <button class="provider-item" type="button" data-model-provider="${escapeHtml(provider.id)}"
      data-base-url="${escapeHtml(provider.base_url || "")}" data-default-model="${escapeHtml(provider.default_model || "")}">
      <span class="provider-mark"><svg class="ui-icon"><use href="/assets/ui-icons.svg#cloud"></use></svg></span>
      <span><b>${escapeHtml(provider.label)}</b><small>${escapeHtml(provider.description)}</small></span>
      <span class="provider-item-state"></span>
    </button>`).join("");
  for (const provider of modelProviderCatalog) modelProviderNames[provider.id] = provider.label;
}

async function loadModelProviderCatalog() {
  const response = await api("/api/models/providers");
  modelProviderCatalog = Array.isArray(response.providers) ? response.providers : [];
  renderModelProviderRail();
}

function captureModelProviderDraft(providerId = selectedModelProvider) {
  if (!providerId || !form.elements.translation_api_base_url) return;
  modelProviderDrafts[providerId] = {
    base_url: String(form.elements.translation_api_base_url.value || "").trim(),
    model: String(form.elements.translation_api_model.value || "").trim(),
    auth_mode: String(form.elements.translation_api_auth_mode.value || "bearer"),
    timeout_seconds: Number(form.elements.translation_api_timeout_seconds.value || 300),
  };
}

function loadModelProviderDraft(providerId) {
  const definition = providerDefinition(providerId) || {};
  const profile = modelProviderDrafts[providerId] || {};
  form.elements.translation_api_base_url.value = profile.base_url ?? definition.base_url ?? "";
  const model = profile.model ?? definition.default_model ?? "";
  form.elements.translation_api_model.value = model;
  form.elements.translation_api_auth_mode.value = profile.auth_mode || definition.auth_mode || "bearer";
  form.elements.translation_api_timeout_seconds.value = profile.timeout_seconds || 300;
  const baseHint = $("#baseUrlHint");
  if (baseHint) baseHint.textContent = definition.base_url_hint || "兼容 OpenAI Chat Completions 的服务地址";
  setConnectionModel(model);
}

function syncModelProvider(provider = null) {
  const baseInput = form.elements.translation_api_base_url;
  if (!baseInput) return;
  selectedModelProvider = provider || inferModelProvider(baseInput.value);
  $$('[data-model-provider]').forEach((button) => {
    button.classList.toggle("active", button.dataset.modelProvider === selectedModelProvider);
  });
  syncProviderItemStates();
  $("#modelProviderTitle").textContent = modelProviderNames[selectedModelProvider] || "自定义服务";
  const configured = Boolean(settings?.model_provider_key_set?.[selectedModelProvider]);
  $("#apiBadge").textContent = configured ? "已配置" : "未配置";
  $("#keyHint").textContent = configured
    ? "当前服务商 API Key 已安全保存；留空不会覆盖"
    : "当前服务商尚未保存 API Key";
}

const stageDefinitions = {
  segmentation: [
    ["segmentation", "语义切分", "结合上下文生成更自然的字幕切点"],
    ["segmentation_repair", "Fallback · 切分坏块修复", "任一切分块验收失败后单块重跑；模型允许时关闭思考"],
  ],
  translation: [
    ["translation", "字幕翻译", "结合上下文生成最终 Cue 译文"],
    ["translation_repair", "Fallback · 翻译坏块修复", "只重跑未返回或验收失败的意义组；模型允许时关闭思考"],
  ],
  audit: [
    ["calibration", "AI 校准", "自动应用大小写、标点与确定性文本修复；默认思考 Low"],
  ],
};

const reasoningEffortLabels = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "XHigh",
  max: "Max",
};
const allReasoningEfforts = Object.keys(reasoningEffortLabels);
const nonThinkingPreferredStages = new Set([
  "segmentation_repair", "translation_repair", "audit_repair",
]);
const modelSettingsFields = new Set([
  "translation_api_provider", "translation_api_base_url", "translation_api_model",
  "translation_api_auth_mode", "translation_api_timeout_seconds",
  "translation_api_key", "clear_translation_api_key",
  ...Object.values(stageDefinitions).flatMap((definitions) =>
    definitions.flatMap(([stage]) => [
      `stage_${stage}_model`, `stage_${stage}_thinking_mode`,
      `stage_${stage}_reasoning_effort`, `stage_${stage}_max_tokens`,
      `stage_${stage}_temperature`,
    ]),
  ),
]);

function renderStageSettings(containerId, definitions) {
  const container = document.getElementById(containerId);
  const options = allReasoningEfforts
    .map((value) => `<option value="${value}">${reasoningEffortLabels[value]}</option>`)
    .join("");
  container.innerHTML = definitions.map(([stage, title, description]) => `
    <article class="stage-config-card" data-stage="${stage}">
      <header><div><b>${title}</b><small>${description}</small></div><span class="stage-mode-pill"></span></header>
      <div class="stage-config-grid">
        <label class="field wide">模型<input name="stage_${stage}_model" list="officialModelIds" placeholder="跟随连接测试模型" /><select class="model-catalog-select" data-stage-model-select="${stage}" aria-label="${title}可用模型"><option value="">选择可用模型</option></select></label>
        <label class="field">思考<select name="stage_${stage}_thinking_mode"><option value="disabled">不思考</option><option value="enabled">思考</option></select></label>
        <label class="field reasoning-field">推理强度<select name="stage_${stage}_reasoning_effort">${options}</select><small data-reasoning-note>正在读取模型能力…</small></label>
        <label class="field">输出上限<input name="stage_${stage}_max_tokens" type="number" min="256" max="393216" step="256" /></label>
        <label class="field temperature-field">Temperature<input name="stage_${stage}_temperature" type="number" min="0" max="2" step="0.1" /></label>
      </div>
    </article>`).join("");
}

renderStageSettings("segmentationStageSettings", stageDefinitions.segmentation);
renderStageSettings("translationStageSettings", stageDefinitions.translation);
renderStageSettings("auditStageSettings", stageDefinitions.audit);

function stageModel(stage) {
  return String(
    form.elements[`stage_${stage}_model`]?.value
      || form.elements.translation_api_model?.value
      || "",
  ).trim();
}

function reasoningCapabilityKey(baseUrl, authMode, model) {
  return [baseUrl, authMode, model].join("\u0000");
}

async function fetchReasoningCapability(model) {
  const baseUrl = String(form.elements.translation_api_base_url?.value || "").trim();
  const authMode = String(form.elements.translation_api_auth_mode?.value || "bearer");
  if (!baseUrl || !model) return null;
  const key = reasoningCapabilityKey(baseUrl, authMode, model);
  if (!reasoningCapabilityCache.has(key)) {
    const request = api("/api/models/reasoning-capabilities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: baseUrl, model }),
    }).catch((error) => {
      reasoningCapabilityCache.delete(key);
      throw error;
    });
    reasoningCapabilityCache.set(key, request);
  }
  return reasoningCapabilityCache.get(key);
}

function applyReasoningCapability(stage, capability, { modelChanged = false } = {}) {
  const select = form.elements[`stage_${stage}_reasoning_effort`];
  const thinkingSelect = form.elements[`stage_${stage}_thinking_mode`];
  const note = document.querySelector(`[data-stage="${stage}"] [data-reasoning-note]`);
  if (!select || !capability) return;
  const declaredThinkingModes = Array.isArray(capability.supported_thinking_modes) && capability.supported_thinking_modes.length
    ? capability.supported_thinking_modes.filter((value) => ["enabled", "disabled"].includes(value))
    : [];
  const thinkingModes = declaredThinkingModes.length ? declaredThinkingModes : ["enabled"];
  const requestedThinking = thinkingSelect?.value || "disabled";
  if (thinkingSelect) {
    thinkingSelect.innerHTML = thinkingModes.map((value) => {
      const singleRequired = thinkingModes.length === 1;
      const label = value === "enabled"
        ? (singleRequired ? "思考（模型要求）" : "思考")
        : (singleRequired ? "不思考（模型要求）" : "不思考");
      return `<option value="${value}">${label}</option>`;
    }).join("");
    const preferredThinking = nonThinkingPreferredStages.has(stage) && thinkingModes.includes("disabled")
      ? "disabled"
      : (thinkingModes.includes("enabled") ? "enabled" : thinkingModes[0]);
    const preserveThinking = explicitlyConfiguredThinkingStages.has(stage) || !modelChanged;
    thinkingSelect.value = preserveThinking && thinkingModes.includes(requestedThinking)
      ? requestedThinking
      : preferredThinking;
  }
  const levels = allReasoningEfforts;
  const current = select.value;
  const mapped = capability.effort_selection_aliases?.[current] || current;
  select.innerHTML = levels
    .filter((value) => reasoningEffortLabels[value])
    .map((value) => `<option value="${value}">${reasoningEffortLabels[value]}</option>`)
    .join("");
  select.disabled = thinkingSelect?.value !== "enabled";
  const preserveEffort = explicitlyConfiguredEffortStages.has(stage) || !modelChanged;
  select.value = preserveEffort && levels.includes(current) ? current : "low";
  if (note) {
    const mapping = Object.entries(capability.effort_selection_aliases || {})
      .map(([from, to]) => `${reasoningEffortLabels[from] || from}→${reasoningEffortLabels[to] || to}`)
      .join("，");
    note.textContent = mapping ? `五档可选；当前服务商映射：${mapping}` : "五档可选；由当前服务商映射到实际档位";
    note.title = capability.source || "";
  }
}

async function refreshReasoningCapabilities({ changedStages = new Set() } = {}) {
  const stages = [...new Set(Object.values(stageDefinitions).flatMap((items) => items.map(([stage]) => stage)))];
  const entries = await Promise.all(stages.map(async (stage) => {
    try {
      return [stage, await fetchReasoningCapability(stageModel(stage))];
    } catch (_) {
      return [stage, null];
    }
  }));
  for (const [stage, capability] of entries) {
    applyReasoningCapability(stage, capability, { modelChanged: changedStages.has(stage) });
  }
  try {
    connectionModelCapability = await fetchReasoningCapability(
      String(form.elements.translation_api_model?.value || "").trim(),
    );
  } catch (_) {
    connectionModelCapability = null;
  }
  const global = connectionModelCapability
    || entries.find(([stage]) => stage === "segmentation")?.[1]
    || entries[0]?.[1];
  const summary = $("#reasoningCapabilitySummary");
  if (summary && global) {
    const modes = global.supported_thinking_modes || [];
    summary.textContent = modes.length
      ? `接口接受：${modes.map((mode) => mode === "enabled" ? "思考" : "非思考").join(" / ")}；推理强度固定显示五档并自动映射。`
      : "测试连通性时会同时验证思考与非思考模式；推理强度固定显示五档并自动映射。";
  }
  syncStageControls();
}

function scheduleReasoningCapabilitiesRefresh() {
  clearTimeout(reasoningRefreshTimer);
  reasoningRefreshTimer = setTimeout(() => { refreshReasoningCapabilities(); }, 180);
}

function setConnectionModel(value) {
  const next = String(value || "").trim();
  const changedStages = new Set();
  for (const definitions of Object.values(stageDefinitions)) {
    for (const [stage] of definitions) {
      const input = form.elements[`stage_${stage}_model`];
      if (!input) continue;
      if (inheritedModelStages.has(stage) || !input.value.trim() || input.value.trim() === connectionModelValue) {
        inheritedModelStages.add(stage);
        input.value = next;
        changedStages.add(stage);
      }
    }
  }
  connectionModelValue = next;
  reasoningCapabilityCache.clear();
  clearTimeout(reasoningRefreshTimer);
  reasoningRefreshTimer = setTimeout(() => refreshReasoningCapabilities({ changedStages }), 180);
}

const numericFields = new Set([
  "startup_port",
  "whisper_beam_size",
  "whisperx_batch_size",
  "qwen_cloud_request_timeout_seconds",
  "qwen_cloud_task_timeout_seconds",
  "qwen_cloud_poll_interval_seconds",
  "translation_api_timeout_seconds",
  "english_hard_limit",
  "chinese_hard_limit",
  "mixed_hard_limit",
  "japanese_hard_limit",
  "korean_hard_limit",
  "minimum_cue_duration_ms",
  "maximum_cue_duration_ms",
  "snap_threshold_ms",
  "tail_padding_ms",
  "segmentation_chunk_seconds",
  "segmentation_overlap_seconds",
  "segmentation_batch_groups",
  "http_retry_attempts",
  "segmentation_repair_attempts",
  "stage_timeout_seconds",
  "translation_workers",
  "runtime_worker_concurrency",
  "runtime_cloud_concurrency",
  "runtime_media_concurrency",
  "runtime_gpu_concurrency",
  "runtime_download_concurrency",
  "target_visual_width_limit",
  "maximum_cps_latin",
  "maximum_cps_cjk",
  ...Object.values(stageDefinitions).flatMap((definitions) =>
    definitions.flatMap(([stage]) => [
      `stage_${stage}_max_tokens`,
      `stage_${stage}_temperature`,
    ]),
  ),
]);

function syncStageControls() {
  $$(".stage-config-card[data-stage]").forEach((card) => {
    const stage = card.dataset.stage;
    const thinking = form.elements[`stage_${stage}_thinking_mode`].value;
    const enabled = thinking === "enabled";
    const effort = form.elements[`stage_${stage}_reasoning_effort`];
    effort.disabled = !enabled || effort.options.length === 0;
    form.elements[`stage_${stage}_temperature`].disabled = enabled;
    $(".stage-mode-pill", card).textContent = enabled
      ? `思考 · ${reasoningEffortLabels[form.elements[`stage_${stage}_reasoning_effort`].value] || form.elements[`stage_${stage}_reasoning_effort`].value}`
      : "Non-thinking";
    card.classList.toggle("thinking-enabled", enabled);
  });
}

function syncRecognitionProfile() {
  const select = form.elements.recognition_profile_id;
  if (!select) return;
  const profile = recognitionProfiles.find((item) => item.id === select.value);
  if (!profile) return;
  const recognitionConfigured = Boolean(connectedProviders[`engine:${profile.id}`]) || (
    profile.id === "qwen_cloud" && Boolean(settings?.api_key_set)
    && !form.elements.api_key.value.trim()
  );
  const recognitionBadge = $("#recognitionBadge");
  if (recognitionBadge) recognitionBadge.textContent = recognitionConfigured ? "已配置" : "未配置";
  $("#recognitionProfileDescription").textContent = profile.available
    ? profile.description
    : `${profile.description} 缺少依赖：${(profile.missing_modules || []).join(", ")}`;
  $("#transcriptAdapterLabel").textContent = profile.transcript_adapter;
  $("#alignmentAdapterLabel").textContent = profile.alignment_adapter;
  $("#engineProviderTitle").textContent = engineProviderNames[profile.id] || profile.label;
  $$('[data-engine-profile]').forEach((button) => {
    button.classList.toggle("active", button.dataset.engineProfile === profile.id);
  });
  syncProviderItemStates();
  $$('[data-recognition-profiles]').forEach((node) => {
    const supported = String(node.dataset.recognitionProfiles || "").split(/\s+/);
    node.classList.toggle("hidden", !supported.includes(profile.id));
  });
}

function syncDownloadControls() {
  const source = form.elements.model_download_source?.value || "china_mirror";
  const endpoint = form.elements.hf_endpoint;
  if (endpoint) endpoint.disabled = source !== "custom";
}

function syncRuntimePortHint() {
  const node = $("#runtimePortHint");
  const input = form.elements.startup_port;
  if (!node || !input) return;
  const savedPort = Number(input.value || 8769);
  if (!runtimeIdentity) {
    node.textContent = `下次启动将使用 ${savedPort}；当前运行实例无法识别。`;
    return;
  }
  const currentPort = Number(runtimeIdentity.port);
  node.textContent = currentPort === savedPort
    ? `当前运行端口：${currentPort}。保存后下次启动仍使用该端口。`
    : `当前运行端口：${currentPort}；下次启动端口：${savedPort}。关闭并重新启动 CMD 后生效。`;
}

async function loadRecognitionProfiles() {
  const value = await api("/api/recognition/profiles");
  edition = value.edition || "standard";
  recognitionProfiles = value.profiles || [];
  const select = form.elements.recognition_profile_id;
  select.replaceChildren(...recognitionProfiles.map((profile) => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = `${profile.label}${profile.available ? "" : " · 未安装"}`;
    return option;
  }));
  if (edition === "slim") {
    $$('[data-engine-profile]').forEach((button) => {
      button.classList.toggle("hidden", button.dataset.engineProfile !== "qwen_cloud");
    });
    $$(".stage-config-card[data-stage]").forEach((card) => {
      const keep = new Set(["segmentation", "segmentation_repair", "translation", "translation_repair", "calibration"]);
      card.classList.toggle("hidden", !keep.has(card.dataset.stage));
    });
    const localAssets = $("#environmentAssets")?.closest(".environment-section");
    const localProfiles = $("#environmentProfiles")?.closest(".environment-section");
    localAssets?.classList.add("hidden");
    localProfiles?.classList.add("hidden");
  }
}

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

function switchPanel(name) {
  if (name === "environment") name = "api";
  if (["shortcuts", "advanced"].includes(name)) name = "general";
  $$(".category").forEach((button) =>
    button.classList.toggle("active", button.dataset.panel === name),
  );
  $$("[data-settings-panel]").forEach((panel) =>
    panel.classList.toggle("active", panel.dataset.settingsPanel === name),
  );
  $(".save-bar")?.classList.toggle("panel-hidden", ["environment", "prompts"].includes(name));
  history.replaceState(null, "", `#${name}`);
  if (name === "prompts") loadPromptCatalog();
}

const promptLanguageLabels = {
  en: "英文", zh: "中文", ja: "日文", ko: "韩文", mixed: "混合语种",
  default: "默认", generic: "通用", reference: "参考文稿",
  reconstruct: "标点重建", unpunctuated: "无标点重建",
};

function promptVariantLabel(value) {
  if (value.includes("_to_")) {
    const [source, target] = value.split("_to_");
    return `${promptLanguageLabels[source] || source} → ${promptLanguageLabels[target] || target}`;
  }
  return promptLanguageLabels[value] || value;
}

function promptKindLabel(kind) {
  return ({template: "核心模板", rules: "语言规则", case: "构造案例"})[kind] || kind;
}

function promptComponentRecord(path) {
  return promptCatalogData?.components.find((item) => item.path === path) || null;
}

function promptHasUnsavedChanges() {
  return Boolean(selectedPromptComponent) && $("#promptSourceView").value !== loadedPromptText;
}

function mayDiscardPromptChanges(nextPath) {
  if (!promptHasUnsavedChanges() || selectedPromptComponent?.path === nextPath) return true;
  return window.confirm("当前提示词有未保存的修改。要放弃修改并切换吗？");
}

async function showPromptComponent(path, skipDiscardCheck = false) {
  const record = promptComponentRecord(path);
  if (!record) return;
  if (!skipDiscardCheck && !mayDiscardPromptChanges(path)) return;
  $("#promptInspectorKind").textContent = promptKindLabel(record.kind).toUpperCase();
  $("#promptInspectorTitle").textContent = record.title;
  $("#promptInspectorCount").textContent = `${record.characters.toLocaleString()} 字符`;
  $("#promptFileMeta").innerHTML = `<code>${escapeHtml(record.path)}</code><span>SHA-256 · ${escapeHtml(record.sha256.slice(0, 12))}</span>`;
  $("#promptSourceView").value = "正在读取提示词正文…";
  $("#promptSourceView").disabled = true;
  $("#promptSaveButton").disabled = true;
  $("#promptReloadButton").disabled = true;
  $("#promptSaveMessage").textContent = "正在载入…";
  try {
    const component = await api(`/api/prompts/content?path=${encodeURIComponent(path)}`);
    selectedPromptComponent = component;
    loadedPromptText = component.text;
    $("#promptSourceView").value = component.text;
    $("#promptSourceView").disabled = false;
    $("#promptReloadButton").disabled = false;
    $("#promptSaveMessage").textContent = "未修改";
  } catch (error) {
    selectedPromptComponent = null;
    $("#promptSourceView").value = `读取失败：${error.message}`;
    $("#promptSaveMessage").textContent = "读取失败";
  }
}

function updatePromptDirtyState() {
  const dirty = Boolean(selectedPromptComponent) && $("#promptSourceView").value !== loadedPromptText;
  $("#promptSaveButton").disabled = !dirty;
  $("#promptSaveMessage").textContent = dirty ? "有未保存的修改" : "未修改";
}

async function savePromptComponent() {
  if (!selectedPromptComponent) return;
  $("#promptSaveButton").disabled = true;
  $("#promptReloadButton").disabled = true;
  $("#promptSaveMessage").textContent = "正在保存…";
  try {
    const updated = await api(`/api/prompts/content?path=${encodeURIComponent(selectedPromptComponent.path)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: $("#promptSourceView").value,
        expected_sha256: selectedPromptComponent.sha256,
      }),
    });
    selectedPromptComponent = updated;
    loadedPromptText = updated.text;
    const record = promptComponentRecord(updated.path);
    if (record) {
      record.sha256 = updated.sha256;
      record.characters = updated.characters;
      record.title = updated.title;
    }
    $("#promptInspectorTitle").textContent = updated.title;
    $("#promptInspectorCount").textContent = `${updated.characters.toLocaleString()} 字符`;
    $("#promptFileMeta").innerHTML = `<code>${escapeHtml(updated.path)}</code><span>SHA-256 · ${escapeHtml(updated.sha256.slice(0, 12))}</span>`;
    $("#promptSaveMessage").textContent = "已保存；后续读取将使用新版本";
  } catch (error) {
    $("#promptSaveMessage").textContent = `保存失败：${error.message}`;
    $("#promptSaveButton").disabled = false;
  } finally {
    $("#promptReloadButton").disabled = false;
  }
}

function showPromptRoute(family, variant, skipDiscardCheck = false) {
  if (!skipDiscardCheck && !mayDiscardPromptChanges(variant.files[0])) return;
  $$(".prompt-route-pill").forEach((button) => button.classList.remove("active"));
  document.querySelector(`[data-prompt-family="${family.id}"][data-prompt-variant="${variant.id}"]`)?.classList.add("active");
  $("#promptRouteChain").classList.add("prompt-route-chain");
  $("#promptRouteChain").innerHTML = variant.files.map((path, index) => {
    const record = promptComponentRecord(path);
    return `${index ? '<i>→</i>' : ''}<button type="button" data-prompt-component="${escapeHtml(path)}"><small>${promptKindLabel(record?.kind)}</small><b>${escapeHtml(record?.title || path)}</b></button>`;
  }).join("");
  showPromptComponent(variant.files[0], true);
}

function renderPromptCategory(categoryId) {
  if (!promptCatalogData) return;
  const category = promptCatalogData.categories.find((item) => item.id === categoryId);
  const families = promptCatalogData.families.filter((item) => item.category === categoryId);
  const firstFamily = families[0];
  if (firstFamily?.variants[0] && !mayDiscardPromptChanges(firstFamily.variants[0].files[0])) return;
  selectedPromptCategory = categoryId;
  $$(".prompt-category-tab").forEach((button) => button.classList.toggle("active", button.dataset.promptCategory === categoryId));
  $("#promptCategoryTitle").textContent = category?.title || categoryId;
  $("#promptCategorySummary").textContent = `${families.length} 个提示词族 · ${families.reduce((sum, item) => sum + item.variants.length, 0)} 条路由`;
  $("#promptFamilyList").innerHTML = families.map((family) => {
    const files = new Set(family.variants.flatMap((variant) => variant.files));
    return `<article class="prompt-family-card" data-prompt-family-card="${escapeHtml(family.id)}">
      <header><div><small>${escapeHtml(family.id)}</small><h4>${escapeHtml(family.title)}</h4><p>${escapeHtml(family.description)}</p></div><span>v${escapeHtml(family.version)}</span></header>
      <div class="prompt-family-stats"><span>${family.variants.length} 条路由</span><span>${files.size} 个组件</span></div>
      <div class="prompt-route-pills">${family.variants.map((variant) => `<button class="prompt-route-pill" type="button" data-prompt-family="${escapeHtml(family.id)}" data-prompt-variant="${escapeHtml(variant.id)}">${escapeHtml(promptVariantLabel(variant.id))}<i>${variant.files.length}</i></button>`).join("")}</div>
    </article>`;
  }).join("");
  if (firstFamily?.variants[0]) showPromptRoute(firstFamily, firstFamily.variants[0], true);
}

function renderPromptCatalog(catalog) {
  promptCatalogData = catalog;
  const stats = catalog.stats;
  $("#promptBadge").textContent = `${stats.components} 项`;
  const metrics = $$("#promptMetrics article");
  const values = [stats.families, stats.variants, stats.components, `${stats.core_components} / ${stats.cases}`];
  metrics.forEach((card, index) => { $("strong", card).textContent = values[index]; });
  $("#promptCategoryTabs").innerHTML = catalog.categories.map((category) => {
    const count = catalog.families.filter((item) => item.category === category.id).length;
    return `<button class="prompt-category-tab" type="button" data-prompt-category="${escapeHtml(category.id)}"><b>${escapeHtml(category.title)}</b><small>${escapeHtml(category.description)}</small><i>${count}</i></button>`;
  }).join("");
  renderPromptCategory(catalog.categories.some((item) => item.id === selectedPromptCategory) ? selectedPromptCategory : catalog.categories[0]?.id);
}

async function loadPromptCatalog() {
  if (promptCatalogData) return promptCatalogData;
  if (!promptCatalogPromise) {
    promptCatalogPromise = api("/api/prompts").then((catalog) => {
      renderPromptCatalog(catalog);
      return catalog;
    }).catch((error) => {
      $("#promptFamilyList").innerHTML = `<div class="prompt-loading error">读取失败：${escapeHtml(error.message)}</div>`;
      $("#promptBadge").textContent = "异常";
      throw error;
    });
  }
  return promptCatalogPromise;
}

function populate(value) {
  settings = value;
  selectedModelProvider = value.active_model_provider || inferModelProvider(value.translation_api_base_url);
  savedModelProvider = selectedModelProvider;
  modelProviderDrafts = { ...(value.model_provider_profiles || {}) };
  modelProviderDrafts[selectedModelProvider] = {
    ...(modelProviderDrafts[selectedModelProvider] || {}),
    base_url: value.translation_api_base_url,
    model: value.translation_api_model,
    auth_mode: value.translation_api_auth_mode,
    timeout_seconds: value.translation_api_timeout_seconds,
  };
  for (const [name, current] of Object.entries(value)) {
    const input = form.elements[name];
    if (!input || name.endsWith("_key_set")) continue;
    if (input.type === "checkbox") input.checked = Boolean(current);
    else input.value = current ?? "";
  }
  connectionModelValue = String(form.elements.translation_api_model?.value || "").trim();
  inheritedModelStages.clear();
  explicitlyConfiguredThinkingStages.clear();
  explicitlyConfiguredEffortStages.clear();
  for (const definitions of Object.values(stageDefinitions)) {
    for (const [stage] of definitions) {
      const stageValue = String(form.elements[`stage_${stage}_model`]?.value || "").trim();
      if (!stageValue || stageValue === connectionModelValue) inheritedModelStages.add(stage);
    }
  }
  const localPersonalization = window.SubstarTheme?.read();
  if (localPersonalization) {
    for (const name of ["appearance_mode", "accent_color", "surface_style", "ui_density", "motion_level", "font_scale"]) {
      if (form.elements[name]) form.elements[name].value = localPersonalization[name];
    }
  }
  form.elements.translation_api_key.value = "";
  form.elements.clear_translation_api_key.checked = false;
  form.elements.api_key.value = "";
  window.SubstarTheme?.apply(localPersonalization);
  const configured = Boolean(value.translation_api_key_set);
  $("#apiBadge").textContent = configured ? "已配置" : "未配置";
  $("#recognitionBadge").textContent = value.api_key_set ? "已配置" : "未配置";
  $("#keyHint").textContent = configured
    ? "API Key 已安全保存；留空不会覆盖"
    : "尚未保存 API Key";
  $("#qwenCloudKeyHint").textContent = value.api_key_set
    ? "Qwen 云端听写密钥已安全保存在本机；输入新密钥并保存会覆盖，留空则继续使用。"
    : "Qwen 云端听写密钥尚未保存；输入后保存即可配置。";
  syncModelProvider(selectedModelProvider);
  syncStageControls();
  syncRecognitionProfile();
  syncDownloadControls();
  syncRuntimePortHint();
  dirty = false;
  setHeader("配置已载入", "saved");
}

function buildPayload() {
  captureModelProviderDraft();
  const payload = { ...settings };
  delete payload.api_key_set;
  delete payload.alignment_api_key_set;
  delete payload.translation_api_key_set;
  payload.api_key = "";
  payload.alignment_api_key = "";
  payload.clear_api_key = false;
  payload.clear_alignment_api_key = false;
  for (const element of form.elements) {
    if (!element.name || element.disabled) continue;
    if (element.type === "checkbox") payload[element.name] = element.checked;
    else if (numericFields.has(element.name))
      payload[element.name] = Number(element.value);
    else payload[element.name] = element.value;
  }
  payload.active_model_provider = selectedModelProvider;
  payload.model_provider_profiles = { ...modelProviderDrafts };
  // Every LLM stage follows the active OpenAI-compatible model service.
  payload.alignment_api_provider = payload.translation_api_provider;
  payload.alignment_api_base_url = payload.translation_api_base_url;
  payload.alignment_api_model = payload.translation_api_model;
  payload.alignment_api_auth_mode = payload.translation_api_auth_mode;
  // Segmentation and translation share the same configured model service.
  // Mirror a newly entered key into both protected backend key slots.
  payload.alignment_api_key = payload.translation_api_key;
  payload.clear_alignment_api_key = payload.clear_translation_api_key;
  return payload;
}

async function loadSettings() {
  try {
    await Promise.all([loadRecognitionProfiles(), loadModelProviderCatalog()]);
    try {
      runtimeIdentity = await api("/api/runtime/identity");
    } catch (_) {
      runtimeIdentity = null;
    }
    populate(await api("/api/settings"));
    await refreshReasoningCapabilities();
    discoverModels();
  } catch (error) {
    setHeader("读取失败", "error");
    $("#formMessage").textContent = error.message;
    $("#formMessage").className = "error";
  }
}

function renderModelCatalogOptions(models) {
  const list = $("#officialModelIds");
  list.replaceChildren(...models.map((item) => {
    const option = document.createElement("option");
    option.value = String(typeof item === "string" ? item : item.id || "");
    return option;
  }).filter((option) => option.value));
  const values = models.map((item) => String(typeof item === "string" ? item : item.id || "")).filter(Boolean);
  $$(".model-catalog-select").forEach((select) => {
    const selected = select.value;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = values.length ? `选择可用模型（${values.length}）` : "暂无模型列表，可手动输入";
    select.replaceChildren(placeholder, ...values.map((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      return option;
    }));
    select.value = values.includes(selected) ? selected : "";
  });
}

async function discoverModels() {
  const status = $("#modelDiscoveryStatus");
  const button = $("#refreshModelCatalog");
  $("#officialModelIds").replaceChildren();
  status.textContent = "正在读取当前服务商的模型列表…";
  if (button) button.disabled = true;
  try {
    const f = form.elements;
    const catalog = await api("/api/models/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: f.translation_api_base_url.value,
        auth_mode: f.translation_api_auth_mode.value,
        provider_id: selectedModelProvider,
        api_key: f.translation_api_key.value,
      }),
    });
    const models = catalog.models || catalog.data || [];
    providerModelCatalogs.set(selectedModelProvider, models);
    renderModelCatalogOptions(models);
    status.textContent = models.length
      ? `已载入 ${models.length} 个可用模型；连接测试与所有 Stage 共用此列表。`
      : "服务商未返回模型列表，仍可手动输入模型 ID。";
  } catch (error) {
    const cached = providerModelCatalogs.get(selectedModelProvider) || [];
    renderModelCatalogOptions(cached);
    status.textContent = cached.length
      ? `刷新失败，继续使用已载入的 ${cached.length} 个模型：${error.message}`
      : `无法读取模型列表，可手动输入模型 ID：${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

function currentPersonalization() {
  return {
    appearance_mode: form.elements.appearance_mode.value,
    accent_color: form.elements.accent_color.value,
    surface_style: form.elements.surface_style.value,
    ui_density: form.elements.ui_density.value,
    motion_level: form.elements.motion_level.value,
    font_scale: form.elements.font_scale.value,
  };
}

async function persistSettings({ manual = false, revision = editRevision } = {}) {
  const personalization = {
    ...currentPersonalization(),
  };
  const button = $("#saveButton");
  if (manual) {
    button.disabled = true;
    button.textContent = "保存中…";
  } else {
    $("#formMessage").textContent = "自动保存中…";
  }
  $("#formMessage").className = "";
  try {
    if (!form.elements.translation_api_base_url.value.trim() || !form.elements.translation_api_model.value.trim()) {
      throw new Error("当前 LLM 服务商的 Base URL 和模型 ID 不能为空");
    }
    if (
      !settings?.model_provider_key_set?.[selectedModelProvider]
      && !form.elements.translation_api_key.value.trim()
      && !form.elements.clear_translation_api_key.checked
    ) {
      throw new Error("切换服务商时必须输入该服务商的 API Key；原服务商密钥不会被自动清除或借用");
    }
    const saved = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    });
    window.SubstarTheme?.save(personalization);
    settings = { ...saved, ...personalization };
    savedModelProvider = settings.active_model_provider || inferModelProvider(settings.translation_api_base_url);
    if (revision === editRevision) {
      populate(settings);
      await refreshReasoningCapabilities();
      setHeader(manual ? "已保存" : "已自动保存", "saved");
      $("#formMessage").textContent = manual ? "已保存" : "已自动保存";
    }
    return saved;
  } catch (error) {
    $("#formMessage").textContent = error.message;
    $("#formMessage").className = "error";
    setHeader("保存失败", "error");
    throw error;
  } finally {
    if (manual) {
      button.disabled = false;
      button.textContent = "保存设置";
    }
  }
}

function scheduleAutoSave(delay = 800) {
  clearTimeout(autoSaveTimer);
  const revision = editRevision;
  autoSaveTimer = setTimeout(() => {
    saveQueue = saveQueue
      .catch(() => undefined)
      .then(() => persistSettings({ manual: false, revision }))
      .catch(() => undefined);
  }, delay);
}

function saveSettings(event) {
  event.preventDefault();
  clearTimeout(autoSaveTimer);
  const revision = editRevision;
  saveQueue = saveQueue
    .catch(() => undefined)
    .then(() => persistSettings({ manual: true, revision }))
    .catch(() => undefined);
}

async function testConnection() {
  const button = $(".test-api");
  const result = $("#testResult");
  button.disabled = true;
  button.textContent = "连接中…";
  result.className = "test-result wide";
  result.textContent = "正在请求模型，请稍候…";
  try {
    const f = form.elements;
    if (!settings?.model_provider_key_set?.[selectedModelProvider] && !f.translation_api_key.value.trim()) {
      throw new Error("请先输入当前服务商的 API Key；切换服务商不会借用或清除原服务商密钥");
    }
    const response = await api("/api/settings/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        role: "translation",
        source: "api",
        provider: f.translation_api_provider.value,
        provider_id: selectedModelProvider,
        base_url: f.translation_api_base_url.value,
        model: f.translation_api_model.value,
        auth_mode: f.translation_api_auth_mode.value,
        timeout_seconds: Number(f.translation_api_timeout_seconds.value),
        api_key: f.translation_api_key.value,
        thinking_mode: "",
        reasoning_effort: "high",
      }),
    });
    result.textContent = `${response.message || "连接成功"}；正在验证思考模式…`;
    const model = String(f.translation_api_model.value || "").trim();
    const capability = await api("/api/models/reasoning-probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: f.translation_api_base_url.value,
        model,
        auth_mode: f.translation_api_auth_mode.value,
        timeout_seconds: Number(f.translation_api_timeout_seconds.value),
        api_key: f.translation_api_key.value,
        provider_id: selectedModelProvider,
      }),
    });
    const key = reasoningCapabilityKey(f.translation_api_base_url.value, f.translation_api_auth_mode.value, model);
    reasoningCapabilityCache.set(key, Promise.resolve(capability));
    connectionModelCapability = capability;
    const modes = capability.probe?.accepted_thinking_modes || capability.supported_thinking_modes || [];
    result.textContent = `${response.message || "连接成功"}；接口接受：${modes.length ? modes.map((mode) => mode === "enabled" ? "思考" : "非思考").join(" / ") : "未确认"}`;
    result.classList.add("good");
    setProviderConnected(`model:${selectedModelProvider}`, true);
    syncProviderItemStates();
    await refreshReasoningCapabilities();
  } catch (error) {
    result.textContent = error.message;
    result.classList.add("bad");
    setProviderConnected(`model:${selectedModelProvider}`, false);
    syncProviderItemStates();
  } finally {
    button.disabled = false;
    button.textContent = "测试连通性";
  }
}

async function testQwenCloudConnection() {
  const button = $(".test-qwen-cloud");
  const result = $("#qwenCloudTestResult");
  button.disabled = true;
  button.textContent = "连接中…";
  result.className = "test-result wide";
  result.textContent = "正在验证 DashScope 密钥和临时上传权限…";
  try {
    const f = form.elements;
    const regionBase = f.qwen_cloud_region.value === "singapore"
      ? "https://dashscope-intl.aliyuncs.com/api/v1"
      : "https://dashscope.aliyuncs.com/api/v1";
    const response = await api("/api/settings/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        role: "sentence",
        source: "qwen_cloud",
        provider: "qwen_cloud",
        base_url: f.qwen_cloud_base_url.value || regionBase,
        model: f.qwen_cloud_model.value,
        auth_mode: "bearer",
        timeout_seconds: Number(f.qwen_cloud_request_timeout_seconds.value),
        api_key: f.api_key.value,
      }),
    });
    result.textContent = response.message || "Qwen 云端听写已联通";
    result.classList.add("good");
    setProviderConnected("engine:qwen_cloud", true);
    syncProviderItemStates();
  } catch (error) {
    result.textContent = error.message;
    result.classList.add("bad");
    setProviderConnected("engine:qwen_cloud", false);
    syncProviderItemStates();
  } finally {
    button.disabled = false;
    button.textContent = "测试连通性";
  }
}

function assetState(asset) {
  if (asset.ready) return ["可用", "ready"];
  if (asset.installed) return ["缺少依赖", "warning"];
  return ["未安装", "missing"];
}

function modelAssetMarkup(asset, compact = false) {
  const [label, state] = assetState(asset);
  const missing = (asset.missing_modules || []).length
    ? `缺少 Python 依赖：${asset.missing_modules.join(", ")}`
    : asset.path || "尚未在程序 models 目录中找到完整资产";
  const profiles = (asset.profiles || []).map((id) => engineProviderNames[id] || id).join(" · ");
  return `<article class="model-asset-row ${state}" data-asset-id="${escapeHtml(asset.id)}">
    <div class="asset-status-dot"></div>
    <div class="asset-main"><header><b>${escapeHtml(asset.label)}</b><span class="asset-state ${state}">${label}</span></header>
      <p>${escapeHtml(asset.purpose)}${compact ? "" : ` · ${escapeHtml(profiles)}`}</p>
      <small title="${escapeHtml(missing)}">${escapeHtml(missing)}</small>
      <div class="asset-download-message" hidden></div>
    </div>
    <div class="asset-actions">
      ${asset.downloadable && !asset.installed ? `<button class="secondary asset-download" type="button" data-download-asset="${escapeHtml(asset.id)}">下载安装</button>` : ""}
      ${asset.installed ? `<button class="secondary settings-jump" type="button" data-jump-panel="recognition" data-engine-target="${escapeHtml((asset.profiles || [])[0] || "")}">查看配置</button>` : ""}
      <a class="asset-official-link" href="${escapeHtml(asset.official_url)}" target="_blank" rel="noreferrer">官方地址 ↗</a>
    </div>
  </article>`;
}

function renderModelAssets(report) {
  const container = $("#modelAssetList");
  if (!container) return;
  const root = $("#modelAssetRoot");
  if (root) root.textContent = report.asset_root || "—";
  container.innerHTML = (report.assets || []).map((asset) => modelAssetMarkup(asset)).join("");
}

function renderEnvironment(report) {
  const labels = {
    python: "Python", ffmpeg: "FFmpeg", ffprobe: "FFprobe",
    nvidia_gpu: "NVIDIA GPU / 驱动", torch: "PyTorch / CUDA", disk_free: "可用磁盘空间",
  };
  const assets = report.assets || [];
  const installedAssets = assets.filter((asset) => asset.installed).length;
  if ($("#environmentBadge")) $("#environmentBadge").textContent = `${report.ready_profile_count || 0}/${report.profile_count || 0} 可用`;
  $("#environmentProfilesMetric").textContent = `${report.ready_profile_count || 0} / ${report.profile_count || 0}`;
  $("#environmentGpuMetric").textContent = report.gpu_acceleration ? "CUDA 可用" : "CPU / 待配置";
  $("#environmentAssetsMetric").textContent = `${installedAssets} / ${assets.length}`;
  $("#environmentSummary").textContent = `${report.package?.edition || "development"} · ${report.ready ? "基础组件可用" : "基础组件不完整"} · 模型目录已统一`;
  $("#environmentChecks").innerHTML = Object.entries(report.checks || {}).map(([name, item]) => {
    const detail = [item.value, item.path, item.message].filter(Boolean).join(" · ");
    const state = item.status || (item.ok ? "ready" : "missing");
    return `<article class="runtime-row ${escapeHtml(state)}"><span class="runtime-icon"><svg class="ui-icon"><use href="/assets/ui-icons.svg#${item.ok ? "check" : "close"}"></use></svg></span><div><b>${escapeHtml(labels[name] || name)}</b><small title="${escapeHtml(detail)}">${escapeHtml(detail)}</small></div><i>${item.ok ? "可用" : state === "optional" ? "可选" : "需处理"}</i></article>`;
  }).join("");
  $("#environmentAssets").innerHTML = assets.map((asset) => modelAssetMarkup(asset, true)).join("");
  $("#environmentProfiles").innerHTML = (report.profiles || []).map((profile) => {
    const details = profile.ready
      ? "依赖与模型资产完整"
      : [
          (profile.missing_assets || []).length ? `缺少 ${profile.missing_assets.length} 项模型资产` : "",
          (profile.missing_modules || []).length ? `缺少依赖：${profile.missing_modules.join(", ")}` : "",
        ].filter(Boolean).join(" · ");
    return `<article class="profile-health ${profile.ready ? "ready" : "missing"}"><span>${profile.ready ? "可运行" : "待配置"}</span><div><b>${escapeHtml(profile.label)}</b><small>${escapeHtml(details)}</small></div><button class="secondary settings-jump" data-jump-panel="recognition" data-engine-target="${escapeHtml(profile.id)}" type="button">配置</button></article>`;
  }).join("");
  renderModelAssets(report);
}

async function detectEnvironment() {
  const button = $("#detectEnvironment");
  button.disabled = true;
  try { renderEnvironment(await api("/api/environment/status")); }
  catch (error) { $("#environmentSummary").textContent = error.message; }
  finally { button.disabled = false; }
}

async function configureEnvironment() {
  const button = $("#configureEnvironment");
  button.disabled = true;
  button.textContent = "配置中…";
  try { renderEnvironment((await api("/api/environment/configure", { method: "POST" })).status); }
  catch (error) { $("#environmentSummary").textContent = error.message; }
  finally { button.disabled = false; button.textContent = "初始化程序目录"; }
}

async function startAssetDownload(assetId, button) {
  button.disabled = true;
  button.textContent = "准备下载…";
  const row = button.closest(".model-asset-row");
  const message = row?.querySelector(".asset-download-message");
  if (message) message.hidden = false;
  try {
    let job = await api(`/api/model-assets/${encodeURIComponent(assetId)}/download`, { method: "POST" });
    if (message) message.textContent = job.message;
    while (["queued", "running"].includes(job.status)) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      job = await api(`/api/model-assets/downloads/${encodeURIComponent(job.job_id)}`);
      if (message) message.textContent = job.message;
    }
    if (job.status !== "completed") throw new Error(job.message || "下载失败");
    await detectEnvironment();
  } catch (error) {
    button.disabled = false;
    button.textContent = "重试下载";
    if (message) message.textContent = error.message;
  }
}

function jumpToSettings(panel, anchor = "", engineTarget = "") {
  switchPanel(panel);
  if (engineTarget && form.elements.recognition_profile_id) {
    form.elements.recognition_profile_id.value = engineTarget;
    syncRecognitionProfile();
  }
  const target = anchor ? document.getElementById(anchor) : null;
  if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
}

$("#modelProviderList")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-model-provider]");
    if (!button) return;
    const provider = button.dataset.modelProvider;
    const providerChanged = provider !== selectedModelProvider;
    if (providerChanged) captureModelProviderDraft(selectedModelProvider);
    selectedModelProvider = provider;
    loadModelProviderDraft(provider);
    form.elements.translation_api_key.value = "";
    form.elements.clear_translation_api_key.checked = false;
    syncModelProvider(provider);
    preserveConnectedStateOnProviderSwitch = true;
    form.elements.translation_api_base_url.dispatchEvent(new Event("input", { bubbles: true }));
    preserveConnectedStateOnProviderSwitch = false;
    discoverModels();
});

form.elements.translation_api_key.addEventListener("input", () => {
  setProviderConnected(`model:${selectedModelProvider}`, false);
  if (form.elements.translation_api_key.value.trim()) {
    form.elements.clear_translation_api_key.checked = false;
  }
  syncModelProvider(selectedModelProvider);
});

for (const name of ["translation_api_base_url", "translation_api_model"]) {
  form.elements[name]?.addEventListener("input", () => {
    if (preserveConnectedStateOnProviderSwitch) return;
    setProviderConnected(`model:${selectedModelProvider}`, false);
    syncProviderItemStates();
    if (name === "translation_api_model") setConnectionModel(form.elements[name].value);
  });
  form.elements[name]?.addEventListener("change", scheduleReasoningCapabilitiesRefresh);
}

for (const definitions of Object.values(stageDefinitions)) {
  for (const [stage] of definitions) {
    form.elements[`stage_${stage}_model`]?.addEventListener("input", () => {
      const input = form.elements[`stage_${stage}_model`];
      if (!input.value.trim() || input.value.trim() === connectionModelValue) inheritedModelStages.add(stage);
      else inheritedModelStages.delete(stage);
    });
    form.elements[`stage_${stage}_model`]?.addEventListener("change", () => {
      reasoningCapabilityCache.clear();
      refreshReasoningCapabilities({ changedStages: new Set([stage]) });
    });
    form.elements[`stage_${stage}_thinking_mode`]?.addEventListener("change", () => {
      explicitlyConfiguredThinkingStages.add(stage);
      syncStageControls();
    });
    form.elements[`stage_${stage}_reasoning_effort`]?.addEventListener("change", () => {
      explicitlyConfiguredEffortStages.add(stage);
    });
  }
}

for (const name of ["api_key", "qwen_cloud_base_url", "qwen_cloud_model", "qwen_cloud_region"]) {
  form.elements[name]?.addEventListener("input", () => {
    setProviderConnected("engine:qwen_cloud", false);
    syncProviderItemStates();
  });
}

$$('[data-engine-profile]').forEach((button) => {
  button.addEventListener("click", () => {
    const select = form.elements.recognition_profile_id;
    if (![...select.options].some((option) => option.value === button.dataset.engineProfile)) return;
    select.value = button.dataset.engineProfile;
    select.dispatchEvent(new Event("input", { bubbles: true }));
  });
});

$$(".category").forEach((button) =>
  button.addEventListener("click", () => switchPanel(button.dataset.panel)),
);
$("#promptCategoryTabs")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-prompt-category]");
  if (button) renderPromptCategory(button.dataset.promptCategory);
});
$$('[data-shortcut-input]').forEach((input) => {
  input.addEventListener("focus", () => {
    input.classList.add("is-recording");
    const single = singleKeyShortcutCommands.has(input.dataset.shortcutInput);
    setShortcutMessage(single ? "请按 Space 或 Backspace；按 Esc 取消。" : "请按下包含 Ctrl、Alt 或 Meta 的组合键；按 Esc 取消。");
  });
  input.addEventListener("blur", () => input.classList.remove("is-recording"));
  input.addEventListener("keydown", (event) => {
    event.preventDefault();
    if (event.key === "Escape") return input.blur();
    const single = singleKeyShortcutCommands.has(input.dataset.shortcutInput);
    const shortcut = shortcutFromEvent(event, {allowSingleKey:single});
    if (!shortcut || (single && !["Space", "Backspace"].includes(shortcut))) {
      return setShortcutMessage(single ? "此命令只能设置为 Space 或 Backspace。" : "快捷键必须包含 Ctrl、Alt 或 Meta。", true);
    }
    assignShortcut(input, shortcut);
    input.blur();
  });
});
$$('[data-shortcut-reset]').forEach((button) => button.addEventListener("click", () => {
  const command = button.dataset.shortcutReset;
  assignShortcut(form.elements[`shortcut_${command}`], shortcutDefaults[command]);
}));
$("#promptFamilyList")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-prompt-family][data-prompt-variant]");
  if (!button || !promptCatalogData) return;
  const family = promptCatalogData.families.find((item) => item.id === button.dataset.promptFamily);
  const variant = family?.variants.find((item) => item.id === button.dataset.promptVariant);
  if (family && variant) showPromptRoute(family, variant);
});
$("#promptRouteChain")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-prompt-component]");
  if (button) showPromptComponent(button.dataset.promptComponent);
});
$("#promptSourceView")?.addEventListener("input", updatePromptDirtyState);
$("#promptSaveButton")?.addEventListener("click", savePromptComponent);
$("#promptReloadButton")?.addEventListener("click", () => {
  if (selectedPromptComponent) showPromptComponent(selectedPromptComponent.path, true);
});
window.addEventListener("beforeunload", (event) => {
  if (!promptHasUnsavedChanges()) return;
  event.preventDefault();
  event.returnValue = "";
});
$(".test-api").addEventListener("click", testConnection);
$("#refreshModelCatalog")?.addEventListener("click", discoverModels);
$("#officialModelSelect")?.addEventListener("change", (event) => {
  if (!event.target.value) return;
  form.elements.translation_api_model.value = event.target.value;
  form.elements.translation_api_model.dispatchEvent(new Event("input", {bubbles:true}));
  form.elements.translation_api_model.dispatchEvent(new Event("change", {bubbles:true}));
});
$$('[data-stage-model-select]').forEach((select) => select.addEventListener("change", () => {
  if (!select.value) return;
  const input = form.elements[`stage_${select.dataset.stageModelSelect}_model`];
  input.value = select.value;
  input.dispatchEvent(new Event("input", {bubbles:true}));
  input.dispatchEvent(new Event("change", {bubbles:true}));
}));
$(".test-qwen-cloud").addEventListener("click", testQwenCloudConnection);
$("#detectEnvironment").addEventListener("click", detectEnvironment);
$("#configureEnvironment").addEventListener("click", configureEnvironment);
$("#refreshModelAssets")?.addEventListener("click", detectEnvironment);
document.addEventListener("click", (event) => {
  const download = event.target.closest("[data-download-asset]");
  if (download) {
    startAssetDownload(download.dataset.downloadAsset, download);
    return;
  }
  const jump = event.target.closest(".settings-jump");
  if (jump) jumpToSettings(
    jump.dataset.jumpPanel || "recognition",
    jump.dataset.jumpAnchor || "localModelAssets",
    jump.dataset.engineTarget || "",
  );
});
$(".reveal-key").addEventListener("click", (event) => {
  const input = form.elements.translation_api_key;
  input.type = input.type === "password" ? "text" : "password";
  event.currentTarget.setAttribute("aria-label", input.type === "password" ? "显示密钥" : "隐藏密钥");
});
form.addEventListener("input", (event) => {
  if (event.target?.matches?.("[data-background-input]")) return;
  editRevision += 1;
  if (["appearance_mode", "accent_color", "surface_style", "ui_density", "motion_level", "font_scale"].includes(event.target?.name)) {
    window.SubstarTheme?.preview({
      appearance_mode: form.elements.appearance_mode.value,
      accent_color: form.elements.accent_color.value,
      surface_style: form.elements.surface_style.value,
      ui_density: form.elements.ui_density.value,
      motion_level: form.elements.motion_level.value,
      font_scale: form.elements.font_scale.value,
    });
  }
  syncStageControls();
  syncRecognitionProfile();
  syncDownloadControls();
  syncModelProvider(selectedModelProvider);
  syncRuntimePortHint();
  if (!dirty) {
    setHeader("有未保存修改");
    $("#formMessage").textContent = "修改尚未保存";
  }
  dirty = true;
  if (!sensitiveAutoSaveFields.has(event.target?.name) && !modelSettingsFields.has(event.target?.name)) {
    scheduleAutoSave();
  }
});
form.addEventListener("change", (event) => {
  if (sensitiveAutoSaveFields.has(event.target?.name) && !modelSettingsFields.has(event.target?.name)) {
    scheduleAutoSave(150);
  }
});
form.addEventListener("submit", saveSettings);
const backgroundPreviewUrls = new Map();

function setBackgroundCardPreview(card, record) {
  const page = card.dataset.backgroundPage;
  const oldUrl = backgroundPreviewUrls.get(page);
  if (oldUrl) URL.revokeObjectURL(oldUrl);
  backgroundPreviewUrls.delete(page);
  const preview = card.querySelector(".background-page-preview");
  const name = card.querySelector("[data-background-name]");
  const remove = card.querySelector("[data-background-remove]");
  const applyAll = card.querySelector("[data-background-all]");
  if (record?.blob) {
    const url = URL.createObjectURL(record.blob);
    backgroundPreviewUrls.set(page, url);
    preview.style.backgroundImage = `linear-gradient(180deg, transparent, rgb(0 0 0 / 45%)), url("${url}")`;
    name.textContent = record.name || "已设置背景图";
    card.classList.add("has-image");
    remove.disabled = false;
    applyAll.disabled = false;
  } else {
    preview.style.removeProperty("background-image");
    name.textContent = "使用默认背景";
    card.classList.remove("has-image");
    remove.disabled = true;
    applyAll.disabled = true;
  }
}

async function refreshBackgroundCards() {
  if (!window.SubstarBackgrounds) return;
  const records = await window.SubstarBackgrounds.list();
  document.querySelectorAll("[data-background-page]").forEach((card) => {
    setBackgroundCardPreview(card, records[card.dataset.backgroundPage]);
  });
}

document.addEventListener("click", async (event) => {
  const card = event.target.closest("[data-background-page]");
  if (!card || !window.SubstarBackgrounds) return;
  if (event.target.closest("[data-background-select]")) {
    card.querySelector("[data-background-input]").click();
    return;
  }
  if (event.target.closest("[data-background-remove]")) {
    card.classList.add("is-loading");
    try {
      await window.SubstarBackgrounds.remove(card.dataset.backgroundPage);
      setBackgroundCardPreview(card, null);
      $("#formMessage").textContent = `${window.SubstarBackgrounds.labels[card.dataset.backgroundPage]}背景已移除`;
    } catch (error) { $("#formMessage").textContent = error.message; }
    finally { card.classList.remove("is-loading"); }
    return;
  }
  if (event.target.closest("[data-background-all]")) {
    card.classList.add("is-loading");
    try {
      await window.SubstarBackgrounds.applyToAll(card.dataset.backgroundPage);
      await refreshBackgroundCards();
      $("#formMessage").textContent = "背景图已应用到全部页面";
    } catch (error) { $("#formMessage").textContent = error.message; }
    finally { card.classList.remove("is-loading"); }
  }
});

document.addEventListener("change", async (event) => {
  if (!event.target.matches("[data-background-input]") || !window.SubstarBackgrounds) return;
  const file = event.target.files?.[0];
  if (!file) return;
  const card = event.target.closest("[data-background-page]");
  card.classList.add("is-loading");
  try {
    const record = await window.SubstarBackgrounds.set(card.dataset.backgroundPage, file);
    setBackgroundCardPreview(card, record);
    $("#formMessage").textContent = `${window.SubstarBackgrounds.labels[card.dataset.backgroundPage]}背景已保存`;
  } catch (error) { $("#formMessage").textContent = error.message; }
  finally {
    event.target.value = "";
    card.classList.remove("is-loading");
  }
});

window.addEventListener("pagehide", () => {
  for (const url of backgroundPreviewUrls.values()) URL.revokeObjectURL(url);
});
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
switchPanel(location.hash.slice(1) || "general");
loadSettings();
detectEnvironment();
refreshBackgroundCards();
