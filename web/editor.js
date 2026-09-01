(function () {
  "use strict";

  const contract = window.EditorDocument;
  const languageLayout = window.SubstarLanguageLayout;
  const externalReview = window.SubstarExternalReview;
  const tutorialResolver = window.EditorTutorial;
  const systemSaveAs = window.SubstarSystemSaveAs;
  const $ = selector => document.querySelector(selector);
  const CUE_PAGE_SIZE = 160;
  const state = {
    projects:[],
    projectId:"",
    taskInfo:null,
    llmOptions:null,
    failedTaskKind:"",
    revision:null,
    view:null,
    selectedTokenIds:new Set(),
    selectionAnchorTokenId:null,
    marquee:null,
    activeCueId:null,
    playbackCueId:null,
    timelineSelectedCueId:null,
    followPlayback:false,
    subtitlePolicy:{
      englishHardLimit:55,
      chineseHardLimit:25,
      mixedHardLimit:25,
      japaneseHardLimit:25,
      koreanHardLimit:32,
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
    fontSize:14,
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
    const response = await fetch(path, options);
    let body = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("json")) {
      try { body = await response.json(); } catch (_) { body = null; }
    }
    if (!response.ok) {
      const detail = body?.detail || body || {};
      const error = new Error(detail.message || `请求失败 (${response.status})`);
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
      preRollStored === null ? 20 : Number(preRollStored)
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
    const previousLocked = editorAiTaskLocksEditor();
    const previousTaskId = state.editorAiTask?.task_id || "";
    state.editorAiTask = await api(projectPath("/ai-task")).catch(() => null);
    const locked = editorAiTaskLocksEditor();
    document.body.classList.toggle("editor-ai-task-locked", locked);
    renderHeader();
    const genericTaskOwnsPanel = state.editorAiTask?.kind !== "translation";
    if ((locked || state.editorAiTask?.state === "succeeded_with_issues")
      && state.editorAiTask && genericTaskOwnsPanel) {
      const title = ({calibration:"AI 校准", translation:"字幕翻译"})[
        state.editorAiTask.kind
      ] || "AI 任务";
      const baseMessage = state.editorAiTask.display_error || state.editorAiTask.message
        || state.editorAiTask.error?.message || "任务运行中";
      const elapsedSeconds = Math.max(0, Number(state.editorAiTask.elapsed_seconds || 0));
      const taskMessage = elapsedSeconds >= 1
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
    if (state.mediaElement === next && state.mediaBindingsCleanup) return;
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
      $("#mediaPreviewLabel").textContent = normalized === "audio" ? "音频预览" : "视频预览";
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

  function syncMediaControls() {
    const media = activeMedia();
    const duration = Number.isFinite(media?.duration) ? media.duration : 0;
    $("#mediaSeek").max = String(duration);
    $("#mediaSeek").value = String(Math.min(duration, Number(media?.currentTime) || 0));
    $("#mediaTime").textContent = `${mediaTimeLabel(media?.currentTime)} / ${mediaTimeLabel(duration)}`;
    $("#mediaPlayToggle").textContent = media?.paused === false ? "❚❚" : "▶";
    $("#mediaPlayToggle").ariaLabel = media?.paused === false ? "暂停" : "播放";
    $("#mediaMute").textContent = media?.muted ? "🔇" : "🔊";
    const speed = Number(media?.playbackRate) || 1;
    $("#mediaSpeed").value = String(speed);
    $("#mediaSpeedValue").textContent = `${speed.toFixed(2)}x`;
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
        setMediaMessage();
        return;
      }
      scheduleMediaRetry();
    });
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
        languageLayout.layoutTokens(
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
    const problemCueIds = new Set(state.view.cue_views.filter(cue => {
      const lines = projectedCueLines(cue);
      const sourceMetric = subtitleLengthMetric(lines.source, state.trackLanguages.source, "source");
      const targetMetric = subtitleLengthMetric(lines.target, state.trackLanguages.target, "target");
      return sourceMetric.count > sourceMetric.limit
        || (String(lines.target || "").trim() && targetMetric.count > targetMetric.limit)
        || (latestTranslation && !String(lines.target || "").trim());
    }).map(cue => cue.cue_id));
    (state.view.changes || []).forEach(change => {
      if (!["build_from_segmentation", "build_from_split_stages"].includes(change?.operation)) return;
      (change?.metadata?.segmentation?.review_cue_ids || []).forEach(
        cueId => problemCueIds.add(String(cueId))
      );
    });
    (latestTranslation?.metadata?.translation_problem_cue_ids || []).forEach(
      cueId => problemCueIds.add(String(cueId))
    );
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
    const sourceUpper = presentation.display_order !== "target_above_source";
    const source = projectPunctuation(
      cueSourceText(cue),
      sourceUpper ? presentation.upper_remove : presentation.lower_remove,
      sourceUpper ? presentation.upper_space : presentation.lower_space
    );
    const target = projectPunctuation(
      languageLayout.formatText(cue.target?.target_text || "", state.trackLanguages?.target),
      sourceUpper ? presentation.lower_remove : presentation.upper_remove,
      sourceUpper ? presentation.lower_space : presentation.upper_space
    );
    return {source, target, sourceUpper};
  }

  function projectedSourceTokenText(text) {
    const presentation = state.view?.presentation || {};
    const sourceUpper = presentation.display_order !== "target_above_source";
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
    state.revision = contract.consumeRevision(payload);
    state.view = contract.buildEditorView(state.revision);
    rebuildViewIndexes();
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
    const previousActiveCue = preserveCueViewport
      ? state.view?.cue_views.find(cue => cue.cue_id === state.activeCueId) || null
      : null;
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
        button.setAttribute("aria-label", `${project.display_name || project.project_id}${project.tutorial_case_id ? "，教程案例" : project.complete ? "，完成稿" : ""}`);
        button.textContent = project.display_name || project.project_id;
        button.title = `${project.display_name || project.project_id}${project.complete ? " · 完成稿" : ""}`;
        list.append(button);
      });
  }

  function currentProjectBaseName() {
    const project = state.projects.find(item => item.project_id === state.projectId);
    return project?.display_name || project?.project_id || state.projectId || "当前项目";
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
    if (provenance.operation === "checkpoint") return `${baseName} · 第${checkpointNumber(item)}稿`;
    if (item.is_latest && item.complete) return `${baseName} · 完成稿`;
    if (item.is_latest) return `${baseName} · 当前编辑`;
    return `修订 r${item.revision_number}`;
  }

  function renderRevisionMenu() {
    const menu = $("#revisionMenu");
    if (!menu) return;
    const visible = state.revisions.filter(item =>
      item.is_latest || item.provenance?.operation === "checkpoint"
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
      restore.textContent = item.is_latest ? "当前" : "打开";
      row.append(label, restore);
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
      provenance:provenance || {kind:"manual", operation:"editor_commit", metadata:{}}
    };
  }

  function syncRevisionControls() {
    $("#undoDocument").disabled = state.historyNavigationPending || !state.undoRevisionIds.length;
    $("#redoDocument").disabled = state.historyNavigationPending || !state.redoRevisionIds.length;
    $("#resetDocument").disabled = !state.revisions.some(item =>
      item.provenance?.operation === "checkpoint" && !item.is_latest
    );
  }

  function logicalUndoStack(revisionId, metadataById, seen = new Set()) {
    if (!revisionId || seen.has(revisionId)) return [];
    seen.add(revisionId);
    const revision = metadataById.get(revisionId);
    if (!revision) return [];
    const metadata = revision.provenance?.metadata || {};
    if (revision.provenance?.operation === "restore_revision") {
      return Array.isArray(metadata.undo_revision_ids)
        ? metadata.undo_revision_ids.map(String) : [];
    }
    const parentId = revision.parent_revision_id;
    if (!parentId) return [];
    return [...logicalUndoStack(parentId, metadataById, seen), parentId];
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
    if (provenance?.operation !== "restore_revision") {
      if (previousRevisionId && previousRevisionId !== current.revision_id) {
        state.undoRevisionIds.push(previousRevisionId);
      } else if (!state.undoRevisionIds.length && current.parent_revision_id) {
        state.undoRevisionIds.push(current.parent_revision_id);
      }
      state.redoRevisionIds = [];
    }
    renderRevisionMenu();
    syncRevisionControls();
  }

  async function loadRevisionHistory({force = false} = {}) {
    if (!state.projectId || state.revisionHistoryLoading || (!force && state.revisionHistoryLoaded)) return;
    const projectId = state.projectId;
    state.revisionHistoryLoading = true;
    try {
      const result = await api(projectPath("/revisions"));
      if (state.projectId !== projectId) return;
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
          state.redoRevisionIds = [];
        }
      }
      renderRevisionMenu();
      syncRevisionControls();
    } catch (error) {
      ordinaryError(`版本历史读取失败：${error.message}`);
    } finally {
      if (state.projectId === projectId) state.revisionHistoryLoading = false;
    }
  }

  async function restoreRevision(revisionId, {
    navigation = "direct", undoRevisionIds = null, redoRevisionIds = null
  } = {}) {
    if (!state.revision || !revisionId) return;
    const nextUndoRevisionIds = Array.isArray(undoRevisionIds)
      ? undoRevisionIds.slice()
      : [...state.undoRevisionIds, state.revision.revision_id];
    const nextRedoRevisionIds = Array.isArray(redoRevisionIds)
      ? redoRevisionIds.slice() : [];
    try {
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
      ordinaryError(`版本恢复失败：${error.message}`);
      return null;
    }
  }

  async function createCheckpoint() {
    if (!state.revision) return;
    await loadRevisionHistory();
    const count = state.revisions.filter(item => item.provenance?.operation === "checkpoint").length;
    const checkpointNumber = count + 1;
    const label = `第${checkpointNumber}稿`;
    try {
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
      $("#draftState").textContent = `已暂存：${currentProjectBaseName()} · 第${checkpointNumber}稿`;
    } catch (error) {
      ordinaryError(`暂存失败：${error.message}`);
    }
  }

  async function undoLatestRevision() {
    if (state.historyNavigationPending || !state.revision) return;
    await loadRevisionHistory();
    if (!state.undoRevisionIds.length) return;
    const target = state.undoRevisionIds[state.undoRevisionIds.length - 1];
    const undoRevisionIds = state.undoRevisionIds.slice(0, -1);
    const redoRevisionIds = [...state.redoRevisionIds, state.revision.revision_id];
    state.historyNavigationPending = true;
    syncRevisionControls();
    await restoreRevision(target, {navigation:"undo", undoRevisionIds, redoRevisionIds});
    state.historyNavigationPending = false;
    syncRevisionControls();
  }

  async function redoLatestRevision() {
    if (state.historyNavigationPending || !state.redoRevisionIds.length || !state.revision) return;
    const target = state.redoRevisionIds[state.redoRevisionIds.length - 1];
    const redoRevisionIds = state.redoRevisionIds.slice(0, -1);
    const undoRevisionIds = [...state.undoRevisionIds, state.revision.revision_id];
    state.historyNavigationPending = true;
    syncRevisionControls();
    await restoreRevision(target, {navigation:"redo", undoRevisionIds, redoRevisionIds});
    state.historyNavigationPending = false;
    syncRevisionControls();
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
    $("#taskInfoName").value = info.display_name || projectId;
    $("#taskInfoSourceLanguage").value = info.language === "zh-CN" ? "zh" : info.language;
    $("#taskInfoTargetLanguage").value = info.target_language_mode;
    $("#taskInfoSourceLimit").value = String(info.source_hard_limit);
    $("#taskInfoTargetLimit").value = String(info.target_hard_limit);
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
          language:$("#taskInfoSourceLanguage").value,
          target_language_mode:$("#taskInfoTargetLanguage").value,
          glossary_id:String(state.taskInfo?.glossary_id || ""),
          llm_provider_id:$("#taskInfoLlmProvider").value || "inherit",
          source_hard_limit:sourceLimit,
          target_hard_limit:targetLimit
        })
      });
      state.taskInfo = updated;
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
      const revision = await api(projectPath("/presentation"), {
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
      });
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
    $("#startEditorTutorial").classList.toggle("hidden", !tutorial);
    $("#startEditorTutorial").textContent = isAdvancedTutorial() ? "启动进阶教程" : "启动初级教程";
    $("#toggleComplete").classList.toggle("complete", complete);
    $("#toggleComplete").title = complete ? "完成只是属性；取消后仍是同一可编辑项目" : "标记后仍可继续编辑";
    $("#exportMenu").classList.toggle("disabled", !revision);
    $("#exportDocument").setAttribute("aria-disabled", String(!revision));
    $("#translateDocument").disabled = !revision || locked
      || ["queued", "running", "cancelling"].includes(state.translationTask?.state);
    $("#translationMenu").classList.toggle("disabled", !revision);
    $("#aiCalibrationMenu")?.classList.toggle("disabled", !revision || locked);
    $("#aiCalibrate")?.setAttribute("aria-disabled", String(!revision || locked));
    if ((!revision || locked) && $("#aiCalibrationMenu")?.open) $("#aiCalibrationMenu").open = false;
    ["#saveCheckpoint", "#toolSearch", "#toolReplace",
      "#toolSearchNext", "#toolReplaceCurrent", "#toolReplaceAll", "#toolUndoReplace",
      "#applyAutoSnap", "#upperPunctuationRemove", "#upperPunctuationSpace",
      "#lowerPunctuationRemove", "#lowerPunctuationSpace", "#applyPunctuation",
      "#runAiCalibration", "#convertOriginal", "#convertSimplified", "#convertTraditional", "#convertTaiwan", "#convertHongKong", "#detectSpeakers"
    ].forEach(selector => { const node = $(selector); if (node) node.disabled = !revision || locked; });
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
    if (referenceButton) referenceButton.disabled = !revision || locked;
    const reviewMenu = $("#aiReviewMenu");
    const reviewDisabled = !revision || locked;
    reviewMenu?.classList.toggle("disabled", reviewDisabled);
    if (reviewDisabled && reviewMenu?.open) reviewMenu.open = false;
    $("#aiReview")?.setAttribute("aria-disabled", String(reviewDisabled));
    ["#copyExternalReview", "#downloadExternalReview"].forEach(selector => {
      const control = $(selector);
      if (control) control.disabled = !revision || locked;
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
    selectCue(entry.cue_id, true);
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
    if (match?.cue_id) selectCue(match.cue_id, true);
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
      task?.ai_progress,
      progress,
      task?.display_error || task?.error?.message || task?.message || "等待启动"
    );
    button.disabled = !state.revision || ["queued", "running", "cancelling"].includes(task?.state);
    const sourceLanguageSelect = $("#translationSourceLanguage");
    const languageSelect = $("#translationTargetLanguage");
    const mappingModeSelect = $("#translationMappingMode");
    const running = ["queued", "running", "cancelling"].includes(task?.state);
    sourceLanguageSelect.disabled = running;
    languageSelect.disabled = running;
    mappingModeSelect.disabled = running;
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
      $("#translationTaskMessage").textContent = staleCount
        ? `翻译已过期：${staleCount} 条源文在翻译后发生变化`
        : `${task.message || "翻译完成"}${task.target_language ? ` · 目标语言 ${task.target_language}` : ""}`;
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
    renderAiProgress(aiProgress, progress, message);
  }

  function renderTaskFailureRecovery(failed, kind) {
    const row = $("#taskFailureRecovery");
    if (!row) return;
    row.classList.toggle("hidden", !failed);
    if (!failed) return;
    state.failedTaskKind = kind === "translation" ? "translation" : "calibration";
    const options = state.llmOptions?.options || [];
    const select = $("#failedTaskModel");
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

  const AI_PHASE_ORDER = [
    "executing", "repair", "validating", "materializing", "publishing", "completed"
  ];

  function renderAiProgress(aiProgress, percent, fallbackMessage) {
    const counter = $("#translationTaskPercent");
    const message = $("#translationTaskMessage");
    const steps = $("#translationTaskSteps");
    if (!aiProgress?.phase || !aiProgress?.units) {
      counter.textContent = `${Math.round(percent)}%`;
      message.textContent = fallbackMessage;
      steps.classList.add("hidden");
      steps.replaceChildren();
      return;
    }
    const units = aiProgress.units;
    if (["repair", "repairing"].includes(aiProgress.phase) && Number(units.repair_planned || 0) > 0) {
      counter.textContent = `${units.repair_completed}/${units.repair_planned}`;
    } else if (Number(units.planned || 0) > 0) {
      counter.textContent = `${units.completed}/${units.planned}`;
    } else {
      counter.textContent = `${Math.round(percent)}%`;
    }
    message.textContent = aiProgress.message || fallbackMessage;
    const activeIndex = AI_PHASE_ORDER.indexOf(aiProgress.phase);
    steps.replaceChildren(...(aiProgress.steps || []).map((row, index) => {
      const item = document.createElement("li");
      item.textContent = row.label;
      item.classList.toggle("done", index < activeIndex || aiProgress.phase === "completed");
      item.classList.toggle("active", index === activeIndex && aiProgress.phase !== "completed");
      return item;
    }));
    steps.classList.toggle("hidden", !steps.childElementCount);
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
    $("#translateDocument").disabled = true;
    $("#translationSourceLanguage").disabled = true;
    $("#translationTargetLanguage").disabled = true;
    $("#translationMappingMode").disabled = true;
    $("#translationMenu").open = false;
    try {
      const task = await api(projectPath("/translation"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          source_language:sourceLanguage,
          target_language:targetLanguage,
          mapping_mode:mappingMode
        })
      });
      followTranslationTask(task);
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
    if (interactive) button.dataset.tokenId = token.token_id;
    const label = document.createElement("b");
    const projectedText = projectedSourceTokenText(token.text);
    label.textContent = projectedText;
    button.classList.toggle("punctuation-elided", !projectedText);
    const source = sourceTextForToken(token);
    if (source && source !== token.text) button.title = `原词：${source}`;
    else if (!source) button.title = "人工新增词元";
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
      chineseHardLimit:Number(settings.chinese_hard_limit ?? 25),
      mixedHardLimit:Number(settings.mixed_hard_limit ?? 25),
      japaneseHardLimit:Number(settings.japanese_hard_limit ?? 25),
      koreanHardLimit:Number(settings.korean_hard_limit ?? 32),
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
    const resolvedLanguage = language || detectSubtitleLanguage(value);
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
    const limit = override || limits[resolvedLanguage] || limits.en;
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
    const hasTarget = Boolean(cue.target);
    const sourceMetric = subtitleLengthMetric(projected.source, state.trackLanguages.source, "source");
    const targetMetric = subtitleLengthMetric(projected.target, state.trackLanguages.target, "target");
    const overLimit = sourceMetric.count > sourceMetric.limit
      || (hasTarget && targetMetric.count > targetMetric.limit);
    row.className = `cue-row${hasTarget ? " has-target" : " source-only"}${cue.cue_id === state.activeCueId ? " current" : ""}${cue.state === "deleted" ? " deleted" : ""}${overLimit ? " over-limit" : ""}`;
    row.dataset.cueId = cue.cue_id;
    if (cue.speaker) row.dataset.speaker = cue.speaker;
    const meta = document.createElement("button");
    meta.type = "button";
    meta.className = "cue-meta";
    meta.dataset.cueSelect = cue.cue_id;
    meta.innerHTML = `<strong>${cue.index + 1}</strong><span class="cue-count"><span class="cue-count-line${metricClass(sourceMetric)}">原 ${sourceMetric.count}/${sourceMetric.limit}</span>${hasTarget ? `<span class="cue-count-line${metricClass(targetMetric)}">译 ${targetMetric.count}/${targetMetric.limit}</span>` : ""}</span>`;
    meta.title = `${timeLabel(cue.start)} → ${timeLabel(cue.end)}`;
    const content = document.createElement("div");
    content.className = "cue-content";
    const line = document.createElement("div");
    const virtual = state.cueSplitView === "virtual";
    line.className = `source-token-line${virtual ? " virtual-line" : ""}`;
    const hasNext = index < state.view.cue_views.length - 1
      && state.view.cue_views[index + 1].state === "active" && cue.state === "active";
    cue.display_token_ids.forEach((id, tokenIndex) => {
      const token = tokenById.get(id);
      if (!token) return;
      line.append(tokenElement(token, {interactive:cue.state === "active"}));
      const atEnd = tokenIndex === cue.display_token_ids.length - 1;
      if (virtual && !atEnd) {
        const nextToken = tokenById.get(cue.display_token_ids[tokenIndex + 1]);
        const boundary = document.createElement(cue.state === "active" ? "button" : "span");
        const boundaryKind = languageLayout.virtualBoundaryKind(
          token.text, nextToken?.text || "", state.trackLanguages?.source
        );
        boundary.className = `virtual-boundary ${boundaryKind}-based`;
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
      line.append(connector);
    });
    content.append(line);
    if (hasTarget) {
      const target = document.createElement("textarea");
      target.rows = 1;
      target.className = "translation-track";
      target.dataset.targetEdit = cue.cue_id;
      target.dataset.originalTarget = cue.target.target_text;
      target.dataset.projectedTarget = projected.target;
      target.dataset.rawEditing = "false";
      target.title = "直接编辑译文";
      target.disabled = cue.state === "deleted" || editorAiTaskLocksEditor();
      target.value = projected.target;
      content.append(target);
    }
    const actions = document.createElement("div");
    actions.className = "cue-actions";
    actions.innerHTML = cue.state === "deleted" ? `
      <button type="button" class="restore" data-cue-action="restore" title="恢复 Cue">↶ 恢复</button>` : `
      <button type="button" class="hide-cue" data-cue-action="hide" title="隐藏 Cue（可恢复）"><svg class="ui-icon"><use href="/assets/ui-icons.svg#eye-off"></use></svg></button>
      <button type="button" class="danger" data-cue-action="purge" title="永久删除 Cue（可用全局撤销恢复）"><svg class="ui-icon"><use href="/assets/ui-icons.svg#trash"></use></svg></button>
      <button type="button" class="speaker${cue.speaker ? ` ${cue.speaker.replace("_", "-")}` : ""}" data-cue-action="speaker" title="${speakerLabel(cue.speaker)}；点击切换"><svg class="ui-icon"><use href="/assets/ui-icons.svg#user"></use></svg></button>`;
    row.append(meta, content, actions);
    return row;
  }

  function speakerNames() {
    return state.view?.properties?.speaker_names || {};
  }

  function speakerLabel(speaker) {
    if (!speaker) return "未分配说话人";
    const index = Number(String(speaker).split("_").at(-1));
    return speakerNames()[speaker] || `说话人 ${Number.isFinite(index) ? index + 1 : ""}`.trim();
  }

  function nextSpeaker(speaker) {
    const values = [null, "speaker_0", "speaker_1", "speaker_2", "speaker_3"];
    return values[(values.indexOf(speaker ?? null) + 1) % values.length];
  }

  function openSpeakerDialog() {
    if (!state.revision) return;
    const names = speakerNames();
    document.querySelectorAll("[data-speaker-name]").forEach((input, index) => {
      input.value = names[input.dataset.speakerName] || `说话人 ${index + 1}`;
    });
    $("#speakerDialog").classList.remove("hidden");
    requestAnimationFrame(() => $("[data-speaker-name]")?.focus());
  }

  function closeSpeakerDialog() { $("#speakerDialog").classList.add("hidden"); }

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
      status = token.state === "deleted" ? "未采用" : "已采用";
    } else {
      status = "已保留";
    }
    $("#referenceSelectionType").textContent = labels[type] || "参考稿差异";
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

  function centerCueInList(cue) {
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
  }

  function activateCue(cueId, {
    seek = false,
    scroll = false,
    revealTimeline = false,
    centerTimeline = false
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
    if (scroll && cue && !isEditingText) centerCueInList(cue);
  }

  function selectCue(cueId, seek = false) {
    if (seek) syncPlaybackFollowAfterSeek();
    else suspendPlaybackFollow();
    activateCue(cueId, {seek, scroll:false, revealTimeline:true, centerTimeline:seek});
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

  function updateTokenMarquee(event) {
    if (!state.marquee) return;
    const rect = marqueeRect(state.marquee.x, state.marquee.y, event.clientX, event.clientY);
    if (rect.width < 4 && rect.height < 4) return;
    state.marquee.dragged = true;
    const node = $("#tokenMarquee");
    node.classList.remove("hidden");
    Object.assign(node.style, {
      left:`${rect.left}px`, top:`${rect.top}px`, width:`${rect.width}px`, height:`${rect.height}px`
    });
  }

  function finishTokenMarquee(event) {
    if (!state.marquee) return;
    const marquee = state.marquee;
    state.marquee = null;
    $("#tokenMarquee").classList.add("hidden");
    if (!marquee.dragged) {
      if (!marquee.additive) {
        state.selectedTokenIds.clear();
        refreshTokenSelectionUi();
      }
      return;
    }
    const rect = marqueeRect(marquee.x, marquee.y, event.clientX, event.clientY);
    const selected = [...document.querySelectorAll(".display-token")].filter(node => {
      const tokenRect = node.getBoundingClientRect();
      return tokenRect.right >= rect.left && tokenRect.left <= rect.right
        && tokenRect.bottom >= rect.top && tokenRect.top <= rect.bottom;
    }).map(node => node.dataset.tokenId);
    if (!marquee.additive) state.selectedTokenIds.clear();
    selected.forEach(id => state.selectedTokenIds.add(id));
    if (selected.length) state.selectionAnchorTokenId = selected.at(-1);
    refreshTokenSelectionUi();
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
    "insert_cue", "purge_cue", "split_cue", "merge_cues"
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
    let wasBusy = false;
    state.operationQueueProjectId = state.projectId;
    state.operationQueue = factory.createOperationQueue({
      debounceMs:60,
      maxBatchSize:100,
      maxRetries:3,
      initialOperations,
      base() {
        const revision = state.documentStore?.acknowledged() || state.revision;
        return {
          document_id:revision.document.document_id,
          revision_id:revision.revision_id,
          document_hash:revision.document_hash
        };
      },
      async sendBatch(batch) {
        const batchHasTopology = batch.operations.some(isTopologyOperation);
        try {
          const response = await api(projectPath("/operation-batches"), {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify(batch)
          });
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
            if (batchHasTopology
              && visibleRevisionSignature(optimisticProjection) === visibleRevisionSignature(projection)) {
              // The optimistic topology projection already produced exactly
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
            const latest = contract.consumeRevision(await api(projectPath()));
            const applied = appliedOperationIds(latest);
            if (!operationIds.every(id => applied.has(id))) throw clientApplyError;
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
          recordCommittedRevision(revision, {
            kind:"manual",
            operation:"operation_batch",
            metadata:{
              batch_id:batch.batch_id,
              operation_ids:operationIds
            }
          });
          observeEditorTutorialOperations(batch.operations);
          return revision;
        } catch (error) {
          if (isDanglingEntityOperation(error)) {
            // A topology edit can legitimately retire cue/token IDs. A saved
            // browser journal from the pre-topology view must not retry those
            // impossible operations forever. The server remains authoritative:
            // discard only this rejected batch and rebuild from its latest
            // revision without creating a new edit.
            const latest = contract.consumeRevision(await api(projectPath()));
            const operationIds = batch.operations.map(operation => operation.operation_id);
            state.documentStore?.discard(operationIds);
            const projection = state.documentStore?.replaceAcknowledged(latest) || latest;
            setRevision(projection, {
              localProjection:true
            });
            recordCommittedRevision(latest, {
              kind:"manual", operation:"dangling_operation_reconcile", metadata:{operation_ids:operationIds}
            });
            ordinaryError("Cue 结构已变化，已自动同步最新版本；过期编辑未重复执行");
            return latest;
          }
          if (error.status !== 409) throw error;
          const latestPayload = await api(projectPath());
          const latest = contract.consumeRevision(latestPayload);
          const projection = state.documentStore?.replaceAcknowledged(latest) || latest;
          setRevision(projection, {
            localProjection:true
          });
          recordCommittedRevision(latest, {
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
        return !error?.status || error.status === 409 || error.status >= 500;
      },
      onStatus(status) {
        state.operationPending = status.busy;
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
        } catch (_) { /* queue remains in memory if browser storage is unavailable */ }
      },
      onFailed(error) {
        ordinaryError(`编辑尚未保存：${error.message}。操作已保留，可刷新前重试。`);
      }
    });
    return state.operationQueue;
  }

  async function sendOperation(operation) {
    ordinaryError("");
    suspendPlaybackFollow();
    if (editorAiTaskLocksEditor()) {
      ordinaryError(`正在执行${state.editorAiTask?.kind || "AI"}任务，完成前编辑器为只读`);
      return null;
    }
    const topology = isTopologyOperation(operation);
    if (state.topologyOperationPending || (topology && state.operationPending)) {
      ordinaryError("Cue 结构正在更新，请稍候再编辑");
      return null;
    }
    if (topology) state.topologyOperationPending = true;
    try {
      if (topology && document.activeElement?.closest?.("#cueList")) {
        document.activeElement.blur?.();
      }
      const projection = state.documentStore?.enqueue(operation);
      if (projection) {
        setRevision(projection, {
          deferTimeline:activeMedia()?.paused === false,
          localProjection:true
        });
      }
      return await ensureOperationQueue().enqueue(operation);
    } catch (error) {
      ordinaryError(`编辑尚未保存：${error.message}`);
      return null;
    } finally {
      if (topology) state.topologyOperationPending = false;
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

  function beginInlineTokenEdit(button) {
    if (state.topologyOperationPending) return;
    const tokenId = button?.dataset.tokenId;
    const token = state.view?.token_views.find(item => item.token_id === tokenId);
    const label = button?.querySelector("b");
    if (!token || !label || button.querySelector("input")) return;
    const input = document.createElement("input");
    input.className = "inline-token-editor";
    input.value = token.text;
    input.size = Math.max(3, [...token.text].length + 1);
    label.replaceWith(input);
    let finished = false;
    const finish = async (commit = true) => {
      if (finished) return;
      finished = true;
      const text = input.value.trim();
      if (!commit || !text || text === token.text) {
        // A cancelled/no-op edit does not change the document, so do not
        // rebuild the Cue list at all. Reusing the original label also avoids
        // the browser's focus-restoration scroll after the input disappears.
        label.textContent = token.text;
        input.replaceWith(label);
        return;
      }
      await sendOperation(contract.replaceOperation(
        state.revision, tokenId, text, manualProvenance("inline_replace")
      ));
    };
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); input.blur(); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
      event.stopPropagation();
    });
    input.addEventListener("blur", () => finish(true), {once:true});
    input.addEventListener("click", event => event.stopPropagation());
    input.focus();
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
      sourceAliases[configuredSource.toLowerCase()] || "Auto";
    const configuredTarget = String(projectSettings.target_language_mode || "zh-CN");
    $("#translationTargetLanguage").value =
      ["zh-CN", "en", "ja", "ko"].includes(configuredTarget)
        ? configuredTarget : "zh-CN";
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
      const result = await api(projectPath("/ai-calibrate"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          instruction
        })
      });
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
      const revision = await api(projectPath("/convert-script"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          target
        })
      });
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
      const result = await api(projectPath("/reference-manuscript"), {method:"POST", body:form});
      if (result.revision?.revision_id !== state.revision.revision_id) {
        setRevision(result.revision);
        recordCommittedRevision(state.revision, {
          kind:"import", operation:"reference_manuscript_apply", metadata:{}
        });
      }
      const similarity = Number(result.match?.similarity || 0);
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

  async function insertTimelineCue(start, end) {
    const text = await askText("新建人工 cue", "字幕文字（该 cue 没有词级时间）", "");
    if (!text?.trim()) return;
    await sendOperation(contract.insertCueOperation(
      state.revision, start, end, text.trim(), manualProvenance("insert_cue")
    ));
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
      await insertTimelineCue(intent.start, intent.end);
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
    const factory = window.EditorTimeline;
    const canvas = $("#timelineCanvas");
    if (!factory?.createTimelineController || !canvas) return;
    state.timelineController?.destroy?.();
    state.waveformCache = window.EditorWaveformCache?.createWaveformCache?.({limit:16}) || null;
    state.timelineController = factory.createTimelineController({
      canvas,
      media:activeMedia(),
      keyboardTarget:document,
      hideCueShortcut:() => state.shortcuts.hideCue,
      zoomModifier:() => state.shortcuts.zoomModifier,
      onWaveformWindow({start, end, points}) {
        const query = new URLSearchParams({
          start:String(start), end:String(end), points:String(points)
        });
        if (!state.waveformCache) return api(projectPath(`/waveform?${query}`));
        return state.waveformCache.request({
          mediaId:state.projectId,
          latestGroup:state.projectId,
          start, end, points,
          load:({signal}) => api(projectPath(`/waveform?${query}`), {signal})
        });
      },
      onWaveformError(error) {
        if (error?.name === "AbortError") return;
        if (error?.status !== 404) ordinaryError(`波形读取失败：${error.message}`);
      },
      onSelectCue(cueId) {
        syncPlaybackFollowAfterSeek();
        state.timelineSelectedCueId = cueId || null;
        if (cueId) activateCue(cueId, {seek:false, scroll:true, revealTimeline:false});
        else activateCue(null);
      },
      onSeek(time) {
        syncPlaybackFollowAfterSeek();
        const cue = state.view?.cue_views.find(item =>
          item.state === "active" && time >= Number(item.start) && time < Number(item.end)
        );
        if ((cue?.cue_id || null) !== state.activeCueId) activateCue(cue?.cue_id || null, {
          scroll:true,
          revealTimeline:false
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
    await sendOperation(contract.mergeOperation(
      state.revision, selected.map(token => token.token_id), text, manualProvenance("merge")
    ));
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
        if (!window.confirm("永久删除这条 Cue？可通过顶部“撤销”恢复。")) return;
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
        if (!next) return;
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
        await sendOperation(contract.setCueSpeakerOperation(
          state.revision, cueId, nextSpeaker(cue.speaker), manualProvenance("set_cue_speaker")
        ));
      }
    } catch (error) {
      ordinaryError(error?.message || String(error));
    }
  }

  async function loadProject(projectId, {restoreTranslation = true, resetTutorial = false, showTutorialIntro = true} = {}) {
    if (!projectId) return;
    if (state.operationQueue?.getState().busy) {
      ordinaryError("当前编辑仍在排队保存，完成前不会切换或刷新项目");
      renderProjectList();
      return;
    }
    stopTranslationPoll();
    if (projectId !== state.projectId) {
      renderTranslationTask(null);
      state.searchReplaceUndo = null;
    }
    ordinaryError("");
    state.projectId = projectId;
    state.cueSplitView = localStorage.getItem(`substar.editor.cue-split-view:${projectId}`) || "virtual";
    if ($("#cueSplitView")) $("#cueSplitView").value = state.cueSplitView;
    renderProjectList();
    try {
      const project = state.projects.find(item => item.project_id === projectId);
      hideEditorTutorialIntro();
      const resetRevision = resetTutorial && project?.tutorial_case_id
        ? await api(projectPath("/tutorial/reset"), {method:"POST"}) : null;
      const [revision, taskInfo, mediaInfo, llmOptions] = await Promise.all([
        resetRevision ? Promise.resolve(resetRevision) : api(projectPath()),
        api(projectPath("/task-info")),
        api(projectPath("/media-info")),
        api(projectPath("/llm-options"))
      ]);
      state.taskInfo = taskInfo;
      state.llmOptions = llmOptions;
      applySubtitlePolicy(taskInfo);
      configureTranslationLanguageDefaults(taskInfo);
      state.mediaInfo = mediaInfo;
      configureMedia(mediaInfo?.kind);
      setRevision(revision, {preserveCueViewport:false});
      await refreshEditorAiTask();
      startEditorAiTaskPoll();
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
      if (restoreTranslation) {
        const task = await refreshTranslationTask();
        if (task && ["queued", "running", "cancelling"].includes(task.state)) {
          followTranslationTask(task);
        }
      }
      const url = new URL(window.location.href);
      url.searchParams.set("project", projectId);
      history.replaceState(null, "", url);
      if (project?.tutorial_case_id && showTutorialIntro) maybeShowTutorialIntro(project.tutorial_case_id);
    } catch (error) {
      ordinaryError(error.message);
    }
  }

  async function loadProjects() {
    ordinaryError("");
    try {
      const response = await api("/api/projects");
      state.projects = response.projects || [];
      const query = new URLSearchParams(window.location.search);
      //  links use `project`; accepting the same project id from the
      // short-lived `job` link keeps already-open  pages from becoming an
      // empty editor while all new links are emitted with the  contract.
      const requested = query.get("project") || query.get("job");
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
      const revision = await api(projectPath("/complete"), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          expected_revision_id:state.revision.revision_id,
          complete:state.view.properties.complete !== true
        })
      });
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

  const EDITOR_TUTORIAL_STEPS = [
    {title:"选择与播放", description:"自由选择 Cue 并播放，观察 Cue 编辑区、字幕预览和时间轴的三方联动。体验完成后继续。", focus:() => $("#editorWorkbench")},
    {title:"定位问题字幕", description:"点击“问题字幕”的下一条，定位到第一条超出字符上限的字幕。", focus:() => $("#toolHardIssueNext")?.closest(".tool-locator-row")},
    {title:"合并相邻 Cue", description:"找到只有“但，”的字幕，在行尾紫色点上右键，把它和下一条合并。", focus:() => tutorialCueRow(state.tutorial.anchors?.mergeLeftCue)},
    {title:"在文字间切分", description:"在合并后的字幕中，右键点击“压缩”和“章邯”之间的边界条。", focus:() => tutorialCueRow(state.tutorial.anchors?.mergedCue)},
    {title:"处理下一条问题", description:"再次点击问题字幕的“下一条”，定位到下一条超限字幕。", focus:() => $("#toolHardIssueNext")?.closest(".tool-locator-row")},
    {title:"再次切分", description:"右键点击“威胁”和“是”之间的边界条。", focus:() => tutorialCueRow(state.tutorial.anchors?.threatCue)},
    {title:"判断参考稿差异", description:"已定位到包含“二十万”的参考差异。点击“二十”，查看听写结果与参考稿的差别。", focus:() => tutorialCueRow(state.tutorial.anchors?.referenceCue)},
    {title:"选择采用哪一稿", description:"当前采用的是参考稿“二十”。点击“保留听写结果”，切换回原听写“20”。", focus:() => [tutorialCueRow(state.tutorial.anchors?.referenceCue), $("#referenceSelectionMenu:not(.hidden)")]},
    {title:"编辑词元", description:"双击“20”进入编辑，改成“二十”，然后按 Enter 或点击空白处保存。", focus:() => tutorialCueRow(state.tutorial.anchors?.referenceCue)},
    {title:"选择并合并词元", description:"选中“二十”和“万”：可以按住左键拖选，也可以用 Ctrl＋左键逐个选择，或用 Shift＋左键连续选择。然后点击浮动菜单中的“合并”。", focus:() => [tutorialCueRow(state.tutorial.anchors?.referenceCue), $("#tokenSelectionMenu:not(.hidden)")]},
    {title:"撤销与重做", description:() => `点击编辑区底部的“撤销”，恢复合并前的状态。也可以按 ${state.shortcuts.undo}；重做可使用 ${state.shortcuts.redo}。`, focus:() => document.querySelector("#cueTaskIsland .cue-history-row")},
    {title:"调整时间边界", description:() => tutorialPlayheadBlocksBoundary()
      ? "红色播放线挡住了左边界。先单击当前时间块中部，把播放线移开，再把左边界向左拖动。"
      : "沿虚拟鼠标提示，把当前字幕的左边界向左拖动。", focus:() => tutorialTimelineFocus()},
    {title:"放大时间轴", description:"教程已标出一处稳定空隙。把鼠标放在标记线上，按住 Alt 并向上滚动鼠标滚轮，放大时间轴。", focus:() => tutorialTimelineFocus()},
    {title:"创建新 Cue", description:"在标记线位置右键创建 Cue。字幕内容可自由填写，任意非空字符都算完成。", focus:() => tutorialTimelineFocus()},
    {title:"隐藏、恢复与删除", description:() => tutorialCreatedCueInstruction(), focus:() => tutorialCreatedCueFocus()},
    {title:"查找字幕", description:"查找“控制”，然后点击查找下一个。", focus:() => document.querySelector('[data-tool-panel="search"]')},
    {title:"标点处理", description:"在上行字幕的“消除符号”中填写“，。”，然后应用规则。", focus:() => document.querySelector('[data-tool-panel="punctuation"]')},
    {title:"自动吸附", description:"选择智能吸附，把阈值从 400 改为 500，然后点击执行。", focus:() => $("#autoSnapMenu")},
    {title:"导出字幕", description:"点击“导出...”，选择一种字幕格式。", focus:() => [$("#exportDocument"), $("#exportMenu[open] .export-popover")]}
  ];

  const ADVANCED_EDITOR_TUTORIAL_STEPS = [
    {title:"AI 切分结果已载入", description:"切分页刚才载入的 34 条 Cue 已按正式文档契约注册。进阶教程从这里开始，不再重复选择、播放和基础编辑操作。", focus:() => $("#cueList"), continue:true},
    {title:"执行 AI 校准", description:"打开“AI 校准”，可以填写本次额外要求，再点击“执行校准”。教程会载入预存的真实校准结果，不会请求云端模型。", focus:() => [$("#aiCalibrate"), $("#aiCalibrationMenu[open] .calibration-popover")]},
    {title:"查看校准痕迹", description:"浅蓝色背景标出 AI 实际改动过的词元。校准结果保留原 Cue 身份与可追溯 provenance。", focus:() => $("#cueList"), continue:true},
    {title:"执行 AI 翻译", description:"打开“AI 翻译”，确认英文到简体中文后点击“执行”。载入的翻译快照包含一对一和多对多意义组映射。", focus:() => [$("#translationMenuSummary"), $("#translationMenu[open] .translation-popover")]},
    {title:"查看多对多译文", description:"译文已经进入 B 语言轨道。相邻 Cue 可以共享一个意义单元，映射信息保存在 Cue，而不是靠逐行硬译。", focus:() => $("#cueList"), continue:true},
    {title:"打开外部 AI 审阅", description:"点击“外部 AI 审阅”，打开可拖动吸附面板；它不会调用工程内模型，也不会改写稿件。", focus:() => $("#aiReview")},
    {title:"复制审阅内容", description:"选择审阅范围并点击“复制审阅内容”。内容会带上固定的前后各 5 条上下文，可粘贴到任意 Web AI。", focus:() => [$("#aiReviewMenu[open] .external-review-popover"), $("#copyExternalReview")]},
    {title:"进阶教程完成", description:"你已经走完 AI 切分、AI 校准、AI 翻译，并生成了可交给外部 Web AI 的审阅内容。", focus:() => $("#aiReviewMenu[open] .external-review-popover"), continue:true},
  ];

  function tutorialProject() {
    return state.projects.find(item => item.project_id === state.projectId && item.tutorial_case_id);
  }

  function isAdvancedTutorial() {
    return tutorialProject()?.tutorial_case_id === "advanced-ai-v1";
  }

  function currentEditorTutorialSteps() {
    return isAdvancedTutorial() ? ADVANCED_EDITOR_TUTORIAL_STEPS : EDITOR_TUTORIAL_STEPS;
  }

  async function runPackagedTutorialStage(stage) {
    if (!state.revision || state.operationPending) return;
    const title = ({calibration:"AI 校准", translation:"AI 翻译"})[stage];
    const expectedStep = ({calibration:1, translation:3})[stage];
    state.operationPending = true;
    renderHeader();
    renderWorkbenchTask(title, 18, "正在读取内置阶段快照…");
    try {
      await new Promise(resolve => window.setTimeout(resolve, 420));
      renderWorkbenchTask(title, 64, "正在校验身份、结构与阶段依赖…");
      const result = await api(projectPath(`/tutorial/stages/${stage}`), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({expected_revision_id:state.revision.revision_id})
      });
      setRevision(result);
      recordCommittedRevision(state.revision, {
        kind:"ai", operation:`tutorial_snapshot_${stage}`, metadata:{simulated:true}
      });
      renderWorkbenchTask(title, 100, "内置结果已通过校验并载入", "completed");
      if (state.tutorial.active && state.tutorial.step === expectedStep) advanceEditorTutorial(expectedStep);
    } catch (error) {
      renderWorkbenchTask(title, 100, error.message, "failed");
    } finally {
      state.operationPending = false;
      renderHeader();
    }
  }

  function tutorialCueEnd(cueId) {
    return cueId ? document.querySelector(`.cue-row[data-cue-id="${CSS.escape(cueId)}"] [data-boundary-action="merge-next"]`) : null;
  }

  function tutorialCueRow(cueId) {
    return cueId ? document.querySelector(`.cue-row[data-cue-id="${CSS.escape(cueId)}"]`) : null;
  }

  function tutorialTokenView(cueId, text) {
    const cue = state.view?.cue_views.find(item => item.cue_id === cueId);
    return cue?.display_token_ids.map(id => state.view.token_views.find(item => item.token_id === id))
      .find(item => item?.text === text) || null;
  }

  function tutorialToken(cueId, text) {
    const token = tutorialTokenView(cueId, text);
    return token ? document.querySelector(`[data-token-id="${CSS.escape(token.token_id)}"]`) : null;
  }

  function tutorialBoundary(cueId, leftText) {
    const cue = state.view?.cue_views.find(item => item.cue_id === cueId);
    if (!cue) return null;
    const token = cue.display_token_ids.map(id => state.view.token_views.find(item => item.token_id === id))
      .find(item => item?.text === leftText);
    return token ? document.querySelector(`[data-cue-id="${CSS.escape(cueId)}"][data-boundary-after="${CSS.escape(token.token_id)}"]`) : null;
  }

  function clearEditorTutorialTarget() {
    state.tutorial.focus = null;
    $("#editorTutorialSpotlight").style.display = "none";
    $("#editorTutorialTimelineTarget")?.classList.add("hidden");
    $("#editorTutorialGhostMouse")?.classList.add("hidden");
  }

  function tutorialTimelineFocus() {
    const target = $("#editorTutorialTimelineTarget");
    return target && !target.classList.contains("hidden") ? target : $("#timelineTrack");
  }

  function tutorialPlayheadBlocksBoundary() {
    const anchors = state.tutorial.anchors;
    const timeline = state.timelineController;
    if (!anchors?.referenceCue || !timeline?.getState) return false;
    const cue = state.view?.cue_views.find(item => item.cue_id === anchors.referenceCue);
    const info = timeline.getState();
    if (!cue || !Number.isFinite(Number(info?.playhead_time))) return false;
    const span = Math.max(.001, Number(info.view_end) - Number(info.view_start));
    return Math.abs(Number(info.playhead_time) - Number(cue.start)) <= Math.max(.04, span * .009);
  }

  function tutorialCreatedCueInstruction() {
    const flags = state.tutorial.flags || {};
    if (!flags.hiddenCue) return "教程已定位并高亮你刚创建的 Cue。点击右侧的隐藏按钮。";
    if (!flags.restoredCue) return "该 Cue 已隐藏。点击它右侧的“恢复”，让它重新回到时间轴。";
    return "Cue 已恢复。点击右侧的删除按钮并确认；之后仍可通过全局撤销或历史版本找回。";
  }

  function tutorialCreatedCueFocus() {
    const row = tutorialCueRow(state.tutorial.flags?.createdCueId);
    if (!row) return $("#cueList");
    const selector = !state.tutorial.flags?.hiddenCue
      ? '[data-cue-action="hide"]'
      : !state.tutorial.flags?.restoredCue ? '[data-cue-action="restore"]' : '[data-cue-action="purge"]';
    return [row, row.querySelector(selector)];
  }

  function positionEditorTutorialTimelineGuide() {
    const target = $("#editorTutorialTimelineTarget");
    const ghost = $("#editorTutorialGhostMouse");
    if (!target || !ghost) return;
    target.classList.add("hidden");
    ghost.className = "editor-tutorial-ghost-mouse hidden";
    if (isAdvancedTutorial() || ![11,12,13].includes(state.tutorial.step)) return;
    const timeline = state.timelineController;
    const anchors = state.tutorial.anchors || {};
    let rect = null;
    let ghostX = null;
    let label = "";
    let mode = "";
    let badge = "";
    if (state.tutorial.step === 11) {
      const cue = state.view?.cue_views.find(item => item.cue_id === anchors.referenceCue);
      if (!cue) return;
      rect = timeline?.getTimeRect?.(cue.start, cue.start);
      if (!rect) return;
      const cueRect = timeline.getTimeRect(cue.start, cue.end);
      const blocked = tutorialPlayheadBlocksBoundary();
      const description = $("#editorTutorialDescription");
      if (description) description.textContent = blocked
        ? "红色播放线挡住了左边界。先单击当前时间块中部，把播放线移开，再把左边界向左拖动。"
        : "沿虚拟鼠标提示，把当前字幕的左边界向左拖动。";
      ghostX = blocked ? Math.min(cueRect.right - 18, cueRect.left + Math.max(42, cueRect.width * .45)) : rect.left;
      label = blocked ? "先移开红线" : "向左拖边界";
      mode = blocked ? "click" : "drag";
      badge = blocked ? "单击" : "拖动";
      rect = {...rect, left:rect.left - 8, right:rect.right + 8, width:16};
    } else {
      const gap = anchors.manualGap;
      if (!gap) return;
      rect = timeline?.getTimeRect?.(gap.start, gap.end);
      if (!rect) return;
      ghostX = rect.left + rect.width / 2;
      label = state.tutorial.step === 12 ? "在这里放大" : "在这里右键";
      mode = state.tutorial.step === 12 ? "zoom" : "context";
      badge = state.tutorial.step === 12 ? "Alt＋滚轮↑" : "右键";
    }
    const visibleLeft = Math.max(0, rect.left);
    const visibleRight = Math.min(innerWidth, rect.right);
    if (!(visibleRight > visibleLeft)) return;
    Object.assign(target.style, {
      left:`${visibleLeft}px`, top:`${rect.top + 22}px`,
      width:`${Math.max(8, visibleRight - visibleLeft)}px`, height:`${Math.max(28, rect.bottom - rect.top - 26)}px`
    });
    $("#editorTutorialTimelineTargetLabel").textContent = label;
    target.classList.remove("hidden");
    $("#editorTutorialGhostBadge").textContent = badge;
    ghost.className = `editor-tutorial-ghost-mouse ${mode}`;
    Object.assign(ghost.style, {
      left:`${Math.max(4, Math.min(innerWidth - 34, ghostX - 12))}px`,
      top:`${Math.max(4, rect.top + Math.max(42, (rect.bottom - rect.top) * .48))}px`
    });
  }

  function editorTutorialFocusRect(value) {
    const nodes = (Array.isArray(value) ? value : [value]).filter(node => node?.isConnected);
    if (!nodes.length) return null;
    const rects = nodes.map(node => node.getBoundingClientRect()).filter(rect => rect.width > 0 && rect.height > 0);
    if (!rects.length) return null;
    return {
      left:Math.min(...rects.map(rect => rect.left)), top:Math.min(...rects.map(rect => rect.top)),
      right:Math.max(...rects.map(rect => rect.right)), bottom:Math.max(...rects.map(rect => rect.bottom))
    };
  }

  function positionEditorTutorial() {
    if (!state.tutorial.active) return;
    positionEditorTutorialTimelineGuide();
    const step = currentEditorTutorialSteps()[state.tutorial.step];
    const focus = step?.focus?.() || $("#editorWorkbench");
    state.tutorial.focus = focus;
    const margin = 8;
    const rect = editorTutorialFocusRect(focus) || $("#editorWorkbench").getBoundingClientRect();
    const hole = {
      left:Math.max(0, rect.left - margin), top:Math.max(0, rect.top - margin),
      right:Math.min(innerWidth, rect.right + margin), bottom:Math.min(innerHeight, rect.bottom + margin)
    };
    const shades = [$("#editorTutorialShadeTop"), $("#editorTutorialShadeLeft"), $("#editorTutorialShadeRight"), $("#editorTutorialShadeBottom")];
    Object.assign(shades[0].style, {left:"0px", top:"0px", width:"100vw", height:`${hole.top}px`});
    Object.assign(shades[1].style, {left:"0px", top:`${hole.top}px`, width:`${hole.left}px`, height:`${Math.max(0, hole.bottom-hole.top)}px`});
    Object.assign(shades[2].style, {left:`${hole.right}px`, top:`${hole.top}px`, width:`${Math.max(0, innerWidth-hole.right)}px`, height:`${Math.max(0, hole.bottom-hole.top)}px`});
    Object.assign(shades[3].style, {left:"0px", top:`${hole.bottom}px`, width:"100vw", height:`${Math.max(0, innerHeight-hole.bottom)}px`});
    const spotlight = $("#editorTutorialSpotlight");
    spotlight.style.display = "block";
    Object.assign(spotlight.style, {left:`${hole.left}px`, top:`${hole.top}px`, width:`${Math.max(0, hole.right-hole.left)}px`, height:`${Math.max(0, hole.bottom-hole.top)}px`});
    const card = $("#editorTutorialCard");
    const gap = 20;
    const cardWidth = Math.min(460, innerWidth - 36);
    const cardHeight = Math.min(card.scrollHeight || 260, innerHeight - 36);
    const spaces = [
      {side:"right", room:innerWidth-hole.right, left:hole.right+gap, top:hole.top},
      {side:"left", room:hole.left, left:hole.left-gap-cardWidth, top:hole.top},
      {side:"bottom", room:innerHeight-hole.bottom, left:hole.left, top:hole.bottom+gap},
      {side:"top", room:hole.top, left:hole.left, top:hole.top-gap-cardHeight}
    ].sort((a,b) => b.room-a.room);
    const place = spaces.find(item => item.room >= (item.side === "left" || item.side === "right" ? cardWidth+gap : cardHeight+gap)) || spaces[0];
    card.style.left = `${Math.max(18, Math.min(innerWidth-cardWidth-18, place.left))}px`;
    card.style.top = `${Math.max(18, Math.min(innerHeight-cardHeight-18, place.top))}px`;
  }

  function scheduleEditorTutorialPosition() {
    if (!state.tutorial.active) return;
    requestAnimationFrame(positionEditorTutorial);
    window.setTimeout(positionEditorTutorial, 100);
    window.setTimeout(positionEditorTutorial, 320);
  }

  function centerEditorTutorialCue(cueId) {
    const container = $("#cueList");
    const row = tutorialCueRow(cueId);
    if (!container || !row) return;
    const desired = row.offsetTop - (container.clientHeight - row.offsetHeight) / 2;
    container.scrollTop = Math.max(0, Math.min(container.scrollHeight - container.clientHeight, desired));
    positionEditorTutorial();
  }

  function scheduleEditorTutorialCueCenter(cueId) {
    if (!cueId) return;
    requestAnimationFrame(() => centerEditorTutorialCue(cueId));
    window.setTimeout(() => centerEditorTutorialCue(cueId), 100);
  }

  function prepareEditorTutorialStep() {
    if (isAdvancedTutorial()) {
      state.tutorial.centerCueId = null;
      if (state.tutorial.step === 0) {
        $("#cueList").scrollTop = 0;
        const cue = state.view?.cue_views.find(item => item.state === "active");
        if (cue) selectCue(cue.cue_id, true);
      }
      if (state.tutorial.step === 3) $("#aiCalibrationMenu").open = false;
      if ([2,4].includes(state.tutorial.step)) $("#cueList").scrollTop = 0;
      if ([6,7].includes(state.tutorial.step)) openAiReview();
      scheduleEditorTutorialPosition();
      return;
    }
    const anchors = state.tutorial.anchors;
    if (!anchors) return;
    state.tutorial.centerCueId = null;
    let cueId = null;
    if (state.tutorial.step === 0) { $("#cueList").scrollTop = 0; selectCue(anchors.cue1, true); }
    if (state.tutorial.step === 2) cueId = anchors.mergeLeftCue;
    if (state.tutorial.step === 3) cueId = anchors.mergedCue;
    if (state.tutorial.step === 5) cueId = anchors.threatCue;
    if ([6,7,8,9,10].includes(state.tutorial.step)) cueId = anchors.referenceCue;
    if ([12,13].includes(state.tutorial.step)) cueId = anchors.manualGap?.followingCueId;
    if (state.tutorial.step === 14) cueId = state.tutorial.flags?.createdCueId;
    if (cueId) {
      selectCue(cueId, false);
      state.tutorial.centerCueId = cueId;
      scheduleEditorTutorialCueCenter(cueId);
    }
    if ([12,13].includes(state.tutorial.step) && anchors.manualGap) {
      state.timelineController?.revealRange?.(anchors.manualGap.start, anchors.manualGap.end);
    }
    const panel = ({1:"locator", 4:"locator", 15:"search", 16:"punctuation", 17:"auto-snap"})[state.tutorial.step];
    if (panel) document.querySelector(`[data-tool-panel="${panel}"]`)?.setAttribute("open", "");
    if (state.tutorial.step === 17) $("#snapThreshold").value = "400";
    scheduleEditorTutorialPosition();
  }

  function renderEditorTutorial() {
    const steps = currentEditorTutorialSteps();
    const step = steps[state.tutorial.step];
    if (!step) return exitEditorTutorial();
    $("#editorTutorialProgress").textContent = `${isAdvancedTutorial() ? "进阶教程" : "初级教程"} · ${state.tutorial.step + 1} / ${steps.length}`;
    $("#editorTutorialTitle").textContent = step.title;
    $("#editorTutorialDescription").textContent = typeof step.description === "function"
      ? step.description() : step.description;
    $("#editorTutorialFooter").classList.toggle("hidden", !step.continue && state.tutorial.step !== 0);
    $("#editorTutorialNext").textContent = state.tutorial.step === steps.length - 1 ? "完成教程" : "继续";
    prepareEditorTutorialStep();
    positionEditorTutorial();
  }

  function exitEditorTutorial() {
    state.tutorial.active = false;
    if (state.tutorial.positionTimer) window.clearInterval(state.tutorial.positionTimer);
    state.tutorial.positionTimer = 0;
    clearEditorTutorialTarget();
    $("#editorTutorialLayer").classList.add("hidden");
    $("#editorTutorialLayer").setAttribute("aria-hidden", "true");
  }

  function advanceEditorTutorial(expectedStep = state.tutorial.step) {
    if (!state.tutorial.active || state.tutorial.step !== expectedStep) return;
    if (expectedStep >= currentEditorTutorialSteps().length - 1) {
      exitEditorTutorial();
      ordinaryError("案例教程已完成");
      return;
    }
    const retained = expectedStep === 13 && state.tutorial.flags?.createdCueId
      ? {createdCueId:state.tutorial.flags.createdCueId, createdCueText:state.tutorial.flags.createdCueText} : {};
    state.tutorial.step += 1;
    state.tutorial.flags = retained;
    renderEditorTutorial();
  }

  function tutorialOperationName(operation) {
    return String(operation?.payload?.provenance?.operation || operation?.type || "");
  }

  function tutorialOperationCueIds(operation) {
    const payload = operation?.payload || {};
    return new Set([
      payload.cue_id, ...(payload.cue_ids || []), ...(payload.cues || []).map(cue => cue.cue_id)
    ].filter(Boolean).map(String));
  }

  function observeEditorTutorialOperations(operations) {
    if (!state.tutorial.active) return;
    if (isAdvancedTutorial()) return;
    for (const operation of operations || []) observeEditorTutorialOperation(operation);
  }

  function observeEditorTutorialOperation(operation) {
    const step = state.tutorial.step;
    const anchors = state.tutorial.anchors || {};
    const flags = state.tutorial.flags || (state.tutorial.flags = {});
    const payload = operation?.payload || {};
    const name = tutorialOperationName(operation);
    const cueIds = tutorialOperationCueIds(operation);
    if (step === 2 && name === "merge_cues" && cueIds.has(anchors.mergeLeftCue) && cueIds.has(anchors.mergeRightCue)) {
      const tokenById = new Map((state.view?.token_views || []).map(token => [token.token_id, token]));
      const merged = state.view?.cue_views.find(cue => {
        const text = cue.display_token_ids.map(id => tokenById.get(id)?.text || "").join("");
        return cue.state === "active" && text.includes("但，") && text.includes("压缩章邯");
      });
      if (!merged) return;
      anchors.mergedCue = merged.cue_id;
      return advanceEditorTutorial(2);
    }
    if (step === 3 && name === "split_cue" && payload.cue_id === anchors.mergedCue && payload.after_token_id === anchors.firstSplitAfter) return advanceEditorTutorial(3);
    if (step === 5 && name === "split_cue" && payload.cue_id === anchors.threatCue && payload.after_token_id === anchors.threatSplitAfter) return advanceEditorTutorial(5);
    if (step === 7 && name === "reference_keep_asr" && payload.token_id === anchors.referenceTwenty) return advanceEditorTutorial(7);
    if (step === 8 && name === "inline_replace" && payload.token_id === anchors.referenceTwenty && payload.text === "二十") return advanceEditorTutorial(8);
    if (step === 9 && name === "merge"
      && (payload.token_ids || []).includes(anchors.referenceTwenty) && (payload.token_ids || []).includes(anchors.referenceWan)) return advanceEditorTutorial(9);
    if (step === 11 && ["timeline_boundary", "timeline_shared_boundary"].includes(name) && cueIds.has(anchors.referenceCue)) {
      const cue = state.view?.cue_views.find(item => item.cue_id === anchors.referenceCue);
      if (cue && Number(cue.start) < Number(anchors.referenceStart) - 0.0005) return advanceEditorTutorial(11);
    }
    if (step === 13 && name === "insert_cue" && String(payload.text || "").trim()) {
      const baselineCueIds = new Set((anchors.baselineCueIds || []).map(String));
      const gap = anchors.manualGap || {};
      const tokenById = new Map((state.view?.token_views || []).map(token => [String(token.token_id), token]));
      const created = state.view?.cue_views.find(cue => {
        const text = (cue.display_token_ids || []).map(id => tokenById.get(String(id))?.text || "").join("").trim();
        return cue.state === "active" && !baselineCueIds.has(String(cue.cue_id)) && text
          && Number(cue.start) >= Number(gap.start) - .01 && Number(cue.end) <= Number(gap.end) + .01;
      });
      if (created) {
        flags.createdCueId = created.cue_id;
        flags.createdCueText = (created.display_token_ids || []).map(id => tokenById.get(String(id))?.text || "").join("").trim();
      }
      return created ? advanceEditorTutorial(13) : undefined;
    }
    const createdCueId = flags.createdCueId;
    if (step === 14 && createdCueId && cueIds.has(createdCueId)) {
      if (name === "hide_cue") flags.hiddenCue = true;
      if (name === "restore_cue" && flags.hiddenCue) flags.restoredCue = true;
      if (name === "purge_cue" && flags.restoredCue) return advanceEditorTutorial(14);
      renderEditorTutorial();
    }
  }

  function observeEditorTutorialEvent(type, detail = {}) {
    if (!state.tutorial.active) return;
    if (isAdvancedTutorial()) return;
    const step = state.tutorial.step;
    const flags = state.tutorial.flags || (state.tutorial.flags = {});
    if (step === 1 && type === "hard_issue_next") return advanceEditorTutorial(1);
    if (step === 4 && type === "hard_issue_next") return advanceEditorTutorial(4);
    if (step === 6 && type === "token_select" && detail.tokenId === state.tutorial.anchors?.referenceTwenty) return advanceEditorTutorial(6);
    if (step === 10 && type === "revision_restore" && detail.navigation === "undo") return advanceEditorTutorial(10);
    if (step === 12 && type === "timeline_zoom" && detail.direction === "in") {
      const gap = state.tutorial.anchors?.manualGap;
      if (gap && Number(detail.pointer_time) >= Number(gap.start) - .02 && Number(detail.pointer_time) <= Number(gap.end) + .02) {
        return advanceEditorTutorial(12);
      }
    }
    if (step === 15 && type === "search" && detail.query === "控制" && detail.found) return advanceEditorTutorial(15);
    if (step === 16 && type === "punctuation_apply" && detail.upperRemove?.includes("，") && detail.upperRemove?.includes("。")) return advanceEditorTutorial(16);
    if (step === 17 && type === "auto_snap" && detail.smart && detail.threshold === 500) return advanceEditorTutorial(17);
    if (step === 18 && type === "export") return advanceEditorTutorial(18);
  }

  function beginEditorTutorialState() {
    const cues = (state.view?.cue_views || []).filter(cue => cue.state === "active");
    if (isAdvancedTutorial()) {
      if (cues.length !== 34) return ordinaryError("进阶教程案例内容不完整，无法启动引导");
      state.cueSplitView = "virtual";
      $("#cueSplitView").value = "virtual";
      state.tutorial = {active:true, step:0, focus:null, flags:{}, anchors:{}, positionTimer:0, centerCueId:null};
      $("#editorTutorialLayer").classList.remove("hidden");
      $("#editorTutorialLayer").setAttribute("aria-hidden", "false");
      renderCues();
      renderEditorTutorial();
      state.tutorial.positionTimer = window.setInterval(positionEditorTutorial, 80);
      return;
    }
    if (cues.length < 37) return ordinaryError("教程案例内容不完整，无法启动引导");
    if (!tutorialResolver?.resolveBeginnerAnchors) return ordinaryError("初级教程解析器未载入，请刷新后重试");
    const resolved = tutorialResolver.resolveBeginnerAnchors(
      state.view, [...(state.referenceChangeByTokenId || new Map()).entries()]
    );
    if (!resolved.ok) return ordinaryError(`教程案例缺少：${resolved.missing.join("、")}，无法启动引导`);
    state.cueSplitView = "virtual";
    $("#cueSplitView").value = "virtual";
    const anchors = resolved.anchors;
    state.tutorial = {
      active:true, step:0, focus:null, flags:{}, anchors, positionTimer:0,
      centerCueId:null
    };
    $("#editorTutorialLayer").classList.remove("hidden");
    $("#editorTutorialLayer").setAttribute("aria-hidden", "false");
    renderCues();
    renderEditorTutorial();
    state.tutorial.positionTimer = window.setInterval(() => {
      positionEditorTutorial();
    }, 80);
  }

  async function restartEditorTutorial() {
    if (!tutorialProject()) return;
    hideEditorTutorialIntro({seen:true});
    exitEditorTutorial();
    await loadProject(state.projectId, {restoreTranslation:false, resetTutorial:true, showTutorialIntro:false});
    beginEditorTutorialState();
  }

  function positionEditorTutorialIntro() {
    const intro = $("#editorTutorialIntro");
    const target = $("#startEditorTutorial");
    const card = intro?.querySelector("section");
    if (!intro || intro.classList.contains("hidden") || !target || !card) return;
    const targetRect = target.getBoundingClientRect();
    const cardWidth = card.offsetWidth;
    const cardHeight = card.offsetHeight;
    const gap = 14;
    const edge = 12;
    const left = Math.max(edge, Math.min(window.innerWidth - cardWidth - edge, targetRect.right - cardWidth));
    let top = targetRect.bottom + gap;
    let above = false;
    if (top + cardHeight > window.innerHeight - edge) {
      top = Math.max(edge, targetRect.top - cardHeight - gap);
      above = true;
    }
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
    card.style.setProperty(
      "--tutorial-entry-arrow-x",
      `${Math.max(18, Math.min(cardWidth - 18, targetRect.left + targetRect.width / 2 - left))}px`
    );
    intro.classList.toggle("above", above);
  }

  function hideEditorTutorialIntro({seen = false} = {}) {
    const intro = $("#editorTutorialIntro");
    if (!intro) return;
    if (seen && intro.dataset.storageKey) localStorage.setItem(intro.dataset.storageKey, "seen");
    intro.classList.add("hidden");
    intro.classList.remove("above");
    $("#startEditorTutorial")?.classList.remove("tutorial-entry-target");
  }

  function maybeShowTutorialIntro(caseId) {
    const key = `substar.editor.tutorial-entry-hint:v1:${caseId}`;
    if (localStorage.getItem(key) === "seen") return;
    const intro = $("#editorTutorialIntro");
    intro.dataset.storageKey = key;
    const advanced = caseId === "advanced-ai-v1";
    const buttonLabel = advanced ? "启动进阶教程" : "启动初级教程";
    intro.querySelector("span").textContent = advanced ? "进阶教程" : "初级教程";
    $("#editorTutorialIntroTitle").textContent = `从这里${buttonLabel}`;
    $("#editorTutorialIntroTitle").nextElementSibling.textContent = advanced
      ? "点击右上方按钮，开始体验 AI 切分、校准、翻译和审阅。以后也可以随时从这里重新启动。"
      : "点击右上方按钮，开始学习定位、切分、参考稿取舍、时间轴调整和导出。以后也可以随时从这里重新启动。";
    intro.classList.remove("hidden");
    $("#startEditorTutorial").classList.add("tutorial-entry-target");
    window.requestAnimationFrame(positionEditorTutorialIntro);
  }

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
  $("#saveCheckpoint").onclick = createCheckpoint;
  $("#taskInfoMenu").addEventListener("toggle", () => {
    if ($("#taskInfoMenu").open) openTaskInfoMenu();
  });
  $("#taskInfoForm").onsubmit = submitTaskInfo;
  $("#cancelTaskInfo").onclick = closeTaskInfoMenu;
  $("#toggleComplete").onclick = toggleComplete;
  $("#startEditorTutorial").onclick = restartEditorTutorial;
  $("#dismissEditorTutorialIntro").onclick = () => hideEditorTutorialIntro({seen:true});
  $("#exitEditorTutorial").onclick = exitEditorTutorial;
  $("#editorTutorialNext").onclick = () => {
    const step = currentEditorTutorialSteps()[state.tutorial.step];
    if (state.tutorial.step === 0 || step?.continue) advanceEditorTutorial(state.tutorial.step);
  };
  $("#translateDocument").onclick = startTranslation;
  $("#dismissTaskPanel").onclick = cancelOrDismissTaskPanel;
  $("#dismissOrdinaryError").onclick = () => ordinaryError("");
  $("#exportMenu").addEventListener("click", async event => {
    const exchange = event.target.closest("[data-exchange-export]");
    if (exchange && state.projectId && state.revision) {
      $("#exportMenu").open = false;
      try {
        const sequence = nextExportSequence();
        const query = new URLSearchParams({export_sequence:String(sequence.value)});
        const spec = systemSaveAs.exchangeSpec(
          state.taskInfo?.display_name || state.projectId,
          exchange.dataset.exchangeExport,
          sequence.value,
          projectPath(`/exchange/${encodeURIComponent(exchange.dataset.exchangeExport)}?${query}`)
        );
        const result = await systemSaveAs.saveUrl(spec);
        if (result.cancelled) return;
        commitExportSequence(sequence);
        ordinaryError(`已保存：${result.filename}`, "completed");
      } catch (error) {
        ordinaryError(`导出失败：${error.message}`);
      }
      return;
    }
    const button = event.target.closest("[data-export-mode]");
    if (!button || !state.projectId || !state.revision) return;
    $("#exportMenu").open = false;
    try {
      const sequence = nextExportSequence();
      const query = new URLSearchParams({export_sequence:String(sequence.value)});
      const spec = systemSaveAs.subtitleSpec(
        state.taskInfo?.display_name || state.projectId,
        button.dataset.exportMode,
        sequence.value,
        projectPath(`/export/${encodeURIComponent(button.dataset.exportMode)}?${query}`)
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
      $("#autoSnapMenu").open = false;
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
  $("#cueSplitView").onchange = event => {
    state.cueSplitView = event.target.value === "auxiliary" ? "auxiliary" : "virtual";
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
  $("#importProject").onclick = () => $("#importProjectInput").click();
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
    if (event.target.closest("[data-target-edit]")) return;
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
      selectCue(cue.dataset.cueSelect, true);
      return;
    }
    const row = event.target.closest(".cue-row");
    const interactive = event.target.closest("button, textarea, input, select, a, [contenteditable='true']");
    if (row && !interactive) selectCue(row.dataset.cueId, true);
  });
  $("#cueList").addEventListener("dblclick", event => {
    const connector = event.target.closest("[data-token-connector]");
    if (connector && !connector.disabled) {
      event.preventDefault();
      event.stopPropagation();
      insertTokenAfter(connector.dataset.cueId, connector.dataset.insertAfter);
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
  $("#cueList").addEventListener("focusin", event => {
    const target = event.target.closest("[data-target-edit]");
    if (target) {
      state.activeCueId = target.dataset.targetEdit;
      if (target.dataset.rawEditing !== "true") {
        target.value = target.dataset.originalTarget || "";
        target.dataset.rawEditing = "true";
        resizeTranslationField(target);
      }
    }
  });
  $("#cueList").addEventListener("focusout", event => {
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
  $("#cueList").addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.target.closest("button,input,textarea,.token-selection-menu")) return;
    state.followPlayback = false;
    state.marquee = {
      x:event.clientX, y:event.clientY, dragged:false,
      additive:event.ctrlKey || event.metaKey
    };
  });
  $("#cueList").addEventListener("scroll", () => {
    positionSelectionMenus();
    positionEditorTutorial();
  }, {passive:true});
  new MutationObserver(() => {
    positionEditorTutorial();
  }).observe($("#cueList"), {
    childList:true, subtree:true
  });
  window.addEventListener("pointermove", updateTokenMarquee);
  window.addEventListener("pointerup", finishTokenMarquee);
  document.addEventListener("pointerdown", event => {
    if (event.target.closest(".display-token,.token-selection-menu,.reference-selection-menu")) return;
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
  $("#detectSpeakers").onclick = openSpeakerDialog;
  $("#cancelSpeakerDialog").onclick = closeSpeakerDialog;
  $("#speakerDialogForm").addEventListener("submit", async event => {
    event.preventDefault();
    const names = Object.fromEntries([...document.querySelectorAll("[data-speaker-name]")]
      .map(input => [input.dataset.speakerName, input.value.trim()]));
    closeSpeakerDialog();
    await sendOperation(contract.setSpeakerNamesOperation(
      state.revision, names, manualProvenance("set_speaker_names")
    ));
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
  $("#mediaSpeed").addEventListener("input", event => {
    const speed = Math.max(0.25, Math.min(3, Number(event.currentTarget.value) || 1));
    if (activeMedia()) activeMedia().playbackRate = speed;
    $("#mediaSpeedValue").textContent = `${speed.toFixed(2)}x`;
  });
  $("#mediaPlayToggle").onclick = toggleMediaPlayback;
  $("#mediaSeek").addEventListener("input", event => {
    const time = Number(event.currentTarget.value) || 0;
    if (activeMedia()) activeMedia().currentTime = time;
    const cue = state.view?.cue_views.find(item =>
      item.state === "active" && time >= Number(item.start) && time < Number(item.end)
    );
    syncPlaybackFollowAfterSeek();
    if ((cue?.cue_id || null) !== state.activeCueId) {
      activateCue(cue?.cue_id || null, {scroll:true, revealTimeline:true});
    }
    state.timelineController?.revealTime(time, false);
    syncMediaControls();
  });
  $("#mediaMute").onclick = () => {
    const media = activeMedia();
    if (media) media.muted = !media.muted;
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
      if (!panel.open) return;
      document.querySelectorAll("#editorTools > .tool-accordion[open]").forEach(other => {
        if (other !== panel) other.open = false;
      });
    });
  });

  initializeCueTimeController();
  initializeCueListView();
  configureMedia("video");
  loadEditorSettings();
  loadProjects();
})();
