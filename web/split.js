(() => {
  "use strict";

  let asrAssist;
  const $ = (selector) => document.querySelector(selector);
  const SPLIT_WORKFLOWS = new Set(["subtitle_creation"]);
  const QUEUE_WORKFLOWS = new Set(["subtitle_creation", "editor_task"]);
  const ACTIVE_STATUSES = new Set(["queued", "running", "failed", "interrupted", "cancelled"]);
  const COMPLETE_STATUSES = new Set(["awaiting_edit", "completed"]);
  const MAIN_SPLIT_BRANCH = "A";
  // Task drafts contain media-specific prompt and punctuation choices.  Use a
  // versioned key so a newly installed build never revives an older build's
  // browser draft merely because both launch on 127.0.0.1:8769.
  const TASK_CONFIG_STORAGE_KEY = "substar.split.task-config.v3";
  const state = {
    videos: [],
    references: [],
    settings: null,
    recognitionProfiles: [],
    edition: "standard",
    capabilities: {},
    settingsDirty: false,
    taskConfigLoaded: false,
    savingSettings: false,
    submitting: false,
    submissionKey: "",
    refreshing: false,
    jobs: [],
    removedProjectIds: new Set(),
    removedTaskIds: new Set(),
    toastTimer: 0,
    runtimeEvents: new Map(),
    runtimeSnapshots: new Map(),
    activeQueueCount: 0,
    runtimeConnected: true,
    runtimeFailureCount: 0,
    quickTesting: "",
    tutorial: {
      active: false,
      kind: "beginner",
      step: 0,
      submitted: false,
      projectId: "",
      snapshot: null,
      tests: { qwen: false, glm: false },
    },
  };

  function errorMessage(error) {
    if (!error) return "发生未知错误";
    if (typeof error === "string") return error;
    return error.detail || error.message || "请求未完成";
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    let body = null;
    try { body = await response.json(); } catch (error) {
      if (options.signal?.aborted) throw error;
      body = null;
    }
    if (!response.ok) {
      const error = new Error(body ? errorMessage(body) : `请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  async function pollApi(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      return await api(url, {signal:controller.signal});
    } catch (error) {
      if (controller.signal.aborted) {
        const timeout = new Error("任务读取超时，将自动重试，也可点击刷新");
        timeout.code = "POLL_TIMEOUT";
        throw timeout;
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove("show"), 2800);
  }

  function quickProviderConfigured(kind) {
    if (!state.settings) return false;
    return kind === "qwen"
      ? Boolean(state.settings.api_key_set)
      : Boolean(state.settings.model_provider_key_set?.glm);
  }

  function quickStartConfigured() {
    return quickProviderConfigured("qwen") && quickProviderConfigured("glm");
  }

  function quickProviderComplete(kind) {
    if (state.tutorial.active && state.tutorial.kind === "beginner" && state.tutorial.step <= 1) return state.tutorial.tests[kind];
    return quickProviderConfigured(kind);
  }

  function renderQuickProvider(kind) {
    const complete = quickProviderComplete(kind);
    const configured = quickProviderConfigured(kind);
    const card = kind === "qwen" ? $("#qwenQuickCard") : $("#glmQuickCard");
    const status = kind === "qwen" ? $("#qwenQuickState") : $("#glmQuickState");
    const input = kind === "qwen" ? $("#qwenQuickKey") : $("#glmQuickKey");
    card.classList.toggle("complete", complete);
    status.textContent = complete ? "连接成功" : configured ? "已保存 · 待测试" : "待配置";
    input.placeholder = configured ? "密钥已保存；留空可直接复测" : "输入 API Key";
  }

  function syncPrimaryPanel() {
    if (!state.settings) return;
    const tutorialNeedsQuickStart = state.tutorial.active && state.tutorial.kind === "beginner" && state.tutorial.step <= 1;
    const showQuickStart = tutorialNeedsQuickStart
      || (state.activeQueueCount === 0 && !quickStartConfigured());
    $("#quickStartPanel").classList.toggle("hidden", !showQuickStart);
    $("#pipelinePanel").classList.toggle("hidden", showQuickStart);
    syncTutorialEntry();
    $("#pipelineTitle").textContent = showQuickStart ? "快速开始" : "流水线作业";
    if (showQuickStart) {
      const completeCount = Number(quickProviderComplete("qwen")) + Number(quickProviderComplete("glm"));
      $("#quickStartProgress").textContent = `完成 ${completeCount} / 2`;
    }
    renderQuickProvider("qwen");
    renderQuickProvider("glm");
  }

  function quickSettingsPayload(kind, key) {
    const payload = {
      ...state.settings,
      api_key: "",
      alignment_api_key: "",
      translation_api_key: "",
      clear_api_key: false,
      clear_alignment_api_key: false,
      clear_translation_api_key: false,
    };
    delete payload.api_key_set;
    delete payload.alignment_api_key_set;
    delete payload.translation_api_key_set;
    if (kind === "qwen") payload.api_key = key;
    else {
      payload.translation_api_provider = "openai_chat";
      payload.active_model_provider = "glm";
      payload.translation_api_base_url = "https://open.bigmodel.cn/api/paas/v4";
      payload.translation_api_model = "glm-5.3-flash";
      payload.translation_api_auth_mode = "bearer";
      payload.model_provider_profiles = {
        ...(payload.model_provider_profiles || {}),
        glm: {
          base_url: payload.translation_api_base_url,
          model: payload.translation_api_model,
          auth_mode: "bearer",
          timeout_seconds: Number(payload.translation_api_timeout_seconds || 300),
        },
      };
      payload.alignment_api_provider = payload.translation_api_provider;
      payload.alignment_api_base_url = payload.translation_api_base_url;
      payload.alignment_api_model = payload.translation_api_model;
      payload.alignment_api_auth_mode = "bearer";
      payload.translation_api_key = key;
      payload.alignment_api_key = key;
      // Quick Start changes the provider for the whole text-model pipeline.
      // Do not freeze historical DeepSeek defaults into GLM tasks.
      for (const stage of ["segmentation", "segmentation_repair", "translation", "translation_repair", "calibration", "audit_repair"]) {
        payload[`stage_${stage}_model`] = payload.translation_api_model;
      }
    }
    return payload;
  }

  async function testQuickProvider(kind) {
    if (!state.settings || state.quickTesting) return;
    const qwen = kind === "qwen";
    const input = qwen ? $("#qwenQuickKey") : $("#glmQuickKey");
    const button = qwen ? $("#qwenQuickTest") : $("#glmQuickTest");
    const result = qwen ? $("#qwenQuickResult") : $("#glmQuickResult");
    const key = input.value.trim();
    if (!key && !quickProviderConfigured(kind)) {
      result.className = "quick-test-result bad";
      result.textContent = "请先输入 API Key";
      input.focus();
      return;
    }
    state.quickTesting = kind;
    button.disabled = true;
    button.textContent = "正在连接…";
    result.className = "quick-test-result";
    result.textContent = qwen ? "正在验证密钥和临时上传权限…" : "正在发送最小模型请求…";
    try {
      const regionBase = state.settings.qwen_cloud_region === "singapore"
        ? "https://dashscope-intl.aliyuncs.com/api/v1"
        : "https://dashscope.aliyuncs.com/api/v1";
      const testPayload = qwen ? {
        role: "sentence",
        source: "qwen_cloud",
        provider: "qwen_cloud",
        base_url: state.settings.qwen_cloud_base_url || regionBase,
        model: state.settings.qwen_cloud_model || "qwen-audio-3.0-asr-flash-filetrans",
        auth_mode: "bearer",
        timeout_seconds: Number(state.settings.qwen_cloud_request_timeout_seconds || 120),
        api_key: key,
      } : {
        role: "translation",
        source: "api",
        provider: "openai_chat",
        provider_id: "glm",
        base_url: "https://open.bigmodel.cn/api/paas/v4",
        model: "glm-5.3-flash",
        auth_mode: "bearer",
        timeout_seconds: Number(state.settings.translation_api_timeout_seconds || 300),
        api_key: key,
        // GLM 5.3 has no non-thinking mode.  Quick setup must exercise the
        // same effective policy used by every Stage instead of probing an
        // unsupported request shape and saving a misleading success state.
        thinking_mode: "enabled",
        reasoning_effort: "low",
      };
      const response = await api("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(testPayload),
      });
      let thinkingModes = [];
      if (!qwen) {
        const capability = await api("/api/models/reasoning-probe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base_url:testPayload.base_url,
            model:testPayload.model,
            auth_mode:testPayload.auth_mode,
            timeout_seconds:testPayload.timeout_seconds,
            api_key:key,
            provider_id:"glm",
          }),
        });
        thinkingModes = capability.probe?.accepted_thinking_modes || [];
        // The probe persists its verified capability cache. Refresh the page
        // snapshot so the following full settings save cannot erase it.
        state.settings = await api("/api/settings");
      }
      state.settings = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(quickSettingsPayload(kind, key)),
      });
      state.tutorial.tests[kind] = true;
      input.value = "";
      result.className = "quick-test-result good";
      const thinkingSummary = thinkingModes.length
        ? `；接口接受：${thinkingModes.map((mode) => mode === "enabled" ? "思考" : "非思考").join(" / ")}`
        : "";
      result.textContent = `${response.message || "连接成功"}${thinkingSummary}，密钥已自动保存`;
      toast(`${qwen ? "Qwen ASR" : "智谱 GLM"} 已连接并保存`);
      syncPrimaryPanel();
      maybeAdvanceTutorial();
    } catch (error) {
      result.className = "quick-test-result bad";
      result.textContent = errorMessage(error);
    } finally {
      state.quickTesting = "";
      button.disabled = false;
      button.textContent = "测试连接";
    }
  }

  function setSettingsSaveState(message, kind = "") {
    const node = $("#settingsSaveState");
    if (!node) return;
    node.textContent = message;
    node.className = kind;
  }

  function markSettingsDirty() {
    if (!state.settings) return;
    state.settingsDirty = false;
    try {
      localStorage.setItem(TASK_CONFIG_STORAGE_KEY, JSON.stringify(taskConfigFromControls()));
      setSettingsSaveState("任务配置已自动保留", "saved");
    } catch (_) {
      setSettingsSaveState("配置仅在当前页面保留", "dirty");
    }
    syncWorkflowControl();
    validateForm();
    maybeAdvanceTutorial();
  }

  function parseTemporaryHotwords(text = $("#qwenHotwordsInput").value) {
    const result = [];
    const seen = new Set();
    let superCount = 0;
    for (const [index, raw] of String(text || "").split(/\r?\n/).entries()) {
      const line = raw.trim();
      if (!line) continue;
      const weighted = line.match(/^(.*)[:：]\s*(\d+)$/);
      const word = String(weighted?.[1] ?? line).trim();
      const weight = Number(weighted?.[2] ?? 4);
      if (!word) throw new Error(`第 ${index + 1} 行热词不能为空`);
      if (weighted && /[:：]\d+$/.test(word)) {
        throw new Error(`第 ${index + 1} 行只能指定一个热词权重`);
      }
      if (![1, 2, 3, 4, 5, 50].includes(weight)) {
        throw new Error(`第 ${index + 1} 行热词权重必须为 1–5 或 50`);
      }
      const key = word.toLocaleLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      if (weight === 50 && ++superCount > 50) throw new Error("权重 50 的超级热词最多 50 个");
      result.push({text:word, weight});
      if (result.length > 2000) throw new Error("临时热词最多 2000 个");
    }
    return result;
  }

  function formatTemporaryHotwords(rows) {
    return rows.map((item) => `${item.text}:${Number(item.weight || 4)}`).join("\n");
  }

  function syncQwenEnhancementCounts() {
    $("#qwenPromptCount").textContent = `${$("#qwenPromptInput").value.length} / 400`;
    try {
      const rows = parseTemporaryHotwords();
      const supers = rows.filter((item) => item.weight === 50).length;
      $("#qwenHotwordCount").textContent = `${rows.length} / 2000${supers ? ` · 超级 ${supers} / 50` : ""}`;
      $("#qwenHotwordCount").classList.remove("bad");
    } catch (error) {
      $("#qwenHotwordCount").textContent = errorMessage(error);
      $("#qwenHotwordCount").classList.add("bad");
    }
  }

  function syncQwenEnhancementModel() {
    const model = String(state.settings?.qwen_cloud_model || "");
    const supported = !model.startsWith("qwen3-asr-flash-filetrans");
    $("#qwenHotwordsInput").disabled = !supported;
    if (!supported) {
      $("#qwenHotwordsInput").value = "";
      $("#qwenAssistStatus").textContent = `${model} 仅填写 Prompt；当前模型不支持即时热词。`;
    }
    syncQwenEnhancementCounts();
    glossaryInjection?.refresh();
  }

  async function fillQwenEnhancement() {
    const brief = $("#qwenAiBriefInput").value.trim();
    const button = $("#qwenAssistButton");
    const status = $("#qwenAssistStatus");
    status.className = "";
    if (!brief) {
      status.textContent = "请先填写给 AI 的补充说明。";
      status.classList.add("bad");
      return;
    }
    button.disabled = true;
    button.textContent = "正在填写…";
    status.setAttribute("aria-busy", "true");
    status.textContent = "AI 正在生成 Qwen Prompt 和多语言临时热词…";
    try {
      const generated = await api("/api/qwen-assist", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({source_language:$("#languageInput").value, user_prompt:brief}),
      });
      const existing = parseTemporaryHotwords();
      const merged = new Map(existing.map((item) => [item.text.toLocaleLowerCase(), item]));
      for (const item of generated.hotwords || []) {
        const key = String(item.text || "").toLocaleLowerCase();
        if (key && !merged.has(key)) merged.set(key, item);
      }
      $("#qwenPromptInput").value = String(generated.prompt || "").slice(0, 400);
      $("#qwenHotwordsInput").value = formatTemporaryHotwords([...merged.values()]);
      syncQwenEnhancementCounts();
      markSettingsDirty();
      status.textContent = `已填写 Prompt，并合并 ${generated.hotwords?.length || 0} 个 AI 热词。`;
      status.classList.add("good");
      glossaryInjection?.collect(generated.hotwords || [], "ai_direct");
    } catch (error) {
      status.textContent = errorMessage(error);
      status.classList.add("bad");
    } finally {
      button.disabled = false;
      button.textContent = "直接生成";
      status.setAttribute("aria-busy", "false");
    }
  }

  let glossaryInjection;

  function taskConfigFromControls() {
    const workflow = $("#splitWorkflowInput").value;
    const segmentationEnabled = workflow === "one_step";
    return {
      generate_thumbnails: $("#generateThumbnailsInput").checked,
      language: $("#languageInput").value,
      target_language_mode: $("#targetLanguageInput").value,
      translation_layout:window.SubstarTranslationLayout.read("splitLanguageLayout"),
      glossary_id: $("#glossaryInput").value,
      asr_glossary_ids: glossaryInjection?.ids() || [],
      creation_workflow: workflow,
      subtitle_format: $("#subtitleFormat")?.value || "single",
      subtitle_first_track: $("#subtitleFirstTrack")?.value || "source",
      subtitle_separator: $("#subtitleSeparator")?.value ?? "|",
      segmentation_enabled: segmentationEnabled,
      reference_script_mode: workflow === "reference_script",
      reference_break_symbols: $("#referenceBreakSymbolsInput").value,
      translation_enabled: false,
      split_branch: MAIN_SPLIT_BRANCH,
      english_hard_limit: Number($("#englishLimitInput").value),
      chinese_hard_limit: Number($("#chineseLimitInput").value),
      mixed_hard_limit: Number($("#mixedLimitInput").value),
      other_hard_limit: Number($("#otherLimitInput")?.value || 55),
      japanese_hard_limit: Number($("#japaneseLimitInput").value),
      korean_hard_limit: Number($("#koreanLimitInput").value),
      qwen_ai_brief: $("#qwenAiBriefInput").value,
      context: $("#qwenPromptInput").value,
      qwen_temporary_hotwords: parseTemporaryHotwords(),
    };
  }

  function storedTaskConfig() {
    try {
      const value = JSON.parse(localStorage.getItem(TASK_CONFIG_STORAGE_KEY) || "null");
      return value && typeof value === "object" ? value : {};
    } catch (_) {
      return {};
    }
  }

  function syncWorkflowControl() {
    const workflow = $("#splitWorkflowInput").value;
    const referenceMode = workflow === "reference_script";
    const subtitleMode = workflow === "subtitle";
    const referenceEnabled = workflow === "one_step" || referenceMode || subtitleMode;
    $("#referenceRow").classList.toggle("hidden", !referenceEnabled);
    $("#referenceBreakSymbolsField").classList.toggle("hidden", !referenceMode);
    $("#referenceRequirement").textContent = referenceMode ? "必选" : "选填";
    $("#referenceHelp").textContent = referenceMode
      ? "支持 TXT / DOCX / SRT；参考稿校正词元，并由尾部语义标点机械确定边界"
      : "支持 TXT / DOCX / SRT；用于校正词元，不替代 AI 语义切分";
    $("#splitWorkflowHelp").textContent = referenceMode
      ? "参考稿校正词元，并按有效尾部语义标点机械切分"
      : workflow === "one_step"
        ? "由 AI 根据无句界词级时间轴、语义和字符上限重新组织字幕；参考稿选填"
        : "直接使用听写结果的句级边界";
    syncSubtitleImportControls();
  }

  function selectedRecognitionProfile() {
    return state.recognitionProfiles.find((item) => item.id === $("#recognitionProfileInput").value);
  }

  function populateRecognitionProfiles() {
    const select = $("#recognitionProfileInput");
    select.value = "qwen_cloud";
  }

  function alignmentLanguage(language) {
    return ({en:"English", zh:"Chinese", ja:"Japanese", ko:"Korean"})[language] || "Auto";
  }

  const REFERENCE_BREAK_PRESETS = Object.freeze({
    zh:"，。？！", en:".?!", ja:"。！？", ko:".?!",
    mixed:"，。？！.?!", Auto:"，。？！.?!",
  });

  function referenceBreakPreset(language) {
    return REFERENCE_BREAK_PRESETS[language] || REFERENCE_BREAK_PRESETS.Auto;
  }

  function isReferenceBreakPreset(value) {
    return value === "，。？" || Object.values(REFERENCE_BREAK_PRESETS).includes(value);
  }

  function syncReferenceBreakPreset() {
    const input = $("#referenceBreakSymbolsInput");
    if (!input.value.trim() || isReferenceBreakPreset(input.value)) {
      input.value = referenceBreakPreset($("#languageInput").value);
    }
  }


  // Archived tutorials no longer attach UI, listeners or startup flows.
  function syncTutorialEntry() {}
  function maybeAdvanceTutorial() {}

  function applySharedSettings(settings) {
    window.SubstarTranslationLayout.configureModel(settings.qwen_cloud_model);
    state.settings = settings;
    const effective = {...settings, ...(state.taskConfigLoaded ? taskConfigFromControls() : storedTaskConfig())};
    $("#recognitionProfileInput").value = settings.recognition_profile_id || "faster_whisper_native";
    if (!$("#recognitionProfileInput").value) $("#recognitionProfileInput").selectedIndex = 0;
    $("#languageInput").value = ["Auto", "zh", "mixed", ...window.SubstarTranslationLayout.languages.map(([code]) => code)].includes(effective.language)
      ? effective.language : "Auto";
    const configuredTarget = effective.target_language_mode || "zh-CN";
    $("#targetLanguageInput").value = window.SubstarTranslationLayout.languages.some(([code]) => code === configuredTarget)
      ? configuredTarget
      : "zh-CN";
    window.SubstarTranslationLayout.write("splitLanguageLayout", effective.translation_layout || {});
    // The split page currently uses the global glossary only. Keep the hidden
    // field blank so older saved task settings cannot silently restore a
    // project glossary after the selector has been removed from the UI.
    $("#glossaryInput").value = "";
    $("#englishLimitInput").value = effective.english_hard_limit || 55;
    $("#chineseLimitInput").value = effective.chinese_hard_limit || 20;
    $("#mixedLimitInput").value = effective.mixed_hard_limit || 25;
    if ($("#otherLimitInput")) $("#otherLimitInput").value = effective.other_hard_limit ?? 55;
    $("#japaneseLimitInput").value = effective.japanese_hard_limit || 24;
    $("#koreanLimitInput").value = effective.korean_hard_limit || 28;
    $("#qwenAiBriefInput").value = effective.qwen_ai_brief || "";
    $("#qwenPromptInput").value = effective.context || "";
    $("#qwenHotwordsInput").value = formatTemporaryHotwords(effective.qwen_temporary_hotwords || []);
    syncQwenEnhancementModel();
    $("#splitWorkflowInput").value = ["subtitle", "disabled", "one_step", "reference_script"].includes(effective.creation_workflow)
      ? effective.creation_workflow : effective.reference_script_mode
      ? "reference_script"
      : effective.segmentation_enabled === false ? "disabled" : "one_step";
    const configuredBreakSymbols = String(effective.reference_break_symbols || "");
    $("#referenceBreakSymbolsInput").value = configuredBreakSymbols && !isReferenceBreakPreset(configuredBreakSymbols)
      ? configuredBreakSymbols
      : referenceBreakPreset($("#languageInput").value);
    if (state.edition === "slim") {
      $("#recognitionProfileInput").value = "qwen_cloud";
    }
    $("#generateThumbnailsInput").checked = effective.generate_thumbnails === true;
    $("#chunkSecondsInput").value = settings.segmentation_chunk_seconds || 90;
    $("#workersInput").value = settings.translation_workers || 16;
    $("#retryInput").value = settings.http_retry_attempts ?? 3;
    state.settingsDirty = false;
    state.taskConfigLoaded = true;
    $("#subtitleFormat").value = ["single", "two-files", "bilingual-lines", "bilingual-inline"].includes(effective.subtitle_format) ? effective.subtitle_format : "single";
    $("#subtitleFirstTrack").value = effective.subtitle_first_track === "target" ? "target" : "source";
    $("#subtitleSeparator").value = effective.subtitle_separator ?? "|";
    setSettingsSaveState("任务配置已保留；多个素材将共用此配置", "saved");
    syncWorkflowControl();
    validateForm();
    syncPrimaryPanel();
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "";
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / 1024 / 1024).toFixed(bytes > 100 * 1024 * 1024 ? 0 : 1)} MB`;
  }

  function jobTime(value) {
    if (!value) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
    }).format(new Date(value * 1000));
  }

  function routeLabel(job) {
    if (job.tutorial_case_id) return "内置教程";
    if (job.workflow_mode === "editor_task") {
      return ({translation:"翻译", calibration:"AI 校准", organize:"AI整理", punctuation:"AI标点", review:"AI 审阅"})[job.task_kind] || "编辑器任务";
    }
    if (job.subtitle_basis === "sentence") return "基于句级字幕";
    if (job.workflow_mode === "subtitle_creation") return "基于词级字幕";
    return job.settings_overrides?.recognition_profile_label || "云端听写";
  }

  function jobLabel(job) {
    return window.SubstarProjectLabel(job.display_name || job.filename || job.id);
  }

  function normalizedProgress(job) {
    const value = Number(job.workflow_mode === "subtitle_creation"
      ? job.progress ?? 0
      : job.ai_progress?.progress ?? job.progress ?? 0);
    const stored = value <= 1 ? value * 100 : value;
    const ceiling = job.workflow_mode === "subtitle_creation" && !["awaiting_edit", "completed"].includes(job.status) ? 99 : 100;
    return Math.max(0, Math.min(ceiling, Math.round(stored)));
  }

  function humanStatus(job) {
    if (job.status === "queued") return "排队中";
    if (job.status === "running") return job.message || "处理中";
    if (job.status === "failed") return "处理失败";
    if (job.status === "interrupted") return "任务已中断";
    if (job.status === "cancelled") return "任务已取消，项目文件已保留";
    if (job.status === "succeeded_with_issues") return "已交付，部分内容需要人工处理";
    if (job.status === "awaiting_edit") return "等待编辑";
    return "切分完成";
  }

  const TASK_STEP_LABELS = Object.freeze({
    "transcription.media_probe":"正在读取媒体信息",
    "transcription.audio_prepare":"正在准备听写音频",
    "transcription.provider_audio_encode":"正在编码上传音频",
    "transcription.provider_upload":"正在上传音频",
    "transcription.provider_submit":"正在向 ASR 提交听写任务",
    "transcription.provider_run":"云端正在听写",
    "transcription.evidence_normalize":"正在整理词级听写证据",
    "transcription.artifact_finalize":"正在保存听写结果",
    "segmentation.input_prepare":"正在准备切分输入",
    "segmentation.semantic_grouping":"正在进行语义切分",
    "segmentation.cue_layout":"正在生成 Cue 布局",
    "segmentation.validation":"正在校验字幕",
    "segmentation.document_build":"正在生成可编辑文档",
  });

  function taskPhase(job) {
    if (job.workflow_mode === "subtitle_creation") {
      return job.ai_progress ? `字幕切分 · ${job.message || "处理中"}` : humanStatus(job);
    }
    return window.SubstarAiProgressSummary?.format(job.ai_progress, {
      problemCueIds: job.problem_cue_ids || [],
    }) || String(job.ai_progress?.message || "")
      || TASK_STEP_LABELS[String(job.step || "")]
      || humanStatus(job);
  }

  function taskError(job) {
    const raw = job?.error;
    if (!raw) return "";
    if (typeof raw === "string") return raw;
    try { return JSON.stringify(raw, null, 2); } catch (_) { return String(raw); }
  }

  async function copyText(value, successMessage = "已复制") {
    const text = String(value || "");
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    toast(successMessage);
  }

  function chooseVideo(files) {
    const selected = [...(files || [])];
    if (!selected.length) return;
    if (selected.length > 1) { toast("每次只配置一个项目，请选择一个视频或音频"); return; }
    const accepted = selected;
    state.videos = accepted;
    state.submissionKey = crypto.randomUUID();
    const total = accepted.reduce((sum, file) => sum + file.size, 0);
    const chip = $("#videoFileChip");
    chip.textContent = accepted.length === 1
      ? `${accepted[0].name} · ${formatBytes(total)} · ${accepted[0].token ? '引用原文件' : '复制导入'}`
      : `${accepted.length} 个素材 · ${formatBytes(total)}`;
    chip.title = accepted.map((file) => file.name).join("\n");
    chip.classList.toggle("hidden", !accepted.length);
    validateForm();
    maybeAdvanceTutorial();
  }

  let referenceEpoch = 0;
  let referenceParsing = false;
  let referenceParseError = "";
  async function chooseReferences(files) {
    const selected = [...(files || [])];
    if (!selected.length) return;
    if (selected.some(file => !/\.(txt|docx|srt)$/i.test(file.name))) return toast("请选择 TXT、DOCX 或 SRT");
    const allSrt = selected.every(file => /\.srt$/i.test(file.name));
    const previous = state.references.every(file => /\.srt$/i.test(file.name)) ? state.references : [];
    const combined = allSrt ? [...previous, ...selected].filter((file,index,array) => array.findIndex(x => x.name===file.name && x.size===file.size && x.lastModified===file.lastModified)===index) : selected;
    if (combined.length > (allSrt ? 2 : 1)) return toast("当前项目最多添加两个 SRT，或一份 TXT / DOCX；请先移除不需要的文件");
    state.references = combined;
    state.submissionKey = crypto.randomUUID();
    await updateReferenceSelection();
    $("#referenceInput").value = "";
    maybeAdvanceTutorial();
  }
  async function updateReferenceSelection() {
    const epoch = ++referenceEpoch;
    const files = [...state.references];
    referenceParsing = true; referenceParseError = ""; validateForm();
    const label = $("#referenceFileName");
    label.replaceChildren();
    if (!files.length) label.textContent = "未添加";
    for (const file of files) {
      const chip = document.createElement("span"), remove = document.createElement("button");
      chip.append(document.createTextNode(file.name + " "));
      remove.type="button"; remove.textContent="×"; remove.setAttribute("aria-label",`移除 ${file.name}`);
      remove.addEventListener("click", () => {state.references=state.references.filter(f=>f!==file);updateReferenceSelection();});
      chip.append(remove); label.append(chip);
    }
    if(files.length && files.every(f=>/\.srt$/i.test(f.name))) {
      try {
        const texts=await Promise.all(files.map(f=>f.text()));
        if(epoch!==referenceEpoch)return;
        if(texts.some(t=>!SubstarSubtitleInput.blocks(t).length)) throw new Error("未读取到有效 SRT 字幕，请检查文件");
        const result=SubstarSubtitleInput.infer(texts,$("#languageInput").value,$("#targetLanguageInput").value);
        $("#subtitleFormat").value=result.format; $("#subtitleFirstTrack").value=result.firstTrack; $("#subtitleSeparator").value=result.separator;
        if(result.source) $("#languageInput").value=result.source;
        if(result.target) $("#targetLanguageInput").value=result.target;
        window.SubstarTranslationLayout.write("splitLanguageLayout", {});
      } catch(e) {referenceParseError = errorMessage(e); toast(referenceParseError);}
    }
    referenceParsing = false;
    syncSubtitleImportControls(); validateForm();
  }

  function validateForm() {
    const subtitleMode = $("#splitWorkflowInput").value === "subtitle";
    asrAssist?.sync();
    const referenceMode = $("#splitWorkflowInput").value === "reference_script";
    const symbols = $("#referenceBreakSymbolsInput").value.replace(/\s/g, "");
    const srtValid = !state.references.some(f=>/\.srt$/i.test(f.name)) || state.references.length === ($("#subtitleFormat").value === "two-files" ? 2 : 1);
    const valid = !referenceParsing && !referenceParseError && srtValid && state.videos.length === 1 && (!subtitleMode || (state.videos.length === 1 && state.references.length === ($("#subtitleFormat").value === "two-files" ? 2 : 1) && state.references.every(file => /\.srt$/i.test(file.name))))
      && (!referenceMode || (state.references.length > 0 && symbols.length > 0));
    $("#startButton").disabled = !valid || !state.settings || state.submitting || state.savingSettings;
    const message = $("#formMessage");
    message.classList.remove("error");
    if (!state.settings) message.textContent = "正在读取已保存设置…";
    else if (state.submitting) message.textContent = "正在接收素材并创建任务…";
    else if (!state.videos.length) message.textContent = "选择一个素材后即可开始";
    else if (referenceParsing) message.textContent = "正在识别字幕格式与语言…";
    else if (referenceParseError) message.textContent = referenceParseError;
    else if (!srtValid) message.textContent = "字幕文件数量与所选格式不匹配";
    else if (subtitleMode && !state.references.length) message.textContent = "请添加 SRT 字幕文件";
    else if (referenceMode && !state.references.length) message.textContent = "按参考稿标点切分必须选择参考文稿";
    else if (referenceMode && !symbols.length) message.textContent = "请填写至少一个切分符号";
    else message.textContent = "素材已就绪";
    $("#startButton span").textContent = state.submitting ? "正在投递" : "创建项目";
  }

  function clearSubmission() {
    ++referenceEpoch; referenceParsing = false; referenceParseError = "";
    state.videos = [];
    state.references = [];
    state.submissionKey = "";
    $("#videoInput").value = "";
    $("#referenceInput").value = "";
    $("#videoFileChip").textContent = "";
    $("#videoFileChip").classList.add("hidden");
    $("#referenceFileName").textContent = "未添加";
    $("#referenceFileName").removeAttribute("title");
    clearTaskSpecificFields();
    syncSubtitleImportControls();
    validateForm();
  }

  function clearTaskSpecificFields() {
    $("#qwenAiBriefInput").value = "";
    $("#qwenPromptInput").value = "";
    $("#qwenHotwordsInput").value = "";
    $("#referenceBreakSymbolsInput").value = referenceBreakPreset($("#languageInput").value);
    syncQwenEnhancementCounts();
    try {
      localStorage.setItem(TASK_CONFIG_STORAGE_KEY, JSON.stringify(taskConfigFromControls()));
    } catch (_) {}
  }

  function automaticSettings() {
    const current = state.settings || {};
    const workflow = $("#splitWorkflowInput").value;
    const segmentationEnabled = workflow === "one_step";
    const result = {
      generate_thumbnails: $("#generateThumbnailsInput").checked,
      active_model_provider: current.active_model_provider,
      translation_api_base_url: current.translation_api_base_url,
      translation_api_model: current.translation_api_model,
      translation_api_auth_mode: current.translation_api_auth_mode,
      recognition_profile_id: $("#recognitionProfileInput").value,
      language: $("#languageInput").value,
      alignment_language: alignmentLanguage($("#languageInput").value),
      target_language_mode: $("#targetLanguageInput").value,
      translation_layout:window.SubstarTranslationLayout.read("splitLanguageLayout"),
      glossary_id: $("#glossaryInput").value,
      asr_glossary_ids: glossaryInjection?.ids() || [],
      segmentation_enabled: segmentationEnabled,
      reference_script_mode: workflow === "reference_script",
      reference_break_symbols: $("#referenceBreakSymbolsInput").value,
      translation_enabled: false,
      calibration_enabled: false,
      english_hard_limit: Number($("#englishLimitInput").value),
      chinese_hard_limit: Number($("#chineseLimitInput").value),
      mixed_hard_limit: Number($("#mixedLimitInput").value),
      other_hard_limit: Number($("#otherLimitInput")?.value || 55),
      japanese_hard_limit: Number($("#japaneseLimitInput").value),
      korean_hard_limit: Number($("#koreanLimitInput").value),
      context: $("#qwenPromptInput").value,
      qwen_temporary_hotwords: parseTemporaryHotwords(),
      split_workflow_mode: "one_step",
      split_branch: MAIN_SPLIT_BRANCH,
      segmentation_strategy: "semantic",
      segmentation_chunk_seconds: Number($("#chunkSecondsInput").value),
      translation_workers: Number($("#workersInput").value),
      http_retry_attempts: Number($("#retryInput").value),
    };
    for (const key of [
      "whisper_model", "whisper_model_path", "whisper_device",
      "whisper_compute_type", "whisper_beam_size", "whisper_vad_filter",
      "qwen_asr_model", "qwen_asr_model_path", "qwen_aligner_model",
      "qwen_aligner_model_path", "parakeet_model", "parakeet_model_path",
      "parakeet_device", "parakeet_dtype", "whisperx_alignment_model",
      "whisperx_alignment_model_path", "whisperx_batch_size", "model_cache_dir",
    ]) result[key] = current[key];
    for (const stage of ["segmentation", "segmentation_repair", "translation", "translation_repair", "calibration", "audit_repair"]) {
      for (const suffix of ["model", "thinking_mode", "reasoning_effort", "max_tokens", "temperature"]) {
        const key = `stage_${stage}_${suffix}`;
        result[key] = current[key];
      }
    }
    return result;
  }

  async function submitSingle(media, references) {
    const form = new FormData();
    const workflow = $("#splitWorkflowInput").value;
    form.append("mode", "asr");
    if (media.token) form.append("media_reference_token", media.token);
    else form.append("media", media, media.name);
    if (workflow !== "disabled" && references.length) {
      if (references.every(file => /\.srt$/i.test(file.name))) {
        const texts = await Promise.all(references.map(file=>file.text()));
        const text = SubstarSubtitleInput.reference(texts,$("#subtitleFormat").value,$("#subtitleFirstTrack").value,$("#subtitleSeparator").value);
        form.append("reference_document", new Blob([text],{type:"text/plain"}), "reference.txt");
      } else form.append("reference_document", references[0], references[0].name);
    }
    form.append("settings_json", JSON.stringify(automaticSettings()));
    return api("/api/project-creations", {
      method: "POST",
      headers: {"Idempotency-Key": state.submissionKey},
      body: form,
    });
  }

  async function runPipeline() {
    if (state.submitting || !state.settings || !state.videos.length) return;
    if ($("#splitWorkflowInput").value === "subtitle") return runSubtitleImport();
    const mediaFiles = [...state.videos];
    const references = [...state.references];
    state.submitting = true;
    validateForm();
    try {
      await submitSingle(mediaFiles[0], references);
      state.submitting = false;
      clearSubmission();
      toast(`${mediaFiles.length} 个任务已进入流水线`);
      await refreshJobs();
    } catch (error) {
      state.submitting = false;
      validateForm();
      $("#formMessage").textContent = errorMessage(error);
      $("#formMessage").classList.add("error");
    }
  }

  function makeProgressBar(percent) {
    const track = document.createElement("div");
    track.className = "queue-progress";
    const fill = document.createElement("i");
    fill.style.width = `${percent}%`;
    track.append(fill);
    return track;
  }

  async function retryJob(jobId, button) {
    button.disabled = true;
    button.textContent = "正在重试";
    try {
      await api(`/api/project-creations/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
      toast("任务已重新进入流水线");
      await refreshJobs();
    } catch (error) {
      toast(errorMessage(error));
      button.disabled = false;
      button.textContent = "重试";
    }
  }

  function renderQueue(jobs) {
    // A creation, calibration or translation task keeps its own immutable card
    // identity for its complete lifecycle.  Combining jobs by project made the
    // same DOM card jump between unrelated states and progress counters.
    const active = jobs.filter((job) =>
      QUEUE_WORKFLOWS.has(job.workflow_mode) && ACTIVE_STATUSES.has(job.status)
    );
    const container = $("#pipelineJobs");
    state.activeQueueCount = active.length;
    $("#emptyPipeline").classList.toggle("hidden", active.length > 0);
    container.classList.toggle("hidden", active.length === 0);
    syncPrimaryPanel();
    container.replaceChildren(...active.map((job) => {
      const percent = normalizedProgress(job);
      const card = document.createElement("article");
      const disconnectedActive = !state.runtimeConnected && ["queued", "running"].includes(job.status);
      card.className = `queue-job ${disconnectedActive ? "interrupted" : job.status}`;
      const head = document.createElement("div");
      head.className = "queue-job-head";
      const identity = document.createElement("div");
      identity.className = "queue-job-identity";
      const name = document.createElement("strong");
      name.textContent = jobLabel(job);
      name.title = jobLabel(job);
      const route = document.createElement("span");
      route.className = "route-badge";
      route.textContent = routeLabel(job);
      identity.append(name, route);
      const pct = document.createElement("b");
      pct.textContent = `${percent}%`;
      head.append(identity, pct);
      const errorText = taskError(job);
      const errorBlock = document.createElement("div");
      errorBlock.className = "queue-job-error";
      if (errorText) {
        const errorValue = document.createElement("pre");
        errorValue.textContent = errorText;
        const copyError = document.createElement("button");
        copyError.type = "button";
        copyError.textContent = "复制报错";
        copyError.addEventListener("click", (event) => {
          event.stopPropagation();
          copyText(errorText, "报错已复制");
        });
        const limited = /\b429\b|\b1302\b|rate.?limit|too many requests|限流|并发数超/.test(errorText.toLowerCase());
        if (limited) {
          const explanation = document.createElement("span");
          explanation.textContent = job.segmentation_task_id
            ? "模型服务暂时限流，请稍后重试。已验收的切分块会从检查点恢复。"
            : "模型服务暂时限流，请稍后重试。";
          const details = document.createElement("details");
          const summary = document.createElement("summary");
          summary.textContent = "查看原始错误";
          details.append(summary, errorValue);
          const body = document.createElement("div");
          body.append(explanation, details);
          errorBlock.append(body, copyError);
        } else {
          errorBlock.append(errorValue, copyError);
        }
      }
      const detail = document.createElement("div");
      detail.className = "queue-job-detail";
      const message = document.createElement("span");
      message.textContent = disconnectedActive
        ? "后端连接已断开；正在等待正式后端恢复并核对任务状态"
        : taskPhase(job);
      detail.append(message);
      const actions = document.createElement("div");
      actions.className = "queue-job-actions";
      if (!disconnectedActive && ["failed", "interrupted"].includes(job.status)) {
        if (job.workflow_mode !== "editor_task") {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.textContent = "从断点继续";
          retry.addEventListener("click", () => retryJob(job.id, retry));
          actions.append(retry);
        }
      }
      const removableJob = job;
      if (!disconnectedActive && removableJob) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "queue-delete";
        remove.textContent = ["queued", "running"].includes(job.status) ? "取消" : "删除";
        remove.addEventListener("click", (event) => {
          event.stopPropagation();
          if (removableJob.workflow_mode === "editor_task") {
            deleteEditorTask(removableJob, card, remove);
          } else {
            deleteJob(removableJob, card, remove);
          }
        });
        actions.append(remove);
      }
      detail.append(actions);
      card.append(head);
      if (errorText) card.append(errorBlock);
      card.append(makeProgressBar(percent), detail);
      return card;
    }));
  }

  function exportEndpoint(jobId, kind) {
    const id = encodeURIComponent(jobId);
    return `/api/project-creations/${id}/subtitles/${kind}`;
  }

  function hasTranslation(job) {
    return Array.isArray(job.files) && job.files.some((file) => /_(B|AB_inline|AB_two_line)\.srt$/i.test(file.name || ""));
  }

  function createExportMenu(job) {
    const details = document.createElement("details");
    details.className = "export-menu";
    const summary = document.createElement("summary");
    summary.textContent = "导出";
    const panel = document.createElement("div");
    panel.className = "export-menu-panel";
    const positionPanel = () => {
      if (!details.open || !details.closest("#recentJobs")) return;
      const rect = summary.getBoundingClientRect();
      panel.style.left = `${Math.max(8, Math.min(rect.right - panel.offsetWidth, window.innerWidth - panel.offsetWidth - 8))}px`;
      panel.style.top = `${Math.max(8, rect.top - panel.offsetHeight - 7)}px`;
    };
    details.repositionExportPanel = positionPanel;
    const renderOptions = () => {
    panel.replaceChildren();
    const translated = hasTranslation(job);
    const availability = job.export_availability || {};
    [
      ["导出 A 字幕", "a", "a", true],
      ["导出 B 字幕", "b", "b", translated],
      ["导出 AB 单行字幕", "ab_single", "ab_single", translated],
      ["导出 AB 双行字幕", "ab_double", "ab_double", translated],
    ].forEach(([label, kind, availabilityKey, fallbackEnabled]) => {
      const contract = availability[availabilityKey];
      const enabled = contract ? Boolean(contract.available) : fallbackEnabled;
      const reason = contract?.reason || "尚无译文";
      const link = document.createElement("a");
      link.textContent = enabled ? label : `${label} · ${reason}`;
      const projectMode = ({a:"source", b:"target", ab_single:"ab-single", ab_double:"ab-double"})[kind];
      link.href = enabled ? (contract?.url || (job.project_only ? `/api/projects/${encodeURIComponent(job.id)}/export/${projectMode}` : exportEndpoint(job.id, kind))) : "#";
      if (enabled) link.addEventListener("click", async event => {
        event.preventDefault();
        try {
          const mode = ({a:"source", b:"target", ab_single:"ab-single", ab_double:"ab-double"})[kind];
          const saver = window.SubstarSystemSaveAs;
          const result = await saver.saveUrl(saver.subtitleSpec(jobLabel(job), mode, 1, link.href));
          if (!result.cancelled) toast(`已保存：${result.filename}`);
        } catch (error) { toast(`导出失败：${errorMessage(error)}`); }
      });
      if (!enabled) {
        link.className = "disabled";
        link.setAttribute("aria-disabled", "true");
        link.addEventListener("click", (event) => event.preventDefault());
      }
      panel.append(link);
    });
    };
    renderOptions();
    let loading = false;
    details.addEventListener("toggle", async () => {
      positionPanel();
      if (!details.open || loading || job.export_availability || job.project_only) return;
      loading = true;
      panel.textContent = "正在读取导出选项…";
      try {
        const current = await api(`/api/project-creations/${encodeURIComponent(job.id)}`);
        job.export_availability = current.export_availability;
        renderOptions();
      } catch (error) {
        panel.textContent = `读取失败，请重新打开：${errorMessage(error)}`;
      } finally { loading = false; positionPanel(); }
    });
    details.append(summary, panel);
    panel.addEventListener("click", event => {
      if (event.target.closest("a:not(.disabled)")) details.open = false;
    });
    return details;
  }

  function removeRecentProjectCard(projectId) {
    document.querySelectorAll(".recent-item").forEach((candidate) => {
      if (candidate.dataset.projectId === projectId) candidate.remove();
    });
  }

  async function deleteJob(job, item, button) {
    const name = jobLabel(job);
    const running = ["queued", "running"].includes(job.status);
    const prompt = running
      ? `确定取消正在运行的“${name}”吗？已生成的项目文件会保留。`
      : `确定删除“${name}”及其切分、编辑和翻译产物吗？项目将移入可恢复回收区。`;
    if (!window.confirm(prompt)) return;
    button.disabled = true;
    try {
      const result = await api(`/api/project-creations/${encodeURIComponent(job.id)}`, { method: "DELETE" });
      if (result.deleted) {
        state.removedProjectIds.add(job.id);
        state.jobs = state.jobs.filter((candidate) => candidate.id !== job.id);
        item.remove();
        removeRecentProjectCard(job.id);
      }
      toast(result.message || (result.recoverable_to ? "任务已移至回收区" : "任务已删除"));
      window.setTimeout(refreshJobs, 220);
    } catch (error) {
      button.disabled = false;
      toast(errorMessage(error));
    }
  }

  async function deleteEditorTask(job, item, button) {
    const taskId = String(job.runtime_task_id || "");
    if (!taskId) return toast("任务标识缺失，请刷新后重试");
    const running = ["queued", "running", "cancelling"].includes(job.status);
    const prompt = running
      ? `确定取消正在运行的“${routeLabel(job)}”吗？已交付的项目版本会保留。`
      : `确定删除这张“${routeLabel(job)}”任务卡吗？项目版本和编辑成果会保留。`;
    if (!window.confirm(prompt)) return;
    button.disabled = true;
    try {
      if (running) {
        await api(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {method:"POST"});
        toast("已请求取消任务");
      } else {
        await api(`/api/tasks/${encodeURIComponent(taskId)}`, {method:"DELETE"});
        state.removedTaskIds.add(taskId);
        item.classList.add("removing");
        window.setTimeout(() => item.remove(), 180);
        toast("任务卡已删除，项目成果已保留");
      }
      window.setTimeout(refreshJobs, 220);
    } catch (error) {
      button.disabled = false;
      toast(errorMessage(error));
    }
  }

  let pendingRename = null;

  function openRenameDialog(job, title, button) {
    pendingRename = { job, title, button };
    const input = $("#renameProjectName");
    input.value = jobLabel(job);
    $("#renameProjectDialog").classList.remove("hidden");
    requestAnimationFrame(() => { input.focus(); input.select(); });
  }

  function closeRenameDialog() {
    $("#renameProjectDialog").classList.add("hidden");
    pendingRename = null;
  }

  async function submitRename(event) {
    event.preventDefault();
    if (!pendingRename) return closeRenameDialog();
    const { job, title, button } = pendingRename;
    const name = $("#renameProjectName").value.trim();
    if (!name) return toast("项目名称不能为空");
    if (name === jobLabel(job)) return closeRenameDialog();
    button.disabled = true;
    try {
      const updated = await api(`/api/project-creations/${encodeURIComponent(job.id)}/name`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: name }),
      });
      job.display_name = updated.display_name;
      title.textContent = jobLabel(job);
      title.title = jobLabel(job);
      closeRenameDialog();
      toast("任务已重命名");
    } catch (error) {
      toast(errorMessage(error));
    } finally {
      button.disabled = false;
    }
  }

  function renderRecent(jobs) {
    const container = $("#recentJobs");
    if (!container.exportPositioningReady) {
      const reposition = () => container.querySelectorAll(".export-menu[open]").forEach(menu => menu.repositionExportPanel?.());
      container.addEventListener("scroll", reposition);
      window.addEventListener("scroll", reposition, true);
      window.addEventListener("resize", reposition);
      document.addEventListener("click", event => {
        container.querySelectorAll(".export-menu[open]").forEach(menu => {
          if (!menu.contains(event.target)) menu.open = false;
        });
      });
      document.addEventListener("keydown", event => {
        if (event.key !== "Escape") return;
        container.querySelectorAll(".export-menu[open]").forEach(menu => {
          menu.open = false;
          menu.querySelector("summary").focus();
        });
      });
      container.exportPositioningReady = true;
    }
    const complete = jobs.filter((job) => COMPLETE_STATUSES.has(job.status));
    const signature = JSON.stringify(complete.map(job => [job.id, jobLabel(job), routeLabel(job), humanStatus(job), job.created_at,
      job.complete, job.tutorial_case_id, job.project_only, hasTranslation(job), job.export_availability]));
    if (container.recentRenderSignature === signature) return;
    // Keep native details, hover and keyboard focus intact during interaction.
    // The next poll applies any deferred changes after the interaction ends.
    if (container.querySelector(".export-menu[open]") || container.contains(document.activeElement) || container.matches(":hover")) return;
    container.recentRenderSignature = signature;
    const scrollTop = container.scrollTop;
    if (!complete.length) {
      container.innerHTML = '<p class="recent-empty">还没有完成的切分任务，成稿会自动移到这里。</p>';
      return;
    }
    container.replaceChildren(...complete.map((job) => {
      const item = document.createElement("article");
      item.className = "recent-item";
      item.dataset.projectId = job.id;
      item.classList.toggle("complete", job.complete === true);
      item.classList.toggle("tutorial", Boolean(job.tutorial_case_id));
      if (job.complete === true) item.setAttribute("aria-label", `${jobLabel(job)}，完成稿`);
      const info = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = jobLabel(job);
      title.title = jobLabel(job);
      const meta = document.createElement("span");
      meta.textContent = `${routeLabel(job)} · ${humanStatus(job)} · ${jobTime(job.created_at)}`;
      info.append(title, meta);
      const actions = document.createElement("div");
      actions.className = "recent-actions";
      const editor = document.createElement("a");
      // V2 editor addresses projects by their immutable project id.  Keep the
      // link in sync with the editor's canonical query parameter so a fresh
      // navigation never lands on an empty document.
      editor.href = `/editor?project=${encodeURIComponent(job.id)}`;
      editor.textContent = "编辑";
      const rename = document.createElement("button");
      rename.type = "button";
      rename.className = "recent-rename";
      rename.textContent = "重命名";
      rename.addEventListener("click", () => openRenameDialog(job, title, rename));
      const remove = document.createElement("button");
      remove.className = "recent-delete";
      remove.type = "button";
      remove.title = "删除任务";
      remove.setAttribute("aria-label", `删除 ${jobLabel(job)}`);
      remove.innerHTML = '<svg class="ui-icon"><use href="/assets/ui-icons.svg#trash"></use></svg>';
      remove.addEventListener("click", () => deleteJob(job, item, remove));
      actions.append(rename, editor, createExportMenu(job), remove);
      item.append(info, actions);
      return item;
    }));
    container.scrollTop = scrollTop;
  }

  let projectCatalog = {projects:[]};
  let projectCatalogUpdatedAt = 0;
  async function refreshJobs(refreshCatalog = true) {
    if (state.refreshing) {
      if (refreshCatalog) state.refreshCatalogPending = true;
      return;
    }
    state.refreshing = true;
    try {
      const [jobs, editorTasks, projects] = await Promise.all([
        pollApi("/api/project-creations"),
        pollApi("/api/editor-tasks").catch(() => ({tasks:[]})),
        (refreshCatalog || Date.now() - projectCatalogUpdatedAt > 30000
          ? pollApi("/api/projects").then(value => {
              projectCatalog = value;
              projectCatalogUpdatedAt = Date.now();
              return value;
            }).catch(() => projectCatalog)
          : Promise.resolve(projectCatalog)),
      ]);
      state.runtimeConnected = true;
      state.runtimeFailureCount = 0;
      const systemNode = $("#systemState");
      if (systemNode?.dataset.runtimeDisconnected === "true") {
        delete systemNode.dataset.runtimeDisconnected;
        systemNode.classList.remove("error");
        systemNode.classList.add("ready");
        systemNode.querySelector("span").textContent = "就绪";
      }
      const completeProjects = new Set((projects.projects || [])
        .filter(project => !state.removedProjectIds.has(project.project_id))
        .filter(project => project.complete === true)
        .map(project => project.project_id));
      const catalogById = new Map((projects.projects || []).map(project => [project.project_id, project]));
      state.jobs = jobs
        .filter((job) => !state.removedProjectIds.has(job.id))
        .filter((job) => SPLIT_WORKFLOWS.has(job.workflow_mode))
        .map(job => ({...job, subtitle_basis:catalogById.get(job.id)?.subtitle_basis, complete:completeProjects.has(job.id)}));
      const knownProjectIds = new Set(state.jobs.map(job => job.id));
      const tutorialProjectRows = (projects.projects || [])
        .filter(project => !state.removedProjectIds.has(project.project_id))
        .filter(project => !knownProjectIds.has(project.project_id))
        .map(project => ({
          id:project.project_id,
          project_id:project.project_id,
          display_name:project.display_name || project.project_id,
          workflow_mode:"subtitle_creation",
          subtitle_basis:project.subtitle_basis,
          status:"awaiting_edit",
          progress:1,
          message:"项目已创建，可以进入编辑模式",
          created_at:Math.max(0, Date.parse(project.updated_at || "") / 1000 || 0),
          tutorial_case_id:project.tutorial_case_id,
          complete:project.complete === true,
          project_only:true,
        }));
      const taskJobs = (editorTasks.tasks || [])
        .filter(task => !state.removedTaskIds.has(task.task_id) && !state.removedProjectIds.has(task.project_id))
        .map((task) => ({
        id:`editor:${task.project_id}:${task.task_id}`,
        runtime_task_id:task.task_id,
        project_id:task.project_id,
        display_name:task.display_name || task.project_id,
        workflow_mode:"editor_task",
        task_kind:task.kind,
        status:task.status,
        progress:Number(task.progress || 0),
        message:task.message || "",
        error:task.error || "",
        ai_progress:task.ai_progress || null,
        problem_cue_ids:task.problem_cue_ids || [],
        created_at:task.created_at,
      }));
      renderQueue([...taskJobs, ...state.jobs]);
      renderRecent(state.jobs.filter(job => !job.tutorial_case_id)
        .sort((left, right) => Number(right.created_at || 0) - Number(left.created_at || 0)));
    } catch (error) {
      if (error.status || error.code === "POLL_TIMEOUT") {
        // An HTTP error is a reachable server, not a lost backend connection.
        const message = `最近任务加载失败：${errorMessage(error)}`;
        if (!state.jobs.length) $("#recentJobs").textContent = message;
        toast(message);
        return;
      }
      state.runtimeFailureCount += 1;
      state.runtimeConnected = false;
      const systemNode = $("#systemState");
      if (systemNode) {
        systemNode.dataset.runtimeDisconnected = "true";
        systemNode.classList.remove("ready");
        systemNode.classList.add("error");
        systemNode.querySelector("span").textContent = "后端已断开";
      }
      renderQueue(state.jobs);
      if (!state.jobs.length) $("#recentJobs").innerHTML = `<p class="recent-empty">${errorMessage(error)}</p>`;
    } finally {
      state.refreshing = false;
      if (state.refreshCatalogPending) {
        state.refreshCatalogPending = false;
        refreshJobs(true);
      }
    }
  }

  async function loadInitialState() {
    const initialJobs = refreshJobs();
    try {
      const [settings, system, recognition] = await Promise.all([
        api("/api/settings"), api("/api/system"), api("/api/recognition/profiles"),
      ]);
      state.recognitionProfiles = recognition.profiles || [];
      state.edition = recognition.edition || "standard";
      state.capabilities = recognition.capabilities || {};
      populateRecognitionProfiles();
      applySharedSettings(settings);
      const systemNode = $("#systemState");
      systemNode.classList.add(system.ffmpeg_installed ? "ready" : "error");
      systemNode.querySelector("span").textContent = system.ffmpeg_installed
        ? "就绪"
        : "FFmpeg 尚未就绪";
    } catch (error) {
      $("#systemState").classList.add("error");
      $("#systemState span").textContent = "无法读取系统状态";
      toast(errorMessage(error));
    }
    await initialJobs;
    window.setInterval(() => refreshJobs(false), 1300);
  }

  async function refreshSharedSettings() {
    if (!state.settings || state.settingsDirty || state.savingSettings || state.submitting || state.tutorial.active) return;
    try {
      applySharedSettings(await api("/api/settings"));
    } catch (_) {}
  }

  $("#chooseVideoButton").addEventListener("click", async (event) => {
    event.stopPropagation();
    try {
      const selected = await api('/api/media-select', {method:'POST',headers:{'X-Substar-Media-Select':'1'}});
      if (!selected.cancelled) chooseVideo([selected]);
    } catch (error) { toast(`媒体选择失败：${error.message}`); }
  });
  $("#videoDropZone").addEventListener("click", (event) => { if (!event.target.closest("button")) $("#chooseVideoButton").click(); });
  $("#videoDropZone").addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) $("#chooseVideoButton").click(); });
  $("#videoInput").addEventListener("change", (event) => chooseVideo(event.target.files));
  $("#chooseReferenceButton").addEventListener("click", () => $("#referenceInput").click());
  $("#referenceInput").addEventListener("change", (event) => chooseReferences(event.target.files));
  ["dragenter", "dragover"].forEach((name) => $("#referenceRow").addEventListener(name, (event) => {
    event.preventDefault();
    event.stopPropagation();
    $("#referenceRow").classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((name) => $("#referenceRow").addEventListener(name, (event) => {
    event.preventDefault();
    event.stopPropagation();
    $("#referenceRow").classList.remove("dragging");
  }));
  $("#referenceRow").addEventListener("drop", (event) => chooseReferences(event.dataTransfer.files));
  ["dragenter", "dragover"].forEach((name) => $("#videoDropZone").addEventListener(name, (event) => {
    event.preventDefault();
    $("#videoDropZone").classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((name) => $("#videoDropZone").addEventListener(name, (event) => {
    event.preventDefault();
    $("#videoDropZone").classList.remove("dragging");
  }));
  $("#videoDropZone").addEventListener("drop", (event) => chooseVideo(event.dataTransfer.files));
  $("#recognitionProfileInput").addEventListener("change", () => {
    markSettingsDirty();
  });
  $("#splitLanguageLayout").addEventListener("change", () => { markSettingsDirty(); syncReferenceBreakPreset(); validateForm(); });
  $("#languageInput").addEventListener("change", syncReferenceBreakPreset);
  for (const selector of [
    "#languageInput", "#targetLanguageInput", "#glossaryInput", "#splitWorkflowInput", "#referenceBreakSymbolsInput",
    "#englishLimitInput", "#chineseLimitInput", "#mixedLimitInput", "#otherLimitInput", "#japaneseLimitInput", "#koreanLimitInput",
    "#chunkSecondsInput", "#workersInput", "#retryInput", "#generateThumbnailsInput",
    "#qwenAiBriefInput", "#qwenPromptInput", "#qwenHotwordsInput",
  ]) {
    $(selector)?.addEventListener("input", markSettingsDirty);
    $(selector)?.addEventListener("change", markSettingsDirty);
  }
  $("#qwenPromptInput").addEventListener("input", syncQwenEnhancementCounts);
  $("#qwenHotwordsInput").addEventListener("input", syncQwenEnhancementCounts);
  glossaryInjection = window.SubstarGlossaryInjection.init({api, getTemporary:parseTemporaryHotwords, getProject:()=>state.videos.map(f=>f.name).join(", ").slice(0,100)});
  asrAssist = window.SubstarAsrAssist.init({
    api, getFiles:() => state.videos, getSettings:() => state.settings, getGlossaryIds:()=>glossaryInjection.ids(),
    apply(generated, origin = "ai_asr") {
      if (typeof generated.prompt !== "string" || !generated.prompt.trim()) throw new Error("Prompt 不能为空。");
      if (!Array.isArray(generated.hotwords)) throw new Error("hotwords 必须是数组。");
      for (const word of generated.hotwords) {
        if (!word || typeof word !== "object") throw new Error("每个热词必须包含 text 和 weight。");
        if (typeof word.text !== "string" || !word.text.trim() || !Number.isInteger(word.weight) || ![1,2,3,4,5,50].includes(word.weight)) throw new Error("热词格式无效：text 必须为文字，weight 为 1–5 或 50。");
      }
      for (const word of generated.hotwords) {
        if (/[\r\n]/.test(word.text) || (/[^\x00-\x7f]/.test(word.text) ? [...word.text].length > 15 : word.text.trim().split(/\s+/).length > 7)) throw new Error("热词不能换行；中文等热词最多 15 字，拉丁热词最多 7 个单词。");
      }
      const rows = parseTemporaryHotwords(formatTemporaryHotwords(generated.hotwords));
      const merged = new Map(parseTemporaryHotwords().map(item => [item.text.toLocaleLowerCase(), item]));
      for (const item of rows) if (!merged.has(item.text.toLocaleLowerCase())) merged.set(item.text.toLocaleLowerCase(), item);
      const text = formatTemporaryHotwords([...merged.values()]);
      parseTemporaryHotwords(text);
      $("#qwenPromptInput").value = [...generated.prompt.trim()].slice(0, 400).join("");
      $("#qwenHotwordsInput").value = $("#qwenHotwordsInput").disabled ? "" : text;
      syncQwenEnhancementCounts();
      markSettingsDirty();
      glossaryInjection?.collect(generated.hotwords || [], origin);
    },
  });
  $("#qwenAssistButton").addEventListener("click", fillQwenEnhancement);
  $("#startButton").addEventListener("click", runPipeline);
  $("#refreshJobsButton").addEventListener("click", refreshJobs);
  $("#importProjectButton").addEventListener("click", () => $("#importProjectInput").click());
  $("#importProjectInput").addEventListener("change", async event => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      await api("/api/project-imports/subtitle-project", {method:"POST", body:form});
      await refreshJobs();
      toast("字幕工程已导入并注册到项目列表");
    } catch (error) {
      toast(`导入失败：${errorMessage(error)}`);
    }
  });
  $("#renameProjectForm").addEventListener("submit", submitRename);
  $("#cancelRenameProject").addEventListener("click", closeRenameDialog);
  $("#renameProjectDialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeRenameDialog();
  });
  $("#qwenQuickTest").addEventListener("click", () => testQuickProvider("qwen"));
  $("#glmQuickTest").addEventListener("click", () => testQuickProvider("glm"));
  document.querySelectorAll(".quick-reveal").forEach((button) => button.addEventListener("click", () => {
    const input = $(`#${button.dataset.reveal}`);
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    button.textContent = reveal ? "隐藏" : "显示";
  }));
  window.addEventListener("focus", refreshSharedSettings);

  function initializeSubtitleImport() {
    const option = document.createElement("option");
    option.value = "subtitle";
    option.textContent = "导入已有字幕";
    $("#splitWorkflowInput").append(option);
    const panel = document.createElement("div");
    panel.id = "subtitleImportOptions";
    panel.className = "parameter-panel subtitle-import-panel hidden";
    panel.innerHTML = `<div class="subtitle-import-heading"><strong>字幕导入</strong><span>保留原始时间轴 · 无需听写</span></div>
      <div class="parameter-grid subtitle-import-grid">
        <label><span>字幕格式</span><select id="subtitleFormat"><option value="single">单语 SRT</option><option value="two-files">双语 · 两个 SRT 文件</option><option value="bilingual-lines">双语 · 同条分行</option><option value="bilingual-inline">双语 · 同行分隔</option></select></label>
        <label><span id="subtitleTrackLabel">字幕归属</span><select id="subtitleFirstTrack"><option value="source">原文</option><option value="target">译文</option></select></label>
        <label id="subtitleSeparatorField" class="hidden"><span>同行分隔符</span><input id="subtitleSeparator" type="text" value="|" maxlength="12" aria-label="同行分隔符" placeholder="例如 |"></label>
      </div><p id="subtitleFormatHelp" class="subtitle-import-help"></p>`;
    $("#referenceRow").after(panel);
    panel.addEventListener("input", markSettingsDirty);
    panel.addEventListener("change", markSettingsDirty);
  }

  function syncSubtitleImportControls() {
      const panel = $("#subtitleImportOptions");
      if (!panel) return;
      const enabled = $("#splitWorkflowInput").value === "subtitle";
      const hasSrt = state.references.length > 0 && state.references.every(file=>/\.srt$/i.test(file.name));
      panel.classList.toggle("hidden", !enabled && !hasSrt);
      $("#subtitleImportOptions .subtitle-import-heading span").textContent = enabled ? "保留原始时间轴 · 无需听写" : "按字幕格式提取原文，用于参考文稿";
      $("#referenceRow").classList.toggle("hidden", !enabled && $("#splitWorkflowInput").value === "disabled");
      const strong = $("#referenceRow strong");
      strong.firstChild.textContent = enabled ? "字幕文件 " : "参考文稿 ";
      $("#chooseReferenceButton").textContent = enabled ? "选择字幕文件" : "选择参考文稿";
      if (enabled) {
        $("#referenceRequirement").textContent = "必填";
        $("#referenceHelp").textContent = "支持一个或两个 SRT；创建无词元字幕工程，不执行听写";
        $("#splitWorkflowHelp").textContent = "媒体和字幕均必填；普通文本双轨编辑与审阅";
      }
      $("#referenceInput").accept = enabled ? ".srt" : ".txt,.docx,.srt";
      document.querySelectorAll(".qwen-enhancement-settings").forEach(node => node.classList.toggle("hidden", enabled));
      const format = $("#subtitleFormat").value;
      $("#subtitleSeparatorField").classList.toggle("hidden", format !== "bilingual-inline");
      $("#subtitleSeparator").disabled = format !== "bilingual-inline";
      $("#subtitleTrackLabel").textContent = ({single:"字幕归属", "two-files":"第一个文件", "bilingual-lines":"第一行", "bilingual-inline":"分隔符左侧"})[format];
      $("#subtitleFormatHelp").textContent = ({single:"一个单语文件；原有换行将保留，不会自动拆成双语。", "two-files":"按所选轨道顺序读取两个文件；未配对字幕保留各自的时间。", "bilingual-lines":"每条字幕的两行分别对应原文、译文，按所选顺序导入。", "bilingual-inline":"使用指定分隔符区分两种语言，例如 Hello | 你好。"})[format];
  }

  async function runSubtitleImport() {
    state.submitting = true;
    validateForm();
    try {
      const files = [...state.references];
      const media = state.videos[0];
      const form = new FormData();
      form.append("subtitle", files[0]);
      if (files[1]) form.append("secondary", files[1]);
      form.append("mode", $("#subtitleFormat").value);
      form.append("first_track", $("#subtitleFirstTrack").value);
      form.append("separator", $("#subtitleSeparator").value);
      const preview = await api("/api/subtitle-projects/preview", {method:"POST", body:form});
      if (!preview.can_import) {
        const issues = preview.issues || [];
        throw new Error(issues.slice(0, 5).map(issue => `第 ${issue.entry + 1} 条：${issue.message}`).join("；") || "字幕格式无效，请检查所选格式和文件");
      }
      form.append("confirmation", preview.confirmation);
      form.append("generate_thumbnails", String($("#generateThumbnailsInput").checked));
      if (media.token) form.append("media_reference_token", media.token);
      else form.append("media", media, media.name);
      form.append("source_language", $("#languageInput").value);
      form.append("target_language", $("#targetLanguageInput").value);
      const limitFor = language => Number($(({zh:"#chineseLimitInput", "zh-CN":"#chineseLimitInput", en:"#englishLimitInput", ja:"#japaneseLimitInput", ko:"#koreanLimitInput"})[language] || "#otherLimitInput").value);
      form.append("source_hard_limit", String(limitFor($("#languageInput").value)));
      form.append("target_hard_limit", String(limitFor($("#targetLanguageInput").value)));
      $("#formMessage").textContent = "正在生成波形并保存字幕工程，不执行 ASR 或 AI…";
      const result = await api("/api/subtitle-projects", {method:"POST", body:form});
      clearSubmission();
      toast("字幕项目已创建，可以继续配置下一个项目");
      await refreshJobs();
    } catch (error) {toast(errorMessage(error));}
    finally {state.submitting = false; validateForm();}
  }

  initializeSubtitleImport();
  loadInitialState();
})();
