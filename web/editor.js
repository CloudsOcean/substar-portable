(function () {
  "use strict";

  const contract = window.EditorDocument;
  const languageLayout = window.SubstarLanguageLayout;
  const externalReview = window.SubstarExternalReview;
  const systemSaveAs = window.SubstarSystemSaveAs;
  const $ = selector => document.querySelector(selector);
  const CUE_PAGE_SIZE = 160;
  let reviewEnabled = false;
  let reviewData = {version:0, notes:[]};
  let reviewSelection = null;
  let syncTokenReviewForm = () => {};
  const state = {
    projects:[],
    projectId:"",
    taskInfo:null,
    llmOptions:null,
    failedTaskKind:"",
    revision:null,
    view:null,
    selectedTokenIds:new Set(),
    cueSelection:window.EditorCueSelection.createSelection(),
    selectionAnchorTokenId:null,
    activeCueId:null,
    playbackCueId:null,
    timelineSelectedCueId:null,
    followPlayback:false,
    subtitlePolicy:{
      englishHardLimit:55,
      chineseHardLimit:20,
      mixedHardLimit:25,
      japaneseHardLimit:24,
      koreanHardLimit:28,
      sourceLanguage:"Auto",
      sourceHardLimit:null,
      targetHardLimit:null,
      countSpaces:true,
      countPunctuation:true
    },
    dialogResolver:null,
    translationTask:null,
    translationPoll:null,
    editorAiTask:null,
    editorAiTaskPoll:null,
    timelineController:null,
    cueTimeController:null,
    cueListView:null,
    waveformCache:null,
    hardIssues:[],
    hardIssueIndex:-1,
    searchIndex:-1,
    searchScope:"source",
    searchReplaceUndo:null,
    aiChangeIndex:-1,
    referenceChangeIndex:-1,
    fontSize:18,
    shortcuts:{
      undo:"Ctrl+Z", redo:"Ctrl+Y", playPause:"Space", hideCue:"Backspace", zoomModifier:"Alt"
    },
    revisions:[],
    revisionHistoryLoaded:false,
    revisionHistoryLoading:false,
    undoRevisionIds:[],
    redoRevisionIds:[],
    historyNavigationPending:false,
    instructionResolver:null,
    operationPending:false,
    documentStore:null,
    operationQueue:null,
    operationQueueProjectId:"",
    topologyOperationPending:false,
    autoSnapUndo:null,
    autoMergeUndo:null,
    taskPanelDismissed:false,
    mediaLoadAttempts:0,
    mediaRetryTimer:null,
    mediaHadMetadata:false,
    mediaLoadPending:false,
    mediaLoadFailed:false,
    mediaKind:"video",
    mediaInfo:null,
    mediaElement:null,
    mediaBindingsCleanup:null,
    playbackFrameHandle:0,
    playbackLastUiAt:0,
    cueSplitView:"virtual",
    timelineLayout:"composite",
    referenceChangeByTokenId:new Map(),
    cuePageStart:0,
    indexes:null,
    trackLanguages:{source:"en", target:"zh"},
    navigationEntries:{hard:[], ai:[], reference:[]},
    translationResizeFrame:0,
    skipNextQueueIdleTimelineRedraw:false,
    authoritativeResyncPending:false,
    tutorial:{active:false, step:0, anchors:null, focus:null, flags:{}, positionTimer:0}
  };

  async function api(path, options = {}) {
    const requestEpoch = projectEpoch;
    const projectRequest = String(path).startsWith("/api/projects/");
    const response = await fetch(path, options);
    let body = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("json")) {
      try { body = await response.json(); } catch (_) { body = null; }
    }
    if (projectRequest && requestEpoch !== projectEpoch) {
      const error = new Error("项目会话已变化，忽略旧响应");
      error.status = 410;
      throw error;
    }
    if (!response.ok) {
      const detail = body?.detail || body || {};
      const reason = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map(item => item.msg || "请求参数无效").join("；") : detail.message;
      const error = new Error(reason || `请求失败 (${response.status})`);
      error.code = detail.code || "request_failed";
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
    return body;
  }

  function shortcutFromEvent(event) {
    const parts = [];
    if (event.ctrlKey) parts.push("Ctrl");
    if (event.altKey) parts.push("Alt");
    if (event.shiftKey) parts.push("Shift");
    if (event.metaKey) parts.push("Meta");
    const key = event.key.length === 1 ? event.key.toUpperCase() : event.key;
    parts.push(key === " " ? "Space" : key);
    return parts.join("+");
  }

  function applyEditorSettings(settings = {}) {
    window.SubstarTranslationLayout.configureModel(settings.qwen_cloud_model);
    state.shortcuts = {
      undo:String(settings.shortcut_undo || "Ctrl+Z"),
      redo:String(settings.shortcut_redo || "Ctrl+Y"),
      playPause:String(settings.shortcut_play_pause || "Space"),
      hideCue:String(settings.shortcut_hide_cue || "Backspace"),
      zoomModifier:String(settings.timeline_zoom_modifier || "Alt"),
    };
    $("#undoDocument").title = `撤销（${state.shortcuts.undo}）`;
    $("#redoDocument").title = `重做（${state.shortcuts.redo}）`;
    const preRollStored = localStorage.getItem("substar.editor.forward-snap-pre-roll-ms");
    const preRoll = Math.max(0, Math.min(100,
      preRollStored === null ? 0 : Number(preRollStored)
    ));
    const sensitivityStored = localStorage.getItem("substar.editor.forward-snap-sensitivity");
    const sensitivity = Math.max(0, Math.min(100,
      sensitivityStored === null ? 50 : Number(sensitivityStored)
    ));
    if ($("#forwardSnapPreRoll")) $("#forwardSnapPreRoll").value = String(preRoll);
    if ($("#forwardSnapSensitivity")) $("#forwardSnapSensitivity").value = String(sensitivity);
  }

  async function loadEditorSettings() {
    try {
      applyEditorSettings(await api("/api/settings"));
    } catch (_) {
      applyEditorSettings();
    }
  }

  function editorAiTaskLocksEditor() {
    return ["queued", "running", "cancelling"].includes(
      state.editorAiTask?.state
    );
  }

  async function refreshEditorAiTask() {
    if (!state.projectId) return null;
    const refreshEpoch = projectEpoch;
    const previousLocked = editorAiTaskLocksEditor();
    const previousTaskId = state.editorAiTask?.task_id || "";
    const nextTask = await api(projectPath("/ai-task")).catch(() => null);
    if (refreshEpoch !== projectEpoch) return null;
    state.editorAiTask = nextTask;
    const locked = editorAiTaskLocksEditor();
    document.body.classList.toggle("editor-ai-task-locked", locked);
    renderHeader();
    const genericTaskOwnsPanel = state.editorAiTask?.kind !== "translation";
    const terminalSuccess = ["succeeded", "succeeded_with_issues"].includes(
      state.editorAiTask?.state
    );
    if ((locked || terminalSuccess || state.editorAiTask?.state === "failed") && state.editorAiTask && genericTaskOwnsPanel) {
      const title = ({calibration:"AI 校准", translation:"字幕翻译"})[
        state.editorAiTask.kind
      ] || "AI 任务";
      const baseMessage = state.editorAiTask.display_error || state.editorAiTask.message
        || state.editorAiTask.error?.message || "任务运行中";
      const elapsedSeconds = Math.max(0, Number(state.editorAiTask.elapsed_seconds || 0));
      const taskMessage = locked && elapsedSeconds >= 1
        ? `${baseMessage} · 已等待 ${Math.round(elapsedSeconds)} 秒`
        : baseMessage;
      renderWorkbenchTask(
        title,
        Math.max(0, Math.min(100, Number(state.editorAiTask.progress || 0) * 100)),
        taskMessage,
        state.editorAiTask.state
      );
    }
    if (state.editorAiTask?.state === "cancelled" && genericTaskOwnsPanel) {
      const title = ({calibration:"AI 校准", translation:"字幕翻译"})[
        state.editorAiTask.kind
      ] || "AI 任务";
      renderWorkbenchTask(title, 0, "任务已取消", "cancelled");
    }
    if (
      previousLocked && !locked && previousTaskId
      && state.editorAiTask?.task_id === previousTaskId
      && ["succeeded", "succeeded_with_issues"].includes(state.editorAiTask?.state)
    ) {
      const revision = contract.consumeRevision(await api(projectPath()));
      setRevision(revision);
    }
    return state.editorAiTask;
  }

  function startEditorAiTaskPoll() {
    if (state.editorAiTaskPoll) clearInterval(state.editorAiTaskPoll);
    state.editorAiTaskPoll = window.setInterval(() => {
      refreshEditorAiTask().catch(error => ordinaryError(error.message));
    }, 800);
  }

  function ordinaryError(message = "", tone = "error") {
    const node = $("#ordinaryError");
    $("#ordinaryErrorMessage").textContent = message;
    node.classList.toggle("notice", Boolean(message) && ["notice", "completed", "success"].includes(tone));
    node.classList.toggle("hidden", !message);
  }

  function setMediaMessage(message = "") {
    const node = $("#mediaMessage");
    node.textContent = message;
    node.classList.toggle("hidden", !message);
  }

  function clearMediaRetry() {
    if (state.mediaRetryTimer) {
      clearTimeout(state.mediaRetryTimer);
      state.mediaRetryTimer = null;
    }
  }

  function activeMedia() {
    return state.mediaElement || $("#projectVideo");
  }

  function configureMedia(kind) {
    const normalized = kind === "audio" ? "audio" : "video";
    const next = normalized === "audio" ? $("#projectAudio") : $("#projectVideo");
    if (!next) return;
    if (state.mediaElement === next && state.mediaBindingsCleanup) {
      initializeTimelineController();
      return;
    }
    stopPlaybackUiLoop();
    state.mediaBindingsCleanup?.();
    const previous = state.mediaElement;
    previous?.pause?.();
    if (previous && previous !== next) {
      previous.removeAttribute("src");
      previous.load?.();
    }
    state.mediaKind = normalized;
    state.mediaElement = next;
    $("#mediaViewport")?.classList.toggle("audio-mode", normalized === "audio");
    $("#audioPreview")?.classList.toggle("hidden", normalized !== "audio");
    if ($("#mediaPreviewLabel")) {
      $("#mediaPreviewLabel").textContent = "预览";
    }
    if ($("#mediaControls")) {
      $("#mediaControls").ariaLabel = normalized === "audio" ? "音频播放控制" : "视频播放控制";
    }
    state.mediaBindingsCleanup = bindMediaEvents(next);
    initializeTimelineController();
  }

  function loadProjectMedia({retry = false} = {}) {
    const media = activeMedia();
    if (!media || !state.projectId) return;
    clearMediaRetry();
    state.mediaLoadPending = true;
    state.mediaLoadFailed = false;
    const base = projectPath("/media");
    // Cache-bust only recovery attempts. Normal project loads keep the stable
    // URL so the browser can reuse the local file and its range requests.
    media.src = retry ? `${base}?retry=${Date.now()}` : base;
    media.load();
  }

  function scheduleMediaRetry() {
    if (state.mediaLoadPending && !state.mediaLoadFailed) return;
    clearMediaRetry();
    const media = activeMedia();
    if (!media || state.mediaLoadAttempts >= 5) {
      setMediaMessage("媒体暂时无法加载，可刷新后重试");
      return;
    }
    state.mediaLoadAttempts += 1;
    setMediaMessage(`媒体加载失败，正在重试（${state.mediaLoadAttempts}/5）…`);
    state.mediaRetryTimer = setTimeout(() => loadProjectMedia({retry:true}),
      Math.min(8000, 500 * (2 ** (state.mediaLoadAttempts - 1))));
  }

  function mediaTimeLabel(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
  }

  function updateMediaViewport() {
    const stage = $("#mediaStage");
    const viewport = $("#mediaViewport");
    const media = activeMedia();
    if (!stage || !viewport || !media) return;
    if (state.mediaKind === "audio") {
      viewport.style.width = "100%";
      viewport.style.height = "100%";
      return;
    }
    if (!media.videoWidth || !media.videoHeight) return;
    const availableWidth = stage.clientWidth;
    const availableHeight = stage.clientHeight;
    const scale = Math.min(availableWidth / media.videoWidth, availableHeight / media.videoHeight);
    viewport.style.width = `${Math.max(1, media.videoWidth * scale)}px`;
    viewport.style.height = `${Math.max(1, media.videoHeight * scale)}px`;
  }

  let lastAudibleVolume = 1;

  function syncMediaControls() {
    const media = activeMedia();
    const duration = Number.isFinite(media?.duration) ? media.duration : 0;
    $("#mediaSeek").max = String(duration);
    $("#mediaSeek").value = String(Math.min(duration, Number(media?.currentTime) || 0));
    $("#mediaTime").textContent = `${mediaTimeLabel(media?.currentTime)} / ${mediaTimeLabel(duration)}`;
    $("#mediaPlayToggle").textContent = media?.paused === false ? "❚❚" : "▶";
    $("#mediaPlayToggle").ariaLabel = media?.paused === false ? "暂停" : "播放";
    const silent = !!media?.muted || media?.volume === 0;
    const volume = silent ? 0 : Math.round((media?.volume ?? 1) * 100);
    if (media?.volume > 0) lastAudibleVolume = media.volume;
    $("#mediaVolume").value = String(volume);
    $("#mediaVolume").title = `音量 ${volume}%`;
    if ($("#mediaVolumeValue")) $("#mediaVolumeValue").textContent = `${volume}%`;
    $("#mediaVolume").setAttribute("aria-valuetext", `${volume}%`);
    $("#mediaMute").textContent = silent ? "🔇" : "🔊";
    $("#mediaMute").setAttribute("aria-label", silent ? "取消静音" : "静音");
    $("#mediaMute").setAttribute("aria-pressed", String(silent));
    $("#mediaMute").title = silent ? "已静音，点击恢复声音" : "静音";
    const speed = Number(media?.playbackRate) || 1;
    $("#mediaSpeed").value = String(speed);
  }

  function syncPlaybackPosition(time) {
    const media = activeMedia();
    const duration = Number.isFinite(media?.duration) ? media.duration : 0;
    $("#mediaSeek").value = String(Math.min(duration, Number(time) || 0));
    $("#mediaTime").textContent = `${mediaTimeLabel(time)} / ${mediaTimeLabel(duration)}`;
    const playhead = $("#timelinePlayhead");
    if (playhead) playhead.style.left = `${Math.max(0, Math.min(100, time / timelineDuration() * 100))}%`;
  }

  function cueAtPlaybackTime(time) {
    return state.view?.cue_views.find(item =>
      item.state === "active" && time >= Number(item.start) && time < Number(item.end)
    ) || null;
  }

  function updatePlaybackUi(time, {playing = false, timestamp = 0} = {}) {
    const cue = cueAtPlaybackTime(time);
    state.playbackCueId = cue?.cue_id || null;
    if (state.followPlayback && state.playbackCueId !== state.activeCueId) activateCue(state.playbackCueId, {
      // Cue-list following is event driven: this branch only runs when the
      // playhead crosses a cue boundary while following is enabled.
      scroll:true,
      revealTimeline:false
    });
    if (!playing || !state.playbackLastUiAt || timestamp - state.playbackLastUiAt >= 50) {
      state.playbackLastUiAt = timestamp;
      syncPlaybackPosition(time);
    }
  }

  function stopPlaybackUiLoop() {
    if (state.playbackFrameHandle) {
      cancelAnimationFrame(state.playbackFrameHandle);
    }
    state.playbackFrameHandle = 0;
    state.playbackLastUiAt = 0;
  }

  function startPlaybackUiLoop() {
    stopPlaybackUiLoop();
    const media = activeMedia();
    const tick = timestamp => {
      state.playbackFrameHandle = 0;
      if (!media || media.paused || media.ended || media !== activeMedia()) return;
      updatePlaybackUi(Number(media.currentTime) || 0, {playing:true, timestamp});
      state.playbackFrameHandle = requestAnimationFrame(tick);
    };
    state.playbackFrameHandle = requestAnimationFrame(tick);
  }

  async function toggleMediaPlayback() {
    const media = activeMedia();
    if (!media) {
      ordinaryError("媒体尚未就绪，暂时无法播放");
      return;
    }
    if (!media.paused && !media.ended) {
      media.pause();
      return;
    }
    try {
      await media.play();
    } catch (error) {
      // Pausing or switching media cancels an outstanding play request normally.
      if (error?.name === "AbortError") return;
      ordinaryError(`播放失败：${error?.message || "浏览器拒绝了播放请求"}`);
    }
  }

  function isTextEditingContext(event) {
    if (event.isComposing) return true;
    const editableSelector = [
      "textarea",
      "[role='textbox']",
      "[contenteditable]:not([contenteditable='false'])",
      "input:not([type])",
      "input[type='text']",
      "input[type='search']",
      "input[type='number']",
      "input[type='email']",
      "input[type='url']",
      "input[type='password']"
    ].join(",");
    return event.composedPath().some(node =>
      node instanceof Element && node.matches(editableSelector)
    );
  }

  function syncPlaybackFollowAfterSeek() {
    const media = activeMedia();
    const continueFollowing = !!media && !media.paused && !media.ended;
    state.followPlayback = continueFollowing;
    state.timelineController?.setFollowPlayback?.(continueFollowing);
    return continueFollowing;
  }

  function suspendPlaybackFollow() {
    state.followPlayback = false;
    state.timelineController?.setFollowPlayback?.(false);
  }

  function bindMediaEvents(media) {
    const bindings = [];
    const on = (name, handler) => {
      media.addEventListener(name, handler);
      bindings.push([name, handler]);
    };
    const metadataReady = () => {
      clearMediaRetry();
      state.mediaLoadAttempts = 0;
      state.mediaHadMetadata = true;
      state.mediaLoadPending = false;
      setMediaMessage();
      updateMediaViewport();
      syncMediaControls();
      state.timelineController?.setView(state.view);
    };
    ["loadedmetadata", "loadeddata", "canplay"].forEach(name => on(name, metadataReady));
    on("error", event => {
      const current = event.currentTarget;
      state.mediaLoadPending = false;
      state.mediaLoadFailed = true;
      if (
        state.mediaHadMetadata
        || current.readyState >= HTMLMediaElement.HAVE_METADATA
        || Number.isFinite(current.duration)
      ) {
        setMediaMessage(`媒体播放中断：${current.error?.message || "媒体数据读取失败，请重新打开工程"}`);
        return;
      }
      scheduleMediaRetry();
    });
    on("waiting", () => setMediaMessage("正在缓冲媒体…"));
    on("playing", () => setMediaMessage());
    on("timeupdate", event => {
      updatePlaybackUi(event.currentTarget.currentTime, {
        playing:!event.currentTarget.paused,
        timestamp:performance.now()
      });
    });
    on("seeked", event => {
      const time = Number(event.currentTarget.currentTime) || 0;
      const cue = cueAtPlaybackTime(time);
      state.playbackCueId = cue?.cue_id || null;
      if (state.followPlayback) {
        if (state.playbackCueId !== state.activeCueId) {
          activateCue(state.playbackCueId, {scroll:true, revealTimeline:true});
        } else {
          centerCueInList(cue);
        }
        state.timelineController?.revealTime(time, false);
      }
      syncPlaybackPosition(time);
    });
    on("play", () => {
      state.followPlayback = true;
      state.timelineController?.setFollowPlayback?.(true);
      startPlaybackUiLoop();
    });
    on("pause", () => {
      suspendPlaybackFollow();
      stopPlaybackUiLoop();
      syncPlaybackPosition(Number(media.currentTime) || 0);
    });
    ["play", "pause", "ended", "durationchange", "volumechange"].forEach(name => {
      on(name, syncMediaControls);
    });
    on("click", toggleMediaPlayback);
    return () => bindings.forEach(([name, handler]) => media.removeEventListener(name, handler));
  }

  function projectPath(suffix = "") {
    return `/api/projects/${encodeURIComponent(state.projectId)}${suffix}`;
  }

  function nextExportSequence() {
    const key = `substar.editor.export-sequence:${state.projectId}`;
    const current = Math.max(0, Number.parseInt(localStorage.getItem(key) || "0", 10) || 0);
    return {key, value:current + 1};
  }

  function commitExportSequence(sequence) {
    localStorage.setItem(sequence.key, String(sequence.value));
  }

  function sourceTextForToken(tokenView) {
    const sourceById = state.indexes?.sourceById || new Map();
    return languageLayout.layoutTokens(
      tokenView.source_token_ids.map(id => sourceById.get(id)?.text || "").filter(Boolean),
      state.trackLanguages?.source
    );
  }

  function cueSourceText(cueView) {
    if (cueView.source_text != null) return cueView.source_text;
    const cached = state.indexes?.sourceTextByCueId?.get(cueView.cue_id);
    if (cached !== undefined) return cached;
    const tokens = state.indexes?.tokenById || new Map();
    return languageLayout.layoutTokens(
      cueView.active_display_token_ids.map(id => tokens.get(id)?.text || "").filter(Boolean),
      state.trackLanguages?.source
    );
  }

  function rebuildViewIndexes() {
    if (!state.view) {
      state.indexes = null;
      state.navigationEntries = {hard:[], ai:[], reference:[]};
      return;
    }
    const sourceById = new Map(state.view.source_tokens.map(token => [token.token_id, token]));
    const tokenById = new Map(state.view.token_views.map(token => [token.token_id, token]));
    const cueById = new Map(state.view.cue_views.map(cue => [cue.cue_id, cue]));
    const tokenToCueId = new Map();
    const sourceTextByCueId = new Map();
    const activeTokenOrder = [];
    state.view.cue_views.forEach(cue => {
      cue.display_token_ids.forEach(id => tokenToCueId.set(id, cue.cue_id));
      if (cue.state === "active") activeTokenOrder.push(...cue.active_display_token_ids);
      sourceTextByCueId.set(
        cue.cue_id,
        cue.source_text ?? languageLayout.layoutTokens(
          cue.active_display_token_ids.map(id => tokenById.get(id)?.text || "").filter(Boolean)
        )
      );
    });
    const activeTokenPosition = new Map(activeTokenOrder.map((id, index) => [id, index]));
    state.indexes = {
      sourceById, tokenById, cueById, tokenToCueId, sourceTextByCueId,
      activeTokenOrder, activeTokenPosition
    };
    state.trackLanguages = inferTrackLanguages();

    const latestTranslation = [...(state.view.changes || [])].reverse().find(
      change => change?.operation === "contextual_translation"
    );
    const latestCalibration = [...(state.view.changes || [])].reverse().find(
      change => change?.operation === "ai_calibration_apply"
    );
    const translationNotRequired = new Set((state.revision?.document?.cues || [])
      .filter(cue => cue.mapping?.translation_not_required).map(cue => cue.cue_id));
    const problemCueIds = new Set(state.view.cue_views.filter(cue => {
      const lines = projectedCueLines(cue);
      const sourceMetric = subtitleLengthMetric(lines.source, state.view?.presentation?.translation_layout?.enabled ? null : state.trackLanguages.source, "source");
      const targetMetric = subtitleLengthMetric(lines.target, cue.target?.language || state.trackLanguages.target, "target");
      return sourceMetric.count > sourceMetric.limit
        || (String(lines.target || "").trim() && targetMetric.count > targetMetric.limit)
        || (latestTranslation && !translationNotRequired.has(cue.cue_id) && !String(lines.target || "").trim());
    }).map(cue => cue.cue_id));
    (state.view.changes || []).forEach(change => {
      if (!["build_from_segmentation", "build_from_split_stages"].includes(change?.operation)) return;
      (change?.metadata?.segmentation?.review_cue_ids || []).forEach(
        cueId => problemCueIds.add(String(cueId))
      );
    });
    state.view.cue_views.filter(cue => cue.target && cue.target.translation_status !== "translated")
      .forEach(cue => problemCueIds.add(cue.cue_id));
    const calibrationMetadata = latestCalibration?.metadata || {};
    const calibrationFailedBlocks = calibrationMetadata.failed_blocks || [];
    const calibrationExecutionBlocks = calibrationMetadata.execution_blocks || [];
    const calibrationCompletelyFailed = calibrationExecutionBlocks.length > 0
      && calibrationFailedBlocks.length >= calibrationExecutionBlocks.length;
    if (!calibrationCompletelyFailed) {
      (calibrationMetadata.calibration_problem_cue_ids || []).forEach(
        cueId => problemCueIds.add(String(cueId))
      );
    }
    const hard = state.view.cue_views
      .filter(cue => cue.state === "active" && problemCueIds.has(cue.cue_id))
      .sort((left, right) => left.start - right.start || left.index - right.index)
      .map(cue => ({cue_id:cue.cue_id}));
    const ai = [];
    const reference = [];
    const referenceByTokenId = new Map();
    const referenceByChangeId = new Map();
    (state.view.changes || []).forEach(change => {
      const rows = change?.metadata?.reference_changes;
      if (!Array.isArray(rows)) return;
      rows.forEach(raw => {
        const item = {...raw, token_ids:[...(raw?.token_ids || [])].map(String)};
        if (!item.change_id || !item.token_ids.length) return;
        referenceByChangeId.set(String(item.change_id), item);
        item.token_ids.forEach(id => referenceByTokenId.set(id, item));
      });
    });
    state.view.token_views.forEach(token => {
      const cueId = tokenToCueId.get(token.token_id);
      if (!cueId) return;
      const entry = {token_id:token.token_id, cue_id:cueId};
      if (token.provenance?.kind === "ai") ai.push(entry);
      if (isReferenceToken(token) && !referenceByTokenId.has(token.token_id)) {
        referenceByTokenId.set(token.token_id, {
          change_id:`reference-token-${token.token_id}`,
          type:"replace", token_ids:[token.token_id], before:token.original_text,
          after:token.text, status:"applied"
        });
      }
    });
    referenceByChangeId.forEach(item => {
      const tokenId = item.token_ids.find(id => tokenToCueId.has(id));
      const cueId = tokenId ? tokenToCueId.get(tokenId) : null;
      if (tokenId && cueId) reference.push({token_id:tokenId, cue_id:cueId, change_id:item.change_id});
    });
    if (!reference.length) {
      referenceByTokenId.forEach((item, tokenId) => {
        const cueId = tokenToCueId.get(tokenId);
        if (cueId) reference.push({token_id:tokenId, cue_id:cueId, change_id:item.change_id});
      });
    }
    state.referenceChangeByTokenId = referenceByTokenId;
    state.navigationEntries = {hard, ai, reference};
  }

  function projectPunctuation(text, remove = "", space = "") {
    const removed = new Set([...String(remove || "")]);
    const spaced = new Set([...String(space || "")].filter(char => !removed.has(char)));
    return [...String(text || "")].map(char => removed.has(char) ? "" : spaced.has(char) ? " " : char)
      .join("").replace(/\s+/g, " ").trim();
  }

  function projectedCueLines(cue) {
    const presentation = state.view?.presentation || {};
    const sourceUpper = languageLayout.routedSourceUpper(cueSourceText(cue), presentation);
    const source = projectPunctuation(
      cueSourceText(cue),
      sourceUpper ? presentation.upper_remove : presentation.lower_remove,
      sourceUpper ? presentation.upper_space : presentation.lower_space
    );
    const target = projectPunctuation(
      languageLayout.formatText(cue.target?.target_text || "", cue.target?.language || state.trackLanguages?.target),
      sourceUpper ? presentation.lower_remove : presentation.upper_remove,
      sourceUpper ? presentation.lower_space : presentation.upper_space
    );
    return {source, target, sourceUpper};
  }

  function projectedSourceTokenText(text, cue = null) {
    const presentation = state.view?.presentation || {};
    const sourceUpper = languageLayout.routedSourceUpper(cue ? cueSourceText(cue) : text, presentation);
    return projectPunctuation(
      text,
      sourceUpper ? presentation.upper_remove : presentation.lower_remove,
      sourceUpper ? presentation.upper_space : presentation.lower_space
    );
  }

  function timeLabel(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    const minutes = Math.floor(value / 60);
    const remainder = (value % 60).toFixed(2).padStart(5, "0");
    return `${String(minutes).padStart(2, "0")}:${remainder}`;
  }

  function visibleRevisionSignature(revision) {
    if (!revision?.document) return "";
    const cues = contract.canonicalCueOrder(revision.document.cues).map(cue => ({
      cue_id:cue.cue_id,
      index:cue.index,
      state:cue.state,
      display_token_ids:[...cue.display_token_ids],
      start:Number(cue.start),
      end:Number(cue.end),
      target_text:String(cue.target?.target_text || ""),
      speaker:cue.speaker ?? null
    }));
    const displayTokens = revision.document.display_tokens.map(token => ({
      token_id:token.token_id,
      state:token.state,
      text:token.text
    }));
    return JSON.stringify({cues, display_tokens:displayTokens});
  }

  function adoptRevisionWithoutRender(payload) {
    const previousActiveCue = state.view?.cue_views.find(cue => cue.cue_id === state.activeCueId) || null;
    if (state.revision?.document?.document_id !== payload.document?.document_id) state.cueSelection.clear();
    state.revision = contract.consumeRevision(payload);
    state.view = contract.buildEditorView(state.revision);
    rebuildViewIndexes();
    assPanel?.sync();
    const activeCues = state.view.cue_views.filter(cue => cue.state === "active");
    state.activeCueId = reconciledActiveCueId(previousActiveCue, activeCues, state.view.cue_views);
  }

  function reconciledActiveCueId(previousCue, activeCues, allCues) {
    if (activeCues.some(cue => cue.cue_id === state.activeCueId)) return state.activeCueId;
    if (previousCue && activeCues.length) {
      const anchor = (Number(previousCue.start) + Number(previousCue.end)) / 2;
      const containing = activeCues.find(cue => anchor >= Number(cue.start) && anchor < Number(cue.end));
      if (containing) return containing.cue_id;
      return activeCues.reduce((nearest, cue) => {
        const cueAnchor = (Number(cue.start) + Number(cue.end)) / 2;
        const nearestAnchor = (Number(nearest.start) + Number(nearest.end)) / 2;
        return Math.abs(cueAnchor - anchor) < Math.abs(nearestAnchor - anchor) ? cue : nearest;
      }).cue_id;
    }
    return activeCues[0]?.cue_id || allCues[0]?.cue_id || null;
  }

  function setRevision(payload, {
    deferTimeline = false,
    localProjection = false,
    preserveCueViewport = true
  } = {}) {
    if (!localProjection && state.documentStore?.pending().length) {
      throw new Error("仍有本地编辑尚未保存，已保留输入；请先完成保存再同步版本");
    }
    const previousActiveCue = preserveCueViewport
      ? state.view?.cue_views.find(cue => cue.cue_id === state.activeCueId) || null
      : null;
    if (state.revision?.document?.document_id !== payload.document?.document_id) state.cueSelection.clear();
    state.revision = contract.consumeRevision(payload);
    if (!localProjection) {
      const factory = window.EditorDocumentStore;
      if (!factory?.createDocumentStore) throw new Error("DocumentStore 未加载，请刷新后重试");
      state.documentStore = factory.createDocumentStore();
      state.documentStore.reset(state.revision);
    }
    state.view = contract.buildEditorView(state.revision);
    state.selectedTokenIds.clear();
    state.hardIssues = [];
    state.hardIssueIndex = -1;
    const activeCues = state.view.cue_views.filter(cue => cue.state === "active");
    state.activeCueId = reconciledActiveCueId(previousActiveCue, activeCues, state.view.cue_views);
    const presentation = state.view.presentation || {};
    const presentationInputs = {
      upperPunctuationRemove:presentation.upper_remove || "",
      upperPunctuationSpace:presentation.upper_space || "",
      lowerPunctuationRemove:presentation.lower_remove || "",
      lowerPunctuationSpace:presentation.lower_space || ""
    };
    Object.entries(presentationInputs).forEach(([id, value]) => {
      const input = document.getElementById(id);
      if (input) input.value = value;
    });
    state.cueSelection.reconcile(activeCues.map(cue => cue.cue_id));
    assPanel?.sync();
    rebuildViewIndexes();
    try {
      render({deferTimeline, preserveCueViewport});
    } catch (error) {
      // A structural edit may shrink or reorder the Cue collection while a
      // virtualized list still holds the previous viewport window.  Retry a
      // clean render before allowing a successfully committed edit to fail.
      render({deferTimeline:false, preserveCueViewport});
      console.warn("Editor view recovered with a clean render", error);
    }
  }

  function scheduleAuthoritativeResync(message = "编辑已保存，正在同步最新界面") {
    if (state.authoritativeResyncPending || !state.projectId) return;
    const projectId = state.projectId;
    state.authoritativeResyncPending = true;
    ordinaryError(message);
    window.setTimeout(async () => {
      try {
        if (state.projectId === projectId) {
          await loadProject(projectId, {restoreTranslation:false});
        }
      } finally {
        state.authoritativeResyncPending = false;
      }
    }, 0);
  }

  function renderProjectList() {
    const list = $("#projectList");
    list.replaceChildren();
    if (!state.projects.length) {
      const empty = document.createElement("span");
      empty.className = "project-list-empty";
      empty.textContent = "没有可编辑项目";
      list.append(empty);
      return;
    }
    state.projects
      .slice()
      .sort((left, right) => Number(right.updated_at || 0) - Number(left.updated_at || 0))
      .forEach(project => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.projectId = project.project_id;
        button.classList.toggle("complete", project.complete === true);
        button.classList.toggle("tutorial", Boolean(project.tutorial_case_id));
        button.setAttribute("role", "option");
        button.setAttribute("aria-selected", String(project.project_id === state.projectId));
        button.setAttribute("aria-label", `${window.SubstarProjectLabel(project.display_name || project.project_id)}${project.tutorial_case_id ? "，教程案例" : project.complete ? "，完成稿" : ""}`);
        button.textContent = window.SubstarProjectLabel(project.display_name || project.project_id);
        button.title = `${window.SubstarProjectLabel(project.display_name || project.project_id)}${project.complete ? " · 完成稿" : ""}`;
        list.append(button);
      });
  }

  function currentProjectBaseName() {
    const project = state.projects.find(item => item.project_id === state.projectId);
    return window.SubstarProjectLabel(project?.display_name || project?.project_id || state.projectId || "当前项目");
  }

  function checkpointNumber(item) {
    const stored = Number(item.provenance?.metadata?.checkpoint_number || 0);
    if (stored > 0) return stored;
    const chronological = state.revisions
      .filter(revision => revision.provenance?.operation === "checkpoint")
      .slice()
      .sort((left, right) => left.revision_number - right.revision_number);
    const index = chronological.findIndex(revision => revision.revision_id === item.revision_id);
    return index >= 0 ? index + 1 : Math.max(1, item.revision_number);
  }

  function revisionTitle(item) {
    const provenance = item.provenance || {};
    const baseName = currentProjectBaseName();
    if (provenance.operation === "checkpoint") return `${baseName} · ${provenance.metadata?.label || `第${checkpointNumber(item)}稿`}`;
    if (item.is_latest && item.complete) return `${baseName} · 完成稿`;
    if (item.is_latest) return `${baseName} · 当前编辑`;
    const operation = provenance.metadata?.user_action || provenance.operation;
    const names = {merge:"合并", split_token:"拆分词元", replace:"修改文字", batch_replace:"批量修改文字",
      set_timeline:"修改标记与剪辑方案", set_cue_times:"调整时间", set_cue_time:"调整时间", split_cue:"字幕换行", merge_cues:"合并字幕", auto_merge_cues:"拼接 / 合并字幕",
      set_target:"修改译文", selective_restore:"选择性恢复", delete:"删除", restore_revision:"恢复版本"};
    return `${names[operation] || operation || "编辑"} · r${item.revision_number}`;
  }

  function renderRevisionMenu() {
    const menu = $("#revisionMenu");
    if (!menu) return;
    const checkpointsOnly = document.documentElement.dataset.editorLayout === "experimental";
    const visible = state.revisions.filter((item, index) => checkpointsOnly
      ? item.provenance?.operation === "checkpoint"
      : index < 30 || item.is_latest || item.provenance?.operation === "checkpoint"
    );
    if (!visible.length) {
      menu.innerHTML = '<span class="muted">暂无人工暂存</span>';
      return;
    }
    const fragment = document.createDocumentFragment();
    visible.forEach(item => {
      const row = document.createElement("div");
      row.className = "revision-menu-row";
      const label = document.createElement("span");
      label.textContent = revisionTitle(item);
      const restore = document.createElement("button");
      restore.type = "button";
      restore.dataset.restoreRevision = item.revision_id;
      restore.disabled = item.is_latest;
      restore.textContent = item.is_latest ? "当前" : "恢复整版";
      row.append(label, restore);
      if (!item.is_latest && !checkpointsOnly) {
        const scope = document.createElement("select");
        scope.setAttribute("aria-label", "选择性恢复范围");
        [["text", "仅文字"], ["timing", "仅时间"], ["translation", "仅译文"], ["style", "仅字幕样式"]].forEach(([value, text]) => {
          scope.add(new Option(text, value));
        });
        const selected = document.createElement("button");
        selected.type = "button";
        selected.textContent = "恢复所选字幕";
        selected.onclick = () => restoreSelectedFields(item.revision_id, scope.value);
        row.append(scope, selected);
      }
      fragment.append(row);
    });
    menu.replaceChildren(fragment);
  }

  function revisionMetadata(revision, provenance = null) {
    return {
      revision_id:revision.revision_id,
      revision_number:revision.revision_number,
      parent_revision_id:revision.parent_revision_id ?? null,
      created_at:revision.created_at,
      complete:state.view?.properties?.complete === true,
      is_latest:true,
      provenance:provenance || revision.provenance || {kind:"manual", operation:"editor_commit", metadata:{}}
    };
  }

  function syncRevisionControls() {
    $("#undoDocument").disabled = editorAiTaskLocksEditor() || state.historyNavigationPending || !state.undoRevisionIds.length;
    $("#redoDocument").disabled = editorAiTaskLocksEditor() || state.historyNavigationPending || !state.redoRevisionIds.length;
    const versionsToggle = $("#checkpointVersionsToggle");
    if (versionsToggle) {
      const disabled = !state.projectId || editorAiTaskLocksEditor() || state.historyNavigationPending;
      versionsToggle.setAttribute("aria-disabled", String(disabled));
      $("#versionMenu").classList.toggle("disabled", disabled);
      if (disabled) $("#versionMenu").open = false;
    }
    $("#resetDocument").disabled = !state.revisions.some(item =>
      item.provenance?.operation === "checkpoint" && !item.is_latest
    );
  }

  function logicalUndoStack(revisionId, metadataById, seen = new Set()) {
    const tail = [];
    while (revisionId && !seen.has(revisionId) && tail.length < 1000) {
      seen.add(revisionId);
      const revision = metadataById.get(revisionId);
      if (!revision) break;
      if (revision.provenance?.operation === "restore_revision") {
        const ids = revision.provenance.metadata?.undo_revision_ids;
        return [...(Array.isArray(ids) ? ids.map(String) : []), ...tail.reverse()].slice(-1000);
      }
      const parentId = revision.parent_revision_id;
      if (!parentId) break;
      if (revision.provenance?.operation !== "checkpoint") tail.push(parentId);
      revisionId = parentId;
    }
    return tail.reverse();
  }

  function logicalRedoStack(revisionId, metadataById, seen = new Set()) {
    while (revisionId && !seen.has(revisionId)) {
      seen.add(revisionId);
      const revision = metadataById.get(revisionId);
      if (revision?.provenance?.operation !== "checkpoint") {
        const ids = revision?.provenance?.operation === "restore_revision"
          ? revision.provenance.metadata?.redo_revision_ids : null;
        return Array.isArray(ids) ? ids.map(String).slice(-1000) : [];
      }
      revisionId = revision.parent_revision_id;
    }
    return [];
  }

  function seedRevisionMetadata(revision) {
    state.revisions = [revisionMetadata(revision)];
    const navigation = revision.provenance?.metadata || {};
    if (revision.provenance?.operation === "restore_revision") {
      state.undoRevisionIds = Array.isArray(navigation.undo_revision_ids)
        ? navigation.undo_revision_ids.map(String) : [];
      state.redoRevisionIds = Array.isArray(navigation.redo_revision_ids)
        ? navigation.redo_revision_ids.map(String) : [];
    } else {
      state.undoRevisionIds = revision.parent_revision_id ? [revision.parent_revision_id] : [];
      state.redoRevisionIds = [];
    }
    state.historyNavigationPending = false;
    state.revisionHistoryLoaded = false;
    state.revisionHistoryLoading = false;
    state.revisionHistoryPromise = null;
    renderRevisionMenu();
    syncRevisionControls();
  }

  function recordCommittedRevision(revision, provenance = null) {
    const previousRevisionId = state.revisions.find(item => item.is_latest)?.revision_id || null;
    const current = revisionMetadata(revision, provenance);
    if (state.searchReplaceUndo && state.searchReplaceUndo.after !== current.revision_id) {
      state.searchReplaceUndo = null;
    }
    state.revisions = [
      current,
      ...state.revisions
        .filter(item => item.revision_id !== current.revision_id)
        .map(item => ({...item, is_latest:false}))
    ];
    if (current.provenance?.operation === "restore_revision") {
      const navigation = current.provenance.metadata || {};
      state.undoRevisionIds = (navigation.undo_revision_ids || []).map(String).slice(-1000);
      state.redoRevisionIds = (navigation.redo_revision_ids || []).map(String).slice(-1000);
    } else if (previousRevisionId !== current.revision_id && current.provenance?.operation !== "checkpoint") {
      if (previousRevisionId && previousRevisionId !== current.revision_id) {
        state.undoRevisionIds.push(previousRevisionId);
      } else if (!state.undoRevisionIds.length && current.parent_revision_id) {
        state.undoRevisionIds.push(current.parent_revision_id);
      }
      state.redoRevisionIds = [];
      state.undoRevisionIds = state.undoRevisionIds.slice(-1000);
    }
    renderRevisionMenu();
    syncRevisionControls();
  }

  async function loadRevisionHistory({force = false} = {}) {
    if (!state.projectId) return false;
    if (state.revisionHistoryLoading) {
      const projectId = state.projectId, revisionId = state.revision?.revision_id;
      const loaded = await state.revisionHistoryPromise;
      if (state.projectId !== projectId) return false;
      if (loaded && state.revisionHistoryLoaded) return true;
      if (revisionId !== state.revision?.revision_id) return loadRevisionHistory({force:true});
      return false;
    }
    if (!force && state.revisionHistoryLoaded) return true;
    const projectId = state.projectId;
    const revisionId = state.revision?.revision_id;
    state.revisionHistoryLoading = true;
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    state.revisionHistoryPromise = pending;
    let loaded = false;
    try {
      const result = await api(projectPath("/revisions"));
      if (state.projectId !== projectId || state.revisionHistoryPromise !== pending) return false;
      if (state.revision?.revision_id !== revisionId) {
        state.revisionHistoryLoaded = false;
        return false;
      }
      state.revisions = result.revisions || [];
      state.revisionHistoryLoaded = true;
      if (state.revision) {
        const metadataById = new Map(state.revisions.map(item => [item.revision_id, item]));
        const current = metadataById.get(state.revision.revision_id);
        const navigation = current?.provenance?.metadata || {};
        if (current?.provenance?.operation === "restore_revision") {
          state.undoRevisionIds = Array.isArray(navigation.undo_revision_ids)
            ? navigation.undo_revision_ids.map(String) : [];
          state.redoRevisionIds = Array.isArray(navigation.redo_revision_ids)
            ? navigation.redo_revision_ids.map(String) : [];
        } else {
          state.undoRevisionIds = logicalUndoStack(state.revision.revision_id, metadataById);
          state.redoRevisionIds = logicalRedoStack(state.revision.revision_id, metadataById);
        }
      }
      renderRevisionMenu();
      syncRevisionControls();
      loaded = true;
      return true;
    } catch (error) {
      if (state.projectId !== projectId || state.revisionHistoryPromise !== pending) return false;
      state.revisionHistoryLoaded = false;
      ordinaryError(`版本历史读取失败：${error.message}`);
      return false;
    } finally {
      if (state.revisionHistoryPromise === pending) {
        state.revisionHistoryLoading = false;
        state.revisionHistoryPromise = null;
      }
      finish(loaded);
    }
  }

  async function flushActiveEditorInput() {
    const active = document.activeElement;
    if (active?.closest?.("#cueList") && active.matches?.("input,textarea,[contenteditable]")) active.blur();
    // Whole-line edits prepare a structural operation asynchronously on blur.
    // Its queue entry may not exist yet, so draining the queue alone is insufficient.
    if (active?.editorCommit && await active.editorCommit === null) {
      throw new Error("当前输入未能保存，请检查编辑内容");
    }
    await Promise.resolve();
    return await ensureOperationQueue().flushAndWait();
  }

  async function withSavedDocument(write) {
    if (state.documentWritePending || state.checkpointPending || state.restorePreparationPending
      || state.historyNavigationPending) throw new Error("正在保存或恢复文档，请稍候重试");
    state.documentWritePending = true;
    const workbench = $("#editorWorkbench"), wasInert = workbench.inert;
    try {
      const saving = flushActiveEditorInput();
      workbench.inert = true;
      await saving;
      state.documentWriteLocked = true;
      return await write();
    } finally {
      state.documentWriteLocked = false;
      state.documentWritePending = false;
      workbench.inert = wasInert;
    }
  }

  function hasUncommittedEditorInput() {
    return [...document.querySelectorAll("#cueList input,#cueList textarea")].some(field => {
      if (field.dataset.sourceEdit) return field.value !== field.dataset.originalSource;
      if (field.dataset.targetEdit) return field.dataset.rawEditing === "true"
        && field.value.trim() !== (field.dataset.originalTarget || "");
      return field.dataset.originalEdit !== undefined && field.value.trim() !== field.dataset.originalEdit;
    });
  }

  window.addEventListener("beforeunload", event => {
    if (!hasUncommittedEditorInput() && !state.operationQueue?.getState().hasUnsavedChanges
      && !state.checkpointPending && !state.restorePreparationPending && !state.documentWritePending) return;
    event.preventDefault();
    event.returnValue = "";
  });

  let exitingEditor = false;
  async function exitEditor(event) {
    // Keep modified-link behavior (new tab/window) unchanged.
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) return;
    event.preventDefault();
    if (exitingEditor) return;
    if (state.documentWritePending || state.checkpointPending || state.restorePreparationPending) {
      ordinaryError("文档正在保存或恢复，请完成后再退出");
      return;
    }
    exitingEditor = true;
    const link = event.currentTarget;
    let exitTimer;
    link.textContent = "保存后退出…";
    try {
      if (state.revision) await Promise.race([
        flushActiveEditorInput(),
        new Promise((_, reject) => {
          exitTimer = setTimeout(() => reject(new Error("保存响应较慢，当前页面仍可继续编辑")), 10000);
        })
      ]);
      activeMedia()?.pause();
      window.location.assign(link.href);
    } catch (error) {
      ordinaryError(`退出前保存未完成：${error.message}。编辑已保留，请重试保存。`);
    } finally {
      clearTimeout(exitTimer);
      exitingEditor = false;
      link.textContent = "退出";
    }
  }

  let idleRevisionCheck = false;
  async function syncIdleRevision() {
    if (idleRevisionCheck || !state.revision || state.operationPending || state.historyNavigationPending
      || state.structuralPreparationPending || state.restorePreparationPending || state.documentWritePending || editorAiTaskLocksEditor()
      || document.activeElement?.closest?.("#cueList input,#cueList textarea,[contenteditable=true]")) return;
    idleRevisionCheck = true;
    const project = state.projectId, revision = state.revision.revision_id;
    try {
      const latest = contract.consumeRevision(await api(projectPath()));
      if (project !== state.projectId || revision !== state.revision?.revision_id || state.operationPending
        || state.historyNavigationPending || state.structuralPreparationPending || state.restorePreparationPending || state.documentWritePending
        || document.activeElement?.closest?.("#cueList input,#cueList textarea,[contenteditable=true]")) return;
      if (latest.revision_id !== revision) { setRevision(latest); await loadRevisionHistory({force:true}); }
    } catch (error) { console.warn("Idle revision check failed", error); }
    finally { idleRevisionCheck = false; }
  }
  window.addEventListener("focus", syncIdleRevision);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) syncIdleRevision(); });

  async function restoreRevision(revisionId, {
    navigation = "direct", undoRevisionIds = null, redoRevisionIds = null
  } = {}) {
    if (!state.revision || !revisionId || state.restorePreparationPending || state.checkpointPending || state.documentWritePending) return;
    state.restorePreparationPending = true;
    const nextUndoRevisionIds = Array.isArray(undoRevisionIds)
      ? undoRevisionIds.slice(-1000)
      : [...state.undoRevisionIds, state.revision.revision_id].slice(-1000);
    const nextRedoRevisionIds = Array.isArray(redoRevisionIds)
      ? redoRevisionIds.slice(-1000) : [];
    try {
      const before = state.revision.revision_id;
      await flushActiveEditorInput();
      state.restoreWriting = true;
      $("#editorWorkbench").inert = true;
      const latest = contract.consumeRevision(await api(projectPath()));
      if (before !== state.revision.revision_id || latest.revision_id !== state.revision.revision_id) {
        setRevision(latest);
        await loadRevisionHistory({force:true});
        ordinaryError("当前编辑已同步，版本历史已更新；请重新选择撤销或恢复目标");
        return null;
      }
      const revision = await api(projectPath("/restore"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          revision_id:revisionId,
          navigation,
          undo_revision_ids:nextUndoRevisionIds,
          redo_revision_ids:nextRedoRevisionIds
        })
      });
      setRevision(revision);
      recordCommittedRevision(state.revision, {
        kind:"manual", operation:"restore_revision", metadata:{
          restored_revision_id:revisionId,
          navigation,
          undo_revision_ids:nextUndoRevisionIds,
          redo_revision_ids:nextRedoRevisionIds
        }
      });
      observeEditorTutorialEvent("revision_restore", {navigation});
      state.undoRevisionIds = nextUndoRevisionIds;
      state.redoRevisionIds = nextRedoRevisionIds;
      syncRevisionControls();
      return revision;
    } catch (error) {
      if (error.status === 409 && !state.operationQueue?.getState().hasUnsavedChanges) {
        try { setRevision(await api(projectPath())); await loadRevisionHistory({force:true}); }
        catch (_) { /* Preserve the original restore failure. */ }
      }
      ordinaryError(`版本恢复失败：${error.message}。输入保留，请核对最新版本历史后重试`);
      return null;
    } finally {
      state.restoreWriting = false;
      state.restorePreparationPending = false;
      $("#editorWorkbench").inert = false;
    }
  }

  function setCheckpointNaming(open) {
    $("#checkpointNaming").hidden = !open;
    $("#confirmCheckpoint").hidden = !open;
    $("#saveCheckpoint").textContent = open ? "取消" : "暂存";
    $("#saveCheckpoint").setAttribute("aria-expanded", String(open));
    if (open) {
      $("#checkpointName").value = "";
      $("#checkpointName").focus();
    }
  }

  async function createCheckpoint() {
    if (!state.revision || state.checkpointPending || state.restorePreparationPending || state.documentWritePending
      || state.historyNavigationPending || editorAiTaskLocksEditor()) return;
    const label = $("#checkpointName").value.trim();
    if (!label) { $("#checkpointName").focus(); return; }
    state.checkpointPending = true;
    const workbench = $("#editorWorkbench");
    const wasInert = workbench.inert;
    try {
      const saving = flushActiveEditorInput();
      workbench.inert = true;
      await saving;
      if (await loadRevisionHistory() === false) throw new Error("版本历史未能读取，请重试暂存");
      const count = state.revisions.filter(item => item.provenance?.operation === "checkpoint").length;
      const checkpointNumber = count + 1;
      const revision = await api(projectPath("/checkpoints"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          label
        })
      });
      setRevision(revision);
      recordCommittedRevision(state.revision, {
        kind:"manual", operation:"checkpoint", metadata:{label, checkpoint_number:checkpointNumber}
      });
      $("#draftState").textContent = `已暂存：${currentProjectBaseName()} · ${label}`;
      setCheckpointNaming(false);
    } catch (error) {
      ordinaryError(`暂存失败：${error.message}`);
    } finally {
      state.checkpointPending = false;
      workbench.inert = wasInert;
    }
  }

  async function undoLatestRevision() {
    if (editorAiTaskLocksEditor()) return;
    if (state.historyNavigationPending || state.checkpointPending || state.documentWritePending || !state.revision) return;
    state.historyNavigationPending = true;
    syncRevisionControls();
    try {
      await flushActiveEditorInput();
      if (await loadRevisionHistory() === false) return;
      if (!state.undoRevisionIds.length) return;
      const target = state.undoRevisionIds[state.undoRevisionIds.length - 1];
      const undoRevisionIds = state.undoRevisionIds.slice(0, -1);
      const redoRevisionIds = [...state.redoRevisionIds, state.revision.revision_id];
      await restoreRevision(target, {navigation:"undo", undoRevisionIds, redoRevisionIds});
    } catch (error) {
      ordinaryError(`撤销前保存未完成：${error.message}`);
    } finally {
      state.historyNavigationPending = false;
      syncRevisionControls();
    }
  }

  async function redoLatestRevision() {
    if (editorAiTaskLocksEditor()) return;
    if (state.historyNavigationPending || state.checkpointPending || state.documentWritePending || !state.revision) return;
    state.historyNavigationPending = true;
    syncRevisionControls();
    try {
      await flushActiveEditorInput();
      if (await loadRevisionHistory() === false) return;
      if (!state.redoRevisionIds.length) return;
      const target = state.redoRevisionIds[state.redoRevisionIds.length - 1];
      const redoRevisionIds = state.redoRevisionIds.slice(0, -1);
      const undoRevisionIds = [...state.undoRevisionIds, state.revision.revision_id];
      await restoreRevision(target, {navigation:"redo", undoRevisionIds, redoRevisionIds});
    } catch (error) {
      ordinaryError(`重做前保存未完成：${error.message}`);
    } finally {
      state.historyNavigationPending = false;
      syncRevisionControls();
    }
  }

  async function resetToCheckpoint() {
    const checkpoint = state.revisions.find(item =>
      item.provenance?.operation === "checkpoint" && !item.is_latest
    );
    if (!checkpoint) return;
    if (!window.confirm("重置会放弃当前暂存点之后的修改，确定继续吗？")) return;
    await restoreRevision(checkpoint.revision_id);
  }

  async function openTaskInfoMenu() {
    if (!state.projectId) return;
    const projectId = state.projectId;
    let info = state.taskInfo;
    try {
      info = await api(projectPath("/task-info"));
    } catch (error) {
      ordinaryError(`任务信息读取失败：${error.message}`);
      if (state.projectId === projectId) closeTaskInfoMenu();
      return;
    }
    if (!$("#taskInfoMenu").open || state.projectId !== projectId) return;
    state.taskInfo = info;
    await loadProjectLlmOptions().catch(error => ordinaryError(`模型列表读取失败：${error.message}`));
    $("#taskInfoName").value = window.SubstarProjectLabel(info.display_name || projectId);
    $("#taskInfoMediaPath").value = info.media_path || "";
    $("#taskInfoMediaPath").title = info.media_path || "";
    $("#taskInfoMediaPath").dataset.selection = "";
    $("#taskMediaStatus").hidden = !info.media_missing;
    $("#taskMediaStatus").textContent = info.media_missing ? "文件不存在，请重新链接" : "";
    $("#taskInfoSourceLanguage").value = info.language === "zh-CN" ? "zh" : info.language;
    $("#taskInfoTargetLanguage").value = info.target_language_mode;
    window.SubstarTranslationLayout.write("taskInfoLanguageLayout", info.translation_layout || {});
    $("#taskInfoSourceLimit").value = String(info.source_hard_limit);
    $("#taskInfoTargetLimit").value = String(info.target_hard_limit);
    $("#taskInfoAutosaveDelay").value = String(info.autosave_delay_seconds ?? 1);
    $("#taskInfoAutosaveRetries").value = String(info.autosave_retry_count ?? 3);
    renderProjectModelSelect();
    requestAnimationFrame(() => {
      $("#taskInfoName").focus();
      $("#taskInfoName").select();
    });
  }

  function closeTaskInfoMenu() {
    $("#taskInfoMenu").open = false;
  }

  async function submitTaskInfo(event) {
    event.preventDefault();
    if (!state.projectId) return closeTaskInfoMenu();
    const current = state.projects.find(item => item.project_id === state.projectId);
    const displayName = $("#taskInfoName").value.trim();
    const sourceLimit = Number($("#taskInfoSourceLimit").value);
    const targetLimit = Number($("#taskInfoTargetLimit").value);
    if (!displayName) return ordinaryError("任务名称不能为空");
    if (!Number.isInteger(sourceLimit) || sourceLimit < 1 || sourceLimit > 500
      || !Number.isInteger(targetLimit) || targetLimit < 1 || targetLimit > 500) {
      return ordinaryError("原文行长和译文行长必须是 1–500 的整数");
    }
    try {
      const updated = await api(projectPath("/task-info"), {
        method:"PUT", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          display_name:displayName,
          media_selection_token:$("#taskInfoMediaPath").dataset.selection || "",
          language:$("#taskInfoSourceLanguage").value,
          target_language_mode:$("#taskInfoTargetLanguage").value,
          translation_layout:window.SubstarTranslationLayout.read("taskInfoLanguageLayout"),
          glossary_id:String(state.taskInfo?.glossary_id || ""),
          llm_provider_id:$("#taskInfoLlmProvider").value || "inherit",
          source_hard_limit:sourceLimit,
          target_hard_limit:targetLimit,
          autosave_delay_seconds:Number($("#taskInfoAutosaveDelay").value),
          autosave_retry_count:Number($("#taskInfoAutosaveRetries").value)
        })
      });
      state.taskInfo = updated;
      state.operationQueue?.configure({
        debounceMs:(updated.autosave_delay_seconds ?? 1) * 1000,
        maxRetries:updated.autosave_retry_count ?? 3
      });
      if ($("#taskInfoMediaPath").dataset.selection) loadProjectMedia();
      applySubtitlePolicy(updated);
      configureTranslationLanguageDefaults(updated);
      if (current) current.display_name = updated.display_name || displayName;
      rebuildViewIndexes();
      render();
      renderProjectList();
      renderRevisionMenu();
      renderHeader();
      closeTaskInfoMenu();
      ordinaryError("");
    } catch (error) {
      ordinaryError(`任务信息保存失败：${error.message}`);
    }
  }

  async function undoAutoSnap() {
    if (!state.autoSnapUndo || state.revision?.revision_id !== state.autoSnapUndo.after) return;
    const target = state.autoSnapUndo.before;
    $("#undoAutoSnap").disabled = true;
    const restored = await restoreRevision(target);
    if (restored !== null) state.autoSnapUndo = null;
  }

  async function applyPresentationSettings() {
    if (!state.revision) return;
    try {
      const revision = await withSavedDocument(() => api(projectPath("/presentation"), {
        method:"PUT",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          operation_id:`op_presentation_${Date.now().toString(36)}`,
          upper_remove:$("#upperPunctuationRemove").value,
          upper_space:$("#upperPunctuationSpace").value,
          lower_remove:$("#lowerPunctuationRemove").value,
          lower_space:$("#lowerPunctuationSpace").value
        })
      }));
      setRevision(revision);
      recordCommittedRevision(state.revision, {
        kind:"manual", operation:"set_presentation", metadata:{}
      });
      observeEditorTutorialEvent("punctuation_apply", {
        upperRemove:$("#upperPunctuationRemove").value
      });
    } catch (error) {
      ordinaryError(`标点设置保存失败：${error.message}`);
    }
  }

  function renderHeader() {
    const complete = state.view?.properties.complete === true;
    const revision = state.revision;
    const locked = editorAiTaskLocksEditor();
    const tutorial = Boolean(state.projects.find(item => item.project_id === state.projectId)?.tutorial_case_id);
    $("#revisionLabel").textContent = revision
      ? `${currentProjectBaseName()} · ${complete ? "完成稿" : "当前编辑"}` : "尚未打开文档";
    $("#toggleComplete").textContent = complete ? "取消完成标记" : "标记完成";
    $("#toggleComplete").disabled = !revision || locked || tutorial;
    $("#toggleComplete").classList.toggle("hidden", tutorial);
    $("#toggleComplete").classList.toggle("complete", complete);
    $("#toggleComplete").title = complete ? "完成只是属性；取消后仍是同一可编辑项目" : "标记后仍可继续编辑";
    $("#exportMenu").classList.toggle("disabled", !revision);
    $("#exportDocument").setAttribute("aria-disabled", String(!revision));
    const textProject = state.view?.cue_views.some(cue => cue.source_text != null && cue.subtitle_track !== "overlay");
    $("#translateDocument").disabled = !revision || locked || textProject
      || ["queued", "running", "cancelling"].includes(state.translationTask?.state);
    const editingCommandDisabled = !revision || locked || textProject;
    $("#applyAutoMerge").disabled = !revision || locked || state.topologyOperationPending;
    [
      ["#translationMenu", "#translationMenuSummary"],
      ["#aiCalibrationMenu", "#aiCalibrate"],
      ["#scriptProjectionMenu", "#scriptProjectionSummary"],
    ].forEach(([menuSelector, summarySelector]) => {
      const menu = $(menuSelector);
      menu?.classList.toggle("disabled", editingCommandDisabled);
      $(summarySelector)?.setAttribute("aria-disabled", String(editingCommandDisabled));
      if (editingCommandDisabled && menu?.open) menu.open = false;
    });
    ["#saveCheckpoint", "#toolSearch", "#toolReplace",
      "#toolSearchNext", "#toolReplaceCurrent", "#toolReplaceAll", "#toolUndoReplace",
      "#applyAutoSnap", "#upperPunctuationRemove", "#upperPunctuationSpace",
      "#lowerPunctuationRemove", "#lowerPunctuationSpace", "#applyPunctuation",
      "#runAiCalibration", "#convertOriginal", "#convertSimplified", "#convertTraditional", "#convertTaiwan", "#convertHongKong", "#detectSpeakers"
    ].forEach(selector => { const node = $(selector); if (node) node.disabled = !revision || locked || textProject; });
    ["#toolReplaceCurrent", "#toolReplaceAll"].forEach(selector => {
      const node = $(selector);
      if (node) node.disabled = node.disabled || state.operationPending;
    });
    $("#toolUndoReplace").disabled = !state.searchReplaceUndo
      || state.searchReplaceUndo.projectId !== state.projectId
      || state.searchReplaceUndo.after !== revision?.revision_id
      || state.operationPending || locked;
    $("#autoSnapMenu")?.classList.toggle("disabled", !revision || locked);
    $("#autoSnapMenuSummary")?.setAttribute("aria-disabled", String(!revision || locked));
    $("#undoAutoSnap").disabled = !state.autoSnapUndo
      || state.autoSnapUndo.after !== revision?.revision_id || locked;
    $("#undoAutoMerge").disabled = !state.autoMergeUndo
      || state.autoMergeUndo.projectId !== state.projectId
      || state.autoMergeUndo.after !== revision?.revision_id || locked || state.historyNavigationPending;
    const taskInfoDisabled = !state.projectId || locked;
    const taskInfoMenu = $("#taskInfoMenu");
    taskInfoMenu?.classList.toggle("disabled", taskInfoDisabled);
    if (taskInfoDisabled && taskInfoMenu?.open) taskInfoMenu.open = false;
    $("#taskInfo")?.setAttribute("aria-disabled", String(taskInfoDisabled));
    ["#toolSearchScopeSource", "#toolSearchScopeTarget"].forEach(selector => {
      const node = $(selector);
      if (node) node.disabled = !revision || locked;
    });
    const projection = revision?.document?.properties?.script_projection || "original";
    const projectionSummary = $("#scriptProjectionSummary");
    if (projectionSummary) projectionSummary.textContent = "繁简转换";
    ["#convertOriginal", "#convertSimplified", "#convertTraditional", "#convertTaiwan", "#convertHongKong"]
      .forEach(selector => {
        const node = $(selector);
        if (node) node.setAttribute("aria-pressed", String(node.dataset.scriptTarget === projection));
      });
    const sourceScope = $("#toolSearchScopeSource");
    const targetScope = $("#toolSearchScopeTarget");
    if (sourceScope && targetScope) {
      sourceScope.classList.toggle("active", state.searchScope === "source");
      targetScope.classList.toggle("active", state.searchScope === "target");
      sourceScope.setAttribute("aria-pressed", String(state.searchScope === "source"));
      targetScope.setAttribute("aria-pressed", String(state.searchScope === "target"));
    }
    const searchInput = $("#toolSearch");
    if (searchInput) {
      searchInput.placeholder = state.searchScope === "target" ? "查找译文" : "查找源语词元或短语";
    }
    const referenceButton = $("#referenceManuscript");
    if (referenceButton) referenceButton.disabled = !revision || locked || textProject;
    const reviewMenu = $("#aiReviewMenu");
    $("#detectSpeakers").setAttribute("aria-disabled", String($("#detectSpeakers").disabled));
    const reviewDisabled = !revision || locked || textProject;
    $("#cueSplitView").disabled = Boolean(textProject || locked);
    syncCueViewSwitch();
    reviewMenu?.classList.toggle("disabled", reviewDisabled);
    if (reviewDisabled && reviewMenu?.open) reviewMenu.open = false;
    $("#aiReview")?.setAttribute("aria-disabled", String(reviewDisabled));
    ["#copyExternalReview", "#downloadExternalReview"].forEach(selector => {
      const control = $(selector);
      if (control) control.disabled = !revision || locked || textProject;
    });
  }

  function isReferenceToken(token) {
    const metadata = token?.provenance?.metadata || {};
    return metadata.reference === true
      || String(metadata.source || "").toLowerCase() === "reference"
      || String(token?.provenance?.operation || "").toLowerCase().includes("reference");
  }

  function navigationEntries(kind) {
    return state.navigationEntries[kind] || [];
  }

  function renderNavigators() {
    const mappings = [
      ["hard", "hardIssueIndex", "#toolHardIssuePrev", "#toolHardIssueNext"],
      ["ai", "aiChangeIndex", "#toolAiChangePrev", "#toolAiChangeNext"],
      ["reference", "referenceChangeIndex", "#toolReferencePrev", "#toolReferenceNext"]
    ];
    mappings.forEach(([kind, indexKey, previousSelector, nextSelector]) => {
      const entries = navigationEntries(kind);
      const disabled = !entries.length;
      const previous = $(previousSelector);
      const next = $(nextSelector);
      if (previous) previous.disabled = disabled;
      if (next) next.disabled = disabled;
      if (!entries.length) state[indexKey] = -1;
    });
    const referenceCounter = $("#toolReferenceCounter");
    const overLimitCounter = $("#toolOverLimitCounter");
    const aiCounter = $("#toolAiCounter");
    const overLimits = navigationEntries("hard");
    const aiChanges = navigationEntries("ai");
    const references = navigationEntries("reference");
    if (overLimitCounter) overLimitCounter.textContent = overLimits.length
      ? `${Math.max(0, state.hardIssueIndex) + 1} / ${overLimits.length}` : "0 / 0";
    if (aiCounter) aiCounter.textContent = aiChanges.length
      ? `${Math.max(0, state.aiChangeIndex) + 1} / ${aiChanges.length}` : "0 / 0";
    if (referenceCounter) referenceCounter.textContent = references.length
      ? `${Math.max(0, state.referenceChangeIndex) + 1} / ${references.length}` : "0 / 0";
  }

  function navigateEntries(kind, delta) {
    const entries = navigationEntries(kind);
    if (!entries.length) return;
    const indexKey = kind === "hard" ? "hardIssueIndex"
      : kind === "ai" ? "aiChangeIndex" : "referenceChangeIndex";
    state[indexKey] = (state[indexKey] + delta + entries.length) % entries.length;
    const entry = entries[state[indexKey]];
    state.selectedTokenIds = entry.token_id ? new Set([entry.token_id]) : new Set();
    state.selectionAnchorTokenId = entry.token_id || null;
    // Locator buttons are explicit navigation commands: keep the matching Cue
    // visible as well as seeking the media and timeline. This also materializes
    // an off-window Cue before centering it in the virtualized list.
    selectCue(entry.cue_id, true, true);
    renderNavigators();
    refreshTokenSelectionUi();
  }

  function searchMatches() {
    const input = $("#toolSearch");
    const query = String(input?.value || "").trim();
    if (!query || !state.view) return [];
    const needle = query.toLocaleLowerCase();
    const normalize = value => String(value || "").toLocaleLowerCase();
    const matches = [];
    if (state.searchScope === "target") {
      state.view.cue_views.forEach(cue => {
        if (cue.state !== "active" || !cue.target) return;
        if (normalize(cue.target.target_text).includes(needle)) {
          matches.push({kind:"target", cue_id:cue.cue_id});
        }
      });
      return matches;
    }
    const tokenById = new Map(state.view.token_views.map(token => [token.token_id, token]));
    state.view.cue_views.forEach(cue => {
      if (cue.state !== "active") return;
      const tokens = cue.display_token_ids
        .map(tokenId => tokenById.get(tokenId))
        .filter(token => token?.state === "active");
      const queryParts = query.split(/\s+/).filter(Boolean).map(normalize);
      if (queryParts.length > 1) {
        for (let start = 0; start + queryParts.length <= tokens.length; start += 1) {
          const candidate = tokens.slice(start, start + queryParts.length)
            .map(token => normalize(token.text)).join(" ");
          if (candidate === queryParts.join(" ")) {
            matches.push({
              kind:"token",
              cue_id:cue.cue_id,
              token_ids:tokens.slice(start, start + queryParts.length)
                .map(token => token.token_id)
            });
          }
        }
      } else {
        contract.findContiguousTokenMatches(tokens, query).forEach(match => {
          const coveredText = String(match.combined_text || "");
          const unmatchedEdges = `${coveredText.slice(0, Number(match.start_offset || 0))}${coveredText.slice(Number(match.end_offset ?? coveredText.length))}`;
          if (/[\p{L}\p{N}]/u.test(unmatchedEdges)) return;
          matches.push({kind:"token", cue_id:cue.cue_id, ...match});
        });
      }
    });
    return matches;
  }

  function setSearchScope(scope) {
    state.searchScope = scope === "target" ? "target" : "source";
    state.searchIndex = -1;
    state.selectedTokenIds.clear();
    state.selectionAnchorTokenId = null;
    const status = $("#toolSearchStatus");
    if (status) status.textContent = `查找范围：${state.searchScope === "target" ? "译文" : "源语词元与短语"}`;
    renderHeader();
    refreshTokenSelectionUi();
  }

  function updateSearchSelection(match, index, total) {
    const tokenIds = match?.token_ids || [];
    state.selectedTokenIds = new Set(tokenIds);
    state.selectionAnchorTokenId = tokenIds.at(-1) || null;
    // Search is an explicit navigation command, so materialize and center the
    // matching Cue instead of only changing the active selection and playback.
    if (match?.cue_id) selectCue(match.cue_id, true, true);
    const status = $("#toolSearchStatus");
    if (status && match) {
      status.textContent = `${index + 1} / ${total} · ${match.kind === "target" ? "译文" : "源语"}`;
    }
  }

  function findNextSearch() {
    const matches = searchMatches();
    if (!matches.length) {
      state.searchIndex = -1;
      $("#toolSearchStatus").textContent = "没有匹配结果";
      return;
    }
    state.searchIndex = (state.searchIndex + 1) % matches.length;
    const match = matches[state.searchIndex];
    updateSearchSelection(match, state.searchIndex, matches.length);
  }

  function renderTranslationTask(task = null) {
    state.translationTask = task;
    const panel = $("#translationTaskPanel");
    const button = $("#translateDocument");
    const visible = !!task;
    $("#translationTaskTitle").textContent = "字幕翻译";
    panel.classList.toggle("hidden", !visible || state.taskPanelDismissed);
    document.body.classList.toggle("translation-task-visible", visible && !state.taskPanelDismissed);
    panel.classList.toggle("failed", task?.state === "failed");
    panel.classList.toggle("issues", task?.state === "succeeded_with_issues");
    panel.classList.toggle("completed", ["succeeded", "succeeded_with_issues"].includes(task?.state));
    renderTaskFailureRecovery(task?.state === "failed", "translation");
    const progress = Math.max(0, Math.min(100, Number(task?.progress || 0) * 100));
    $("#translationProgressBar").style.width = `${progress}%`;
    renderAiProgress(
      task?.state === "failed" ? null : task?.ai_progress,
      progress,
      task?.display_error || task?.error?.message || task?.message || "等待启动",
      task?.problem_cue_ids || []
    );
    button.disabled = !state.revision || ["queued", "running", "cancelling"].includes(task?.state);
    const sourceLanguageSelect = $("#translationSourceLanguage");
    const languageSelect = $("#translationTargetLanguage");
    const mappingModeSelect = $("#translationMappingMode");
    const running = ["queued", "running", "cancelling"].includes(task?.state);
    sourceLanguageSelect.disabled = running;
    languageSelect.disabled = running;
    mappingModeSelect.disabled = running;
    const regroupControl = $("#translationRegroup");
    if (regroupControl) regroupControl.disabled = running;
    $("#translationLanguageLayout").disabled = running;
    if (running && task?.source_language_selection) {
      sourceLanguageSelect.value = task.source_language_selection;
    }
    if (task?.target_language && !running) languageSelect.value = task.target_language;
    if (task?.mapping_mode) mappingModeSelect.value = task.mapping_mode;
    button.classList.toggle("retry", task?.state === "failed");
    button.textContent = task?.state === "failed"
      ? "重试" : ["succeeded", "succeeded_with_issues"].includes(task?.state) ? "再次执行"
        : running ? "翻译中…" : "执行";
    $("#translationMenuSummary").textContent = running ? "AI 翻译中…" : "AI 翻译";
    if (["succeeded", "succeeded_with_issues"].includes(task?.state)) {
      const staleCount = task.stale_cue_ids?.length || 0;
      const taskMessage = $("#translationTaskMessage");
      if (staleCount) {
        taskMessage.textContent = `翻译已过期：${staleCount} 条源文在翻译后发生变化`;
        taskMessage.classList.remove("hidden");
      }
    }
  }

  function renderWorkbenchTask(title, progress, message, status = "running") {
    const panel = $("#translationTaskPanel");
    panel.classList.remove("failed", "completed", "issues");
    panel.classList.toggle("hidden", state.taskPanelDismissed);
    panel.classList.toggle("failed", status === "failed");
    panel.classList.toggle("completed", ["completed", "succeeded", "succeeded_with_issues"].includes(status));
    panel.classList.toggle("issues", status === "succeeded_with_issues");
    renderTaskFailureRecovery(status === "failed", state.editorAiTask?.kind || "calibration");
    document.body.classList.toggle("translation-task-visible", !state.taskPanelDismissed);
    $("#translationTaskTitle").textContent = title;
    $("#translationProgressBar").style.width = `${Math.max(0, Math.min(100, progress))}%`;
    const aiProgress = ["running", "cancelling", "succeeded", "succeeded_with_issues"].includes(status)
      && state.editorAiTask?.kind === "calibration"
      ? state.editorAiTask?.ai_progress : null;
    renderAiProgress(aiProgress, progress, message, state.editorAiTask?.problem_cue_ids || []);
  }

  function renderTaskFailureRecovery(failed, kind) {
    const row = $("#taskFailureRecovery");
    if (!row) return;
    row.classList.toggle("hidden", !failed);
    if (!failed) return;
    state.failedTaskKind = kind === "translation" ? "translation" : "calibration";
    const options = state.llmOptions?.options || [];
    const select = $("#failedTaskModel");
    // Polling must preserve the model choice and an in-flight retry's disabled button.
    const recoveryKey = JSON.stringify([
      state.failedTaskKind,
      kind === "translation" ? state.translationTask?.task_id : state.editorAiTask?.task_id,
      options,
      state.llmOptions?.effective_provider_id
    ]);
    if (select.dataset.recoveryKey === recoveryKey) return;
    select.dataset.recoveryKey = recoveryKey;
    select.replaceChildren(...options.map((item, index) => {
      const option = document.createElement("option");
      option.value = item.provider_id;
      option.textContent = numberedModelLabel(item, index);
      return option;
    }));
    const effective = state.llmOptions?.effective_provider_id;
    if (options.some(item => item.provider_id === effective)) select.value = effective;
    if (!options.length) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "没有已配置模型";
      select.append(empty);
    }
    $("#retryFailedTask").disabled = !options.length;
  }

  async function saveProjectModelProvider(providerId) {
    if (!state.taskInfo) throw new Error("任务信息尚未载入");
    state.taskInfo = await api(projectPath("/task-info"), {
      method:"PUT", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...state.taskInfo, llm_provider_id:providerId})
    });
    await loadProjectLlmOptions();
  }

  async function retryFailedTask() {
    const providerId = $("#failedTaskModel").value;
    if (!providerId) return;
    const button = $("#retryFailedTask");
    button.disabled = true;
    try {
      await saveProjectModelProvider(providerId);
      state.taskPanelDismissed = false;
      if (state.failedTaskKind === "translation") await startTranslation();
      else await runAiCalibration();
    } catch (error) {
      ordinaryError(`重试失败：${error.message}`);
      button.disabled = false;
    }
  }

  function numberedModelLabel(option, index) {
    return `${String(index + 1).padStart(2, "0")} ${option.label} · ${option.model}`;
  }

  async function loadProjectLlmOptions() {
    if (!state.projectId) return null;
    state.llmOptions = await api(projectPath("/llm-options"));
    return state.llmOptions;
  }

  function renderProjectModelSelect() {
    const select = $("#taskInfoLlmProvider");
    if (!select) return;
    const options = state.llmOptions?.options || [];
    const active = options.find(item => item.provider_id === state.llmOptions?.active_provider_id);
    const inherit = document.createElement("option");
    inherit.value = "inherit";
    inherit.textContent = active
      ? `跟随全局设置（${active.label} · ${active.model}）`
      : "跟随全局设置";
    select.replaceChildren(inherit, ...options.map((item, index) => {
      const option = document.createElement("option");
      option.value = item.provider_id;
      option.textContent = numberedModelLabel(item, index);
      return option;
    }));
    const selected = state.taskInfo?.llm_provider_id || "inherit";
    if (selected !== "inherit" && !options.some(item => item.provider_id === selected)) {
      const unavailable = document.createElement("option");
      unavailable.value = selected;
      unavailable.textContent = `${selected}（当前未配置）`;
      select.append(unavailable);
    }
    select.value = selected;
  }

  function renderAiProgress(aiProgress, percent, fallbackMessage, problemCueIds = []) {
    const counter = $("#translationTaskPercent");
    const message = $("#translationTaskMessage");
    const steps = $("#translationTaskSteps");
    const summary = window.SubstarAiProgressSummary?.summarize(aiProgress, {problemCueIds});
    if (!summary) {
      counter.textContent = `${Math.round(percent)}%`;
      message.textContent = fallbackMessage;
      message.classList.remove("hidden");
      steps.classList.add("hidden");
      steps.replaceChildren();
      return;
    }
    counter.textContent = `${Math.round(percent)}%`;
    message.textContent = aiProgress.message || fallbackMessage;
    message.classList.add("hidden");
    steps.textContent = window.SubstarAiProgressSummary.format(aiProgress, {problemCueIds});
    steps.classList.toggle("hidden", !steps.textContent);
  }

  async function cancelOrDismissTaskPanel() {
    const running = editorAiTaskLocksEditor()
      || ["queued", "running", "cancelling"].includes(state.translationTask?.state)
      || state.operationPending;
    if (!running || !state.projectId) {
      state.taskPanelDismissed = true;
      $("#translationTaskPanel").classList.add("hidden");
      document.body.classList.remove("translation-task-visible");
      return;
    }
    state.taskPanelDismissed = false;
    const button = $("#dismissTaskPanel");
    button.disabled = true;
    renderWorkbenchTask(
      $("#translationTaskTitle").textContent || "AI 任务", 0, "正在取消任务…", "cancelling"
    );
    try {
      state.editorAiTask = await api(projectPath("/ai-task"), {method:"DELETE"});
      await refreshEditorAiTask();
    } catch (error) {
      ordinaryError(`取消任务失败：${error.message}`);
    } finally {
      button.disabled = false;
    }
  }

  function stopTranslationPoll() {
    if (state.translationPoll) window.clearInterval(state.translationPoll);
    state.translationPoll = null;
  }

  async function refreshTranslationTask({reloadOnComplete = false} = {}) {
    if (!state.projectId) return null;
    try {
      const task = await api(projectPath("/translation"));
      const wasComplete = ["succeeded", "succeeded_with_issues"].includes(state.translationTask?.state)
        && state.translationTask?.result_revision_id === task.result_revision_id;
      renderTranslationTask(task);
      if (!["queued", "running", "cancelling"].includes(task.state)) stopTranslationPoll();
      if (reloadOnComplete && ["succeeded", "succeeded_with_issues"].includes(task.state) && !wasComplete
        && task.result_revision_id !== state.revision?.revision_id) {
        await loadProject(state.projectId, {restoreTranslation:false});
      }
      return task;
    } catch (error) {
      if (error.status === 404) {
        renderTranslationTask(null);
        return null;
      }
      ordinaryError(`翻译状态读取失败：${error.message}`);
      return null;
    }
  }

  function followTranslationTask(task) {
    stopTranslationPoll();
    renderTranslationTask(task);
    if (!["queued", "running", "cancelling"].includes(task.state)) return;
    state.translationPoll = window.setInterval(
      () => refreshTranslationTask({reloadOnComplete:true}), 1000
    );
  }

  let translationGlossaryLoaded = false;
  let translationGlossaryLoading = null;
  async function loadTranslationGlossaries() {
    if (translationGlossaryLoading) return translationGlossaryLoading;
    translationGlossaryLoading = (async () => {
      const container = $("#translationGlossaryChoices");
      const selected = new Set(Array.from(container.querySelectorAll("input:checked"), input => input.value));
      try {
        const library = await api("/api/glossary");
        const collections = (library.collections || []).filter(c => c.injection_permissions?.includes("translation"));
        container.replaceChildren();
        for (const collection of collections) {
          const label = document.createElement("label");
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = collection.id;
          input.checked = translationGlossaryLoaded ? selected.has(collection.id) : true;
          const count = (library.entries || []).filter(e => e.enabled && e.glossary_id === collection.id).length;
          label.append(input, document.createTextNode(`${collection.name}（${count} 条）`));
          container.append(label);
        }
        if (!collections.length) container.textContent = "暂无可用词库";
        translationGlossaryLoaded = true;
      } catch (error) {
        throw new Error(`词库读取失败：${error.message}`);
      }
    })();
    try { await translationGlossaryLoading; }
    finally { translationGlossaryLoading = null; }
  }
  $("#translationMenu").addEventListener("toggle", () => {
    if ($("#translationMenu").open) loadTranslationGlossaries().catch(error => ordinaryError(error.message));
  });

  async function startTranslation() {
    if (!state.projectId || !state.revision) return;
    if (isAdvancedTutorial()) {
      $("#translationMenu").open = false;
      return runPackagedTutorialStage("translation");
    }
    ordinaryError("");
    state.taskPanelDismissed = false;
    const sourceLanguage = $("#translationSourceLanguage").value;
    const targetLanguage = $("#translationTargetLanguage").value;
    const mappingMode = $("#translationMappingMode").value;
    const regroupControl = $("#translationRegroup");
    const regroup = Boolean(regroupControl?.checked || (
      state.translationTask?.state === "failed" && state.translationTask?.regroup
    ));
    $("#translateDocument").disabled = true;
    $("#translationSourceLanguage").disabled = true;
    $("#translationTargetLanguage").disabled = true;
    $("#translationMappingMode").disabled = true;
    $("#translationMenu").open = false;
    renderTranslationTask({state:"queued", progress:0, message:"正在启动 AI 翻译…"});
    try {
      await loadTranslationGlossaries();
      const glossaryIds = Array.from($("#translationGlossaryChoices").querySelectorAll("input:checked"), input => input.value);
      const task = await withSavedDocument(() => api(projectPath("/translation"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          source_language:sourceLanguage,
          target_language:targetLanguage,
          translation_layout:window.SubstarTranslationLayout.read("translationLanguageLayout"),
          mapping_mode:mappingMode,
          regroup,
          glossary_ids:glossaryIds
        })
      }));
      followTranslationTask(task);
      if (regroupControl) regroupControl.checked = false;
    } catch (error) {
      ordinaryError(`翻译启动失败：${error.message}`);
      renderTranslationTask({
        state:"failed", progress:0, message:"翻译启动失败", error:error.message
      });
    }
  }

  function tokenElement(token, {interactive = true} = {}) {
    const button = document.createElement(interactive ? "button" : "span");
    if (interactive) button.type = "button";
    const tone = token.tone;
    const referenceChange = state.referenceChangeByTokenId?.get(token.token_id);
    let referenceClass = "";
    if (referenceChange) {
      const type = String(referenceChange.type || "replace");
      if (type === "replace") {
        referenceClass = token.text === String(referenceChange.after || "")
          ? " reference-applied" : " reference-suggested";
      } else if (type === "insert") {
        referenceClass = token.state === "deleted"
          ? " reference-inserted-deleted" : " reference-inserted";
      } else if (type === "retained_source") {
        referenceClass = " reference-asr-retained";
      }
    }
    const externalClass = token.provenance?.operation === "external_ai_prooftranslation" ? " external-ai-prooftranslation" : "";
    button.className = `display-token tone-${tone}${referenceClass}${externalClass}${token.state === "deleted" ? " deleted" : ""}${state.selectedTokenIds.has(token.token_id) ? " selected" : ""}`;
    const annotated = reviewEnabled && reviewData.notes.some(note => note.status !== "deleted" && note.anchor.track === "source" && note.anchor_status !== "needs_relocation"
      && (note.anchor.token_ids?.length ? note.anchor.token_ids.includes(token.token_id)
        : !note.anchor.text_range && state.indexes?.tokenToCueId?.get(token.token_id) === note.anchor.cue_id));
    button.classList.toggle("review-annotated", annotated);
    if (interactive) button.dataset.tokenId = token.token_id;
    const label = document.createElement("b");
    const tokenCueId = state.indexes?.tokenToCueId?.get(token.token_id);
    const projectedText = projectedSourceTokenText(token.text, state.indexes?.cueById?.get(tokenCueId));
    label.textContent = projectedText;
    button.classList.toggle("punctuation-elided", !projectedText);
    const source = sourceTextForToken(token);
    if (source && source !== token.text) button.title = `原词：${source}`;
    else if (!source) button.title = "人工新增词元";
    const timings = token.source_token_ids.map(id => state.indexes?.sourceById.get(id)?.timing);
    const timingLabels = {native:"识别词时间", "envelope-inherited":"继承时间区间", subdivided:"区间内估算时间", manual:"人工时间", unknown:"来源未标注"};
    if (timings.length) button.title += `${button.title ? "；" : ""}时间来源：${[...new Set(timings.map(value => timingLabels[value?.kind || "unknown"]))].join("、")}`;
    button.append(label);
    return button;
  }

  function resizeTranslationField(field) {
    if (typeof CSS === "object" && CSS.supports?.("field-sizing", "content")) {
      field.style.height = "";
      return;
    }
    field.style.height = "auto";
    field.style.height = `${Math.max(31, field.scrollHeight)}px`;
  }

  function applySubtitlePolicy(settings = {}) {
    state.subtitlePolicy = {
      englishHardLimit:Number(settings.english_hard_limit ?? 55),
      chineseHardLimit:Number(settings.chinese_hard_limit ?? 20),
      mixedHardLimit:Number(settings.mixed_hard_limit ?? 25),
      japaneseHardLimit:Number(settings.japanese_hard_limit ?? 24),
      koreanHardLimit:Number(settings.korean_hard_limit ?? 28),
      sourceLanguage:String(settings.language || "Auto"),
      sourceHardLimit:Number.isFinite(Number(settings.source_hard_limit)) ? Number(settings.source_hard_limit) : null,
      targetHardLimit:Number.isFinite(Number(settings.target_hard_limit)) ? Number(settings.target_hard_limit) : null,
      countSpaces:true,
      countPunctuation:true
    };
  }

  function detectSubtitleLanguage(text) {
    return languageLayout.detectLanguage(text);
  }

  function inferTrackLanguages() {
    const lines = (state.view?.cue_views || [])
      .filter(cue => cue.state === "active")
      .map(cue => projectedCueLines(cue));
    const sourceText = lines.map(item => item.source).filter(Boolean).join(" ");
    const targetText = lines.map(item => item.target).filter(Boolean).join(" ");
    const configured = String(state.subtitlePolicy.sourceLanguage || "Auto").toLowerCase();
    const configuredSource = ({zh:"zh", "zh-cn":"zh", en:"en", mixed:"mixed", ja:"ja", ko:"ko"})[configured];
    // The user-confirmed project snapshot is authoritative. Content detection
    // is reserved for Auto so an editor render cannot reinterpret the task.
    const source = languageLayout.resolveSourceLanguage(configuredSource, sourceText);
    const target = targetText ? detectSubtitleLanguage(targetText) : source === "zh" ? "en" : "zh";
    return {source, target};
  }

  function subtitleLengthMetric(text, language = null, track = null) {
    const value = String(text || "").trim();
    const routed = Boolean(state.view?.presentation?.translation_layout?.enabled);
    const resolvedLanguage = routed ? languageLayout.normalizeLanguage(language || detectSubtitleLanguage(value)) : language || detectSubtitleLanguage(value);
    const limits = {
      zh:state.subtitlePolicy.chineseHardLimit,
      mixed:state.subtitlePolicy.mixedHardLimit,
      ja:state.subtitlePolicy.japaneseHardLimit,
      ko:state.subtitlePolicy.koreanHardLimit,
      en:state.subtitlePolicy.englishHardLimit
    };
    const override = track === "source"
      ? state.subtitlePolicy.sourceHardLimit
      : track === "target" ? state.subtitlePolicy.targetHardLimit : null;
    const limit = (!routed && override) || limits[resolvedLanguage] || limits.en;
    const count = languageLayout.characterCount(value, resolvedLanguage, {
      countSpaces: state.subtitlePolicy.countSpaces,
      countPunctuation: state.subtitlePolicy.countPunctuation
    });
    return {
      language:resolvedLanguage,
      count,
      limit
    };
  }

  function metricClass(metric) {
    return metric.count > metric.limit ? " over-limit" : "";
  }

  function validationTrackLimit(track) {
    const override = track === "source"
      ? state.subtitlePolicy.sourceHardLimit : state.subtitlePolicy.targetHardLimit;
    if (override) return override;
    const limits = {
      zh:state.subtitlePolicy.chineseHardLimit,
      mixed:state.subtitlePolicy.mixedHardLimit,
      ja:state.subtitlePolicy.japaneseHardLimit,
      ko:state.subtitlePolicy.koreanHardLimit,
      en:state.subtitlePolicy.englishHardLimit
    };
    return limits[state.trackLanguages?.[track]] || limits.en;
  }

  function cueElement(cue, index, tokenById) {
    const row = document.createElement("article");
    const projected = projectedCueLines(cue);
    // A target track is a real editable deliverable even when the model left
    // its text blank and marked it for manual completion.  Rendering based on
    // non-empty text hid exactly the recovery row the user needed to fill.
    const bilingualProject = state.view?.cue_views.some(item => item.subtitle_track !== "overlay" && item.target);
    const hasTarget = (Boolean(cue.target) || (bilingualProject && cue.subtitle_track !== "overlay"))
      && $("#showTranslations").checked;
    const sourceMetric = subtitleLengthMetric(projected.source, state.view?.presentation?.translation_layout?.enabled ? null : state.trackLanguages.source, "source");
    const targetMetric = subtitleLengthMetric(projected.target, cue.target?.language || state.trackLanguages.target, "target");
    const overLimit = sourceMetric.count > sourceMetric.limit
      || (Boolean(cue.target) && targetMetric.count > targetMetric.limit);
    row.className = `cue-row${hasTarget ? " has-target" : " source-only"}${cue.cue_id === state.activeCueId ? " current" : ""}${cue.state === "deleted" ? " deleted" : ""}${overLimit ? " over-limit" : ""}`;
    row.dataset.cueId = cue.cue_id;
    row.classList.toggle("overlay-cue", cue.subtitle_track === "overlay");
    const noteCount = reviewEnabled ? reviewData.notes.filter(note => note.anchor.cue_id === cue.cue_id && note.status === "open").length : 0;
    if (noteCount) {row.dataset.reviewCount = String(noteCount); row.title = `${noteCount} 条未解决修订`;}
    if (cue.speaker) row.dataset.speaker = cue.speaker;
    const meta = document.createElement("button");
    meta.type = "button";
    meta.className = "cue-meta";
    meta.dataset.cueSelect = cue.cue_id;
    meta.innerHTML = `<strong>${cue.index + 1}</strong><span class="cue-count"><span class="cue-count-line${metricClass(sourceMetric)}">${document.documentElement.dataset.editorLayout === "experimental" ? "原文" : "原"} ${sourceMetric.count}/${sourceMetric.limit}</span>${hasTarget ? `<span class="cue-count-line${metricClass(targetMetric)}">${document.documentElement.dataset.editorLayout === "experimental" ? "译文" : "译"} ${targetMetric.count}/${targetMetric.limit}</span>` : ""}</span>`;
    meta.title = `${timeLabel(cue.start)} → ${timeLabel(cue.end)}`;
    const content = document.createElement("div");
    content.className = "cue-content";
    const line = document.createElement("div");
    const virtual = state.cueSplitView === "virtual";
    line.className = `source-token-line${virtual ? " virtual-line" : ""}`;
    const hasNext = cue.subtitle_track !== "overlay" && cue.state === "active"
      && state.view.cue_views[index+1]?.state === "active" && state.view.cue_views[index+1]?.subtitle_track !== "overlay";
    const reviewGroupByToken = new Map();
    if (reviewEnabled) reviewData.notes.forEach(note => {
      if (note.status === "deleted" || note.anchor.track !== "source" || note.anchor.cue_id !== cue.cue_id || note.anchor_status === "needs_relocation") return;
      const ids = note.anchor.token_ids?.length ? note.anchor.token_ids : !note.anchor.text_range ? cue.display_token_ids : [];
      ids.forEach(id => reviewGroupByToken.set(id, note.id));
    });
    cue.display_token_ids.forEach((id, tokenIndex) => {
      const token = tokenById.get(id);
      if (!token) return;
      const group = reviewGroupByToken.get(id);
      const joinsPrevious = !!group && group === reviewGroupByToken.get(cue.display_token_ids[tokenIndex - 1]);
      const joinsNext = !!group && group === reviewGroupByToken.get(cue.display_token_ids[tokenIndex + 1]);
      const tokenNode = tokenElement(token, {interactive:cue.state === "active"});
      tokenNode.classList.toggle("review-joins-previous", joinsPrevious);
      tokenNode.classList.toggle("review-joins-next", joinsNext);
      line.append(tokenNode);
      const atEnd = tokenIndex === cue.display_token_ids.length - 1;
      if (virtual && !atEnd) {
        const nextToken = tokenById.get(cue.display_token_ids[tokenIndex + 1]);
        const boundary = document.createElement(cue.state === "active" ? "button" : "span");
        const boundaryKind = languageLayout.virtualBoundaryKind(
          token.text, nextToken?.text || "", state.trackLanguages?.source
        );
        boundary.className = `virtual-boundary ${boundaryKind}-based`;
        boundary.classList.toggle("review-group-bridge", joinsNext);
        if (boundaryKind === "word") line.classList.add("word-based");
        if (cue.state === "active") {
          boundary.type = "button";
          boundary.dataset.virtualBoundary = "";
          boundary.dataset.boundaryControl = "";
          boundary.dataset.boundaryAction = "split";
          boundary.dataset.boundaryAfter = id;
          boundary.dataset.cueId = cue.cue_id;
          boundary.title = "右键切分";
          boundary.setAttribute("aria-label", "右键在此处切分 Cue");
        } else {
          boundary.setAttribute("aria-hidden", "true");
        }
        line.append(boundary);
        return;
      }
      const connector = document.createElement(cue.state === "active" ? "button" : "span");
      connector.className = `token-connector${atEnd ? " cue-end" : ""}`;
      if (cue.state === "active") {
        connector.type = "button";
        connector.dataset.tokenConnector = "";
        connector.dataset.insertAfter = id;
        connector.dataset.cueId = cue.cue_id;
      }
      if (cue.state === "active" && tokenIndex < cue.display_token_ids.length - 1) {
        connector.dataset.boundaryAfter = id;
        connector.dataset.cueAction = "split";
        connector.dataset.boundaryControl = "";
        connector.dataset.boundaryAction = "split";
        connector.title = "右键切分";
        connector.setAttribute("aria-label", "右键在此处切分 Cue");
      } else if (cue.state === "active" && hasNext) {
        connector.dataset.cueAction = "merge-next";
        connector.dataset.boundaryControl = "";
        connector.dataset.boundaryAction = "merge-next";
        connector.title = "右键与下一 Cue 合并";
        connector.setAttribute("aria-label", "右键与下一 Cue 合并");
      }
      if (atEnd && cue.state === "active" && document.documentElement.dataset.editorLayout === "experimental") {
        connector.title = "双击编辑整行，点击外部保存；右键与下一 Cue 合并";
        connector.setAttribute("aria-label", "双击编辑整行原文");
      }
      line.append(connector);
    });
    content.append(line);
    if (cue.source_text != null) {
      line.remove();
      const source = document.createElement("textarea");
      source.className = "translation-track source-text-track";
      source.rows = 1;
      source.value = cue.source_text;
      source.dataset.sourceEdit = cue.cue_id;
      source.dataset.originalSource = cue.source_text;
      source.setAttribute("aria-label", cue.subtitle_track === "overlay" ? "叠加字幕" : "原文字幕");
      if (cue.subtitle_track === "overlay") {
        const badge = document.createElement("span"); badge.className = "overlay-cue-label"; badge.textContent = "叠加字幕"; content.append(badge);
      }
      source.disabled = cue.state !== "active" || editorAiTaskLocksEditor();
      content.append(source);
    }
    if (hasTarget) {
      const target = document.createElement("textarea");
      target.rows = 1;
      target.className = "translation-track";
      target.dataset.targetEdit = cue.cue_id;
      target.dataset.originalTarget = cue.target?.target_text || "";
      target.dataset.expectedTarget = cue.target?.canonical_target_text ?? cue.target?.target_text ?? "";
      target.dataset.projectedTarget = projected.target;
      target.dataset.rawEditing = "false";
      target.title = cue.target?.translation_status === "needs_review" ? (cue.target?.issue_code === "source_changed" ? "原文已变更，请复核译文" : "译文质量或行长需要复核") : cue.target?.translation_status === "manual_required" ? "译文需要人工处理" : "直接编辑译文";
      target.dataset.translationStatus = cue.target?.translation_status || "manual_required";
      target.disabled = cue.state === "deleted" || editorAiTaskLocksEditor();
      target.value = projected.target;
      if (!cue.target) target.placeholder = "\u8bd1\u6587";
      if (projected.sourceUpper) content.append(target);
      else content.prepend(target);
    }
    const actions = document.createElement("div");
    actions.className = "cue-actions";
    actions.innerHTML = cue.state === "deleted" ? `
      <button type="button" class="restore" data-cue-action="restore" title="恢复 Cue">↶ 恢复</button>` : `
      <button type="button" class="hide-cue" data-cue-action="hide" title="隐藏 Cue（可恢复）"><svg class="ui-icon"><use href="/assets/ui-icons.svg#eye-off"></use></svg></button>
      <button type="button" class="danger" data-cue-action="purge" title="永久删除 Cue（可用全局撤销恢复）"><svg class="ui-icon"><use href="/assets/ui-icons.svg#trash"></use></svg></button>
      <button type="button" class="speaker" data-cue-action="speaker"><svg class="ui-icon"><use href="/assets/ui-icons.svg#user"></use></svg></button>`;
    const speakerButton = actions.querySelector('[data-cue-action="speaker"]');
    if (speakerButton) {
      speakerButton.title = `${speakerLabel(cue.speaker)}；点击切换下一位说话人`;
      speakerButton.style.setProperty("--cue-speaker-color", window.EditorSpeakers.color(cue.speaker));
      speakerButton.setAttribute("aria-label", speakerButton.title);
      speakerButton.style.color = window.EditorSpeakers.color(cue.speaker);
      speakerButton.style.borderColor = window.EditorSpeakers.color(cue.speaker);
    }
    row.append(meta, content, actions);
    if ((cue.source_text != null && cue.subtitle_track !== "overlay") || editorAiTaskLocksEditor()) actions.querySelectorAll("button").forEach(node => {node.disabled = true;});
    return row;
  }

  function speakerNames() {
    return state.view?.properties?.speaker_names || {};
  }

  function speakerLabel(speaker) {
    return window.EditorSpeakers.label(speaker, speakerNames());
  }

  function appendSpeakerInput(id, name) {
    const label = document.createElement("label");
    const swatch = document.createElement("i");
    swatch.className = "speaker-swatch";
    swatch.style.background = window.EditorSpeakers.color(id);
    const input = document.createElement("input");
    input.dataset.speakerName = id; input.maxLength = 60;
    input.value = name; input.placeholder = window.EditorSpeakers.label(id);
    input.setAttribute("aria-label", `${id} 名称`);
    label.append(swatch, input);
    $("#speakerDialogForm .speaker-name-grid").append(label);
    return input;
  }

  function populateSpeakerSettings() {
    if (!state.revision) return;
    const names = speakerNames();
    $("#speakerDialogForm .speaker-name-grid").replaceChildren();
    const ids = window.EditorSpeakers.ids(state.view);
    if (!ids.length) ids.push("speaker_0");
    ids.forEach(id => appendSpeakerInput(id, names[id] || window.EditorSpeakers.label(id)));
  }
  document.addEventListener('substar:tool-activated',event=>{if(event.detail==='dubbing')populateSpeakerSettings();});
  function openSpeakerDialog() {
    populateSpeakerSettings();
    if($('#speakerDialogForm').closest('[data-tool-panel="dubbing"]')){
      document.dispatchEvent(new CustomEvent('substar:activate-tool',{detail:'dubbing'}));return;
    }
    const menu = $("#speakerMenu");
    if (menu) menu.open = true;
    else $("#speakerDialog").classList.remove("hidden");
    requestAnimationFrame(() => $("[data-speaker-name]")?.focus());
  }

  function closeSpeakerDialog() { if($("#speakerDialogForm").closest('[data-tool-panel="dubbing"]')){populateSpeakerSettings();return;} if ($("#speakerMenu")) $("#speakerMenu").open = false; $("#speakerDialog").classList.add("hidden"); }

  function renderCues({preservePage = false} = {}) {
    const list = $("#cueList");
    if (!state.view) {
      list.innerHTML = '<div class="empty-state">选择一个项目开始编辑</div>';
      return;
    }
    const cues = state.view.cue_views;
    const tokenById = state.indexes?.tokenById || new Map();
    const page = state.cueListView?.render({
      cues, tokenById, activeCueId:state.activeCueId,
      pageStart:state.cuePageStart, preservePage
    });
    if (page) state.cuePageStart = page.start;
    refreshCueSelection();
    if (state.translationResizeFrame) cancelAnimationFrame(state.translationResizeFrame);
    state.translationResizeFrame = requestAnimationFrame(() => {
      state.translationResizeFrame = 0;
      if (typeof CSS === "object" && CSS.supports?.("field-sizing", "content")) return;
      const fields = [...list.querySelectorAll(".translation-track")];
      fields.forEach(field => { field.style.height = "auto"; });
      const heights = fields.map(field => Math.max(31, field.scrollHeight));
      fields.forEach((field, index) => { field.style.height = `${heights[index]}px`; });
    });
  }

  function renderSelectionActions() {
    const ids = [...state.selectedTokenIds];
    const tokenById = state.indexes?.tokenById || new Map();
    const activeIds = ids.filter(id => tokenById.get(id)?.state === "active");
    const deletedIds = ids.filter(id => tokenById.get(id)?.state === "deleted");
    const aiCalibrationState = token => {
      if (token?.provenance?.kind === "ai") return "applied";
      const record = token?.provenance?.metadata?.ai_calibration;
      return record && typeof record === "object" && record.applied === false
        && typeof record.after_text === "string"
        ? "canceled"
        : "none";
    };
    const aiAppliedIds = activeIds.filter(id => aiCalibrationState(tokenById.get(id)) === "applied");
    const aiCanceledIds = activeIds.filter(id => aiCalibrationState(tokenById.get(id)) === "canceled");
    const aiCalibrationIds = aiAppliedIds;
    const deleteButton = $("#deleteTokens");
    const restoreButton = $("#restoreTokens");
    deleteButton.disabled = activeIds.length < 1;
    restoreButton.disabled = deletedIds.length < 1 && aiCalibrationIds.length < 1;
    deleteButton.textContent = deletedIds.length ? `删除未删项 (${activeIds.length})` : `删除 (${activeIds.length})`;
    restoreButton.textContent = aiCalibrationIds.length
      ? `恢复 AI 修正 (${aiCalibrationIds.length})`
      : activeIds.length ? `恢复已删项 (${deletedIds.length})` : `恢复 (${deletedIds.length})`;
    const onlyDeleted = deletedIds.length > 0 && activeIds.length === 0;
    const canCancelAi = activeIds.length > 0 && deletedIds.length === 0
      && aiAppliedIds.length === activeIds.length;
    const canRestoreAi = activeIds.length > 0 && deletedIds.length === 0
      && aiCanceledIds.length === activeIds.length;
    const restoreEnabled = deletedIds.length > 0 || canCancelAi || canRestoreAi;
    deleteButton.style.display = activeIds.length ? "" : "none";
    restoreButton.style.display = restoreEnabled ? "" : "none";
    deleteButton.textContent = `删除 (${activeIds.length})`;
    restoreButton.textContent = onlyDeleted
      ? `恢复 (${deletedIds.length})`
      : canCancelAi
        ? `取消 AI 修正 (${aiAppliedIds.length})`
        : canRestoreAi
          ? `恢复 AI 修正 (${aiCanceledIds.length})`
          : `恢复 (${deletedIds.length})`;
    restoreButton.disabled = !restoreEnabled;
    let mergeable = activeIds.length >= 2 && deletedIds.length === 0;
    let selectedCue = null;
    let positions = [];
    if (activeIds.length) {
      const cueIds = new Set(activeIds.map(id => state.indexes?.tokenToCueId?.get(id)));
      selectedCue = cueIds.size === 1
        ? state.indexes?.cueById?.get([...cueIds][0]) || null
        : null;
      if (selectedCue?.state !== "active") selectedCue = null;
      positions = selectedCue ? activeIds.map(id => selectedCue.display_token_ids.indexOf(id)).sort((a, b) => a - b) : [];
    }
    if (mergeable) {
      mergeable = !!selectedCue && positions.every((value, index) => index === 0 || value === positions[index - 1] + 1);
    }
    $("#mergeTokens").disabled = !mergeable;
    const menu = $("#tokenSelectionMenu");
    menu.classList.toggle("hidden", !ids.length);
    syncTokenReviewForm();
    renderReferenceSelectionMenu();
    if (ids.length) requestAnimationFrame(positionSelectionMenus);
  }

  function selectedReferenceChange() {
    if (state.selectedTokenIds.size !== 1) return null;
    const tokenId = [...state.selectedTokenIds][0];
    const change = state.referenceChangeByTokenId?.get(tokenId);
    const token = state.indexes?.tokenById?.get(tokenId);
    return change && token ? {change, token, tokenId} : null;
  }

  function renderReferenceSelectionMenu() {
    const menu = $("#referenceSelectionMenu");
    const selected = selectedReferenceChange();
    menu.classList.toggle("hidden", !selected);
    if (!selected) return;
    const {change, token} = selected;
    const labels = {replace:"参考修正", insert:"参考增补", retained_source:"参考稿未包含"};
    const type = String(change.type || "replace");
    const before = String(change.before || "");
    const after = String(change.after || "");
    let status = String(change.status || "");
    if (type === "replace") {
      status = token.text === after ? "已采用" : token.text === before ? "保留听写结果" : "已手动修改";
    } else if (type === "insert") {
      status = token.state === "deleted" ? "默认隐藏，可恢复" : "已采用";
    } else {
      status = "已保留";
    }
    $("#referenceSelectionType").textContent = labels[type] || "参考稿差异";
    if (type === "insert" && ["ambiguous_reference", "existing_cue_boundary"].includes(change.reason)) {
      $("#referenceSelectionType").textContent = "待确认参考文字";
    }
    $("#referenceSelectionStatus").textContent = status;
    $("#referenceSelectionBefore").textContent = before || "（无）";
    $("#referenceSelectionAfter").textContent = after || "（参考稿未包含）";
    const keep = $("#referenceKeepAsr");
    const apply = $("#referenceApply");
    $("#referenceSelectionComparison").querySelector("p:last-child").classList.toggle("hidden", type === "retained_source");
    $("#referenceSelectionMenu").querySelector(".reference-selection-actions").classList.toggle("hidden", type === "retained_source");
    keep.disabled = type === "retained_source" || (type === "replace" && token.text === before)
      || (type === "insert" && token.state === "deleted");
    apply.disabled = type === "retained_source" || (type === "replace" && token.text === after)
      || (type === "insert" && token.state !== "deleted");
  }

  function selectedTokenRect() {
    const nodes = [...document.querySelectorAll(".display-token.selected")];
    if (!nodes.length) return null;
    const rects = nodes.map(node => node.getBoundingClientRect());
    return {
      left:Math.min(...rects.map(rect => rect.left)),
      right:Math.max(...rects.map(rect => rect.right)),
      top:Math.min(...rects.map(rect => rect.top)),
      bottom:Math.max(...rects.map(rect => rect.bottom))
    };
  }

  function positionSelectionMenu(menu, direction, anchor, bounds) {
    if (!menu || menu.classList.contains("hidden")) return;
    const width = menu.offsetWidth || (direction === "above" ? 310 : 150);
    const height = menu.offsetHeight || 35;
    const center = (anchor.left + anchor.right) / 2;
    const left = Math.max(bounds.left, Math.min(bounds.right - width, center - width / 2));
    const preferredTop = direction === "above" ? anchor.top - height - 9 : anchor.bottom + 9;
    const top = Math.max(bounds.top, Math.min(bounds.bottom - height, preferredTop));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    menu.style.setProperty("--menu-arrow-left", `${Math.max(14, Math.min(width - 14, center - left))}px`);
    menu.classList.toggle("menu-above", direction === "above");
    menu.classList.toggle("menu-below", direction === "below");
  }

  function positionSelectionMenus() {
    const anchor = selectedTokenRect();
    if (!anchor) return;
    const paneRect = $(".editor-document-pane")?.getBoundingClientRect();
    const bounds = {
      left:Math.max(8, paneRect?.left || 8) + 8,
      right:Math.min(window.innerWidth - 8, paneRect?.right || window.innerWidth - 8) - 8,
      top:8,
      bottom:window.innerHeight - 8
    };
    positionSelectionMenu($("#referenceSelectionMenu"), "above", anchor, bounds);
    positionSelectionMenu($("#tokenSelectionMenu"), "below", anchor, bounds);
    if (state.tutorial.active) positionEditorTutorial();
  }

  function currentCue() {
    return state.indexes?.cueById?.get(state.activeCueId) || null;
  }

  function renderPreview() {
    const cue = currentCue();
    const overlayPreview=$("#previewOverlay");
    const time=activeMedia()?.currentTime || 0;
    if(overlayPreview) overlayPreview.textContent=(state.view?.cue_views || [])
      .filter(item=>item.subtitle_track === "overlay" && item.state === "active" && item.start<=time && item.end>time)
      .map(item=>item.source_text || "").join("\n");
    if (state.view?.cue_views.some(item => item.source_text != null)) {
      const time = activeMedia()?.currentTime || 0;
      const visible = state.view.cue_views.filter(item => item.state === "active" && item.start <= time && item.end > time && (!overlayPreview || item.subtitle_track !== "overlay"));
      const lines = visible.map(projectedCueLines);
      $("#previewSource").textContent = lines.map(line => line.sourceUpper ? line.source : line.target).filter(Boolean).join("\n");
      $("#previewTarget").textContent = lines.map(line => line.sourceUpper ? line.target : line.source).filter(Boolean).join("\n");
      return;
    }
    if (!cue) {
      $("#previewSource").textContent = "";
      $("#previewTarget").textContent = "";
      return;
    }
    const projected = projectedCueLines(cue);
    const source = projected.source;
    const target = projected.target;
    const first = projected.sourceUpper ? source : target;
    const second = projected.sourceUpper ? target : source;
    $("#previewSource").textContent = first;
    $("#previewTarget").textContent = second;
  }

  function renderTimeline({deferDraw = false} = {}) {
    updateTimelineHeight();
    const track = $("#timelineTrack");
    if (state.timelineController) {
      if (state.timelineSelectedCueId && !state.view?.cue_views.some(cue =>
        cue.state === "active" && cue.cue_id === state.timelineSelectedCueId
      )) state.timelineSelectedCueId = null;
      state.timelineController.setView(state.view, {drawNow:!deferDraw});
      state.timelineController.setActiveCue(state.activeCueId, {drawNow:!deferDraw});
      state.timelineController.setSelectedCue(state.timelineSelectedCueId, {drawNow:!deferDraw});
      if (deferDraw) requestAnimationFrame(() => {
        if (!state.operationPending) state.timelineController?.redraw();
      });
      return;
    }
    track.replaceChildren();
    const cues = state.view?.cue_views || [];
    const media = activeMedia();
    const videoDuration = Number.isFinite(media?.duration) ? media.duration : 0;
    const duration = Math.max(...cues.map(cue => Number(cue.end)), videoDuration, 0.001);
    cues.forEach(cue => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `timeline-cue${cue.cue_id === state.activeCueId ? " current" : ""}${cue.state === "deleted" ? " deleted" : ""}`;
      if (cue.speaker) button.classList.add(cue.speaker.replace("_", "-"));
      button.dataset.timelineCue = cue.cue_id;
      button.style.left = `${Number(cue.start) / duration * 100}%`;
      button.style.width = `${Math.max(.45, (Number(cue.end) - Number(cue.start)) / duration * 100)}%`;
      const source = cueSourceText(cue);
      const title = document.createElement("strong");
      title.textContent = `${cue.index + 1} ${source}`;
      const time = document.createElement("span");
      time.textContent = `${timeLabel(cue.start)}–${timeLabel(cue.end)}`;
      button.append(title, time);
      track.append(button);
    });
    const playhead = document.createElement("i");
    playhead.id = "timelinePlayhead";
    playhead.className = "timeline-playhead";
    playhead.setAttribute("aria-hidden", "true");
    const currentTime = Number(activeMedia()?.currentTime) || 0;
    playhead.style.left = `${Math.max(0, Math.min(100, currentTime / duration * 100))}%`;
    track.append(playhead);
  }

  function timelineDuration() {
    const cues = state.view?.cue_views || [];
    const media = activeMedia();
    const mediaDuration = Number.isFinite(media?.duration) ? media.duration : 0;
    return Math.max(...cues.map(cue => Number(cue.end)), mediaDuration, 0.001);
  }

  function seekTimeline(event) {
    const track = $("#timelineTrack");
    const rect = track.getBoundingClientRect();
    if (!rect.width) return;
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const time = ratio * timelineDuration();
    const video = activeMedia();
    if (Number.isFinite(video.duration)) video.currentTime = Math.min(time, video.duration);
    const cue = state.view?.cue_views.find(item =>
      item.state === "active" && time >= Number(item.start) && time < Number(item.end)
    );
    if (cue) state.activeCueId = cue.cue_id;
    render();
  }

  async function createCueAtTimelinePoint(event) {
    event.preventDefault();
    const track = $("#timelineTrack");
    const rect = track.getBoundingClientRect();
    if (!rect.width) return;
    const duration = timelineDuration();
    const start = Math.max(0, Math.min(duration, (event.clientX - rect.left) / rect.width * duration));
    const end = Math.max(start + 0.1, Math.min(duration || start + 2, start + 2));
    await insertTimelineCue(start, end);
  }

  function render({deferTimeline = false, preserveCueViewport = true} = {}) {
    renderHeader();
    renderCues({preservePage:preserveCueViewport});
    renderSelectionActions();
    renderPreview();
    renderTimeline({deferDraw:deferTimeline});
    renderNavigators();
    document.documentElement.style.setProperty("--editor-token-font-size", `${state.fontSize}px`);
    const fontValue = $("#fontValue");
    if (fontValue) fontValue.textContent = String(state.fontSize);
  }

  let cueCenterGeneration = 0;
  function centerCueInList(cue) {
    const generation = ++cueCenterGeneration;
    if (!cue) return;
    if (!document.querySelector(`.cue-row[data-cue-id="${CSS.escape(cue.cue_id)}"]`)) {
      const index = state.view?.cue_views.findIndex(item => item.cue_id === cue.cue_id) ?? -1;
      if (index >= 0) {
        state.cuePageStart = Math.max(0, index - Math.floor(CUE_PAGE_SIZE / 3));
        renderCues();
      }
    }
    const row = document.querySelector(`.cue-row[data-cue-id="${CSS.escape(cue.cue_id)}"]`);
    const list = $("#cueList");
    if (!row || !list) return;
    const rowRect = row.getBoundingClientRect();
    const listRect = list.getBoundingClientRect();
    // Centering is invoked only on a cue transition or a completed seek.
    const centerOffset = (listRect.height - rowRect.height) / 2;
    list.scrollTop += rowRect.top - listRect.top - centerOffset;
    // Offscreen rows use content-visibility estimates. Re-measure after layout
    // reveals their real heights, without overriding a newer seek or user scroll.
    let expectedScroll = list.scrollTop;
    const settle = remaining => requestAnimationFrame(() => {
      if (generation !== cueCenterGeneration || state.activeCueId !== cue.cue_id
          || Math.abs(list.scrollTop - expectedScroll) > 1 || !row.isConnected) return;
      const rect = row.getBoundingClientRect(), bounds = list.getBoundingClientRect();
      list.scrollTop += rect.top - bounds.top - (bounds.height - rect.height) / 2;
      expectedScroll = list.scrollTop;
      if (remaining > 1) settle(remaining - 1);
    });
    settle(3);
  }

  function activateCue(cueId, {
    seek = false,
    scroll = false,
    revealTimeline = false,
    centerTimeline = false,
    explicitNavigation = false
  } = {}) {
    state.activeCueId = cueId || null;
    const cue = currentCue();
    const video = activeMedia();
    if (seek && cue && Number.isFinite(video.duration)) video.currentTime = Number(cue.start);
    state.cueListView?.setActive(state.activeCueId);
    state.timelineController?.setActiveCue(state.activeCueId);
    if (cue && revealTimeline) state.timelineController?.revealTime(Number(cue.start), centerTimeline);
    renderPreview();
    const isEditingText = document.activeElement?.matches?.(
      'textarea, [contenteditable="true"], input[type="text"], input[type="search"], input[type="number"], input[type="email"], input[type="url"], input[type="password"]'
    );
    // Pressing Enter in the search box is still an explicit navigation action.
    // Keep the broader guard for in-place edits, where playback-follow scrolling
    // would otherwise pull the row away while the user is typing.
    const searchHasFocus = document.activeElement === $("#toolSearch");
    if (scroll && cue && (explicitNavigation || !isEditingText || searchHasFocus)) centerCueInList(cue);
  }

  function selectCue(cueId, seek = false, scroll = false) {
    if (seek) syncPlaybackFollowAfterSeek();
    else suspendPlaybackFollow();
    activateCue(cueId, {seek, scroll, revealTimeline:true, centerTimeline:seek, explicitNavigation:seek});
  }

  function orderedSelectableTokenIds() {
    return state.indexes?.activeTokenOrder || [];
  }

  function refreshTokenSelectionUi() {
    document.querySelectorAll(".display-token[data-token-id]").forEach(node => {
      node.classList.toggle("selected", state.selectedTokenIds.has(node.dataset.tokenId));
    });
    document.querySelectorAll(".cue-row[data-cue-id]").forEach(node => {
      node.classList.toggle("current", node.dataset.cueId === state.activeCueId);
    });
    renderPreview();
    renderSelectionActions();
  }

  function selectToken(tokenId, {toggle = false, range = false} = {}) {
    const token = state.indexes?.tokenById?.get(tokenId);
    if (!token) return;
    state.cueSelection.clear();
    refreshCueSelection();
    // Deleted display tokens remain selectable so the selection menu can
    // expose the independent “恢复” operation.  They must not enter active
    // token range selection, merge, delete, or AI-calibration actions.
    if (token.state === "deleted") {
      if (toggle) {
        if (state.selectedTokenIds.has(tokenId)) state.selectedTokenIds.delete(tokenId);
        else state.selectedTokenIds.add(tokenId);
      } else {
        state.selectedTokenIds = new Set([tokenId]);
      }
      state.selectionAnchorTokenId = tokenId;
      const cueId = state.indexes?.tokenToCueId?.get(tokenId);
      suspendPlaybackFollow();
      if (cueId) activateCue(cueId);
      refreshTokenSelectionUi();
      return;
    }
    const ordered = orderedSelectableTokenIds();
    const positions = state.indexes?.activeTokenPosition;
    if (!positions?.has(tokenId)) return;
    if (range && state.selectionAnchorTokenId && positions.has(state.selectionAnchorTokenId)) {
      const start = positions.get(state.selectionAnchorTokenId);
      const end = positions.get(tokenId);
      state.selectedTokenIds.clear();
      ordered.slice(Math.min(start, end), Math.max(start, end) + 1)
        .forEach(id => state.selectedTokenIds.add(id));
    } else if (toggle) {
      if (state.selectedTokenIds.has(tokenId)) state.selectedTokenIds.delete(tokenId);
      else state.selectedTokenIds.add(tokenId);
      state.selectionAnchorTokenId = tokenId;
    } else {
      state.selectedTokenIds = new Set([tokenId]);
      state.selectionAnchorTokenId = tokenId;
    }
    const cueId = state.indexes?.tokenToCueId?.get(tokenId);
    suspendPlaybackFollow();
    if (cueId) activateCue(cueId);
    refreshTokenSelectionUi();
  }

  function marqueeRect(startX, startY, endX, endY) {
    return {
      left:Math.min(startX, endX), top:Math.min(startY, endY),
      right:Math.max(startX, endX), bottom:Math.max(startY, endY),
      width:Math.abs(endX - startX), height:Math.abs(endY - startY)
    };
  }

  function askText(title, label, value = "") {
    $("#textDialogTitle").textContent = title;
    $("#textDialogLabel").textContent = label;
    $("#textDialogValue").value = value;
    $("#textDialog").classList.remove("hidden");
    requestAnimationFrame(() => $("#textDialogValue").focus());
    return new Promise(resolve => { state.dialogResolver = resolve; });
  }

  function closeTextDialog(value = null) {
    $("#textDialog").classList.add("hidden");
    const resolve = state.dialogResolver;
    state.dialogResolver = null;
    resolve?.(value);
  }

  function askInstruction(title, label, value = "") {
    $("#instructionDialogTitle").textContent = title;
    $("#instructionDialogLabel").textContent = label;
    $("#instructionDialogValue").value = value;
    $("#instructionDialog").classList.remove("hidden");
    requestAnimationFrame(() => $("#instructionDialogValue").focus());
    return new Promise(resolve => { state.instructionResolver = resolve; });
  }

  function closeInstructionDialog(value = null) {
    $("#instructionDialog").classList.add("hidden");
    const resolve = state.instructionResolver;
    state.instructionResolver = null;
    resolve?.(value);
  }

  function appliedOperationIds(revision) {
    const ids = new Set();
    (revision?.document?.changes || []).forEach(change => {
      const metadata = change?.metadata || {};
      if (metadata.operation_id) ids.add(String(metadata.operation_id));
      (metadata.operation_ids || []).forEach(id => ids.add(String(id)));
    });
    return ids;
  }

  const TOPOLOGY_OPERATION_TYPES = new Set([
    "insert_cue", "purge_cue", "split_cue", "merge_cues", "retime_text", "merge", "auto_merge_cues"
  ]);

  function isTopologyOperation(operation) {
    return TOPOLOGY_OPERATION_TYPES.has(String(operation?.type || ""));
  }

  function isDanglingEntityOperation(error) {
    return error?.status === 422
      && error?.code === "invalid_operation_batch"
      && /unknown (?:cue|display token|source token):/i.test(String(error.message || ""));
  }

  function ensureOperationQueue() {
    if (state.operationQueue && state.operationQueueProjectId === state.projectId) {
      return state.operationQueue;
    }
    state.operationQueue?.destroy();
    const factory = window.EditorOperationQueue;
    if (!factory?.createOperationQueue) throw new Error("编辑队列未加载，请刷新后重试");
    const journalKey = `substar.editor.operations:${state.projectId}:${state.revision.document.document_id}`;
    let initialOperations = [];
    try {
      const saved = JSON.parse(localStorage.getItem(journalKey) || "[]");
      if (Array.isArray(saved)) initialOperations = saved;
    } catch (_) { initialOperations = []; }
    if (initialOperations.length) {
      const projection = state.documentStore?.restore(initialOperations);
      if (projection) setRevision(projection, {localProjection:true});
    }
    const queueProjectId = state.projectId;
    const queueEpoch = projectEpoch;
    const queueCurrent = () => queueProjectId === state.projectId && queueEpoch === projectEpoch;
    const queuePath = suffix => `/api/projects/${encodeURIComponent(queueProjectId)}${suffix || ""}`;
    let wasBusy = false;
    state.operationQueueProjectId = state.projectId;
    state.operationQueue = factory.createOperationQueue({
      groupByUserAction:false,
      preserveUserActions:true,
      debounceMs:(state.taskInfo?.autosave_delay_seconds ?? 1) * 1000,
      maxBatchSize:100,
      maxRetries:state.taskInfo?.autosave_retry_count ?? 3,
      initialOperations,
      base() {
        const revision = state.documentStore?.acknowledged() || state.revision;
        return {
          document_id:revision.document.document_id,
          revision_id:revision.revision_id,
          document_hash:revision.document_hash
        };
      },
      async sendBatch(batch, {signal}) {
        try {
          const response = await api(queuePath("/operation-batches"), {
            method:"POST",
            signal,
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify(batch)
          });
          if (!queueCurrent()) throw new Error("保存会话已变化");
          if (signal.aborted) throw new Error("保存请求已超时，等待重试确认");
          const acknowledged = state.documentStore?.acknowledged() || state.revision;
          const operationIds = response.acknowledged_operation_ids
            || batch.operations.map(item => item.operation_id);
          let revision;
          try {
            revision = contract.applyRevisionDelta(acknowledged, response);
            const optimisticProjection = state.documentStore?.projection() || state.revision;
            const projection = state.documentStore?.acknowledge(
              revision, operationIds
            ) || revision;
            if (visibleRevisionSignature(optimisticProjection) === visibleRevisionSignature(projection)) {
              // The optimistic projection already produced exactly
              // the server-visible result. Adopt authoritative metadata only:
              // touching the DOM here would create a second visual jump.
              adoptRevisionWithoutRender(projection);
              state.skipNextQueueIdleTimelineRedraw = true;
            } else {
              setRevision(projection, {
                deferTimeline:activeMedia()?.paused === false,
                localProjection:true
              });
            }
          } catch (clientApplyError) {
            // The POST has already committed at this point. Recover from any
            // client delta, projection, or render failure using the server's
            // authoritative snapshot, and never leave the committed operation
            // in the retry journal.
            const latest = contract.consumeRevision(await api(queuePath(), {signal}));
            const applied = appliedOperationIds(latest);
            const receipts = new Set(response.acknowledged_operation_ids || []);
            if (!operationIds.every(id => receipts.has(id) || applied.has(id))) throw clientApplyError;
            revision = latest;
            const projection = state.documentStore?.acknowledge(latest, operationIds) || latest;
            try {
              setRevision(projection, {
                localProjection:true
              });
            } catch (authoritativeRenderError) {
              // Saving succeeded even if both incremental and clean rendering
              // failed. Clear the journal now and perform an automatic reload
              // once the queue has left its in-flight state.
              state.documentStore?.replaceAcknowledged(latest);
              scheduleAuthoritativeResync("编辑已保存，界面状态正在自动恢复");
              console.error("Authoritative editor render failed", authoritativeRenderError);
            }
          }
          for (const boundary of response.action_revisions || []) {
            if (boundary.revision_number > acknowledged.revision_number && boundary.revision_id !== revision.revision_id) {
              recordCommittedRevision(boundary, boundary.provenance);
            }
          }
          recordCommittedRevision(revision, revision.provenance || {
            kind:"manual",
            operation:"operation_batch",
            metadata:{
              batch_id:batch.batch_id,
              user_action:batch.operations.length === 1 ? batch.operations[0].type : "batch_edit",
              operation_ids:operationIds
            }
          });
          if (revision.revision_id !== acknowledged.revision_id
            && revision.parent_revision_id !== acknowledged.revision_id
            && !(response.action_revisions || []).some(item => item.parent_revision_id === acknowledged.revision_id)) {
            await loadRevisionHistory({force:true});
          }
          observeEditorTutorialOperations(batch.operations);
          return revision;
        } catch (error) {
          if (isDanglingEntityOperation(error)) {
            // A rejected batch is atomic: keep every operation and its recovery journal.
            error.message = "部分编辑引用的字幕已变化；整批输入仍已保留，请处理冲突";
            throw error;
          }
          if (error.status !== 409) throw error;
          const latestPayload = await api(queuePath(), {signal});
          const latest = contract.consumeRevision(latestPayload);
          const guarded = batch.operations.every(operation =>
            (operation.type === "replace" && Object.hasOwn(operation.payload, "expected_text"))
            || (operation.type === "set_target" && Object.hasOwn(operation.payload, "expected_target_text")));
          const alreadyApplied = batch.operations.every(operation => appliedOperationIds(latest).has(operation.operation_id));
          if (!guarded && !alreadyApplied) throw error;
          const projection = state.documentStore?.replaceAcknowledged(latest) || latest;
          setRevision(projection, {
            localProjection:true
          });
          recordCommittedRevision(latest, latest.provenance || {
            kind:"manual", operation:"conflict_reconcile", metadata:{}
          });
          const applied = appliedOperationIds(latest);
          if (batch.operations.every(operation => applied.has(operation.operation_id))) {
            const reconciled = state.documentStore?.acknowledge(
              latest, batch.operations.map(operation => operation.operation_id)
            ) || latest;
            setRevision(reconciled, {
              localProjection:true
            });
            return latest;
          }
          throw error;
        }
      },
      shouldRetry(error) {
        return !error?.status || error.status >= 500;
      },
      onStatus(status) {
        if (!queueCurrent()) return;
        state.operationPending = status.hasUnsavedChanges;
        $("#saveRecoveryActions")?.classList.toggle("hidden", !status.failed);
        renderHeader();
        if (wasBusy && !status.busy) {
          if (state.skipNextQueueIdleTimelineRedraw) {
            state.skipNextQueueIdleTimelineRedraw = false;
          } else {
            state.timelineController?.redraw();
          }
        }
        wasBusy = status.busy;
      },
      onJournal(operations) {
        try {
          if (operations.length) localStorage.setItem(journalKey, JSON.stringify(operations));
          else localStorage.removeItem(journalKey);
        } catch (_) { ordinaryError("浏览器恢复记录写入失败，编辑仅保留在内存中，请保持此页面打开并完成保存"); }
      },
      onFailed(error) {
        if (!queueCurrent()) return;
        ordinaryError(`编辑尚未保存：${error.message}。操作已保留，可刷新前重试。`);
      },
      onObserverError(error, name) {
        console.error(`Editor queue ${name} failed`, error);
        scheduleAuthoritativeResync("保存状态界面正在自动恢复");
      }
    });
    return state.operationQueue;
  }

  async function sendOperation(operation) {
    ordinaryError("");
    suspendPlaybackFollow();
    if (editorAiTaskLocksEditor()) {
      const kind = {translation:"翻译", calibration:"校准"}[state.editorAiTask?.kind] || "AI";
      ordinaryError(`${kind}任务正在排队或执行中，完成前编辑器为只读`);
      return null;
    }
    if (state.restoreWriting) { ordinaryError("正在恢复版本，请稍候再编辑"); return null; }
    if (state.documentWriteLocked) { ordinaryError("正在保存文档设置，请稍候再编辑"); return null; }
    const topology = isTopologyOperation(operation);
    if (state.topologyOperationPending || (topology && state.structuralPreparationPending)) {
      ordinaryError("Cue 结构正在更新，请稍候再编辑");
      return null;
    }
    let topologyLocked = false;
    try {
      if (topology) {
        await flushActiveEditorInput();
        if (state.topologyOperationPending || state.structuralPreparationPending) {
          ordinaryError("Cue 结构正在更新，请稍候再编辑");
          return null;
        }
        topologyLocked = true;
        state.structuralPreparationPending = true;
        state.topologyOperationPending = true;
      }
      const projection = state.documentStore?.enqueue(operation);
      const saving = ensureOperationQueue().enqueue(operation);
      if (topology) ensureOperationQueue().flushNow();
      saving.catch(() => {});
      if (projection) {
        setRevision(projection, {
          deferTimeline:activeMedia()?.paused === false,
          localProjection:true
        });
      }
      return await saving;
    } catch (error) {
      ordinaryError(`编辑尚未保存：${error.message}`);
      return null;
    } finally {
      if (topologyLocked) { state.topologyOperationPending = false; state.structuralPreparationPending = false; }
    }
  }

  function manualProvenance(operation) {
    return {kind:"manual", operation, metadata:{surface:"editor"}};
  }

  async function chooseReferenceVersion(useReference) {
    const selected = selectedReferenceChange();
    if (!selected || state.operationPending) return;
    const {change, token, tokenId} = selected;
    const type = String(change.type || "replace");
    try {
      if (type === "replace") {
        const text = String(useReference ? change.after : change.before);
        if (!text || text === token.text) return;
        await sendOperation(contract.replaceOperation(
          state.revision, tokenId, text,
          manualProvenance(useReference ? "reference_apply" : "reference_keep_asr")
        ));
      } else if (type === "insert") {
        const operation = useReference
          ? contract.restoreOperation(state.revision, {token_ids:[tokenId]}, manualProvenance("reference_apply"))
          : contract.deleteOperation(state.revision, {token_ids:[tokenId]}, manualProvenance("reference_keep_asr"));
        await sendOperation(operation);
      }
    } catch (error) {
      ordinaryError(error?.message || String(error));
    }
  }

  function beginWholeCueEdit(connector, selectAll = false) {
    if (state.topologyOperationPending || editorAiTaskLocksEditor()) return;
    const cue = state.view?.cue_views.find(item => item.cue_id === connector.dataset.cueId);
    const line = connector.closest(".source-token-line");
    if (!cue || cue.state !== "active" || !line || line.querySelector("textarea")) return;
    const original = cueSourceText(cue);
    const input = document.createElement("textarea");
    input.className = "translation-track whole-cue-editor";
    input.setAttribute("aria-label", "整行编辑原文");
    input.rows = 1;
    input.value = original;
    input.dataset.originalEdit = original;
    const resize = () => { input.style.height = "auto"; input.style.height = `${Math.max(38,input.scrollHeight)}px`; };
    line.hidden = true;
    line.after(input);
    let finished = false;
    const finish = async (commit = true) => {
      if (finished) return;
      finished = true;
      const text = input.value.trim();
      if (!commit || !text || text === original) {
        input.remove(); line.hidden = false; return;
      }
      input.disabled = true;
      try {
        const ids = cue.display_token_ids;
        const provenance = manualProvenance("whole_cue_edit");
        const operation = contract.retimeTextOperation(state.revision, cue.cue_id, ids, text, provenance);
        const saved = await sendOperation(operation);
        if (!saved && input.isConnected) {
          finished = false; input.disabled = false; input.focus({preventScroll:true});
        }
        return saved;
      } catch (error) {
        finished = false; input.disabled = false;
        ordinaryError(`编辑尚未保存：${error.message}`);
        input.focus({preventScroll:true});
        return null;
      }
    };
    input.addEventListener("input", resize);
    input.addEventListener("blur", () => { input.editorCommit = finish(true); });
    input.addEventListener("keydown", event => {
      event.stopPropagation();
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); input.blur(); }
    });
    input.addEventListener("click", event => event.stopPropagation());
    resize(); input.focus({preventScroll:true});
    input.setSelectionRange(selectAll ? 0 : input.value.length,input.value.length);
  }

  function beginInlineTokenEdit(button) {
    if (state.topologyOperationPending || editorAiTaskLocksEditor()) return;
    const tokenId = button?.dataset.tokenId;
    const token = state.view?.token_views.find(item => item.token_id === tokenId);
    const label = button?.querySelector("b");
    if (!token || !label || button.querySelector("input,textarea")) return;
    const input = document.createElement("textarea");
    input.className = "inline-token-editor";
    input.value = token.text;
    input.dataset.originalEdit = token.text;
    input.rows = 1;
    const content = button.closest(".cue-content");
    const oldMaxWidth = button.style.maxWidth;
    button.style.maxWidth = "100%";
    const fontSize = parseFloat(getComputedStyle(label).fontSize) || state.fontSize || 18;
    const resize = () => {
      const available = Math.max(80, (content?.clientWidth || 400) - 16);
      const desired = Math.max(64, [...input.value].reduce((n,char) => n + (char.charCodeAt(0)>255 ? 1 : .65),0)*fontSize + 28);
      input.style.width = `${Math.min(available,desired)}px`;
      input.style.height = "auto";
      input.style.height = `${Math.max(36,input.scrollHeight)}px`;
    };
    Object.assign(input.style, {boxSizing:"border-box", minWidth:"0", maxWidth:"100%",
      height:`${Math.max(36,fontSize*1.8)}px`, padding:"5px 12px 5px 8px"});
    input.setAttribute("aria-label", "编辑词元文字");
    input.addEventListener("input", resize);
    resize();
    label.replaceWith(input);
    resize();
    let finished = false;
    const finish = async (commit = true) => {
      if (finished) return;
      finished = true;
      const text = input.value.trim();
      button.style.maxWidth = oldMaxWidth;
      if (!commit || !text || text === token.text) {
        // A cancelled/no-op edit does not change the document, so do not
        // rebuild the Cue list at all. Reusing the original label also avoids
        // the browser's focus-restoration scroll after the input disappears.
        label.textContent = token.text;
        input.replaceWith(label);
        if (button.closest(".cue-row")?.dataset.deferredRefresh) renderCues({preservePage:true});
        return;
      }
      await sendOperation(contract.replaceOperation(
        state.revision, tokenId, text, manualProvenance("inline_replace"), {expected_text:token.canonical_text ?? token.text}
      ));
    };
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); input.blur(); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
      event.stopPropagation();
    });
    input.addEventListener("blur", () => finish(true), {once:true});
    input.addEventListener("click", event => event.stopPropagation());
    input.focus({preventScroll:true});
    input.select();
  }

  async function replaceCurrentSearch() {
    const beforeRevisionId = state.revision?.revision_id || "";
    const matches = searchMatches();
    const replacement = String($("#toolReplace")?.value || "");
    const deleting = !replacement.trim();
    if (!matches.length) {
      ordinaryError(`没有找到${state.searchScope === "target" ? "译文" : "源语词元"}匹配`);
      return;
    }
    let index = state.searchIndex;
    if (index < 0 || index >= matches.length) index = 0;
    const match = matches[index];
    updateSearchSelection(match, index, matches.length);
    const cue = state.view?.cue_views.find(item => item.cue_id === match.cue_id);
    if (!cue) return;
    const query = String($("#toolSearch")?.value || "").trim();
    if (match.kind === "target") {
      const current = String(cue.target?.target_text || "");
      const next = replaceLiteralOnce(current, query, deleting ? "" : replacement);
      if (next !== current) {
        const revision = await sendOperation(contract.setTargetOperation(
          state.revision, cue.cue_id, next,
          manualProvenance("search_replace_target"), {original_text:current}
        ));
        rememberSearchReplaceUndo(beforeRevisionId, revision);
      }
      return;
    }
    const tokenIds = match.token_ids || [];
    if (deleting) {
      const revision = await sendOperation(contract.deleteOperation(
        state.revision, {token_ids:tokenIds}, manualProvenance("search_delete")
      ));
      rememberSearchReplaceUndo(beforeRevisionId, revision);
      return;
    }
    if (tokenIds.length > 1) {
      const source = String(match.combined_text || "");
      const start = Number(match.start_offset || 0);
      const end = Number.isFinite(Number(match.end_offset)) ? Number(match.end_offset) : source.length;
      const next = `${source.slice(0, start)}${replacement}${source.slice(end)}`;
      const revision = await sendOperation(contract.mergeOperation(
        state.revision, tokenIds, next,
        manualProvenance("search_replace_phrase")
      ));
      rememberSearchReplaceUndo(beforeRevisionId, revision);
      return;
    }
    const token = state.view?.token_views.find(item => item.token_id === tokenIds[0]);
    if (!token) return;
    const next = replaceLiteralOnce(token.text, query, replacement);
    const revision = await sendOperation(contract.replaceOperation(
      state.revision, token.token_id, next, manualProvenance("search_replace")
    ));
    rememberSearchReplaceUndo(beforeRevisionId, revision);
  }

  async function replaceAllSearch() {
    const beforeRevisionId = state.revision?.revision_id || "";
    const matches = searchMatches();
    const replacement = String($("#toolReplace")?.value || "");
    const deleting = !replacement.trim();
    if (!matches.length) {
      ordinaryError(`没有找到${state.searchScope === "target" ? "译文" : "源语词元"}匹配`);
      return;
    }
    const query = String($("#toolSearch")?.value || "").trim();
    if (state.searchScope === "target") {
      const operations = [];
      for (const match of matches) {
        const cue = state.view?.cue_views.find(item => item.cue_id === match.cue_id);
        if (!cue?.target) continue;
        const current = String(cue.target.target_text || "");
        const next = replaceLiteralAll(current, query, deleting ? "" : replacement);
        if (next !== current) {
          operations.push(contract.setTargetOperation(
            state.revision, cue.cue_id, next,
            manualProvenance("search_replace_all_target"), {original_text:current}
          ));
        }
      }
      if (operations.length) {
        const revisions = await Promise.all(operations.map(sendOperation));
        rememberSearchReplaceUndo(beforeRevisionId, revisions.filter(Boolean).at(-1));
      }
      return;
    }
    if (deleting) {
      const tokenIds = [...new Set(matches.flatMap(match => match.token_ids || []))];
      if (tokenIds.length) {
        const revision = await sendOperation(contract.deleteOperation(
          state.revision, {token_ids:tokenIds}, manualProvenance("search_delete_all")
        ));
        rememberSearchReplaceUndo(beforeRevisionId, revision);
      }
      return;
    }
    const phraseMatches = matches.filter(match => (match.token_ids || []).length > 1);
    if (phraseMatches.length) {
      const operations = phraseMatches.map(match => {
        const source = String(match.combined_text || "");
        const start = Number(match.start_offset || 0);
        const end = Number.isFinite(Number(match.end_offset)) ? Number(match.end_offset) : source.length;
        return contract.mergeOperation(
          state.revision, match.token_ids,
          `${source.slice(0, start)}${replacement}${source.slice(end)}`,
          manualProvenance("search_replace_all_phrase")
        );
      });
      const revisions = await Promise.all(operations.map(sendOperation));
      rememberSearchReplaceUndo(beforeRevisionId, revisions.filter(Boolean).at(-1));
      return;
    }
    const replacements = matches.map(match => {
      const token = state.view.token_views.find(item => item.token_id === match.token_ids[0]);
      const next = replaceLiteralAll(token.text, query, replacement);
      return {token_id:match.token_ids[0], text:next, expected_text:token.text};
    });
    const revision = await sendOperation(contract.batchReplaceOperation(
      state.revision,
      replacements,
      manualProvenance("search_replace_all")
    ));
    rememberSearchReplaceUndo(beforeRevisionId, revision);
  }

  function rememberSearchReplaceUndo(beforeRevisionId, revision) {
    const afterRevisionId = revision?.revision_id || "";
    if (!beforeRevisionId || !afterRevisionId || beforeRevisionId === afterRevisionId) return;
    state.searchReplaceUndo = {
      projectId:state.projectId,
      before:beforeRevisionId,
      after:afterRevisionId
    };
    renderHeader();
  }

  async function undoSearchReplace() {
    const entry = state.searchReplaceUndo;
    if (!entry || entry.projectId !== state.projectId
      || entry.after !== state.revision?.revision_id || state.operationPending) return;
    $("#toolUndoReplace").disabled = true;
    const restored = await restoreRevision(entry.before);
    if (restored) {
      state.searchReplaceUndo = null;
      ordinaryError("已撤销最近一次查找替换", "notice");
    }
    renderHeader();
  }

  function replaceLiteralOnce(text, query, replacement) {
    const source = String(text || "");
    const needle = String(query || "");
    if (!needle) return source;
    const index = source.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase());
    return index < 0
      ? source
      : `${source.slice(0, index)}${replacement}${source.slice(index + needle.length)}`;
  }

  function replaceLiteralAll(text, query, replacement) {
    const source = String(text || "");
    const needle = String(query || "");
    if (!needle) return source;
    const lowerSource = source.toLocaleLowerCase();
    const lowerNeedle = needle.toLocaleLowerCase();
    let cursor = 0;
    let result = "";
    let index = lowerSource.indexOf(lowerNeedle, cursor);
    while (index >= 0) {
      result += source.slice(cursor, index) + replacement;
      cursor = index + needle.length;
      index = lowerSource.indexOf(lowerNeedle, cursor);
    }
    return result ? result + source.slice(cursor) : source;
  }

  async function applyBatchReplacements(replacements, origin, metadata = {}) {
    if (!replacements.length || !state.revision) return null;
    const revision = await api(projectPath("/batch-replace"), {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        expected_revision_id:state.revision.revision_id,
        operation_id:`op_batch_${Date.now().toString(36)}`,
        replacements,
        origin,
        metadata:{surface:"editor", ...metadata}
      })
    });
    setRevision(revision);
    recordCommittedRevision(state.revision, {
      kind:origin === "manual" ? "manual" : origin === "ai_calibration" ? "ai" : "import",
      operation:origin === "manual" ? "batch_replace" : `${origin}_apply`,
      metadata
    });
    return revision;
  }

  function configureTranslationLanguageDefaults(projectSettings = {}) {
    const sourceAliases = {
      zh:"zh-CN", "zh-cn":"zh-CN", en:"en", mixed:"mixed",
      ja:"ja", ko:"ko", auto:"Auto", automatic:"Auto"
    };
    const configuredSource = String(projectSettings.language || "Auto");
    $("#translationSourceLanguage").value =
      sourceAliases[configuredSource.toLowerCase()] || (window.SubstarTranslationLayout.languages.some(([code]) => code === configuredSource) ? configuredSource : "Auto");
    const configuredTarget = String(projectSettings.target_language_mode || "zh-CN");
    $("#translationTargetLanguage").value =
      window.SubstarTranslationLayout.languages.some(([code]) => code === configuredTarget)
        ? configuredTarget : "zh-CN";
    const layout = {...(projectSettings.translation_layout || {})};
    // Older layouts store routing languages, which are not source selections.
    if (!Array.isArray(layout.source_languages) && $("#translationSourceLanguage").value !== "Auto") {
      layout.source_languages = [$("#translationSourceLanguage").value];
      layout.auto_detect = false;
    }
    window.SubstarTranslationLayout.write("translationLanguageLayout", layout);
  }

  function openAiReview() {
    const menu = $("#aiReviewMenu");
    if (menu && !menu.classList.contains("disabled")) menu.open = true;
  }

  function selectedReviewCueIds() {
    const cueIds = new Set();
    state.selectedTokenIds.forEach(tokenId => {
      const cueId = state.indexes?.tokenToCueId?.get(tokenId);
      if (cueId) cueIds.add(cueId);
    });
    return [...cueIds];
  }

  function buildExternalReviewContent() {
    if (!state.revision || !state.view || !externalReview) {
      throw new Error("请先打开字幕工程");
    }
    return externalReview.build({
      cues:state.view.cue_views,
      scope:$(".external-review-scope-tabs [aria-pressed='true']")?.dataset.reviewScope || "current",
      rangeExpression:$("#externalReviewRange")?.value || "",
      currentCueId:state.activeCueId,
      selectedCueIds:selectedReviewCueIds(),
      instruction:$("#externalReviewInstruction")?.value || "",
      sourceText:cueSourceText,
      targetText:cue => languageLayout.formatText(
        cue.target?.target_text || "", state.trackLanguages?.target
      )
    });
  }

  async function copyExternalReviewContent() {
    try {
      const built = buildExternalReviewContent();
      await navigator.clipboard.writeText(built.text);
      ordinaryError("审阅内容已复制，可以粘贴到任意 Web AI。", "completed");
      if (isAdvancedTutorial() && state.tutorial.active && state.tutorial.step === 6) {
        advanceEditorTutorial(6);
      }
    } catch (error) {
      ordinaryError(`复制失败：${error.message}`);
    }
  }

  async function downloadExternalReviewContent() {
    try {
      const built = buildExternalReviewContent();
      const safeProjectId = systemSaveAs.safeFilename(state.projectId || "substar");
      const result = await systemSaveAs.saveBlob({
        suggestedName:`${safeProjectId}_外部AI审阅.txt`,
        description:"TXT 文本文档",
        mimeType:"text/plain",
        extension:".txt"
      }, new Blob([built.text], {type:"text/plain;charset=utf-8"}));
      if (result.cancelled) return;
      ordinaryError(`已保存：${result.filename}`, "completed");
    } catch (error) {
      ordinaryError(`下载失败：${error.message}`);
    }
  }

  async function runAiCalibration() {
    if (!state.revision || state.operationPending) return;
    if (isAdvancedTutorial()) {
      $("#aiCalibrationMenu").open = false;
      return runPackagedTutorialStage("calibration");
    }
    state.operationPending = true;
    const instruction = $("#aiCalibrationInstruction")?.value.trim() || "";
    $("#aiCalibrationMenu").open = false;
    renderHeader();
    renderWorkbenchTask("AI 校准", 2, "AI 校准准备中…");
    try {
      const result = await withSavedDocument(() => api(projectPath("/ai-calibrate"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          instruction
        })
      }));
      state.editorAiTask = {...result, kind:"calibration"};
      document.body.classList.add("editor-ai-task-locked");
      renderWorkbenchTask("AI 校准", 0, "等待任务调度…", result.state);
      startEditorAiTaskPoll();
    } catch (error) {
      if (error.code === "editor_ai_task_cancelled") {
        renderWorkbenchTask("AI 校准", 0, "任务已取消", "cancelled");
      } else {
        renderWorkbenchTask("AI 校准", 100, error.message, "failed");
      }
    } finally {
      state.operationPending = false;
      renderHeader();
    }
  }

  async function convertScript(target) {
    if (!state.revision || state.operationPending) return;
    state.operationPending = true;
    renderHeader();
    renderWorkbenchTask("繁简转换", 10, "正在切换显示与导出投影", "running");
    try {
      const revision = await withSavedDocument(() => api(projectPath("/convert-script"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          target
        })
      }));
      setRevision(revision);
      recordCommittedRevision(state.revision, {
        kind:"manual", operation:"set_script_projection", metadata:{target}
      });
      renderWorkbenchTask("繁简转换", 100, "已切换；正文未改写，显示与导出已更新", "completed");
    } catch (error) {
      ordinaryError(`繁简转换失败：${error.message}`);
      renderWorkbenchTask("繁简转换", 0, error.message, "failed");
    } finally {
      state.operationPending = false;
      renderHeader();
    }
  }

  function chooseReferenceFile() {
    return new Promise(resolve => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = ".txt,.docx,.srt,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
      input.onchange = () => resolve(input.files?.[0] || null);
      input.click();
    });
  }

  async function runReferenceManuscript() {
    if (!state.revision || state.operationPending) return;
    const file = await chooseReferenceFile();
    if (!file) return;
    state.operationPending = true;
    renderHeader();
    renderWorkbenchTask("参考文稿", 20, `正在匹配 ${file.name}…`);
    const form = new FormData();
    form.append("expected_revision_id", state.revision.revision_id);
    form.append("file", file, file.name);
    try {
      const result = await withSavedDocument(() => {
        form.set("expected_revision_id", state.revision.revision_id);
        return api(projectPath("/reference-manuscript"), {method:"POST", body:form});
      });
      if (result.revision?.revision_id !== state.revision.revision_id) {
        setRevision(result.revision);
        recordCommittedRevision(state.revision, {
          kind:"import", operation:"reference_manuscript_apply", metadata:{}
        });
      }
      const similarity = Number(result.match?.similarity || 0);
      if (result.match?.requires_review) ordinaryError("参考稿有局部待确认内容，请复核标记；未确定对应的参考文字已默认隐藏。");
      renderWorkbenchTask(
        "参考文稿", 100,
        `已匹配并标记 ${result.applied || 0} 个词元 · 相似度 ${(similarity * 100).toFixed(1)}%`,
        "completed"
      );
    } catch (error) {
      renderWorkbenchTask("参考文稿", 100, error.message, "failed");
    } finally {
      state.operationPending = false;
      renderHeader();
    }
  }

  async function commitInlineTarget(field) {
    if (!field || field.dataset.committing === "true") return;
    if (state.topologyOperationPending) return;
    const cueId = field.dataset.targetEdit;
    const cue = state.view?.cue_views.find(item => item.cue_id === cueId);
    if (!cue) return;
    const current = field.dataset.originalTarget || "";
    const text = field.value.trim();
    if (text === current) {
      field.dataset.rawEditing = "false";
      field.value = field.dataset.projectedTarget || "";
      resizeTranslationField(field);
      return;
    }
    field.dataset.committing = "true";
    try {
      const revision = await sendOperation(contract.setTargetOperation(
        state.revision, cueId, text, manualProvenance("inline_edit_target"), {
          expected_target_text:field.dataset.expectedTarget ?? current,
          original_text:cue.target?.original_text || current,
          language:cue.target?.language || "zh-CN"
        }
      ));
      if (!revision && field.isConnected) {
        field.dataset.rawEditing = "true";
        field.value = text;
        resizeTranslationField(field);
      }
    } finally {
      if (field.isConnected) field.dataset.committing = "false";
    }
  }

  async function editCueTime(cueId) {
    const cue = state.view?.cue_views.find(item => item.cue_id === cueId);
    if (!cue) return;
    const value = await askText(
      "调整 cue 时间", "开始秒数,结束秒数",
      `${Number(cue.start).toFixed(3)},${Number(cue.end).toFixed(3)}`
    );
    if (value === null) return;
    const parts = value.split(/[\s,，;；]+/).filter(Boolean).map(Number);
    if (parts.length !== 2 || parts.some(item => !Number.isFinite(item))) {
      ordinaryError("时间格式应为：开始秒数,结束秒数");
      return;
    }
    await state.cueTimeController?.commitCueTime(cueId, parts[0], parts[1]);
  }

  async function insertTimelineCue(start, end, speaker = null, overlay = false) {
    const experimental = document.documentElement.dataset.editorLayout === "experimental";
    const text = experimental ? "新字幕" : await askText("新建人工 cue", "字幕文字（独立字幕，不含词级时间）", "");
    if (!text?.trim()) return;
    const beforeIds = new Set(state.view.cue_views.map(cue => cue.cue_id));
    const saved = await sendOperation(contract.insertCueOperation(
      state.revision, start, end, text.trim(), manualProvenance("insert_cue"), {speaker,overlay}
    ));
    if (!saved) throw new Error('人工字幕未保存，请重试');
    if (!experimental) return;
    const created = state.view.cue_views.find(cue => !beforeIds.has(cue.cue_id));
    if (!created) return;
    selectCue(created.cue_id);
    const row = document.querySelector(`.cue-row[data-cue-id="${CSS.escape(created.cue_id)}"]`);
    const connector = row?.querySelector(".token-connector.cue-end");
    if (connector) beginWholeCueEdit(connector, true);
  }

  async function applyTimelineBoundary(intent) {
    if (!intent?.changes?.length || !state.cueTimeController) return;
    await state.cueTimeController.commitBoundaryIntent(intent, state.view?.cue_views || []);
  }

  async function applyTimelineAutoSnap(intent) {
    if (!intent?.changes?.length || !state.cueTimeController) return null;
    const result = await state.cueTimeController.commitAutoSnap(
      intent, state.view?.cue_views || []
    );
    if (result.revision) {
      state.autoSnapUndo = {
        before:result.beforeRevisionId,
        after:result.revision.revision_id
      };
      $("#undoAutoSnap").disabled = false;
    }
    return result.revision || null;
  }

  async function handleTimelineIntent(intent) {
    if (!intent) return;
    if (intent.type === "split_cue" || intent.type === "merge_cues") {
      try {
        if (intent.type === "split_cue") {
          await sendOperation(contract.splitCueOperation(state.revision,intent.cue_id,intent.after_token_id,manualProvenance("split_cue")));
        } else {
          const left=state.view.cue_views.find(c=>c.cue_id===intent.left_cue_id);
          const right=state.view.cue_views.find(c=>c.cue_id===intent.right_cue_id);
          if (!left || !right) return;
          if (left.speaker && right.speaker && left.speaker!==right.speaker &&
              !window.confirm("两个 Cue 属于不同说话人。继续合并会清空合并后 Cue 的说话人标签，是否继续？")) return;
          await sendOperation(contract.mergeCuesOperation(state.revision,left.cue_id,right.cue_id,manualProvenance("merge_cues")));
        }
      } catch(error) { ordinaryError(error.message); }
      return;
    }
    if (intent.type === "manual_cue_occupied") {
      state.timelineSelectedCueId = intent.cue_id;
      state.timelineController?.setSelectedCue(intent.cue_id);
      selectCue(intent.cue_id);
      ordinaryError("此处已有 Cue");
      return;
    }
    if (intent.type === "manual_cue_unavailable") {
      ordinaryError("此处没有可用的 Cue 间隙");
      return;
    }
    if (intent.type === "create_manual_cue") {
      await insertTimelineCue(intent.start, intent.end, intent.speaker || null);
      return;
    }
    if (intent.type === "delete_cue") {
      await cueAction(intent.cue_id, "hide");
      return;
    }
    if (intent.type === "auto_snap_once") {
      await applyTimelineAutoSnap(intent);
      return;
    }
    await applyTimelineBoundary(intent);
  }

  function initializeTimelineController() {
    const factory = window.EditorMawTimeline || window.EditorTimelineTools || (document.documentElement.dataset.editorLayout === "experimental"
      ? window.EditorMultilineTimeline : window.EditorTimeline);
    const canvas = $("#timelineCanvas");
    if (!factory?.createTimelineController || !canvas) return;
    state.timelineController?.destroy?.();
    state.waveformCache?.clear();
    state.waveformCache = window.EditorWaveformCache?.createWaveformCache?.({limit:16}) || null;
    state.timelineController = factory.createTimelineController({
      getRevisionId:() => state.revision?.revision_id,
      onWaveformPayload:({signal} = {}) => api(projectPath('/waveform-maw'), {signal}),
      onDetailWaveformPayload:({signal} = {}) => api(projectPath('/waveform-maw-detail'), {signal}),
      async onCommitTimelineTimes(changes,wordTimings) {
        const operation=contract.setCueTimesOperation(state.revision,changes,manualProvenance('maw_waveform'));
        if(wordTimings)operation.payload.word_timings=wordTimings;
        const saved = await sendOperation(operation);
        if (!saved) throw new Error('字幕时间未保存，请重试');
      },
      onNativeMawOperation:async(type,payload) => {
        const operation=type==='hide_cues'?contract.deleteOperation(state.revision,{cue_ids:payload.cue_ids},manualProvenance('maw_hide_cues')):contract.createOperation(state.revision,type,{...payload,provenance:manualProvenance(type)});
        if(!await sendOperation(operation))throw Error('修改未保存，请重试');
      },
      async onTimelineSpeakerChange(cueIds,speaker) {
        const changed=[...new Set(cueIds)].filter(id=>state.view.cue_views.find(cue=>cue.cue_id===id)?.speaker!==speaker);
        if(changed.length&&!await sendOperation(contract.setCueSpeakersOperation(state.revision,changed,speaker,manualProvenance('set_cue_speaker'))))throw Error('说话人未保存，请重试');
        if(document.querySelector('#editor-dubbing-tool-content')?.closest('.experimental-tool-active'))populateSpeakerSettings();
      },
      onUndo:() => $('#undoDocument').click(),
      onRedo:() => $('#redoDocument').click(),
      onCreateTimelineCue:insertTimelineCue,
      getTimelineCueText:cueSourceText,
      async onTimelineTextEdit({cueId,tokenId,text,track}) {
        if(state.topologyOperationPending||editorAiTaskLocksEditor())throw Error('请等待当前任务完成');
        const cue=state.view.cue_views.find(c=>c.cue_id===cueId);
        if(!cue)throw Error('字幕已不存在');
        if(track==='extension') {
          if(!await sendOperation(contract.setTargetOperation(state.revision,cueId,text,manualProvenance('maw_translation_edit'))))throw Error('译文未保存，请重试');
          return;
        }
        const operation=tokenId?contract.replaceOperation(state.revision,tokenId,text,manualProvenance('inline_replace')):cue.source_text!=null?contract.createOperation(state.revision,'set_source_text',{cue_id:cueId,text,provenance:manualProvenance('maw_manual_edit')}):contract.retimeTextOperation(state.revision,cueId,cue.display_token_ids,text,manualProvenance('whole_cue_edit'));
        if(!await sendOperation(operation))throw Error('编辑尚未保存，请重试');
      },
      async onCreateSpeaker(name) {
        const id=window.EditorSpeakers.nextId(window.EditorSpeakers.ids(state.view));
        const names={...state.view.properties.speaker_names,[id]:name};
        await sendOperation(contract.setSpeakerNamesOperation(state.revision,names,manualProvenance("set_speaker_names")));
        if(state.view.properties.speaker_names?.[id]!==name)throw new Error("轨道未保存，请重试");
        return id;
      },
      async onTimelineChange(timeline) {
        const operation = contract.createOperation(state.revision, "set_timeline", {
          timeline, expected_timeline:state.revision.document.properties.timeline || null,
          provenance:manualProvenance("set_timeline")
        });
        await sendOperation(operation);
        if (JSON.stringify(state.view?.properties?.timeline) !== JSON.stringify(timeline)) {
          // The server canonicalizes object key order; compare values recursively.
          const canonical = value => JSON.stringify(value, function(key, item) {
            return item && typeof item === "object" && !Array.isArray(item)
              ? Object.fromEntries(Object.keys(item).sort().map(k=>[k,item[k]])) : item;
          });
          if (canonical(state.view?.properties?.timeline) !== canonical(timeline)) throw new Error("剪辑修改未保存，请重试");
        }
      },
      onAiPlan:(goal,basePlanId) => api(projectPath("/cut-ai"), {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({revision_id:state.revision.revision_id,goal,base_plan_id:basePlanId||null})}),
      async onCutExport(planId, format, subtitleMode) {
        const response = await fetch(projectPath("/cut-export"), {method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({revision_id:state.revision.revision_id,plan_id:planId,format,subtitle_mode:subtitleMode})});
        if (!response.ok) {const error=await response.json();throw new Error(typeof error.detail === "string" ? error.detail : "导出失败");}
        const url=URL.createObjectURL(await response.blob()), link=document.createElement("a");
        link.href=url;link.download=`substar-cut.${({video:"mp4",audio:"wav"})[format]||format}`;link.click();
        setTimeout(()=>URL.revokeObjectURL(url),60000);
      },
      getLayout:() => state.timelineLayout,
      getShowTranslations:() => $("#showTranslations").checked,
      getReviewCueIds:() => reviewEnabled ? reviewData.notes.filter(note => note.status === "open").map(note => note.anchor.cue_id) : [],
      onViewportChange({start, end, duration}) {
        const slider = $("#timelineScrollSlider");
        const halfSpan = Math.min(duration, end - start) / 2;
        slider.min = String(halfSpan);
        slider.max = String(Math.max(halfSpan, duration - halfSpan));
        slider.value = String((start + end) / 2);
        slider.disabled = duration <= end - start;
        slider.setAttribute("aria-valuetext", `${((start + end) / 2).toFixed(1)} 秒`);
      },
      canvas,
      media:activeMedia(),
      thumbnailsUrl:projectPath("/thumbnails"),
      getThumbnailsUrl:() => projectPath("/thumbnails"),
      keyboardTarget:document,
      hideCueShortcut:() => state.shortcuts.hideCue,
      zoomModifier:() => state.shortcuts.zoomModifier,
      spectrogramUrl({start, end}) {
        return projectPath(`/spectrogram?${new URLSearchParams({start:String(start), end:String(end)})}`);
      },
      onWaveformWindow({start, end, points}) {
        const query = new URLSearchParams({
          start:String(start), end:String(end), points:String(points)
        });
        if (!state.waveformCache) return api(projectPath(`/waveform?${query}`));
        return state.waveformCache.request({
          mediaId:state.projectId,
          latestGroup:document.documentElement.dataset.editorLayout === "experimental"
            ? `${state.projectId}:row:${start}` : state.projectId,
          start, end, points,
          load:({signal}) => api(projectPath(`/waveform?${query}`), {signal})
        });
      },
      onWaveformError(error) {
        if (error?.name === "AbortError") return;
        ordinaryError(`波形读取失败：${error.message}`);
      },
      selection:state.cueSelection,
      onSelectCue(cueId, _time, modifiers) {
        if (modifiers) {
          state.selectedTokenIds.clear(); state.selectionAnchorTokenId=null;
          refreshTokenSelectionUi(); refreshCueSelection();
        }
        syncPlaybackFollowAfterSeek();
        state.timelineSelectedCueId = cueId || null;
        if (cueId) activateCue(cueId, {seek:false, scroll:true, revealTimeline:false, explicitNavigation:true});
        else activateCue(null);
      },
      onSeek(time) {
        syncPlaybackFollowAfterSeek();
        const cue = state.view?.cue_views.find(item =>
          item.state === "active" && time >= Number(item.start) && time < Number(item.end)
        );
        if ((cue?.cue_id || null) !== state.activeCueId) activateCue(cue?.cue_id || null, {
          scroll:true,
          revealTimeline:false,
          explicitNavigation:true
        });
      },
      onZoom(detail) {
        observeEditorTutorialEvent("timeline_zoom", detail);
        scheduleEditorTutorialPosition();
      },
      onOperation(_operation, intent) { handleTimelineIntent(intent); }
    });
    if (state.view) {
      state.timelineController.setView(state.view);
      state.timelineController.setActiveCue(state.activeCueId);
      state.timelineController.setSelectedCue(state.timelineSelectedCueId);
    }
  }

  function initializeCueTimeController() {
    const factory = window.EditorCueTimeController;
    if (!factory?.createCueTimeController) return;
    state.cueTimeController = factory.createCueTimeController({
      contract,
      getRevision:() => state.revision,
      sendOperation,
      provenance:manualProvenance
    });
  }

  function initializeCueListView() {
    const factory = window.EditorCueListView;
    if (!factory?.createCueListView) return;
    state.cueListView = factory.createCueListView({
      container:$("#cueList"),
      positionSlider:$("#cueScrollSlider"),
      pageSize:CUE_PAGE_SIZE,
      renderCue:cueElement,
      onWindowChange:page => { state.cuePageStart = page.start; }
    });
  }

  async function mergeSelectedTokens() {
    const selected = state.view.token_views.filter(token =>
      token.state === "active" && state.selectedTokenIds.has(token.token_id)
    );
    if (selected.length < 2) return;
    const positions = state.indexes?.activeTokenPosition || new Map();
    selected.sort((left, right) => positions.get(left.token_id) - positions.get(right.token_id));
    const text = selected.map(token => token.text).join(" ").trim();
    await flushActiveEditorInput();
    const button = [...$("#cueList").querySelectorAll("[data-token-id]")]
      .find(item => item.dataset.tokenId === selected[0].token_id);
    const label = button?.querySelector("b");
    if (!label) return;
    const hidden = [...$("#cueList").querySelectorAll("[data-token-id]")]
      .filter(item => selected.slice(1).some(token => token.token_id === item.dataset.tokenId));
    hidden.forEach(item => { item.hidden = true; });
    const input = document.createElement("input");
    input.className = "inline-token-editor";
    input.value = text;
    input.dataset.originalEdit = text;
    input.size = Math.max(5, [...text].length + 1);
    label.replaceWith(input);
    let finished = false;
    const finish = async commit => {
      if (finished) return;
      finished = true;
      const value = input.value.trim();
      input.replaceWith(label);
      hidden.forEach(item => { item.hidden = false; });
      if (!commit || !value) return;
      return await sendOperation(contract.mergeOperation(
        state.revision, selected.map(token => token.token_id), value, manualProvenance("merge")
      ));
    };
    input.onkeydown = event => {
      event.stopPropagation();
      if (event.key === "Enter") { event.preventDefault(); input.blur(); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    };
    input.onclick = event => event.stopPropagation();
    input.onblur = () => { input.editorCommit = finish(true); };
    input.focus(); input.select();
  }

  async function restoreSelectedFields(revisionId, scope) {
    if (editorAiTaskLocksEditor() || state.historyNavigationPending || state.restorePreparationPending
      || state.checkpointPending || state.documentWritePending) return;
    const hasSelection = state.selectedTokenIds.size > 0;
    const ids = state.view.cue_views.filter(cue => cue.state === "active" &&
      (state.cueSelection.ids.size ? state.cueSelection.ids.has(cue.cue_id) : hasSelection ? cue.display_token_ids.some(id => state.selectedTokenIds.has(id)) : cue.cue_id === state.activeCueId))
      .map(cue => cue.cue_id);
    if (!ids.length) { ordinaryError("请先在编辑区选择要恢复的字幕"); return; }
    const project = state.projectId;
    state.restorePreparationPending = true;
    try {
      await flushActiveEditorInput();
      state.restoreWriting = true;
      $("#editorWorkbench").inert = true;
      const revision = await api(projectPath("/restore-selection"), {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({
          expected_revision_id:state.revision.revision_id, revision_id:revisionId, cue_ids:ids, scope
        })});
      if (project !== state.projectId) return;
      setRevision(revision); await loadRevisionHistory({force:true});
      $("#draftState").textContent = "已恢复所选内容，其他编辑已保留";
    } catch (error) { ordinaryError(`选择性恢复失败：${error.message}`); }
    finally { state.restoreWriting = false; state.restorePreparationPending = false; $("#editorWorkbench").inert = false; }
  }

  let tokenSplitMode = false;
  let tokenSplitCaret = null;
  function hideTokenSplitCaret() {
    if (tokenSplitCaret) tokenSplitCaret.hidden = true;
  }

  function tokenSplitPoint(event) {
    const button = event.target.closest?.("[data-token-id]");
    const label = button?.querySelector("b");
    if (!label || editorAiTaskLocksEditor() || state.restorePreparationPending) return null;
    const token = state.view?.token_views.find(item => item.token_id === button.dataset.tokenId);
    if (!token || token.state !== "active") return null;
    const caret = document.caretPositionFromPoint?.(event.clientX, event.clientY);
    const range = caret ? null : document.caretRangeFromPoint?.(event.clientX, event.clientY);
    const node = caret?.offsetNode || range?.startContainer;
    const offset = caret?.offset ?? range?.startOffset;
    if (!node || !label.contains(node) || node.nodeType !== Node.TEXT_NODE) return null;
    const prefix = document.createRange();
    prefix.selectNodeContents(label); prefix.setEnd(node, offset);
    const index = [...prefix.toString()].length;
    const chars = [...token.text];
    if (index < 1 || index >= chars.length || !chars.slice(0,index).join("").trim()
        || !chars.slice(index).join("").trim()) return null;
    const position = document.createRange();
    position.setStart(node, offset); position.collapse(true);
    const rect = position.getBoundingClientRect();
    if (!rect.height) return null;
    return {token, index, rect, fontSize:parseFloat(getComputedStyle(label).fontSize)};
  }

  function showTokenSplitCaret(event) {
    const point = tokenSplitMode ? tokenSplitPoint(event) : null;
    if (!point) { hideTokenSplitCaret(); return; }
    if (!tokenSplitCaret) {
      tokenSplitCaret = document.createElement("span");
      tokenSplitCaret.className = "token-split-caret";
      tokenSplitCaret.setAttribute("aria-hidden", "true");
      document.body.append(tokenSplitCaret);
    }
    tokenSplitCaret.style.left = `${point.rect.left}px`;
    const height = point.fontSize * 1.3;
    tokenSplitCaret.style.top = `${point.rect.top + (point.rect.height - height) / 2}px`;
    tokenSplitCaret.style.height = `${height}px`;
    tokenSplitCaret.hidden = false;
  }

  function setTokenSplitMode(enabled) {
    tokenSplitMode = enabled;
    hideTokenSplitCaret();
    $("#splitTokens").setAttribute("aria-pressed", String(enabled));
    $("#cueList").classList.toggle("token-split-mode", enabled);
    positionSelectionMenus();
    $("#draftState").textContent = enabled ? "在词元内部右键切分，时间均分；点击其他位置退出" : "已退出词元拆分";
  }

  async function splitTokenAtPointer(event) {
    event.preventDefault(); event.stopPropagation();
    if (state.restorePreparationPending || state.checkpointPending || state.documentWritePending) return;
    const point = tokenSplitPoint(event);
    hideTokenSplitCaret();
    if (!point) { ordinaryError("请在词元内部两个字符之间切分"); return; }
    const {token, index} = point;
    const project = state.projectId;
    state.restorePreparationPending = true;
    try {
      await flushActiveEditorInput();
      state.restoreWriting = true;
      $("#editorWorkbench").inert = true;
      const revision = await api(projectPath("/split-token"), {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({
          expected_revision_id:state.revision.revision_id, token_id:token.token_id, offset:index
        })});
      if (project !== state.projectId) return;
      setRevision(revision); await loadRevisionHistory({force:true});
    } catch (error) { ordinaryError(`拆分失败：${error.message}`); }
    finally { state.restoreWriting = false; state.restorePreparationPending = false; $("#editorWorkbench").inert = false; }
  }

  async function deleteSelectedTokens() {
    const selectable = new Set(orderedSelectableTokenIds());
    const tokenIds = state.view.token_views
      .filter(token => token.state === "active" && selectable.has(token.token_id) && state.selectedTokenIds.has(token.token_id))
      .map(token => token.token_id);
    if (!tokenIds.length) return;
    await sendOperation(contract.deleteOperation(
      state.revision, {token_ids:tokenIds}, manualProvenance("delete")
    ));
  }

  async function restoreSelectedTokens() {
    const selected = state.view.token_views.filter(token => state.selectedTokenIds.has(token.token_id));
    const tokenIds = selected
      .filter(token => token.state === "deleted" && state.selectedTokenIds.has(token.token_id))
      .map(token => token.token_id);
    if (tokenIds.length) {
      await sendOperation(contract.restoreOperation(
        state.revision, {token_ids:tokenIds}, manualProvenance("restore")
      ));
      return;
    }
    const active = selected.filter(token => token.state === "active");
    const aiApplied = active.filter(token => token.provenance?.kind === "ai");
    const aiCanceled = active.filter(token => {
      const record = token.provenance?.metadata?.ai_calibration;
      return record && typeof record === "object" && record.applied === false
        && typeof record.after_text === "string";
    });
    if (aiApplied.length === active.length && active.length) {
      await sendOperation(contract.setAiCalibrationOperation(
        state.revision,
        active.map(token => token.token_id),
        "cancel",
        manualProvenance("cancel_ai_calibration")
      ));
      return;
    }
    if (aiCanceled.length === active.length && active.length) {
      await sendOperation(contract.setAiCalibrationOperation(
        state.revision,
        active.map(token => token.token_id),
        "restore",
        manualProvenance("restore_ai_calibration")
      ));
    }
  }

  async function insertTokenAfter(cueId, anchorTokenId) {
    const cue = state.view?.cue_views.find(item => item.cue_id === cueId);
    if (!cue || cue.state !== "active" || !cue.display_token_ids.includes(anchorTokenId)) return;
    const text = await askText("插入人工词元", "文字", "");
    if (!text?.trim()) return;
    await sendOperation(contract.insertOperation(
      state.revision, cueId, anchorTokenId, {text:text.trim(), source_token_ids:[]},
      manualProvenance("insert")
    ));
  }

  async function cueAction(cueId, action) {
    try {
      const cue = state.view.cue_views.find(item => item.cue_id === cueId);
      if (!cue) throw new Error("Cue 已变化，请刷新后重试");
      if (action === "hide") {
        await sendOperation(contract.deleteOperation(
          state.revision, {cue_ids:[cueId]}, manualProvenance("hide_cue")
        ));
        return;
      }
      if (action === "purge") {
        await sendOperation(contract.purgeCueOperation(
          state.revision, [cueId], manualProvenance("purge_cue")
        ));
        return;
      }
      if (action === "restore") {
        await sendOperation(contract.restoreOperation(
          state.revision, {cue_ids:[cueId]}, manualProvenance("restore_cue")
        ));
        return;
      }
      if (action === "split") {
        const anchor = cue.display_token_ids.find(id => state.selectedTokenIds.has(id));
        if (!anchor) return;
        state.activeCueId = cueId;
        state.cueListView?.setActive(cueId);
        state.timelineController?.setActiveCue(cueId);
        await sendOperation(contract.splitCueOperation(
          state.revision, cueId, anchor, manualProvenance("split_cue")
        ));
        return;
      }
      if (action === "merge-next") {
        const index = state.view.cue_views.findIndex(item => item.cue_id === cueId);
        const next = state.view.cue_views[index + 1];
        if (!next || cue.subtitle_track === "overlay" || next.subtitle_track === "overlay") return;
        if (cue.speaker && next.speaker && cue.speaker !== next.speaker
          && !window.confirm("两个 Cue 属于不同说话人。继续合并会清空合并后 Cue 的说话人标签，是否继续？")) return;
        state.activeCueId = cueId;
        state.cueListView?.setActive(cueId);
        state.timelineController?.setActiveCue(cueId);
        await sendOperation(contract.mergeCuesOperation(
          state.revision, cueId, next.cue_id, manualProvenance("merge_cues")
        ));
        return;
      }
      if (action === "speaker") {
        const speakers = [null, ...window.EditorSpeakers.ids(state.view)];
        const nextSpeaker = speakers[(speakers.indexOf(cue.speaker ?? null) + 1) % speakers.length];
        if (nextSpeaker === (cue.speaker ?? null)) return;
        await sendOperation(contract.setCueSpeakerOperation(
          state.revision, cueId, nextSpeaker, manualProvenance("set_cue_speaker")
        ));
      }
    } catch (error) {
      ordinaryError(error?.message || String(error));
    }
  }

  let projectEpoch = 0;

  async function loadProject(projectId, {restoreTranslation = true, resetTutorial = false, showTutorialIntro = true} = {}) {
    if (!projectId) return;
    if (state.checkpointPending || state.restorePreparationPending || state.historyNavigationPending || state.documentWritePending) return;
    if (state.revision) {
      try { await flushActiveEditorInput(); }
      catch (error) {
        ordinaryError(`切换前保存未完成：${error.message}`);
        renderProjectList();
        return;
      }
    }
    if (state.operationQueue?.getState().hasUnsavedChanges) {
      ordinaryError("当前编辑仍在排队保存，完成前不会切换或刷新项目");
      renderProjectList();
      return;
    }
    const epoch = ++projectEpoch;
    if ($("#translationRegroup")) $("#translationRegroup").checked = false;
    reviewEnabled = false;
    $("#editorWorkbench").classList.remove("review-mode");
    reviewData = {version:0, notes:[]};
    reviewSelection = null;
    $(".review-notes-panel")?.classList.add("hidden");
    if ($("#reviewModeToggle")) {
      $("#reviewModeToggle").textContent = "修订";
      $("#reviewModeToggle").setAttribute("aria-pressed", "false");
    }
    state.operationQueue?.destroy();
    state.operationQueue = null;
    state.operationQueueProjectId = "";
    state.revision = null;
    state.view = null;
    state.documentStore = null;
    $("#editorWorkbench").inert = true;
    renderHeader();
    const current = () => epoch === projectEpoch && projectId === state.projectId;
    stopTranslationPoll();
    if (projectId !== state.projectId) {
      renderTranslationTask(null);
      state.searchReplaceUndo = null;
    }
    ordinaryError("");
    state.projectId = projectId;
    state.timelineLayout = localStorage.getItem(`substar.editor.timeline-layout:${projectId}`) === "separated" ? "separated" : "composite";
    $("#timelineLayout").value = state.timelineLayout;
    state.cueSplitView = localStorage.getItem(`substar.editor.cue-split-view:${projectId}`) || "virtual";
    if ($("#cueSplitView")) { $("#cueSplitView").value = state.cueSplitView; syncCueViewSwitch(); }
    renderProjectList();
    try {
      const project = state.projects.find(item => item.project_id === projectId);
      hideEditorTutorialIntro();
      const resetRevision = resetTutorial && project?.tutorial_case_id
        ? await api(projectPath("/tutorial/reset"), {method:"POST"}) : null;
      if (!current()) return;
      const [revision, taskInfo, mediaInfo, llmOptions] = await Promise.all([
        resetRevision ? Promise.resolve(resetRevision) : api(projectPath()),
        api(projectPath("/task-info")),
        api(projectPath("/media-info")),
        api(projectPath("/llm-options"))
      ]);
      if (!current()) return;
      state.taskInfo = taskInfo;
      state.llmOptions = llmOptions;
      applySubtitlePolicy(taskInfo);
      configureTranslationLanguageDefaults(taskInfo);
      state.mediaInfo = mediaInfo;
      configureMedia(mediaInfo?.kind);
      const textProject = revision.document.cues.some(cue => cue.source_text != null && cue.mapping?.subtitle_track !== "overlay");
      if (textProject && !localStorage.getItem(`substar.editor.timeline-layout:${projectId}`)) {
        state.timelineLayout = "separated";
        $("#timelineLayout").value = "separated";
      }
      setRevision(revision, {preserveCueViewport:false});
      $("#editorWorkbench").inert = false;
      const aiTaskReady = textProject ? Promise.resolve(null) : refreshEditorAiTask();
      if (resetRevision && $("#aiReviewMenu")) $("#aiReviewMenu").open = false;
      seedRevisionMetadata(state.revision);
      loadRevisionHistory().catch(() => {});
      ensureOperationQueue();
      state.mediaLoadAttempts = 0;
      state.mediaLoadPending = false;
      state.mediaLoadFailed = false;
      state.mediaHadMetadata = false;
      setMediaMessage("正在读取媒体…");
      loadProjectMedia();
      state.timelineController?.redraw();
      await aiTaskReady;
      if (!current()) return;
      if (!textProject) startEditorAiTaskPoll();
      if (restoreTranslation && !textProject) {
        const task = await refreshTranslationTask();
        if (task && ["queued", "running", "cancelling"].includes(task.state)) {
          followTranslationTask(task);
        }
      }
      if (!current()) return;
      const url = new URL(window.location.href);
      url.searchParams.set("project", projectId);
      history.replaceState(null, "", url);
      if (project?.tutorial_case_id && showTutorialIntro) maybeShowTutorialIntro(project.tutorial_case_id);
    } catch (error) {
      if (current()) ordinaryError(error.message);
    }
  }

  async function loadProjects() {
    ordinaryError("");
    try {
      const query = new URLSearchParams(window.location.search);
      const requested = query.get("project") || query.get("job");
      // A direct editor link must never wait for the unrelated project catalog.
      if (requested) await loadProject(requested);
      const response = await api("/api/projects");
      state.projects = (response.projects || []).filter(project => !project.tutorial_case_id);
      //  links use `project`; accepting the same project id from the
      // short-lived `job` link keeps already-open  pages from becoming an
      // empty editor while all new links are emitted with the  contract.
      if (requested) {
        renderProjectList();
        return;
      }
      state.projectId = state.projects.some(item => item.project_id === requested)
        ? requested : state.projects[0]?.project_id || "";
      renderProjectList();
      if (state.projectId) await loadProject(state.projectId);
    } catch (error) {
      ordinaryError(error.message);
    }
  }

  async function toggleComplete() {
    if (!state.revision) return;
    ordinaryError("");
    try {
      const revision = await withSavedDocument(() => api(projectPath("/complete"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          complete:state.view.properties.complete !== true
        })
      }));
      setRevision(revision);
      const current = state.projects.find(item => item.project_id === state.projectId);
      if (current) current.complete = state.view.properties.complete === true;
      recordCommittedRevision(state.revision, {
        kind:"manual", operation:"set_complete", metadata:{complete:state.view.properties.complete === true}
      });
      renderProjectList();
    } catch (error) {
      ordinaryError(error.message);
    }
  }

  // Tutorial implementations are archived outside the published application.
  function isAdvancedTutorial() { return false; }
  function positionEditorTutorial() {}
  function scheduleEditorTutorialPosition() {}
  function exitEditorTutorial() {}
  function advanceEditorTutorial() {}
  function observeEditorTutorialOperations() {}
  function observeEditorTutorialEvent() {}
  function positionEditorTutorialIntro() {}
  function hideEditorTutorialIntro() {}
  function maybeShowTutorialIntro() {}

  function closeValidationModal() {
    $("#validationModal").classList.add("hidden");
  }

  async function validateCurrentRevision() {
    if (!state.revision) return;
    ordinaryError("");
    try {
      const report = await api(projectPath("/validate"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          source_hard_limit:validationTrackLimit("source"),
          target_hard_limit:validationTrackLimit("target"),
          count_spaces:state.subtitlePolicy.countSpaces,
          count_punctuation:state.subtitlePolicy.countPunctuation
        })
      });
      if (!contract.validationReportIsCurrent(state.revision, report)) {
        ordinaryError("校验结果已过期，当前文档未被阻断，请重新校验");
        return;
      }
      const hardIssues = (report.issues || []).filter(issue => issue.severity === "hard");
      state.hardIssues = hardIssues;
      state.hardIssueIndex = hardIssues.length ? 0 : -1;
      renderNavigators();
      if (!hardIssues.length) {
        ordinaryError("");
        $("#revisionLabel").textContent = `${state.revision.revision_id} · 硬校验通过`;
        return;
      }
      const list = $("#hardIssueList");
      list.replaceChildren();
      hardIssues.forEach(issue => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "hard-issue";
        button.dataset.issueCue = issue.cue_id || "";
        button.textContent = issue.message;
        list.append(button);
      });
      $("#validationModal").classList.remove("hidden");
    } catch (error) {
      ordinaryError(`校验请求失败：${error.message}`);
    }
  }

  $("#projectList").addEventListener("click", event => {
    const button = event.target.closest("[data-project-id]");
    if (!button) return;
    $("#projectMenu").open = false;
    loadProject(button.dataset.projectId);
  });
  $("#exitEditor").addEventListener("click", exitEditor);
  $("#saveCheckpoint").onclick = async () => {
    if (!$("#checkpointNaming").hidden) { setCheckpointNaming(false); return; }
    await flushActiveEditorInput();
    setCheckpointNaming(true);
  };
  $("#versionMenu").addEventListener("toggle", () => {
    if ($("#versionMenu").open) loadRevisionHistory({force:true});
  });
  $("#revisionMenu").addEventListener("click", event => {
    const button = event.target.closest("[data-restore-revision]");
    if (!button || button.disabled || editorAiTaskLocksEditor()) return;
    $("#versionMenu").open = false;
    const checkpointNavigation = document.documentElement.dataset.editorLayout === "experimental";
    restoreRevision(button.dataset.restoreRevision, checkpointNavigation
      ? {navigation:"direct", undoRevisionIds:[], redoRevisionIds:[]} : {});
  });
  $("#confirmCheckpoint").onclick = createCheckpoint;
  $("#checkpointName").onkeydown = event => {
    event.stopPropagation();
    if (event.key === "Enter") { event.preventDefault(); createCheckpoint(); }
    if (event.key === "Escape") setCheckpointNaming(false);
  };
  $("#splitTokens").onclick = () => setTokenSplitMode(!tokenSplitMode);
  $("#cueList").addEventListener("pointermove", showTokenSplitCaret);
  $("#cueList").addEventListener("pointerleave", hideTokenSplitCaret);
  document.addEventListener("scroll", hideTokenSplitCaret, true);
  window.addEventListener("resize", hideTokenSplitCaret);

  document.addEventListener("pointerdown", event => {
    if (tokenSplitMode && !event.target.closest("#splitTokens") &&
        !(event.button === 2 && event.target.closest("#cueList [data-token-id]"))) setTokenSplitMode(false);
  }, true);
  $("#taskInfoMenu").addEventListener("toggle", () => {
    if ($("#taskInfoMenu").open) openTaskInfoMenu();
  });
  $("#taskInfoForm").onsubmit = submitTaskInfo;
  $("#relinkTaskMedia").onclick = async () => {
    const projectId = state.projectId;
    const button = $("#relinkTaskMedia");
    button.disabled = true;
    try {
      const selection = await api(projectPath("/media-select"), {method:"POST", headers:{"X-Substar-Media-Select":"1"}});
      if (selection.cancelled || projectId !== state.projectId || !$("#taskInfoMenu").open) return;
      if (selection.needs_confirmation && !window.confirm("无法确认这是同一媒体。选择不同剪辑可能使字幕时间错位，仍使用此文件？")) return;
      $("#taskInfoMediaPath").value = selection.path;
      $("#taskInfoMediaPath").title = selection.path;
      $("#taskInfoMediaPath").dataset.selection = selection.token;
      $("#taskMediaStatus").hidden = false;
      $("#taskMediaStatus").textContent = "点击保存后更新媒体引用";
    } catch (error) { ordinaryError(`重新链接失败：${error.message}`); }
    finally { button.disabled = false; }
  };
  $("#cancelTaskInfo").onclick = closeTaskInfoMenu;
  $("#toggleComplete").onclick = toggleComplete;
  $("#translateDocument").onclick = startTranslation;
  $("#dismissTaskPanel").onclick = cancelOrDismissTaskPanel;
  $("#dismissOrdinaryError").onclick = () => ordinaryError("");
  $("#retryUnsaved").onclick = async () => {
    const queue = state.operationQueue;
    if (!queue) return;
    queue.retryFailed();
    try {
      await queue.flushAndWait();
      ordinaryError("");
    } catch (error) {
      ordinaryError(`编辑尚未保存：${error.message}`);
    }
  };
  $("#downloadConflictCopy").onclick = async () => {
    try {
      const text = JSON.stringify({schema_version:"substar.editor-recovery.v1", project_id:state.projectId,
        revision:state.revision, operations:state.documentStore?.pending() || []}, null, 2);
      const result = await systemSaveAs.saveBlob({
        suggestedName:`${systemSaveAs.safeFilename(state.projectId)}_未保存编辑.json`,
        description:"未保存编辑恢复副本", mimeType:"application/json", extension:".json"
      }, new Blob([text], {type:"application/json;charset=utf-8"}));
      if (!result.cancelled) ordinaryError(`恢复副本已保存：${result.filename}`, "completed");
    } catch (error) { ordinaryError(`恢复副本下载失败：${error.message}`); }
  };
  $("#keepConflictCopy").onclick = async () => {
    try {
      const operations = state.documentStore?.pending() || [];
      const key = `substar.editor.conflict-copy:${state.projectId}:${Date.now()}`;
      localStorage.setItem(key, JSON.stringify({revision:state.revision, operations}));
      state.operationQueue?.discardUnsaved();
      state.documentStore?.discard(operations.map(operation => operation.operation_id));
      await loadProject(state.projectId);
      ordinaryError("已载入保存版本。未保存编辑的恢复副本已留在本浏览器。", "completed");
    } catch (error) { ordinaryError(`恢复副本未能保存：${error.message}`); }
  };
  $("#externalCalibration").onclick = async () => {
    const epoch = projectEpoch;
    $("#exportMenu").open = false;
    try {
      const result = await systemSaveAs.saveBlob({suggestedName:`${systemSaveAs.safeFilename(state.taskInfo?.display_name || state.projectId)}_外部校准生成.txt`, description:"外部校准提示词与字幕材料", mimeType:"text/plain", extension:".txt"}, async () => {
      const saved = await flushActiveEditorInput();
      if (epoch !== projectEpoch) throw new Error("项目已切换，请重新生成");
      const query = new URLSearchParams({revision_id:saved.revision_id, instruction:$("#aiCalibrationInstruction").value});
      const built = await api(projectPath(`/exchange/external-calibration?${query}`));
      if (epoch !== projectEpoch) throw new Error("项目已切换，请重新生成");
      return new Blob([built.text], {type:"text/plain;charset=utf-8"});
      });
      if (!result.cancelled) ordinaryError(`已保存：${result.filename}；请导入模型生成的 .json 文件`, "completed");
    } catch(error) { ordinaryError(`生成外部校准材料失败：${error.message}`); }
  };
  $("#importExternalCalibration").onclick = () => {
    $("#importMenu").open = false;
    if (!state.revision || state.operationPending || editorAiTaskLocksEditor()) return ordinaryError("请先打开项目并等待当前操作完成");
    $("#externalCalibrationInput").click();
  };
  $("#externalCalibrationInput").onchange = async event => {
    const file = event.target.files?.[0]; event.target.value = "";
    if (!file) return;
    const epoch = projectEpoch;
    try {
      if (file.size > 20 * 1024 * 1024) throw new Error("校准结果文件不能超过 20 MB");
      await flushActiveEditorInput();
      if (epoch !== projectEpoch) return;
      const submit = apply => {
        const form = new FormData(); form.append("file",file); form.append("apply",String(apply));
        return api(projectPath("/external-calibration"),{method:"POST",body:form});
      };
      const preview = await submit(false);
      if (epoch !== projectEpoch) return;
      if (!window.confirm(`校准结果：修改 ${preview.replacement_count} 个词元，合并 ${preview.merge_count} 组，待复核 ${preview.review_count} 项。应用到当前版本？`)) return;
      state.operationPending = true;
      const result = await submit(true);
      if (epoch !== projectEpoch) return;
      setRevision(result.revision);
      await loadRevisionHistory();
      ordinaryError("外部校准已应用，可通过 AI 校准定位和版本链查看或撤销", "completed");
    } catch(error) {ordinaryError(`导入校准结果失败：${error.message}`);}
    finally {if(epoch === projectEpoch) {state.operationPending = false; renderHeader();}}
  };
  $("#externalTranslation").onclick = async () => {
    const epoch = projectEpoch;
    $("#exportMenu").open = false;
    try {
      const result = await systemSaveAs.saveBlob({suggestedName:`${systemSaveAs.safeFilename(state.taskInfo?.display_name || state.projectId)}_外部翻译生成.txt`, description:"外部翻译提示词与字幕材料", mimeType:"text/plain", extension:".txt"}, async () => {
      const saved = await flushActiveEditorInput();
      if (epoch !== projectEpoch) throw new Error("项目已切换，请重新生成");
      const query = new URLSearchParams({revision_id:saved.revision_id,
        source_language:$("#translationSourceLanguage").value,
        target_language:$("#translationTargetLanguage").value,
        mapping_mode:$("#translationMappingMode").value});
      const built = await api(projectPath(`/exchange/external-translation?${query}`));
      if (epoch !== projectEpoch) throw new Error("项目已切换，请重新生成");
      return new Blob([built.text], {type:"text/plain;charset=utf-8"});
      });
      if (!result.cancelled) ordinaryError(`已保存：${result.filename}；请导入模型生成的 .srt 文件`, "completed");
    } catch (error) { ordinaryError(`生成外部翻译材料失败：${error.message}`); }
  };
  $("#exportMenu").addEventListener("click", async event => {
    const exportEpoch = projectEpoch;
    const exchange = event.target.closest("[data-exchange-export]");
    if (exchange && state.projectId && state.revision) {
      $("#exportMenu").open = false;
      try {
        const sequence = nextExportSequence();
        const spec = systemSaveAs.exchangeSpec(
          window.SubstarProjectLabel(state.taskInfo?.display_name || state.projectId),
          exchange.dataset.exchangeExport, sequence.value, async () => {
            const savedBase = await flushActiveEditorInput();
            if (exportEpoch !== projectEpoch) throw new Error("项目已切换，请重新导出");
            const query = new URLSearchParams({export_sequence:String(sequence.value), revision_id:savedBase.revision_id});
            return projectPath(`/exchange/${encodeURIComponent(exchange.dataset.exchangeExport)}?${query}`);
          }
        );
        const result = await systemSaveAs.saveUrl(spec);
        if (result.cancelled) return;
        commitExportSequence(sequence);
        ordinaryError(`已保存：${result.filename}`, "completed");
      } catch (error) {
        ordinaryError(`导出失败：${error.message}`);
        if (exchange.dataset.exchangeExport === 'subtitle-project' && /媒体|文件不存在|重新链接/.test(error.message)) {
          $("#taskInfoMenu").open = true;
          await openTaskInfoMenu();
          $("#relinkTaskMedia").click();
        }
      }
      return;
    }
    const button = event.target.closest("[data-export-mode]");
    if (!button || !state.projectId || !state.revision) return;
    $("#exportMenu").open = false;
    try {
      const sequence = nextExportSequence();
      const spec = systemSaveAs.subtitleSpec(
        window.SubstarProjectLabel(state.taskInfo?.display_name || state.projectId),
        button.dataset.exportMode,
        sequence.value,
        async () => {
          const savedBase = await flushActiveEditorInput();
          if (exportEpoch !== projectEpoch) throw new Error("项目已切换，请重新导出");
          const query = new URLSearchParams({export_sequence:String(sequence.value), revision_id:savedBase.revision_id});
          return projectPath(`/export/${encodeURIComponent(button.dataset.exportMode)}?${query}`);
        }
      );
      const result = await systemSaveAs.saveUrl(spec);
      if (result.cancelled) return;
      commitExportSequence(sequence);
      ordinaryError(`已保存：${result.filename}`, "completed");
      observeEditorTutorialEvent("export", {mode:button.dataset.exportMode});
    } catch (error) {
      ordinaryError(`导出失败：${error.message}`);
    }
  });
  $("#mergeTokens").onclick = mergeSelectedTokens;
  $("#deleteTokens").onclick = deleteSelectedTokens;
  $("#restoreTokens").onclick = restoreSelectedTokens;
  $("#autoMergeForm").onsubmit = async event => {
    event.preventDefault();
    const button = $("#applyAutoMerge");
    if (button.disabled || !state.revision) return;
    button.disabled = true;
    const status = $("#autoMergeStatus");
    status.textContent = "正在拼接 / 合并字幕…";
    try {
      await flushActiveEditorInput();
      const before = state.revision.document.cues.length;
      const beforeRevision = state.revision.revision_id;
      const mergeProject = state.projectId;
      const result = await sendOperation(contract.autoMergeCuesOperation(state.revision, {
        gap_ms:Number($("#mergeGap").value),
        short_count:Number($("#mergeShortCount").value),
        absorb:$("#mergeAbsorb").checked,
        snap_direction:$("#mergeSnapDirection").value,
        absorb_direction:$("#mergeAbsorbDirection").value
      }, manualProvenance("auto_merge_cues")));
      if (result && state.projectId === mergeProject) state.autoMergeUndo = {
        projectId:mergeProject, before:beforeRevision, after:state.revision.revision_id
      };
      status.textContent = result
        ? `处理完成，合并 ${Math.max(0, before - state.revision.document.cues.length)} 条字幕；间隔已按设置处理。可使用撤销恢复。`
        : "未完成，请查看保存提示。";
    } catch (error) {
      status.textContent = `未完成：${error.message}`;
    } finally { renderHeader(); }
  };
  $("#mergeAbsorb").onchange = () => {
    $("#mergeShortCount").disabled = !$("#mergeAbsorb").checked;
    $("#mergeAbsorbDirection").disabled = !$("#mergeAbsorb").checked;
  };
  $("#undoAutoMerge").onclick = async () => {
    const entry = state.autoMergeUndo;
    if ($("#undoAutoMerge").disabled || !entry || entry.projectId !== state.projectId || entry.after !== state.revision?.revision_id) return;
    $("#undoAutoMerge").disabled = true;
    const restored = await restoreRevision(entry.before);
    if (restored !== null) {
      state.autoMergeUndo = null;
      $("#autoMergeStatus").textContent = "已撤销本次合并。";
    }
    renderHeader();
  };

  $("#toolSearchNext").onclick = () => {
    findNextSearch();
    observeEditorTutorialEvent("search", {query:$("#toolSearch").value.trim(), found:state.searchIndex >= 0});
  };
  $("#toolReplaceCurrent").onclick = replaceCurrentSearch;
  $("#toolUndoReplace").onclick = undoSearchReplace;
  $("#toolReplaceAll").onclick = replaceAllSearch;
  $("#toolSearchScopeSource").onclick = () => setSearchScope("source");
  $("#toolSearchScopeTarget").onclick = () => setSearchScope("target");
  $("#toolSearch").addEventListener("input", () => {
    state.searchIndex = -1;
    const status = $("#toolSearchStatus");
    if (status) status.textContent = `查找范围：${state.searchScope === "target" ? "译文" : "源语词元与短语"}`;
  });
  $("#toolSearch").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      findNextSearch();
      observeEditorTutorialEvent("search", {query:$("#toolSearch").value.trim(), found:state.searchIndex >= 0});
    }
  });
  $("#toolHardIssuePrev").onclick = () => navigateEntries("hard", -1);
  $("#toolHardIssueNext").onclick = () => {
    navigateEntries("hard", 1);
    observeEditorTutorialEvent("hard_issue_next");
  };
  $("#toolAiChangePrev").onclick = () => navigateEntries("ai", -1);
  $("#toolAiChangeNext").onclick = () => navigateEntries("ai", 1);
  $("#toolReferencePrev").onclick = () => navigateEntries("reference", -1);
  $("#toolReferenceNext").onclick = () => navigateEntries("reference", 1);
  $("#applyAutoSnap").onclick = async () => {
    if (!state.revision || state.operationPending) return;
    const forwardMode = document.querySelector(
      'input[name="forwardSnapMode"]:checked'
    )?.value || "none";
    const backwardMode = document.querySelector(
      'input[name="backwardSnapMode"]:checked'
    )?.value || "none";
    const thresholdValue = Math.max(
      0, Math.min(2000, Number($("#snapThreshold").value) || 0)
    );
    const forwardPreRollMs = Math.max(
      0, Math.min(100, Number($("#forwardSnapPreRoll").value) || 0)
    );
    const forwardSensitivity = Math.max(
      0, Math.min(100, Number($("#forwardSnapSensitivity").value) || 0)
    );
    const status = $("#autoSnapStatus");
    $("#applyAutoSnap").disabled = true;
    try {
      let forwardStarts = {};
      let smartCount = 0;
      if (forwardMode === "smart") {
        status.textContent = "正在分析波形起音…";
        const preview = await api(projectPath("/auto-snap/preview"), {
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({
            expected_revision_id:state.revision.revision_id,
            pre_roll_ms:forwardPreRollMs,
            sensitivity:forwardSensitivity
          })
        });
        forwardStarts = Object.fromEntries((preview.changes || []).map(change => [
          String(change.cue_id), Number(change.snapped_start)
        ]));
        smartCount = Object.keys(forwardStarts).length;
      }
      const intent = state.timelineController?.autoSnapOnce({
        forwardStarts,
        backwardThresholdMs:backwardMode === "threshold" ? thresholdValue : null
      });
      if (!intent?.count) {
        status.textContent = forwardMode === "smart"
          ? `已分析，${smartCount} 个可信起音；当前无需修改。`
          : "当前没有需要吸附的字幕边界。";
        observeEditorTutorialEvent("auto_snap", {smart:forwardMode === "smart", threshold:thresholdValue});
        return;
      }
      const revision = await applyTimelineAutoSnap(intent);
      if (!revision) {
        status.textContent = "自动吸附未提交，请刷新后重试。";
        return;
      }
      status.textContent = `已调整 ${intent.count} 条 Cue${forwardMode === "smart"
        ? `，智能起音 ${smartCount} 条` : ""}。`;
      observeEditorTutorialEvent("auto_snap", {smart:forwardMode === "smart", threshold:thresholdValue});
      if (document.documentElement.dataset.editorLayout !== "experimental") {
        $("#autoSnapMenu").open = false;
      }
    } catch (error) {
      status.textContent = `自动吸附失败：${error.message}`;
      ordinaryError(status.textContent);
    } finally {
      renderHeader();
    }
  };
  $("#snapThreshold").oninput = () => {
    const thresholdMode = document.querySelector(
      'input[name="backwardSnapMode"][value="threshold"]'
    );
    if (thresholdMode) thresholdMode.checked = true;
  };
  const selectSmartForwardSnap = () => {
    const smartMode = document.querySelector(
      'input[name="forwardSnapMode"][value="smart"]'
    );
    if (smartMode) smartMode.checked = true;
    const preRoll = Math.max(0, Math.min(100, Number($("#forwardSnapPreRoll").value) || 0));
    const sensitivity = Math.max(0, Math.min(100, Number($("#forwardSnapSensitivity").value) || 0));
    localStorage.setItem("substar.editor.forward-snap-pre-roll-ms", String(preRoll));
    localStorage.setItem("substar.editor.forward-snap-sensitivity", String(sensitivity));
  };
  $("#forwardSnapPreRoll").oninput = selectSmartForwardSnap;
  $("#forwardSnapSensitivity").oninput = selectSmartForwardSnap;
  $("#timelineScrollSlider").oninput = event => {
    const controller = state.timelineController;
    if (controller?.scrollToTime) controller.scrollToTime(Number(event.target.value));
    else controller?.revealTime(Number(event.target.value), true);
  };
  function updateTimelineHeight() {
    const hasTarget = $("#showTranslations").checked && (state.view?.cue_views || []).some(cue => cue.state !== "deleted" && cue.target?.target_text);
    const extra = state.timelineLayout === "separated" ? (hasTarget ? 69 : 36) : 0;
    $("#editorWorkbench").style.setProperty("--timeline-extra-height", `${extra}px`);
  }

  $("#timelineLayout").onchange = event => {
    state.timelineLayout = event.target.value === "separated" ? "separated" : "composite";
    if (state.projectId) localStorage.setItem(`substar.editor.timeline-layout:${state.projectId}`, state.timelineLayout);
    updateTimelineHeight();
    state.timelineController?.redraw();
  };
  function syncCueViewSwitch() {
    const select = $("#cueSplitView");
    document.querySelectorAll("[data-cue-view]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.cueView === select.value));
      button.disabled = select.disabled;
    });
  }
  $("#cueSplitView").onchange = event => {
    state.cueSplitView = event.target.value === "auxiliary" ? "auxiliary" : "virtual";
    syncCueViewSwitch();
    if (state.projectId) {
      localStorage.setItem(`substar.editor.cue-split-view:${state.projectId}`, state.cueSplitView);
    }
    renderCues({preservePage:true});
    refreshTokenSelectionUi();
  };
  $("#undoAutoSnap").onclick = undoAutoSnap;
  $("#applyPunctuation").onclick = applyPresentationSettings;
  function calibrationInstructionKey() {
    return `substar.editor.ai-calibration-instruction:${state.projectId || "unselected"}`;
  }
  $("#aiCalibrationMenu").addEventListener("toggle", () => {
    if (!$("#aiCalibrationMenu").open) return;
    $("#aiCalibrationInstruction").value = localStorage.getItem(calibrationInstructionKey()) || "";
  });
  $("#aiCalibrationInstruction").addEventListener("input", event => {
    localStorage.setItem(calibrationInstructionKey(), event.target.value);
  });
  $("#runAiCalibration").onclick = () => { state.taskPanelDismissed = false; runAiCalibration(); };
  $("#retryFailedTask").onclick = retryFailedTask;
  $("#openModelSettings").onclick = () => { window.location.href = "/settings#api"; };
  ["#convertOriginal", "#convertSimplified", "#convertTraditional", "#convertTaiwan", "#convertHongKong"].forEach(selector => {
    $(selector).onclick = event => convertScript(event.currentTarget.dataset.scriptTarget);
  });
  $("#importProject").onclick = event => {
    event.preventDefault();
    const menu = $("#importMenu");
    const opening = !menu.open;
    if (opening) {
      document.querySelectorAll(".editor-command-menu[open]").forEach(other => {
        if (other !== menu) other.open = false;
      });
    }
    menu.open = opening;
  };
  $("#importProjectPackage").onclick = () => {
    $("#importMenu").open = false;
    $("#importProjectInput").click();
  };
  let srtImport = null;
  let srtPreviewSequence = 0;
  const closeSrtImport = () => {
    srtImport = null;
    srtPreviewSequence += 1;
    $("#srtImportDialog").classList.add("hidden");
  };
  const previewSrtImport = async () => {
    const session = srtImport;
    if (!session || session.projectId !== state.projectId) return closeSrtImport();
    const sequence = ++srtPreviewSequence;
    $("#applySrtImport").disabled = true;
    $("#srtImportSummary").textContent = "正在匹配…";
    $("#srtImportRows").replaceChildren();
    try {
      const saved = await flushActiveEditorInput();
      if (session !== srtImport || sequence !== srtPreviewSequence) return;
      const request = {text:session.text, format:$("#srtImportFormat").value,
        expected_revision_id:saved.revision_id, request_id:crypto.randomUUID()};
      const result = await api(`/api/projects/${encodeURIComponent(session.projectId)}/import-srt-translation`, {
        method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(request)
      });
      if (session !== srtImport || sequence !== srtPreviewSequence) return;
      session.request = request;
      const labels = {source:"原文单行", target:"译文单行", "ab-single":"双语单行", "ab-double":"双语双行"};
      $("#srtImportSummary").textContent = `${labels[result.format]} · 可导入 ${result.matched} 条 · 跳过 ${result.skipped} 条 · 覆盖已有译文 ${result.overwrites} 条`;
      const fragment = document.createDocumentFragment();
      result.rows.forEach(row => {
        const item = document.createElement("p");
        item.textContent = `${row.number} · ${row.importable ? (row.overwrite ? "覆盖译文" : "导入译文") : row.reason}\n${row.target || row.text}`;
        fragment.append(item);
      });
      $("#srtImportRows").append(fragment);
      $("#applySrtImport").disabled = !result.matched;
    } catch (error) {
      if (session === srtImport && sequence === srtPreviewSequence) $("#srtImportSummary").textContent = error.message;
    }
  };
  $("#importSrtTranslation").onclick = () => {
    $("#importMenu").open = false;
    if (!state.revision || state.operationPending) return ordinaryError("请先打开项目并等待当前操作完成");
    $("#importSrtInput").click();
  };
  $("#importSrtInput").onchange = async event => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const epoch = projectEpoch;
    try {
      if (file.size > 5_000_000) throw new Error("SRT 文件不能超过 5 MB");
      const bytes = new Uint8Array(await file.arrayBuffer());
      const encoding = bytes[0] === 255 && bytes[1] === 254 ? "utf-16le" : bytes[0] === 254 && bytes[1] === 255 ? "utf-16be" : "utf-8";
      const text = new TextDecoder(encoding, {fatal:true}).decode(bytes);
      if (epoch !== projectEpoch) return;
      srtImport = {text, projectId:state.projectId};
      $("#srtImportFormat").value = "auto";
      $("#srtImportDialog").classList.remove("hidden");
      await previewSrtImport();
    } catch (error) { ordinaryError(`读取 SRT 失败：${error.message}`); }
  };
  $("#cancelSrtImport").onclick = closeSrtImport;
  $("#previewSrtImport").onclick = previewSrtImport;
  $("#srtImportFormat").onchange = previewSrtImport;
  $("#applySrtImport").onclick = async () => {
    const session = srtImport;
    if (!session?.request || session.projectId !== state.projectId || state.operationPending) return;
    state.operationPending = true;
    $("#applySrtImport").disabled = true;
    $("#cancelSrtImport").disabled = true;
    $("#previewSrtImport").disabled = true;
    $("#srtImportFormat").disabled = true;
    try {
      const result = await api(`/api/projects/${encodeURIComponent(session.projectId)}/import-srt-translation`, {
        method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({...session.request, apply:true})
      });
      setRevision(result.revision);
      recordCommittedRevision(state.revision, {kind:"import", operation:"srt_translation_import", metadata:{}});
      closeSrtImport();
    } catch (error) {
      $("#srtImportSummary").textContent = `导入失败：${error.message}。可重试；项目变化时请重新匹配。`;
      $("#applySrtImport").disabled = false;
    } finally {
      state.operationPending = false;
      $("#cancelSrtImport").disabled = false;
      $("#previewSrtImport").disabled = false;
      $("#srtImportFormat").disabled = false;
      renderHeader();
    }
  };
  $("#importProjectInput").onchange = async event => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await api("/api/project-imports/subtitle-project", {method:"POST", body:form});
      await loadProject(result.project_id);
    } catch (error) {
      ordinaryError(`导入失败：${error.message}`);
    }
  };
  $("#aiReviewMenu").addEventListener("toggle", () => {
    if (!$("#aiReviewMenu").open) return;
    if (isAdvancedTutorial() && state.tutorial.active && state.tutorial.step === 5) advanceEditorTutorial(5);
  });
  $(".external-review-scope-tabs").onclick = event => {
    const button = event.target.closest("[data-review-scope]");
    if (!button) return;
    const scope = button.dataset.reviewScope;
    document.querySelectorAll("[data-review-scope]").forEach(control => {
      const active = control === button;
      control.classList.toggle("active", active);
      control.setAttribute("aria-pressed", String(active));
    });
    $("#externalReviewRangeField").classList.toggle("hidden", scope !== "range");
    if (scope === "range") $("#externalReviewRange").focus();
  };
  $("#copyExternalReview").onclick = copyExternalReviewContent;
  $("#downloadExternalReview").onclick = downloadExternalReviewContent;
  $("#referenceManuscript").onclick = () => { state.taskPanelDismissed = false; runReferenceManuscript(); };
  $("#referenceKeepAsr").onclick = () => chooseReferenceVersion(false);
  $("#referenceApply").onclick = () => chooseReferenceVersion(true);
  $("#fontDecrease").onclick = () => { state.fontSize = Math.max(11, state.fontSize - 1); render(); };
  $("#fontIncrease").onclick = () => { state.fontSize = Math.min(22, state.fontSize + 1); render(); };
  $("#showTranslations").onchange = event => {
    document.body.classList.toggle("hide-editor-translations", !event.target.checked);
    renderCues({preservePage:true});
    updateTimelineHeight();
    state.timelineController?.redraw();
  };
  $("#refreshDocumentBottom").onclick = () => state.projectId && loadProject(state.projectId);
  $("#undoDocument").onclick = undoLatestRevision;
  $("#redoDocument").onclick = redoLatestRevision;
  $("#resetDocument").onclick = resetToCheckpoint;
  $("#cueList").addEventListener("click", event => {
    const pageControl = event.target.closest("[data-cue-page-start]");
    if (pageControl) {
      const index = Math.max(0, Number(pageControl.dataset.cuePageStart) || 0);
      const cue = state.view?.cue_views[index];
      if (cue) {
        state.cuePageStart = index;
        state.activeCueId = cue.cue_id;
        renderCues();
        renderPreview();
        $("#cueList").scrollTop = 0;
      }
      return;
    }
    const deletedRow = event.target.closest(".cue-row.deleted");
    if (deletedRow && !event.target.closest('[data-cue-action="restore"]')) {
      event.preventDefault();
      return;
    }
    const connector = event.target.closest("[data-token-connector]");
    if (connector && !connector.disabled) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    const translation = event.target.closest("[data-target-edit]");
    if (translation) {
      activateTranslationCue(translation);
      return;
    }
    const token = event.target.closest("[data-token-id]");
    if (token) {
      selectToken(token.dataset.tokenId, {
        toggle:event.ctrlKey || event.metaKey,
        range:event.shiftKey
      });
      observeEditorTutorialEvent("token_select", {tokenId:token.dataset.tokenId});
      return;
    }
    const action = event.target.closest("[data-cue-action]");
    if (action) {
      cueAction(action.closest(".cue-row").dataset.cueId, action.dataset.cueAction);
      return;
    }
    const cue = event.target.closest("[data-cue-select]");
    if (cue) {
      selectCueGroup(cue.dataset.cueSelect, event);
      return;
    }
    const row = event.target.closest(".cue-row");
    const interactive = event.target.closest("button, textarea, input, select, a, [contenteditable='true']");
    if (row && !interactive) selectCueGroup(row.dataset.cueId, event);
  });
  $("#cueList").addEventListener("dblclick", event => {
    const connector = event.target.closest("[data-token-connector]");
    if (connector && !connector.disabled) {
      event.preventDefault();
      event.stopPropagation();
      if (document.documentElement.dataset.editorLayout === "experimental" && connector.classList.contains("cue-end")) {
        beginWholeCueEdit(connector);
      } else {
        insertTokenAfter(connector.dataset.cueId, connector.dataset.insertAfter);
      }
      return;
    }
    const cueMeta = event.target.closest("[data-cue-select]");
    if (cueMeta) {
      editCueTime(cueMeta.dataset.cueSelect);
      return;
    }
    const token = event.target.closest("[data-token-id]");
    if (!token || token.disabled) return;
    state.selectedTokenIds = new Set([token.dataset.tokenId]);
    state.selectionAnchorTokenId = token.dataset.tokenId;
    beginInlineTokenEdit(token);
  });
  $("#cueList").addEventListener("contextmenu", event => {
    if (tokenSplitMode) { splitTokenAtPointer(event); return; }
    const boundary = event.target.closest("[data-boundary-control]");
    if (!boundary || boundary.disabled) return;
    event.preventDefault();
    event.stopPropagation();
    const cueId = boundary.dataset.cueId;
    const action = boundary.dataset.boundaryAction;
    const anchor = boundary.dataset.boundaryAfter;
    if (!cueId || !action) return;
    if (action === "split") {
      if (!anchor) return;
      state.selectedTokenIds = new Set([anchor]);
      state.selectionAnchorTokenId = anchor;
    }
    cueAction(cueId, action);
  });
  $("#cueList").addEventListener("input", event => {
    const target = event.target.closest("[data-target-edit]");
    if (target) resizeTranslationField(target);
  });
  function activateTranslationCue(target) {
    if (target.disabled) return;
    suspendPlaybackFollow();
    state.selectedTokenIds.clear();
    state.selectionAnchorTokenId = null;
    activateCue(target.dataset.targetEdit);
    refreshTokenSelectionUi();
  }
  $("#cueList").addEventListener("focusin", event => {
    const source = event.target.closest("[data-source-edit]");
    if (source && !source.disabled) {
      suspendPlaybackFollow();
      state.selectedTokenIds.clear();
      state.selectionAnchorTokenId = null;
      activateCue(source.dataset.sourceEdit);
      refreshTokenSelectionUi();
    }
    const target = event.target.closest("[data-target-edit]");
    if (target) {
      activateTranslationCue(target);
      if (target.dataset.rawEditing !== "true") {
        target.value = target.dataset.originalTarget || "";
        target.dataset.rawEditing = "true";
        resizeTranslationField(target);
      }
    }
  });
  $("#cueList").addEventListener("focusout", event => {
    const source = event.target.closest("[data-source-edit]");
    if (source && !source.readOnly && source.value !== source.dataset.originalSource) {
      const text = source.value;
      sendOperation(contract.createOperation(state.revision, "set_source_text", {
        cue_id:source.dataset.sourceEdit, text, expected_source_text:source.dataset.originalSource,
        provenance:manualProvenance("set_source_text")}))
        .then(result => {if (result) source.dataset.originalSource = text;})
        .catch(error => ordinaryError(error.message));
    }
    const target = event.target.closest("[data-target-edit]");
    if (target) commitInlineTarget(target);
  });
  $("#cueList").addEventListener("keydown", event => {
    const target = event.target.closest("[data-target-edit]");
    if (!target) return;
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      target.blur();
    }
  });
  let cueDrag = null, suppressCueClick = false;
  function cueOrder() { return (state.view?.cue_views || []).filter(c => c.state === "active").map(c => c.cue_id); }
  function refreshCueSelection() {
    const ids=state.cueSelection.ids;
    state.timelineController?.redraw();
    $("#cueList").querySelectorAll(".cue-row[data-cue-id]").forEach(row => {
      row.classList.toggle("cue-selected", ids.has(row.dataset.cueId));
      row.setAttribute("aria-selected", String(ids.has(row.dataset.cueId)));
    });
    assPanel?.selectionChanged();
  }
  function selectCueGroup(id, event) {
    if (suppressCueClick) { suppressCueClick=false; return; }
    state.selectedTokenIds.clear();
    state.selectionAnchorTokenId=null;
    refreshTokenSelectionUi();
    state.cueSelection.select(id, cueOrder(), {range:event.shiftKey,toggle:event.ctrlKey || event.metaKey});
    refreshCueSelection();
    selectCue(id, !event.shiftKey && !event.ctrlKey && !event.metaKey);
  }
  // A gesture keeps the target chosen on pointerdown, even when crossing other regions.
  document.body.append($("#tokenMarquee"));
  function stepCueDrag(schedule = true, scroll = true) {
    if (!cueDrag) return;
    const list=$("#cueList"), bounds=list.getBoundingClientRect();
    if (cueDrag.moved) {
      const delta=cueDrag.y<bounds.top+32 ? -12 : cueDrag.y>bounds.bottom-32 ? 12 : 0;
      if (scroll && delta) list.scrollTop+=delta;
      const offset=list.scrollTop-cueDrag.startScroll;
      const box=marqueeRect(cueDrag.startX,cueDrag.startY-offset,cueDrag.x,cueDrag.y);
      const marquee=$("#tokenMarquee");
      marquee.classList.remove('hidden');marquee.dataset.mode=cueDrag.mode;
      const left=Math.max(bounds.left,box.left), top=Math.max(bounds.top,box.top);
      Object.assign(marquee.style,{left:`${left}px`,top:`${top}px`,
        width:`${Math.max(2,Math.min(bounds.right,box.right)-left)}px`,
        height:`${Math.max(2,Math.min(bounds.bottom,box.bottom)-top)}px`});
      if (cueDrag.mode==='tokens') {
        state.cueSelection.clear();refreshCueSelection();
        // Content coordinates remain stable while scrolling and retain rows leaving the DOM window.
        list.querySelectorAll('.cue-row:not(.deleted) .display-token').forEach(node => {
          const rect=node.getBoundingClientRect();
          cueDrag.tokenBoxes.set(node.dataset.tokenId,{left:rect.left,right:rect.right,
            top:rect.top+list.scrollTop,bottom:rect.bottom+list.scrollTop});
        });
        const contentRect=marqueeRect(cueDrag.startX,cueDrag.startY+cueDrag.startScroll,
          cueDrag.x,cueDrag.y+list.scrollTop);
        const ids=window.EditorCueSelection.intersectingTokens(cueDrag.tokenBoxes,contentRect);
        state.selectedTokenIds=new Set([...cueDrag.originalTokens,...ids]);
        if (ids.length) state.selectionAnchorTokenId=ids.at(-1);
        list.querySelectorAll('.display-token').forEach(node =>
          node.classList.toggle('selected',state.selectedTokenIds.has(node.dataset.tokenId)));
      } else {
        state.selectedTokenIds.clear();state.selectionAnchorTokenId=null;
        list.querySelectorAll('.display-token.selected').forEach(node=>node.classList.remove('selected'));
        const rows=[...list.querySelectorAll('.cue-row:not(.deleted)')];
        const row=rows.find(r => r.getBoundingClientRect().bottom>=Math.max(bounds.top,Math.min(bounds.bottom-1,cueDrag.y))) || rows.at(-1);
        if (row) state.cueSelection.drag(cueDrag.first,row.dataset.cueId,cueOrder(),cueDrag.original);
        refreshCueSelection();
      }
      $("#tokenSelectionMenu").classList.add('hidden');
    }
    if (schedule) cueDrag.frame=requestAnimationFrame(() => stepCueDrag());
  }
  $("#cueList").addEventListener("pointerdown", event => {
    if (event.button!==0) return;
    cueCenterGeneration++;
    const mode=window.EditorCueSelection.dragMode(event.target, state.activeCueId);
    if (!mode || event.target.closest('.cue-row.deleted')) return;
    const row=event.target.closest('.cue-row:not(.deleted)') ||
      [...event.currentTarget.querySelectorAll('.cue-row:not(.deleted)')].find(node => node.getBoundingClientRect().bottom>=event.clientY);
    if (!row) return;
    if (mode === 'tokens') suspendPlaybackFollow();
    suppressCueClick=false;
    const additive=event.ctrlKey || event.metaKey;
    cueDrag={mode,id:event.pointerId,first:row.dataset.cueId,startX:event.clientX,x:event.clientX,
      startY:event.clientY,y:event.clientY,startScroll:event.currentTarget.scrollTop,moved:false,tokenBoxes:new Map(),
      original:additive ? [...state.cueSelection.ids] : [],originalTokens:additive ? [...state.selectedTokenIds] : []};
    // A pending drag must not cancel focus/blur or ordinary clicks.
    stepCueDrag();
  });
  window.addEventListener('pointermove',event => {
    if (!cueDrag || cueDrag.id!==event.pointerId) return;
    cueDrag.x=event.clientX;cueDrag.y=event.clientY;
    if (Math.hypot(event.clientX-cueDrag.startX,event.clientY-cueDrag.startY)>4) {
      if (!cueDrag.moved) suspendPlaybackFollow();
      cueDrag.moved=true;
      const list=$("#cueList");
      if (!list.hasPointerCapture(event.pointerId)) list.setPointerCapture(event.pointerId);
      stepCueDrag(false,false);
      event.preventDefault();
    }
  });
  function endCueDrag(event) {
    if (!cueDrag || event.pointerId!==cueDrag.id) return;
    if (event.type==='pointerup') {
      cueDrag.x=event.clientX;cueDrag.y=event.clientY;
      if (Math.hypot(event.clientX-cueDrag.startX,event.clientY-cueDrag.startY)>4) {
        if (!cueDrag.moved) suspendPlaybackFollow();
        cueDrag.moved=true;
      }
      stepCueDrag(false,false);
    }
    cancelAnimationFrame(cueDrag.frame);
    suppressCueClick=cueDrag.moved;cueDrag=null;
    $("#tokenMarquee").classList.add('hidden');
    if ($("#cueList").hasPointerCapture(event.pointerId)) $("#cueList").releasePointerCapture(event.pointerId);
    if (suppressCueClick) refreshTokenSelectionUi();
    setTimeout(() => { suppressCueClick=false; },0);
  }
  $("#cueList").addEventListener('click',event => {
    if (!suppressCueClick) return;
    event.preventDefault();event.stopImmediatePropagation();suppressCueClick=false;
  },true);
  window.addEventListener('pointerup',endCueDrag);
  window.addEventListener('pointercancel',endCueDrag);
  $("#cueList").addEventListener("wheel", event => {
    cueCenterGeneration++;
    if (event.ctrlKey || event.metaKey || event.shiftKey || !event.deltaY) return;
    const list = event.currentTarget;
    const lineHeight = parseFloat(getComputedStyle(list).lineHeight) || 16;
    const unit = event.deltaMode === 1 ? lineHeight : event.deltaMode === 2 ? list.clientHeight : 1;
    event.preventDefault();
    list.scrollTop += event.deltaY * unit / 3;
  }, {passive:false});
  $("#cueList").addEventListener("scroll", () => {
    positionSelectionMenus();
    positionEditorTutorial();
  }, {passive:true});
  new MutationObserver(() => {
    positionEditorTutorial();
    refreshCueSelection();
  }).observe($("#cueList"), {
    childList:true, subtree:true
  });
  document.addEventListener("pointerdown", event => {
    if (event.target.closest(".display-token,.token-selection-menu,.reference-selection-menu,#revisionMenu,#versionMenu")) return;
    if (event.target.closest("#cueList")) return;
    if (!state.selectedTokenIds.size) return;
    state.selectedTokenIds.clear();
    state.selectionAnchorTokenId = null;
    refreshTokenSelectionUi();
  });
  $("#timelineTrack").addEventListener("click", event => {
    if (state.timelineController) return;
    const cue = event.target.closest("[data-timeline-cue]");
    if (cue) selectCue(cue.dataset.timelineCue, true);
    else seekTimeline(event);
  });
  $("#timelineTrack").addEventListener("contextmenu", event => {
    if (state.timelineController) return;
    createCueAtTimelinePoint(event);
  });
  $("#textDialogForm").addEventListener("submit", event => {
    event.preventDefault();
    closeTextDialog($("#textDialogValue").value);
  });
  $("#cancelTextDialog").onclick = () => closeTextDialog(null);
  $("#instructionDialogForm").addEventListener("submit", event => {
    event.preventDefault();
    closeInstructionDialog($("#instructionDialogValue").value);
  });
  $("#cancelInstruction").onclick = () => closeInstructionDialog(null);
  $("#detectSpeakers").onclick = event => {
    const menu = $("#speakerMenu");
    if (menu) {
      event.preventDefault();
      if ($("#detectSpeakers").disabled) return;
      if (menu.open) { closeSpeakerDialog(); return; }
    }
    openSpeakerDialog();
  };
  $("#cancelSpeakerDialog").onclick = closeSpeakerDialog;
  $("#addSpeaker")?.addEventListener("click", () => {
    const ids = [...document.querySelectorAll("[data-speaker-name]")].map(node => node.dataset.speakerName);
    const id = window.EditorSpeakers.nextId(ids);
    appendSpeakerInput(id, window.EditorSpeakers.label(id)).focus();
  });
  $("#speakerDialogForm").addEventListener("submit", async event => {
    event.preventDefault();
    const names = Object.fromEntries([...document.querySelectorAll("[data-speaker-name]")]
      .map(input => [input.dataset.speakerName, input.value.trim()]));
    closeSpeakerDialog();
    await sendOperation(contract.setSpeakerNamesOperation(
      state.revision, names, manualProvenance("set_speaker_names")
    ));
    if($("#speakerDialogForm").closest('[data-tool-panel="dubbing"]'))populateSpeakerSettings();
  });
  $("#closeValidation").onclick = closeValidationModal;
  $("#dismissValidation").onclick = closeValidationModal;
  $("#hardIssueList").addEventListener("click", event => {
    const issue = event.target.closest("[data-issue-cue]");
    if (!issue?.dataset.issueCue) return;
    closeValidationModal();
    selectCue(issue.dataset.issueCue, true);
  });
  window.addEventListener("online", () => {
    if (state.mediaLoadFailed) scheduleMediaRetry();
  });
  let assPanelPosition = null;
  let assPanelDrag = null;
  function positionAssPanel(x, y) {
    const panel = $("#assForm");
    const rect = panel.getBoundingClientRect();
    const left = Math.max(12, Math.min(x, window.innerWidth - rect.width - 12));
    const top = Math.max(12, Math.min(y, window.innerHeight - rect.height - 12));
    panel.style.left = `${left}px`; panel.style.top = `${top}px`;
    assPanelPosition = {x:left, y:top};
  }
  function openAssPanelPosition() {
    const anchor = $("#assMenu summary").getBoundingClientRect();
    const rect = $("#assForm").getBoundingClientRect();
    positionAssPanel(assPanelPosition?.x ?? anchor.right - rect.width,
      assPanelPosition?.y ?? anchor.bottom + 6);
  }
  $("#assPanelHandle").addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.target.closest("button")) return;
    const rect = $("#assForm").getBoundingClientRect();
    assPanelDrag = {id:event.pointerId, x:event.clientX-rect.left, y:event.clientY-rect.top};
    event.currentTarget.setPointerCapture(event.pointerId);
    event.currentTarget.classList.add("dragging");
    event.preventDefault();
  });
  $("#assPanelHandle").addEventListener("pointermove", event => {
    if (assPanelDrag?.id !== event.pointerId) return;
    positionAssPanel(event.clientX-assPanelDrag.x, event.clientY-assPanelDrag.y);
  });
  ["pointerup", "pointercancel", "lostpointercapture"].forEach(name => {
    $("#assPanelHandle").addEventListener(name, event => {
      if (assPanelDrag?.id !== event.pointerId) return;
      assPanelDrag = null; event.currentTarget.classList.remove("dragging");
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    });
  });
  $("#assClose").onclick = () => {
    $("#assMenu").open = false;
    document.querySelector("#experimental-subtitle-panel-open")?.focus();
  };
  $("#assForm").addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); $("#assClose").click(); }
  });
  window.addEventListener("resize", () => { if ($("#assMenu").open) openAssPanelPosition(); });

  const assPanel = window.EditorAssPanel.create({state, api, projectPath, activeMedia, systemSaveAs,
    flush:flushActiveEditorInput, setRevision, loadRevisionHistory,
    askText, onError:ordinaryError});
  if (window.EditorMawAssPanel) window.SubstarMawAssPanel = window.EditorMawAssPanel.create({bridge:assPanel, state, api});

  $("#mediaSpeed").addEventListener("change", event => {
    const speed = Math.max(0.25, Math.min(3, Number(event.currentTarget.value) || 1));
    if (activeMedia()) activeMedia().playbackRate = speed;
  });
  $("#mediaPlayToggle").onclick = toggleMediaPlayback;
  $("#mediaSeek").addEventListener("input", event => {
    const time = Number(event.currentTarget.value) || 0;
    if (activeMedia()) activeMedia().currentTime = time;
    const cue = state.view?.cue_views.find(item =>
      item.state === "active" && time >= Number(item.start) && time < Number(item.end)
    );
    syncPlaybackFollowAfterSeek();
    activateCue(cue?.cue_id || null, {scroll:true, revealTimeline:true, explicitNavigation:true});
    state.timelineController?.revealTime(time, false);
    syncMediaControls();
  });
  $("#mediaVolume").addEventListener("input", event => {
    const media = activeMedia();
    if (!media) return;
    const volume = Math.max(0, Math.min(100, Number(event.currentTarget.value) || 0)) / 100;
    if (volume > 0) lastAudibleVolume = volume;
    media.volume = volume;
    media.muted = volume === 0;
    syncMediaControls();
  });
  $("#mediaMute").onclick = () => {
    const media = activeMedia();
    if (!media) return;
    if (media.muted || media.volume === 0) {
      if (media.volume === 0) media.volume = lastAudibleVolume;
      media.muted = false;
    } else { media.muted = true; }
    syncMediaControls();
  };
  $("#mediaFullscreen").onclick = () => {
    const frame = document.fullscreenElement ? null : $(".media-frame");
    if (frame?.requestFullscreen) frame.requestFullscreen();
    else if (document.fullscreenElement) document.exitFullscreen();
  };
  new ResizeObserver(updateMediaViewport).observe($("#mediaStage"));
  window.addEventListener("resize", () => {
    updateMediaViewport();
    positionSelectionMenus();
    positionEditorTutorialIntro();
    positionEditorTutorial();
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && state.tutorial.active) {
      event.preventDefault();
      exitEditorTutorial();
      return;
    }
    if (event.repeat || !state.projectId || !state.revision) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
    const shortcut = shortcutFromEvent(event).toLowerCase();
    if (shortcut === state.shortcuts.undo.toLowerCase()) {
      event.preventDefault();
      undoLatestRevision();
    } else if (shortcut === state.shortcuts.redo.toLowerCase()) {
      event.preventDefault();
      redoLatestRevision();
    }
  });

  document.addEventListener("keydown", event => {
    if (event.repeat || shortcutFromEvent(event).toLowerCase() !== state.shortcuts.playPause.toLowerCase()) return;
    if (isTextEditingContext(event)) return;
    event.preventDefault();
    event.stopPropagation();
    toggleMediaPlayback();
  }, true);

  document.addEventListener("keydown", event => {
    if (state.timelineController) return;
    if (shortcutFromEvent(event).toLowerCase() !== state.shortcuts.hideCue.toLowerCase()
      || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
    const cue = currentCue();
    if (!cue || cue.state === "deleted") return;
    event.preventDefault();
    cueAction(cue.cue_id, "hide");
  });

  document.querySelectorAll("#editorTools > .tool-accordion").forEach(panel => {
    panel.addEventListener("toggle", () => {
      if (document.documentElement.dataset.editorLayout === "experimental" || !panel.open) return;
      document.querySelectorAll("#editorTools > .tool-accordion[open]").forEach(other => {
        if (other !== panel) other.open = false;
      });
    });
  });

  function initializeReviewNotes() {
    const importOpinions = document.createElement("button");
    importOpinions.type = "button";
    importOpinions.id = "importReviewOpinions";
    importOpinions.textContent = "审阅意见";
    $("#importMenu .editor-command-popover").append(importOpinions);
    const opinionFile = document.createElement("input");
    opinionFile.type = "file";
    opinionFile.accept = ".json,application/json";
    opinionFile.hidden = true;
    importOpinions.after(opinionFile);
    const exportOpinions = document.createElement("button");
    exportOpinions.type = "button";
    exportOpinions.id = "exportReviewOpinions";
    exportOpinions.textContent = "审阅意见";
    $("#exportMenu .export-popover").append(exportOpinions);
    exportOpinions.onclick = async () => {
      $("#exportMenu").open = false;
      if (!state.projectId) return;
      try {
        const epoch = projectEpoch;
        const result = await systemSaveAs.saveBlob({
          suggestedName:`${systemSaveAs.safeFilename(state.taskInfo?.display_name || state.projectId)}_审阅意见.json`,
          description:"审阅意见", mimeType:"application/json", extension:".json"
        }, async () => {
          await flushActiveEditorInput();
          if (epoch !== projectEpoch) throw new Error("项目已切换，请重新导出");
          const packageData = await api(projectPath("/review-opinions/export"));
          if (epoch !== projectEpoch) throw new Error("项目已切换，请重新导出");
          return new Blob([JSON.stringify(packageData, null, 2)], {type:"application/json;charset=utf-8"});
        });
        if (!result.cancelled) ordinaryError(`已保存：${result.filename}`, "completed");
      } catch (error) {ordinaryError(error.status === 404 ? "请重启 Substar 以加载审阅意见导入、导出功能" : error.message);}
    };
    importOpinions.onclick = () => {
      $("#importMenu").open = false;
      if (state.projectId) opinionFile.click();
    };
    opinionFile.onchange = async () => {
      const file = opinionFile.files[0];
      opinionFile.value = "";
      if (!file) return;
      const project = state.projectId;
      importOpinions.disabled = true;
      try {
        if (file.size > 10_000_000) throw new Error("审阅意见文件不能超过 10 MB");
        let packageData;
        try {packageData = JSON.parse((await file.text()).replace(/^\uFEFF/, ""));}
        catch {throw new Error("审阅意见文件不是有效的 JSON");}
        await flushActiveEditorInput();
        await refresh();
        if (project !== state.projectId) return;
        reviewData = await api(projectPath("/review-opinions/import"), {method:"POST", body:JSON.stringify({
          expected_version:reviewData.version, expected_revision_id:state.revision.revision_id, package:packageData
        })});
        display();
        setEnabled(true);
        const result = reviewData.import_summary;
        ordinaryError(`审阅意见已导入：新增 ${result.added}，未变化 ${result.unchanged}，冲突 ${result.conflicts}。本地字幕保持当前版本。`, "completed");
      } catch (error) {ordinaryError(error.status === 404 ? "请重启 Substar 以加载审阅意见导入、导出功能" : error.message);}
      finally {importOpinions.disabled = false;}
    };
    const button = document.createElement("button");
    button.type = "button";
    button.id = "reviewModeToggle";
    button.textContent = "修订";
    button.setAttribute("aria-pressed", "false");
    const selector = $("#timelineLayout").parentElement;
    if (document.documentElement.dataset.editorLayout === "experimental") $(".pane-live-controls").prepend(button);
    else selector.before(button);
    const panel = document.createElement("section");
    panel.className = "review-notes-panel hidden";
    panel.setAttribute("aria-label", "字幕修订");
    const toolbar = document.createElement("div");
    toolbar.className = "tool-locator-row";
    const heading = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = "修订";
    const counter = document.createElement("span");
    heading.append(title, counter);
    const navigation = document.createElement("div");
    navigation.className = "tool-button-pair";
    toolbar.append(heading, navigation);
    const list = document.createElement("div");
    panel.append(toolbar, list);
    $("#editorTools [data-tool-panel='locator'] .tool-panel").append(panel);
    document.addEventListener("pointerdown", event => {
      if (event.target.closest?.(".display-token")) reviewSelection = null;
    });
    document.addEventListener("click", event => {
      const token = event.target.closest?.(".display-token.review-annotated");
      if (!reviewEnabled || !token) return;
      const index = reviewData.notes.findIndex(note => note.status !== "deleted" && note.anchor.track === "source" && note.anchor_status !== "needs_relocation"
        && (note.anchor.token_ids?.includes(token.dataset.tokenId)
          || (!note.anchor.token_ids?.length && note.anchor.cue_id === token.closest("[data-cue-id]")?.dataset.cueId)));
      if (index < 0) return;
      position = index;
      updateNavigation();
      list.querySelectorAll(".review-note-card").forEach(card => {
        const active = card.dataset.noteId === reviewData.notes[index].id;
        card.classList.toggle("active", active);
        if (active) card.scrollIntoView({block:"nearest"});
      });
    });
    document.addEventListener("select", event => {
      const field = event.target;
      if (!field.matches?.(".translation-track")) return;
      reviewSelection = field.selectionEnd > field.selectionStart ? {
        cueId:field.closest("[data-cue-id]")?.dataset.cueId,
        track:field.classList.contains("source-text-track") ? "source" : "target",
        range:[Array.from(field.value.slice(0, field.selectionStart)).length, Array.from(field.value.slice(0, field.selectionEnd)).length]
      } : null;
    });
    function control(label, action) {
      const node = document.createElement("button");
      node.type = "button";
      node.textContent = label;
      node.onclick = () => Promise.resolve(action()).catch(error => ordinaryError(error.message));
      return node;
    }
    async function refresh() {
      const project = state.projectId;
      const data = await api(projectPath("/review-notes"));
      if (project !== state.projectId) return;
      reviewData = data;
      display();
    }
    async function write(payload) {
      const project = state.projectId;
      const data = await api(projectPath("/review-notes"), {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({...payload, expected_version:reviewData.version, expected_revision_id:state.revision.revision_id})});
      if (project !== state.projectId) return;
      reviewData = data;
      display();
    }
    function locate(note) {
      position = reviewData.notes.findIndex(item => item.id === note.id);
      updateNavigation();
      list.querySelectorAll(".review-note-card").forEach(card => {
        card.classList.toggle("active", card.dataset.noteId === note.id);
        if (card.dataset.noteId === note.id) card.scrollIntoView({block:"nearest"});
      });
      activateCue(note.anchor.cue_id, {seek:true, scroll:true, revealTimeline:true});
      state.selectedTokenIds = new Set(note.anchor.token_ids || []);
      refreshTokenSelectionUi();
      const field = [...document.querySelectorAll("#cueList [data-cue-id]")].find(row => row.dataset.cueId === note.anchor.cue_id)?.querySelector(note.anchor.track === "source" ? ".source-text-track" : ".translation-track:not(.source-text-track)");
      if (field && note.anchor.text_range && note.anchor_status === "current") {
        field.focus();
        const chars = Array.from(field.value);
        field.setSelectionRange(chars.slice(0, note.anchor.text_range[0]).join('').length, chars.slice(0, note.anchor.text_range[1]).join('').length);
      }
      if (note.anchor_status === "needs_relocation") ordinaryError("原字幕已变化，修订需重新定位；原文快照已保留");
    }
    let position = -1;
    function updateNavigation() {
      const total = reviewData.notes.length;
      if (position >= total) position = -1;
      counter.textContent = `${position + 1} / ${total}`;
      previous.disabled = next.disabled = !total;
    }
    function navigate(step) {
      const notes = reviewData.notes;
      if (!notes.length) return;
      position = position < 0 ? (step < 0 ? notes.length - 1 : 0) : (position + step + notes.length) % notes.length;
      locate(notes[position]);
      updateNavigation();
    }
    async function add(fromTokens = false, suppliedText = null) {
      const cueId = state.activeCueId;
      const project = state.projectId;
      if (!cueId || !project) return;
      if (editorAiTaskLocksEditor()) throw new Error("请等待当前 AI 任务完成");
      const cue = state.view.cue_views.find(cue => cue.cue_id === cueId);
      if (!cue) return;
      const track = !fromTokens && reviewSelection?.cueId === cueId ? reviewSelection.track : "source";
      const tokenIds = track === "source" ? [...state.selectedTokenIds].filter(id => cue.display_token_ids.includes(id)) : [];
      const range = !fromTokens && reviewSelection?.cueId === cueId && reviewSelection.track === track ? reviewSelection.range : null;
      if (fromTokens && !tokenIds.length) return;
      await flushActiveEditorInput();
      if (project !== state.projectId) return;
      const revisionId = state.revision?.revision_id;
      await refresh();
      let text = suppliedText;
      if (text == null) {
        setEnabled(true);
        const snapshot = tokenIds.length ? tokenIds.map(id => state.indexes.tokenById.get(id)?.text || "").join(" ") : "当前所选字幕";
        text = await compose(panel, "新修订", snapshot);
      }
      if (!text) return;
      if (project !== state.projectId || revisionId !== state.revision?.revision_id) throw new Error("字幕版本已变化，请重新选择修订位置");
      await write({action:"add", cue_id:cueId, track, token_ids:range ? [] : tokenIds, text_range:range, text});
      setEnabled(true);
      return true;
    }
    const previous = control("上一条", () => navigate(-1));
    const next = control("下一条", () => navigate(1));
    navigation.append(previous, next);
    list.className = "review-notes-list";
    const tokenForm = document.createElement("form");
    tokenForm.id = "tokenReviewForm";
    tokenForm.className = "token-review-form";
    const tokenInput = document.createElement("input");
    tokenInput.type = "text";
    tokenInput.placeholder = "输入修订…";
    tokenInput.setAttribute("aria-label", "修订内容");
    tokenInput.maxLength = 10000;
    tokenInput.autocomplete = "off";
    const confirm = document.createElement("button");
    confirm.type = "submit";
    confirm.textContent = "添加";
    confirm.disabled = true;
    const removeNote = document.createElement("button");
    removeNote.type = "button";
    removeNote.textContent = "删除";
    removeNote.setAttribute("aria-label", "删除修订");
    removeNote.className = "hidden";
    tokenForm.append(tokenInput, confirm, removeNote);
    $("#tokenSelectionMenu").append(tokenForm);
    let submitting = false;
    function selectedNote() {
      const ids = [...state.selectedTokenIds];
      if (!ids.length) return null;
      return reviewData.notes.find(note => note.status !== "deleted" && note.anchor.track === "source"
        && ids.every(id => note.anchor.token_ids?.includes(id))) || null;
    }
    function selectionConflict() {
      const ids = [...state.selectedTokenIds];
      if (!ids.length) return "请选择词元";
      if (new Set(ids.map(id => state.indexes?.tokenToCueId?.get(id))).size !== 1) return "请选择同一字幕中的词元";
      const overlaps = reviewData.notes.filter(note => note.status !== "deleted" && note.anchor.track === "source"
        && ids.some(id => note.anchor.token_ids?.includes(id)));
      if (overlaps.length && (overlaps.length !== 1 || !selectedNote())) return "不能与已有修订组重叠";
      return "";
    }
    syncTokenReviewForm = (force = false) => {
      if (submitting) return;
      const key = [...state.selectedTokenIds].sort().join("|");
      const note = selectedNote();
      const signature = `${key}:${note?.id || ""}:${note?.text || ""}`;
      if (!force && tokenForm.dataset.selectionKey === signature) return;
      tokenForm.dataset.selectionKey = signature;
      tokenInput.value = note?.text || "";
      const conflict = selectionConflict();
      tokenInput.disabled = !!conflict;
      tokenInput.placeholder = conflict || "输入修订…";
      confirm.textContent = note ? "保存" : "添加";
      confirm.disabled = tokenInput.disabled || !tokenInput.value.trim();
      removeNote.classList.toggle("hidden", !note);
    };
    function cancelTokenNote() {
      if (submitting) return;
      syncTokenReviewForm(true);
    }
    tokenInput.oninput = () => {confirm.disabled = submitting || !tokenInput.value.trim();};
    tokenForm.addEventListener("keydown", event => {
      event.stopPropagation();
      if (event.key === "Escape") {
        event.preventDefault();
        cancelTokenNote();
        tokenInput.blur();
        $("#tokenSelectionMenu").classList.add("hidden");
      }
      if (event.key === "Enter" && event.isComposing) event.preventDefault();
    });
    document.addEventListener("pointerdown", event => {
      if (!reviewEnabled || event.target.closest?.("#tokenReviewForm")) return;
      cancelTokenNote();
      if (!event.target.closest?.(".display-token,#tokenSelectionMenu")) $("#tokenSelectionMenu").classList.add("hidden");
    });
    tokenForm.onsubmit = async event => {
      event.preventDefault();
      const text = tokenInput.value.trim();
      if (!reviewEnabled || !text || submitting || selectionConflict()) return;
      submitting = true;
      tokenInput.readOnly = true;
      confirm.disabled = true;
      try {
        if (await add(true, text)) {
          tokenForm.reset();
          tokenInput.blur();
          $("#tokenSelectionMenu").classList.add("hidden");
        }
      } catch (error) {ordinaryError(error.message);}
      finally {
        submitting = false;
        tokenInput.readOnly = false;
        syncTokenReviewForm(true);
      }
    };
    removeNote.onclick = async () => {
      const note = selectedNote();
      if (!reviewEnabled || !note || submitting) return;
      submitting = true;
      removeNote.disabled = confirm.disabled = true;
      tokenInput.readOnly = true;
      try {
        await write({action:"delete", note_id:note.id});
      } catch (error) {
        const outdated = error.status === 422 && Array.isArray(error.detail)
          && error.detail.some(item => item.loc?.includes("action") && item.type === "literal_error");
        ordinaryError(outdated ? "当前后端尚未加载删除修订功能，请重启 Substar 后再删除；修订仍保留。" : error.message);
      }
      finally {
        submitting = false;
        removeNote.disabled = false;
        tokenInput.readOnly = false;
        syncTokenReviewForm(true);
      }
    };
    updateNavigation();
    let cancelComposer = null;
    function compose(container, label, snapshot = "") {
      cancelComposer?.();
      return new Promise(resolve => {
        const form = document.createElement("form");
        form.className = "review-note-composer";
        const title = document.createElement("strong");
        title.textContent = label;
        const quote = document.createElement("blockquote");
        quote.textContent = snapshot;
        const field = document.createElement("textarea");
        field.setAttribute("aria-label", label === "回复修订" ? "回复内容" : "修订内容");
        field.placeholder = label === "回复修订" ? "输入回复…" : "输入修订…";
        field.rows = 2;
        field.maxLength = 10000;
        const actions = document.createElement("div");
        actions.className = "review-note-actions";
        const finish = value => {form.remove(); cancelComposer = null; resolve(value);};
        cancelComposer = () => finish(null);
        const submit = document.createElement("button");
        submit.type = "submit";
        submit.textContent = "保存";
        submit.disabled = true;
        field.oninput = () => {submit.disabled = !field.value.trim();};
        form.onsubmit = event => {event.preventDefault(); if (field.value.trim()) finish(field.value.trim());};
        actions.append(control("取消", () => finish(null)), submit);
        form.append(title);
        if (snapshot) form.append(quote);
        form.append(field, actions);
        if (container === panel) list.before(form); else container.append(form);
        form.scrollIntoView({block:"nearest"});
        field.focus();
      });
    }
    function display() {
      renderCues({preservePage:true});
      state.timelineController?.redraw();
      list.replaceChildren();
      updateNavigation();
      for (const note of reviewData.notes) {
        const card = document.createElement("article");
        card.className = "review-note-card";
        card.classList.toggle("resolved", note.status !== "open");
        card.classList.toggle("active", reviewData.notes[position]?.id === note.id);
        card.dataset.noteId = note.id;
        card.tabIndex = 0;
        card.setAttribute("aria-label", `修订：${note.anchor.text_snapshot}`);
        card.onclick = event => {if (!event.target.closest("button,textarea,form,details")) locate(note);};
        card.onkeydown = event => {
          if (event.target === card && (event.key === "Enter" || event.key === " ")) {event.preventDefault(); locate(note);}
        };
        const header = document.createElement("header");
        const author = document.createElement("strong");
        author.textContent = note.author || "修订";
        const date = document.createElement("time");
        const created = new Date(note.created_at);
        date.dateTime = note.created_at || "";
        date.textContent = Number.isNaN(created.getTime()) ? "" : created.toLocaleString("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false});
        date.title = Number.isNaN(created.getTime()) ? "" : created.toLocaleString("zh-CN");
        header.append(author, date);
        const quote = document.createElement("blockquote");
        quote.textContent = note.anchor.text_snapshot;
        const text = document.createElement("p");
        text.textContent = note.text;
        const status = document.createElement("small");
        status.textContent = `${({open:"待处理", accepted:"已接受", rejected:"已拒绝", resolved:"已解决"})[note.status] || "待处理"}`
          + (note.anchor_status === "text_changed" ? " · 原文已变化" : note.anchor_status === "needs_relocation" ? " · 无法定位" : "")
          + (note.status === "deleted" ? " · 本地已删除" : "");
        const actions = document.createElement("div");
        actions.className = "review-note-actions";
        for (const [action, symbol, label, value] of [["accept", "✓", "接受修订", "accepted"], ["reject", "×", "拒绝修订", "rejected"]]) {
          const decision = control(symbol, () => write({action, note_id:note.id}));
          decision.title = label;
          decision.setAttribute("aria-label", label);
          decision.setAttribute("aria-pressed", String(note.status === value));
          decision.className = `review-decision review-${action}`;
          actions.append(decision);
        }
        const footer = document.createElement("div");
        footer.className = "review-note-footer";
        footer.append(status, actions);
        card.append(header, quote, text);
        if (note.import_conflicts?.length) {
          const conflict = document.createElement("details");
          conflict.className = "review-import-conflict";
          const label = document.createElement("summary");
          label.textContent = `导入冲突（${note.import_conflicts.length}）：已保留本地版本`;
          conflict.append(label);
          for (const variant of note.import_conflicts) {
            const incoming = document.createElement("p");
            incoming.textContent = `导入版本：${variant.anchor.text_snapshot} → ${variant.text}（${({open:"待处理",accepted:"已接受",rejected:"已拒绝",resolved:"已解决",deleted:"已删除"})[variant.status]}）`;
            conflict.append(incoming);
          }
          conflict.append(control("保留本地并清除冲突", () => write({action:"keep_local", note_id:note.id})));
          card.append(conflict);
        }
        for (const reply of note.replies) {
          const text = document.createElement("p");
          text.className = "review-note-reply";
          text.textContent = `${reply.author || "回复"}：${reply.text}`;
          card.append(text);
        }
        list.append(card);
        card.append(footer);
      }
    }
    function setEnabled(enabled) {
      if (!enabled) cancelComposer?.();
      if (!enabled) cancelTokenNote();
      reviewEnabled = enabled;
      syncTokenReviewForm(true);
      $("#editorWorkbench").classList.toggle("review-mode", enabled);
      panel.classList.toggle("hidden", !reviewEnabled);
      if (reviewEnabled) $("#editorTools [data-tool-panel='locator']").open = true;
      button.textContent = reviewEnabled ? "退出修订" : "修订";
      button.setAttribute("aria-pressed", String(reviewEnabled));
      render();
      syncRevisionControls();
    }
    button.onclick = async () => {
      try {
        if (!state.projectId) return;
        if (!reviewEnabled) {
          if (editorAiTaskLocksEditor()) throw new Error("请等待当前 AI 任务完成");
          const project = state.projectId;
          await flushActiveEditorInput();
          await refresh();
          if (project !== state.projectId) return;
        }
        setEnabled(!reviewEnabled);
      } catch (error) {ordinaryError(error.message);}
    };
  }

  // Review notes UI removed; timeline markers are the annotation entry point.
  initializeCueTimeController();
  initializeCueListView();
  configureMedia("video");
  loadEditorSettings();
  loadProjects();
})();
