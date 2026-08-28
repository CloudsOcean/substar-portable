(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const SPLIT_WORKFLOWS = new Set(["subtitle_creation"]);
  const QUEUE_WORKFLOWS = new Set(["subtitle_creation", "editor_task"]);
  const ACTIVE_STATUSES = new Set(["queued", "running", "failed", "interrupted", "cancelled"]);
  const COMPLETE_STATUSES = new Set(["awaiting_edit", "completed"]);
  const MAIN_SPLIT_BRANCH = "A";
  const TASK_CONFIG_STORAGE_KEY = "substar.split.task-config.v1";
  const TUTORIAL_STATUS_KEY = "substar.split.tutorial.v1";
  const TUTORIAL_AUDIO_URL = "/api/examples/tutorials/reference-script-v1/assets/media";
  const TUTORIAL_REFERENCE_URL = "/api/examples/tutorials/reference-script-v1/assets/reference";
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
    toastTimer: 0,
    runtimeJobId: "",
    runtimeLogText: "",
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
    try { body = await response.json(); } catch { body = null; }
    if (!response.ok) throw new Error(errorMessage(body) || `HTTP ${response.status}`);
    return body;
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
    $("#creationTutorialActions").classList.toggle("hidden", showQuickStart);
    $("#pipelineTitle").textContent = showQuickStart ? "快速开始" : "流水线作业";
    const statusPill = $("#statusPill");
    if (showQuickStart) {
      const completeCount = Number(quickProviderComplete("qwen")) + Number(quickProviderComplete("glm"));
      statusPill.className = `status-pill ${completeCount === 2 ? "completed" : "idle"}`;
      statusPill.textContent = completeCount === 2 ? "配置完成" : `${2 - completeCount} 项待完成`;
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
        thinking_mode: "disabled",
        reasoning_effort: "high",
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
      const weighted = line.match(/^(.*):(\d+)$/);
      const word = String(weighted?.[1] ?? line).trim();
      const weight = Number(weighted?.[2] ?? 4);
      if (!word) throw new Error(`第 ${index + 1} 行热词不能为空`);
      if (weighted && /:\d+$/.test(word)) {
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
    } catch (error) {
      status.textContent = errorMessage(error);
      status.classList.add("bad");
    } finally {
      button.disabled = false;
      button.textContent = "AI 智能填写";
    }
  }

  function taskConfigFromControls() {
    const workflow = $("#splitWorkflowInput").value;
    const segmentationEnabled = workflow === "one_step";
    return {
      language: $("#languageInput").value,
      target_language_mode: $("#targetLanguageInput").value,
      glossary_id: $("#glossaryInput").value,
      segmentation_enabled: segmentationEnabled,
      reference_script_mode: workflow === "reference_script",
      reference_break_symbols: $("#referenceBreakSymbolsInput").value,
      translation_enabled: false,
      split_branch: MAIN_SPLIT_BRANCH,
      english_hard_limit: Number($("#englishLimitInput").value),
      chinese_hard_limit: Number($("#chineseLimitInput").value),
      mixed_hard_limit: Number($("#mixedLimitInput").value),
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
    $("#referenceRow").classList.toggle("hidden", !referenceMode);
    $("#referenceBreakSymbolsField").classList.toggle("hidden", !referenceMode);
    $("#splitWorkflowHelp").textContent = referenceMode
      ? "ASR 提供文字与时间证据，参考稿修正差异并提供标点断点"
      : workflow === "one_step"
        ? "由 AI 按语义和字符上限重新组织字幕"
        : "直接使用听写结果的句级边界";
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

  const TUTORIAL_CONTROL_IDS = [
    "languageInput", "targetLanguageInput", "glossaryInput", "splitWorkflowInput", "referenceBreakSymbolsInput",
    "englishLimitInput", "chineseLimitInput", "mixedLimitInput", "japaneseLimitInput", "koreanLimitInput",
  ];

  const TUTORIAL_STEPS = [
    {
      target: "#qwenQuickCard", title: "连接 QWEN 听写服务",
      description: "听写模块负责将音视频转换成文字信息。输入百炼 API Key，然后点击“测试连接”。",
      link: { label: "点击注册并获取 API Key", href: "https://bailian.console.aliyun.com/cn-beijing#/home" },
    },
    {
      target: "#glmQuickCard", title: "连接智谱 GLM 文本服务",
      description: "文本模块负责语义切分，以及后续翻译、校准和审阅。输入智谱 API Key，然后点击“测试连接”。",
      link: { label: "点击注册并获取 API Key", href: "https://bigmodel.cn/" },
    },
    {
      target: "#videoDropZone", title: "载入教程音频",
      description: "点击“载入教程音频”，将内置案例添加到当前任务。",
      primary: "载入教程音频",
    },
    {
      target: "#languageInput", title: "将原文语言改为中文",
      description: "案例音频使用中文。打开“原文语言”，选择“中文”。",
    },
    {
      target: "#targetLanguageInput", title: "将目标语言改为英文",
      description: "本次案例将生成英文字幕。打开“目标语言”，选择“英文”。",
    },
    {
      target: ".language-limit-settings", title: "将中文字符上限改为 16",
      description: "字符数上限控制单条字幕的长度。把“中文”从 25 改为 16。",
    },
    {
      target: "#splitWorkflowInput", title: "选择参考文稿辅助切分",
      description: "仅使用听写结果：直接沿用听写分段\nAI 辅助切分：由 AI 按语义重新分段\n参考文稿辅助切分：结合听写时间信息和参考文稿，本案例请选择此项",
    },
    {
      target: "#referenceRow", title: "载入教程参考文稿",
      description: "参考文稿用于校正听写文字并提供标点断点。点击“载入教程参考稿”。",
      primary: "载入教程参考稿",
    },
    {
      target: "#startButton", title: "创建字幕项目",
      description: "音频、参考文稿和任务配置已准备完成。点击页面中的“创建项目”。",
    },
    {
      target: ".pipeline-card", title: "查看任务进度",
      description: "任务会在后台继续运行。你可以离开本页，失败时这里会显示具体阶段。",
      primary: "完成切分页教程",
    },
  ];

  const ADVANCED_TUTORIAL_STEPS = [
    {
      target: ".language-limit-settings", title: "恢复中文字符上限",
      description: "初级教程会把中文字符数上限改为 16。开始进阶流程前，请展开字符数上限设置，把中文改回 25；设置正确后教程会自动继续。",
    },
    {
      target: "#splitWorkflowInput", title: "AI 辅助切分",
      description: "进阶教程直接使用预存的英文 ASR 材料，并载入已经通过校验的 AI 切分结果。这里展示正式工作流的阶段边界，不重复文件选择和基础设置。",
      primary: "模拟执行 AI 切分",
    },
    {
      target: ".pipeline-card", title: "载入切分阶段快照",
      description: "正在依次载入 ASR 材料、语义分组和 34 条 Cue。此过程只读取软件内置案例，不会调用 Qwen 或 DeepSeek。",
    },
    {
      target: ".recent-section", title: "切分完成，进入 AI 编辑链",
      description: "切分结果已经按正式 EditorDocument 契约注册。下一页将依次展示 AI 校准、AI 翻译和 AI 审阅，每一步同样载入预存结果。",
      primary: "进入进阶教程编辑页",
    },
  ];

  function currentTutorialSteps() {
    return state.tutorial.kind === "advanced" ? ADVANCED_TUTORIAL_STEPS : TUTORIAL_STEPS;
  }

  function tutorialSnapshot() {
    return {
      controls: Object.fromEntries(TUTORIAL_CONTROL_IDS.map((id) => [id, $(`#${id}`).value])),
      videos: [...state.videos],
      references: [...state.references],
      storedTaskConfig: localStorage.getItem(TASK_CONFIG_STORAGE_KEY),
    };
  }

  function clearTutorialTarget() {
    document.querySelectorAll(".tutorial-target").forEach((node) => node.classList.remove("tutorial-target"));
  }

  function restoreTutorialSnapshot(snapshot) {
    if (!snapshot) return;
    for (const [id, value] of Object.entries(snapshot.controls || {})) {
      const control = $(`#${id}`);
      if (control) control.value = value;
    }
    if (snapshot.storedTaskConfig === null) localStorage.removeItem(TASK_CONFIG_STORAGE_KEY);
    else localStorage.setItem(TASK_CONFIG_STORAGE_KEY, snapshot.storedTaskConfig);
    state.videos = [...(snapshot.videos || [])];
    state.references = [...(snapshot.references || [])];
    const total = state.videos.reduce((sum, file) => sum + file.size, 0);
    $("#videoFileChip").textContent = state.videos.length === 1
      ? `${state.videos[0].name} · ${formatBytes(total)}`
      : state.videos.length ? `${state.videos.length} 个素材 · ${formatBytes(total)}` : "";
    $("#videoFileChip").classList.toggle("hidden", !state.videos.length);
    $("#referenceFileName").textContent = state.references.length === 0
      ? "未添加" : state.references.length === 1 ? state.references[0].name : `${state.references.length} 份参考文稿`;
    syncWorkflowControl();
    validateForm();
  }

  function exitSplitTutorial({ completed = false } = {}) {
    const snapshot = state.tutorial.snapshot;
    const shouldRestore = !completed && !state.tutorial.submitted;
    state.tutorial.active = false;
    state.tutorial.kind = "beginner";
    state.tutorial.step = 0;
    state.tutorial.tests = { qwen: false, glm: false };
    state.tutorial.snapshot = null;
    clearTutorialTarget();
    $("#splitTutorialLayer").classList.add("hidden");
    $("#splitTutorialLayer").setAttribute("aria-hidden", "true");
    if (shouldRestore) restoreTutorialSnapshot(snapshot);
    if (completed) localStorage.setItem(TUTORIAL_STATUS_KEY, "completed");
    syncPrimaryPanel();
  }

  function startSplitTutorial() {
    if (!state.settings) return toast("正在读取配置，请稍候再开始教程");
    state.tutorial.snapshot = tutorialSnapshot();
    state.tutorial.active = true;
    state.tutorial.kind = "beginner";
    state.tutorial.step = 0;
    state.tutorial.submitted = false;
    state.tutorial.tests = { qwen: false, glm: false };
    state.videos = [];
    state.references = [];
    $("#videoFileChip").textContent = "";
    $("#videoFileChip").classList.add("hidden");
    $("#referenceFileName").textContent = "未添加";
    $("#languageInput").value = "en";
    $("#targetLanguageInput").value = "zh-CN";
    $("#splitWorkflowInput").value = "disabled";
    $("#chineseLimitInput").value = "25";
    syncReferenceBreakPreset();
    markSettingsDirty();
    $("#splitTutorialLayer").classList.remove("hidden");
    $("#splitTutorialLayer").setAttribute("aria-hidden", "false");
    syncPrimaryPanel();
    renderSplitTutorial();
  }

  function startAdvancedSplitTutorial() {
    if (!state.settings) return toast("正在读取配置，请稍候再开始教程");
    state.tutorial.snapshot = tutorialSnapshot();
    state.tutorial.active = true;
    state.tutorial.kind = "advanced";
    state.tutorial.step = 0;
    state.tutorial.submitted = false;
    state.tutorial.projectId = "";
    $("#languageInput").value = "en";
    $("#targetLanguageInput").value = "zh-CN";
    $("#splitWorkflowInput").value = "one_step";
    syncWorkflowControl();
    $("#splitTutorialLayer").classList.remove("hidden");
    $("#splitTutorialLayer").setAttribute("aria-hidden", "false");
    renderSplitTutorial();
    maybeAdvanceTutorial();
  }

  async function launchAdvancedTutorial() {
    const primary = $("#splitTutorialPrimary");
    primary.disabled = true;
    primary.textContent = "正在载入切分结果…";
    state.tutorial.step = 2;
    renderSplitTutorial();
    try {
      await new Promise(resolve => window.setTimeout(resolve, 650));
      const launch = await api("/api/examples/tutorials/advanced-ai-v1/launch", {method:"POST"});
      state.tutorial.projectId = launch.project_id;
      state.tutorial.submitted = true;
      advanceSplitTutorial(3);
      await refreshJobs();
    } catch (error) {
      toast(`进阶教程载入失败：${errorMessage(error)}`);
      state.tutorial.step = 1;
      renderSplitTutorial();
    } finally {
      primary.disabled = false;
    }
  }

  function positionSplitTutorial() {
    if (!state.tutorial.active) return;
    clearTutorialTarget();
    const step = currentTutorialSteps()[state.tutorial.step];
    const target = document.querySelector(step.target);
    if (!target) return;
    target.classList.add("tutorial-target");
    const rect = target.getBoundingClientRect();
    const pad = 6;
    const spotlight = $("#splitTutorialSpotlight");
    spotlight.style.left = `${Math.max(6, Math.round(rect.left - pad))}px`;
    spotlight.style.top = `${Math.max(6, Math.round(rect.top - pad))}px`;
    spotlight.style.width = `${Math.round(rect.width + pad * 2)}px`;
    spotlight.style.height = `${Math.round(rect.height + pad * 2)}px`;
    const card = $("#splitTutorialCard");
    if (window.innerWidth <= 900) {
      card.dataset.placement = "mobile";
      return;
    }
    const margin = 18;
    const gap = 20;
    const cardWidth = card.offsetWidth;
    const cardHeight = card.offsetHeight;
    const clamp = (value, min, max) => Math.min(Math.max(value, min), Math.max(min, max));
    const centeredTop = clamp(rect.top + (rect.height - cardHeight) / 2, margin, window.innerHeight - cardHeight - margin);
    const centeredLeft = clamp(rect.left + (rect.width - cardWidth) / 2, margin, window.innerWidth - cardWidth - margin);
    const candidates = {
      right: { x: rect.right + gap, y: centeredTop },
      left: { x: rect.left - gap - cardWidth, y: centeredTop },
      below: { x: centeredLeft, y: rect.bottom + gap },
      above: { x: centeredLeft, y: rect.top - gap - cardHeight },
    };
    const horizontalOrder = rect.left + rect.width / 2 > window.innerWidth / 2
      ? ["left", "right"] : ["right", "left"];
    const order = [...horizontalOrder, "below", "above"];
    const fits = ({ x, y }) => x >= margin && y >= margin
      && x + cardWidth <= window.innerWidth - margin
      && y + cardHeight <= window.innerHeight - margin;
    let placement = order.find((name) => fits(candidates[name]));
    if (!placement) {
      const available = {
        right: window.innerWidth - rect.right,
        left: rect.left,
        below: window.innerHeight - rect.bottom,
        above: rect.top,
      };
      placement = order.reduce((best, name) => available[name] > available[best] ? name : best, order[0]);
    }
    const chosen = candidates[placement];
    card.style.left = `${Math.round(clamp(chosen.x, margin, window.innerWidth - cardWidth - margin))}px`;
    card.style.top = `${Math.round(clamp(chosen.y, margin, window.innerHeight - cardHeight - margin))}px`;
    card.style.right = "auto";
    card.style.bottom = "auto";
    card.dataset.placement = placement;
  }

  function renderSplitTutorial() {
    if (!state.tutorial.active) return;
    const steps = currentTutorialSteps();
    const step = steps[state.tutorial.step];
    $("#splitTutorialProgress").textContent = `${state.tutorial.kind === "advanced" ? "进阶教程" : "初级教程"} · ${state.tutorial.step + 1} / ${steps.length}`;
    $("#splitTutorialTitle").textContent = step.title;
    $("#splitTutorialDescription").textContent = step.description;
    const tutorialLink = $("#splitTutorialLink");
    tutorialLink.classList.toggle("hidden", !step.link);
    tutorialLink.textContent = step.link?.label || "";
    tutorialLink.href = step.link?.href || "#";
    const primary = $("#splitTutorialPrimary");
    primary.classList.toggle("hidden", !step.primary);
    primary.textContent = step.primary || "";
    $("#splitTutorialSecondary").classList.add("hidden");
    if ((state.tutorial.kind === "beginner" && state.tutorial.step === 5)
      || (state.tutorial.kind === "advanced" && state.tutorial.step === 0)) {
      $(".language-limit-settings").open = true;
    }
    syncPrimaryPanel();
    const target = document.querySelector(step.target);
    target?.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
    window.setTimeout(positionSplitTutorial, 260);
    $("#splitTutorialCard").focus({ preventScroll: true });
  }

  function advanceSplitTutorial(nextStep = state.tutorial.step + 1) {
    if (!state.tutorial.active || nextStep >= currentTutorialSteps().length) return;
    state.tutorial.step = nextStep;
    renderSplitTutorial();
  }

  function maybeAdvanceTutorial() {
    if (!state.tutorial.active) return;
    if (state.tutorial.kind === "advanced") {
      if (state.tutorial.step === 0 && Number($("#chineseLimitInput").value) === 25) {
        window.setTimeout(() => {
          if (state.tutorial.active
            && state.tutorial.kind === "advanced"
            && state.tutorial.step === 0
            && Number($("#chineseLimitInput").value) === 25) {
            advanceSplitTutorial(1);
          }
        }, 180);
      }
      return;
    }
    if (state.tutorial.kind !== "beginner") return;
    const step = state.tutorial.step;
    const complete = [
      state.tutorial.tests.qwen,
      state.tutorial.tests.glm,
      state.videos.some((file) => file.name === "教程音频.mp3"),
      $("#languageInput").value === "zh",
      $("#targetLanguageInput").value === "en",
      Number($("#chineseLimitInput").value) === 16,
      $("#splitWorkflowInput").value === "reference_script",
      state.references.some((file) => file.name === "教程参考文稿.txt"),
      false,
      false,
    ][step];
    if (complete) window.setTimeout(() => {
      if (state.tutorial.active && state.tutorial.step === step) advanceSplitTutorial();
    }, 180);
  }

  async function loadTutorialAsset(kind) {
    const primary = $("#splitTutorialPrimary");
    primary.disabled = true;
    primary.textContent = "正在载入…";
    try {
      const audio = kind === "audio";
      const response = await fetch(audio ? TUTORIAL_AUDIO_URL : TUTORIAL_REFERENCE_URL);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const file = new File([blob], audio ? "教程音频.mp3" : "教程参考文稿.txt", {
        type: audio ? "audio/mpeg" : "text/plain",
      });
      if (audio) chooseVideo([file]);
      else chooseReferences([file]);
      maybeAdvanceTutorial();
    } catch (error) {
      toast(`教程素材载入失败：${errorMessage(error)}`);
      renderSplitTutorial();
    } finally {
      primary.disabled = false;
    }
  }

  function applySharedSettings(settings) {
    state.settings = settings;
    const effective = {...settings, ...(state.taskConfigLoaded ? taskConfigFromControls() : storedTaskConfig())};
    $("#recognitionProfileInput").value = settings.recognition_profile_id || "faster_whisper_native";
    if (!$("#recognitionProfileInput").value) $("#recognitionProfileInput").selectedIndex = 0;
    $("#languageInput").value = ["Auto", "zh", "en", "mixed", "ja", "ko"].includes(effective.language)
      ? effective.language : "Auto";
    const configuredTarget = effective.target_language_mode || "zh-CN";
    $("#targetLanguageInput").value = ["zh-CN", "en", "ja", "ko"].includes(configuredTarget)
      ? configuredTarget
      : "zh-CN";
    // The split page currently uses the global glossary only. Keep the hidden
    // field blank so older saved task settings cannot silently restore a
    // project glossary after the selector has been removed from the UI.
    $("#glossaryInput").value = "";
    $("#englishLimitInput").value = effective.english_hard_limit || 55;
    $("#chineseLimitInput").value = effective.chinese_hard_limit || 25;
    $("#mixedLimitInput").value = effective.mixed_hard_limit || 25;
    $("#japaneseLimitInput").value = effective.japanese_hard_limit || 25;
    $("#koreanLimitInput").value = effective.korean_hard_limit || 32;
    $("#qwenAiBriefInput").value = effective.qwen_ai_brief || "";
    $("#qwenPromptInput").value = effective.context || "";
    $("#qwenHotwordsInput").value = formatTemporaryHotwords(effective.qwen_temporary_hotwords || []);
    syncQwenEnhancementModel();
    $("#splitWorkflowInput").value = effective.reference_script_mode
      ? "reference_script"
      : effective.segmentation_enabled === false ? "disabled" : "one_step";
    const configuredBreakSymbols = String(effective.reference_break_symbols || "");
    $("#referenceBreakSymbolsInput").value = configuredBreakSymbols && !isReferenceBreakPreset(configuredBreakSymbols)
      ? configuredBreakSymbols
      : referenceBreakPreset($("#languageInput").value);
    if (state.edition === "slim") {
      $("#recognitionProfileInput").value = "qwen_cloud";
      $("#splitWorkflowInput").value = effective.reference_script_mode
        ? "reference_script"
        : effective.segmentation_enabled === false ? "disabled" : "one_step";
    }
    $("#chunkSecondsInput").value = settings.segmentation_chunk_seconds || 90;
    $("#workersInput").value = settings.translation_workers || 16;
    $("#retryInput").value = settings.http_retry_attempts ?? 3;
    $("#repairInput").value = settings.segmentation_repair_attempts ?? 1;
    state.settingsDirty = false;
    state.taskConfigLoaded = true;
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
    if (job.workflow_mode === "project_pipeline") {
      return job.current_phase_label || "项目流水线";
    }
    if (job.workflow_mode === "editor_task") {
      return ({translation:"翻译", calibration:"AI 校准", review:"AI 审阅"})[job.task_kind] || "编辑器任务";
    }
    if (job.workflow_mode === "subtitle_creation") return "词级字幕创建";
    return job.settings_overrides?.recognition_profile_label || "云端听写";
  }

  function jobLabel(job) {
    return job.display_name || job.filename || job.id;
  }

  function editorTaskLabel(kind) {
    return ({translation:"翻译", calibration:"AI 校准", review:"AI 审阅"})[kind] || "编辑器任务";
  }

  function coalescePipelineJobs(jobs) {
    const groups = new Map();
    for (const job of jobs) {
      const projectId = job.project_id || (job.workflow_mode === "editor_task" ? "" : job.id);
      const key = projectId || job.id;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(job);
    }
    return [...groups.entries()].map(([projectId, items]) => {
      if (items.length === 1) return items[0];
      const split = items.find(item => item.workflow_mode !== "editor_task");
      const editorTasks = items.filter(item => item.workflow_mode === "editor_task");
      const current = editorTasks.find(item => item.status === "running")
        || editorTasks.find(item => item.status === "queued")
        || split
        || editorTasks[0];
      const hasRunning = items.some(item => item.status === "running");
      const hasQueued = items.some(item => item.status === "queued");
      const failed = items.find(item => item.status === "failed");
      const status = hasRunning ? "running" : hasQueued ? "queued" : failed ? "failed" : current.status;
      return {
        ...current,
        id:`pipeline:${projectId}`,
        project_id:projectId,
        display_name:split?.display_name || split?.filename || current.display_name || projectId,
        workflow_mode:"project_pipeline",
        status,
        progress:current.progress,
        message:current.message,
        error:status === "failed" ? (failed?.error || "") : (current.error || ""),
        current_phase_label:current.workflow_mode === "editor_task"
          ? editorTaskLabel(current.task_kind) : "切分",
        pipeline_items:items,
        split_job:split || null,
      };
    });
  }

  function normalizedProgress(job) {
    const value = Number(job.progress || 0);
    const stored = value <= 1 ? value * 100 : value;
    return Math.max(0, Math.min(100, Math.round(stored)));
  }

  function humanStatus(job) {
    if (job.status === "queued") return "排队中";
    if (job.status === "running") return job.message || "处理中";
    if (job.status === "failed") return "处理失败";
    if (job.status === "interrupted") return "任务已中断";
    if (job.status === "cancelled") return "任务已取消，项目文件已保留";
    if (job.status === "awaiting_edit") return "等待编辑";
    return "切分完成";
  }

  const TASK_STEP_LABELS = Object.freeze({
    "transcription.media_probe":"正在读取媒体信息",
    "transcription.audio_prepare":"正在准备听写音频",
    "transcription.provider_audio_encode":"正在编码上传音频",
    "transcription.provider_upload":"正在上传音频",
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
    return TASK_STEP_LABELS[String(job.step || "")] || humanStatus(job);
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
    const accepted = selected;
    state.videos = accepted;
    state.submissionKey = crypto.randomUUID();
    const total = accepted.reduce((sum, file) => sum + file.size, 0);
    const chip = $("#videoFileChip");
    chip.textContent = accepted.length === 1
      ? `${accepted[0].name} · ${formatBytes(total)}`
      : `${accepted.length} 个素材 · ${formatBytes(total)}`;
    chip.title = accepted.map((file) => file.name).join("\n");
    chip.classList.toggle("hidden", !accepted.length);
    validateForm();
    maybeAdvanceTutorial();
  }

  function chooseReferences(files) {
    const selected = [...(files || [])];
    state.references = selected.filter((file) => /\.(txt|docx|srt)$/i.test(file.name));
    if (state.references.length !== selected.length) toast("已忽略不支持的参考文稿");
    const label = $("#referenceFileName");
    label.textContent = state.references.length === 0
      ? "未添加"
      : (state.references.length === 1 ? state.references[0].name : `${state.references.length} 份参考文稿`);
    label.title = state.references.map((file) => file.name).join("\n");
    validateForm();
    maybeAdvanceTutorial();
  }

  function validateForm() {
    const referenceMode = $("#splitWorkflowInput").value === "reference_script";
    const symbols = $("#referenceBreakSymbolsInput").value.replace(/\s/g, "");
    const valid = state.videos.length > 0
      && (!referenceMode || (state.references.length > 0 && symbols.length > 0));
    $("#startButton").disabled = !valid || !state.settings || state.submitting || state.savingSettings;
    const message = $("#formMessage");
    message.classList.remove("error");
    if (!state.settings) message.textContent = "正在读取已保存设置…";
    else if (state.submitting) message.textContent = "正在接收素材并创建任务…";
    else if (!state.videos.length) message.textContent = "选择一个或多个素材后即可开始";
    else if (referenceMode && !state.references.length) message.textContent = "参考文稿辅助切分必须选择参考文稿";
    else if (referenceMode && !symbols.length) message.textContent = "请填写至少一个切分符号";
    else message.textContent = state.videos.length > 1
      ? `${state.videos.length} 个素材已就绪，将分别创建项目`
      : "素材已就绪";
    $("#startButton span").textContent = state.submitting
      ? "正在投递"
      : (state.videos.length > 1 ? `创建 ${state.videos.length} 个项目` : "创建项目");
  }

  function clearSubmission() {
    state.videos = [];
    state.references = [];
    state.submissionKey = "";
    $("#videoInput").value = "";
    $("#referenceInput").value = "";
    $("#videoFileChip").textContent = "";
    $("#videoFileChip").classList.add("hidden");
    $("#referenceFileName").textContent = "未添加";
    $("#referenceFileName").removeAttribute("title");
    validateForm();
  }

  function automaticSettings() {
    const current = state.settings || {};
    const workflow = $("#splitWorkflowInput").value;
    const segmentationEnabled = workflow === "one_step";
    const result = {
      translation_api_base_url: current.translation_api_base_url,
      translation_api_model: current.translation_api_model,
      recognition_profile_id: $("#recognitionProfileInput").value,
      language: $("#languageInput").value,
      alignment_language: alignmentLanguage($("#languageInput").value),
      target_language_mode: $("#targetLanguageInput").value,
      glossary_id: $("#glossaryInput").value,
      segmentation_enabled: segmentationEnabled,
      reference_script_mode: workflow === "reference_script",
      reference_break_symbols: $("#referenceBreakSymbolsInput").value,
      translation_enabled: false,
      calibration_enabled: false,
      english_hard_limit: Number($("#englishLimitInput").value),
      chinese_hard_limit: Number($("#chineseLimitInput").value),
      mixed_hard_limit: Number($("#mixedLimitInput").value),
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
      segmentation_repair_attempts: Number($("#repairInput").value),
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
    form.append("mode", "asr");
    form.append("media", media, media.name);
    if ($("#splitWorkflowInput").value === "reference_script" && references.length) {
      form.append("reference_document", references[0], references[0].name);
    }
    form.append("settings_json", JSON.stringify(automaticSettings()));
    if (state.tutorial.active && state.tutorial.kind === "beginner") form.append("tutorial_case_id", "reference-script-v1");
    return api("/api/project-creations", {
      method: "POST",
      headers: {"Idempotency-Key": state.submissionKey},
      body: form,
    });
  }

  async function submitBatch(mediaFiles, references) {
    const form = new FormData();
    form.append("mode", "asr");
    mediaFiles.forEach((file) => form.append("media", file, file.name));
    const mediaStems = new Set(mediaFiles.map((file) => file.name.replace(/\.[^.]+$/, "").toLocaleLowerCase()));
    const referenceMode = $("#splitWorkflowInput").value === "reference_script";
    const matchedReferences = referenceMode
      ? references.filter((file) => mediaStems.has(file.name.replace(/\.[^.]+$/, "").toLocaleLowerCase()))
      : [];
    matchedReferences.forEach((file) => form.append("reference_documents", file, file.name));
    if (references.length > matchedReferences.length) toast(`${references.length - matchedReferences.length} 份参考文稿因未找到同名素材而未提交`);
    form.append("settings_json", JSON.stringify(automaticSettings()));
    return api("/api/project-batches", {
      method: "POST",
      headers: {"Idempotency-Key": state.submissionKey},
      body: form,
    });
  }

  async function runPipeline() {
    if (state.submitting || !state.settings || !state.videos.length) return;
    const mediaFiles = [...state.videos];
    const references = [...state.references];
    state.submitting = true;
    validateForm();
    try {
      if (mediaFiles.length > 1) await submitBatch(mediaFiles, references);
      else await submitSingle(mediaFiles[0], references);
      state.submitting = false;
      clearSubmission();
      toast(`${mediaFiles.length} 个任务已进入流水线`);
      await refreshJobs();
      if (state.tutorial.active && state.tutorial.step === 8) {
        state.tutorial.submitted = true;
        advanceSplitTutorial(9);
      }
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

  function renderRuntimeLog() {
    const container = $("#runtimeLogLines");
    const followingTail = container.scrollHeight - container.scrollTop - container.clientHeight < 28;
    const output = document.createElement("pre");
    output.textContent = state.runtimeLogText || "暂无日志";
    container.replaceChildren(output);
    if (followingTail) container.scrollTop = container.scrollHeight;
  }

  function displayTaskMessage(job) {
    return taskPhase(job);
  }

  function renderTaskProgress(job) {
    if (job.workflow_mode === "project_pipeline") {
      const phaseRows = (job.pipeline_items || []).map(item => {
        const label = item.workflow_mode === "editor_task" ? editorTaskLabel(item.task_kind) : "切分";
        return `${label} · ${taskPhase(item)} · ${normalizedProgress(item)}%`;
      });
      state.runtimeLogText = [
        `${jobLabel(job)} · 当前阶段 ${job.current_phase_label}`,
        ...phaseRows,
        ...(taskError(job) ? [`错误：${taskError(job)}`] : []),
      ].join("\n");
      renderRuntimeLog();
      return;
    }
    if (job.workflow_mode === "editor_task") {
      state.runtimeLogText = [
        `${jobLabel(job)} · ${routeLabel(job)} · ${normalizedProgress(job)}%`,
        displayTaskMessage(job),
        ...(taskError(job) ? [`错误：${taskError(job)}`] : []),
      ].join("\n");
      renderRuntimeLog();
      return;
    }
    const summary = [
      `${jobLabel(job)} · ${normalizedProgress(job)}%`,
      displayTaskMessage(job),
    ];
    if (taskError(job)) summary.push(`错误：${taskError(job)}`);
    state.runtimeLogText = summary.join("\n");
    renderRuntimeLog();
  }

  function updateRuntimeLog(active) {
    const selected = active.find((job) => job.id === state.runtimeJobId) || active[0];
    if (!selected) return;
    if (state.runtimeJobId !== selected.id) {
      state.runtimeJobId = selected.id;
    }
    renderTaskProgress(selected);
  }

  function renderQueue(jobs) {
    const activeEditorProjects = new Set(
      jobs.filter(job => job.workflow_mode === "editor_task" && ACTIVE_STATUSES.has(job.status))
        .map(job => job.project_id)
    );
    const active = coalescePipelineJobs(
      jobs.filter((job) => QUEUE_WORKFLOWS.has(job.workflow_mode) && (
        ACTIVE_STATUSES.has(job.status)
        || (activeEditorProjects.has(job.id) && job.workflow_mode !== "editor_task")
      ))
    );
    const container = $("#pipelineJobs");
    state.activeQueueCount = active.length;
    $("#emptyPipeline").classList.toggle("hidden", active.length > 0);
    container.classList.toggle("hidden", active.length === 0);
    $("#statusPill").className = `status-pill ${!state.runtimeConnected ? "failed" : active.length ? "running" : "idle"}`;
    $("#statusPill").textContent = !state.runtimeConnected
      ? "后端已断开"
      : active.length ? `${active.length} 个任务` : "等待任务";
    syncPrimaryPanel();
    updateRuntimeLog(active);
    container.replaceChildren(...active.map((job) => {
      const percent = normalizedProgress(job);
      const card = document.createElement("article");
      const disconnectedActive = !state.runtimeConnected && ["queued", "running"].includes(job.status);
      card.className = `queue-job ${disconnectedActive ? "interrupted" : job.status}`;
      card.classList.toggle("log-selected", job.id === state.runtimeJobId);
      card.title = "点击查看此任务的阶段进度";
      card.addEventListener("click", () => {
        state.runtimeJobId = job.id;
        renderTaskProgress(job);
        container.querySelectorAll(".queue-job").forEach((node) => node.classList.toggle("log-selected", node === card));
      });
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
        errorBlock.append(errorValue, copyError);
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
        if (!["editor_task", "project_pipeline"].includes(job.workflow_mode)) {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.textContent = "重试";
          retry.addEventListener("click", () => retryJob(job.id, retry));
          actions.append(retry);
        }
      }
      const removableJob = job.workflow_mode === "project_pipeline" ? job.split_job : job;
      if (!disconnectedActive && removableJob && removableJob.workflow_mode !== "editor_task") {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "queue-delete";
        remove.textContent = ["queued", "running"].includes(job.status) ? "取消" : "删除";
        remove.addEventListener("click", () => deleteJob(removableJob, card, remove));
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
      link.href = enabled ? (contract?.url || exportEndpoint(job.id, kind)) : "#";
      if (!enabled) {
        link.className = "disabled";
        link.setAttribute("aria-disabled", "true");
        link.addEventListener("click", (event) => event.preventDefault());
      }
      panel.append(link);
    });
    details.append(summary, panel);
    details.addEventListener("mouseenter", () => { details.open = true; });
    details.addEventListener("mouseleave", () => { details.open = false; });
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
    const complete = jobs.filter((job) => COMPLETE_STATUSES.has(job.status)).slice(0, 8);
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
  }

  async function refreshJobs() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const [jobs, editorTasks, projects] = await Promise.all([
        api("/api/project-creations"),
        api("/api/editor-tasks").catch(() => ({tasks:[]})),
        api("/api/projects").catch(() => ({projects:[]})),
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
      state.jobs = jobs
        .filter((job) => !state.removedProjectIds.has(job.id))
        .filter((job) => SPLIT_WORKFLOWS.has(job.workflow_mode))
        .map(job => ({...job, complete:completeProjects.has(job.id)}));
      const knownProjectIds = new Set(state.jobs.map(job => job.id));
      const tutorialProjectRows = (projects.projects || [])
        .filter(project => !state.removedProjectIds.has(project.project_id))
        .filter(project => project.tutorial_case_id && !knownProjectIds.has(project.project_id))
        .map(project => ({
          id:project.project_id,
          project_id:project.project_id,
          display_name:project.display_name || project.project_id,
          workflow_mode:"subtitle_creation",
          status:"awaiting_edit",
          progress:1,
          message:"教程已准备完成",
          created_at:Math.max(0, Date.parse(project.updated_at || "") / 1000 || 0),
          tutorial_case_id:project.tutorial_case_id,
          complete:project.complete === true,
          project_only:true,
        }));
      const taskJobs = (editorTasks.tasks || []).map((task) => ({
        id:`editor:${task.project_id}:${task.task_id}`,
        project_id:task.project_id,
        display_name:task.display_name || task.project_id,
        workflow_mode:"editor_task",
        task_kind:task.kind,
        status:task.status,
        progress:Number(task.progress || 0),
        message:task.message || "",
        error:task.error || "",
        created_at:task.created_at,
      }));
      renderQueue([...taskJobs, ...state.jobs]);
      renderRecent([...tutorialProjectRows, ...state.jobs]
        .sort((left, right) => Number(right.created_at || 0) - Number(left.created_at || 0)));
    } catch (error) {
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
      state.runtimeLogText = "后端连接已断开。页面已停止把缓存状态显示为正在运行；请从任务栏中的 Substar 后端窗口确认并重新启动。";
      renderRuntimeLog();
      if (!state.jobs.length) $("#recentJobs").innerHTML = `<p class="recent-empty">${errorMessage(error)}</p>`;
    } finally {
      state.refreshing = false;
    }
  }

  async function loadInitialState() {
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
    await refreshJobs();
    window.setInterval(refreshJobs, 1300);
  }

  async function refreshSharedSettings() {
    if (!state.settings || state.settingsDirty || state.savingSettings || state.submitting || state.tutorial.active) return;
    try {
      applySharedSettings(await api("/api/settings"));
    } catch (_) {}
  }

  $("#chooseVideoButton").addEventListener("click", (event) => { event.stopPropagation(); $("#videoInput").click(); });
  $("#videoDropZone").addEventListener("click", (event) => { if (!event.target.closest("button")) $("#videoInput").click(); });
  $("#videoDropZone").addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) $("#videoInput").click(); });
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
  $("#languageInput").addEventListener("change", syncReferenceBreakPreset);
  for (const selector of [
    "#languageInput", "#targetLanguageInput", "#glossaryInput", "#splitWorkflowInput", "#referenceBreakSymbolsInput",
    "#englishLimitInput", "#chineseLimitInput", "#mixedLimitInput", "#japaneseLimitInput", "#koreanLimitInput",
    "#chunkSecondsInput", "#workersInput", "#retryInput", "#repairInput",
    "#qwenAiBriefInput", "#qwenPromptInput", "#qwenHotwordsInput",
  ]) {
    $(selector).addEventListener("input", markSettingsDirty);
    $(selector).addEventListener("change", markSettingsDirty);
  }
  $("#qwenPromptInput").addEventListener("input", syncQwenEnhancementCounts);
  $("#qwenHotwordsInput").addEventListener("input", syncQwenEnhancementCounts);
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
  $("#creationTutorialButton").addEventListener("click", startSplitTutorial);
  $("#advancedTutorialButton").addEventListener("click", startAdvancedSplitTutorial);
  $("#quickStartTutorial").addEventListener("click", startSplitTutorial);
  $("#quickStartAdvancedTutorial").addEventListener("click", startAdvancedSplitTutorial);
  $("#qwenQuickTest").addEventListener("click", () => testQuickProvider("qwen"));
  $("#glmQuickTest").addEventListener("click", () => testQuickProvider("glm"));
  $("#copyRuntimeLog")?.addEventListener("click", () => copyText(state.runtimeLogText, "任务进度已复制"));
  document.querySelectorAll(".quick-reveal").forEach((button) => button.addEventListener("click", () => {
    const input = $(`#${button.dataset.reveal}`);
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    button.textContent = reveal ? "隐藏" : "显示";
  }));
  $("#skipSplitTutorial").addEventListener("click", () => exitSplitTutorial());
  $("#splitTutorialPrimary").addEventListener("click", () => {
    if (state.tutorial.kind === "advanced" && state.tutorial.step === 1) launchAdvancedTutorial();
    else if (state.tutorial.kind === "advanced" && state.tutorial.step === 3) {
      const projectId = state.tutorial.projectId;
      exitSplitTutorial({completed:true});
      window.location.href = `/editor?project=${encodeURIComponent(projectId)}&tutorial=advanced`;
    }
    else if (state.tutorial.step === 2) loadTutorialAsset("audio");
    else if (state.tutorial.step === 7) loadTutorialAsset("reference");
    else if (state.tutorial.step === 9) exitSplitTutorial({ completed: true });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (state.tutorial.active) {
      event.preventDefault();
      exitSplitTutorial();
    }
  });
  window.addEventListener("resize", positionSplitTutorial);
  window.addEventListener("scroll", positionSplitTutorial, true);
  window.addEventListener("focus", refreshSharedSettings);

  loadInitialState();
})();
