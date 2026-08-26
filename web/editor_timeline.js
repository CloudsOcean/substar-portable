(function (root, factory) {
  const ordering = root?.EditorCueOrdering
    || (typeof module === "object" && module.exports ? require("./editor_cue_ordering.js") : null);
  const api = factory(ordering);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorTimeline = api;
})(typeof globalThis === "object" ? globalThis : this, function (ordering) {
  "use strict";

  if (!ordering?.canonicalCueOrder) throw new Error("Cue ordering contract is required");
  const languageLayout = (typeof globalThis === "object" && globalThis.SubstarLanguageLayout)
    || {layoutTokens:values => (values || []).join(" ")};

  const MIN_CUE_SECONDS = 0.04;
  const DEFAULT_MANUAL_SECONDS = 1.5;
  const DEFAULT_WINDOW_SECONDS = 18;
  const SHARED_EPSILON = 0.041;
  const WAVEFORM_BAR_STEP = 4;
  const WAVEFORM_BAR_WIDTH = 3.2;

  function number(value, fallback = 0) {
    const result = Number(value);
    return Number.isFinite(result) ? result : fallback;
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function activeCues(view) {
    const tokens = new Map((view?.token_views || []).map(token => [String(token.token_id), token]));
    return ordering.canonicalCueOrder(view?.cue_views || [])
      .filter(cue => cue.state !== "deleted")
      .map(cue => ({
        cue_id:String(cue.cue_id),
        index:Number(cue.index),
        start:number(cue.start),
        end:number(cue.end),
        state:String(cue.state || "active"),
        speaker:cue.speaker ?? null,
        display_token_ids:[...(cue.display_token_ids || [])],
        text:languageLayout.layoutTokens((cue.active_display_token_ids || cue.display_token_ids || [])
          .map(id => tokens.get(String(id))?.text || "").filter(Boolean))
      }));
  }

  function timelineDuration(view, mediaDuration = 0) {
    return Math.max(
      number(mediaDuration),
      ...(view?.cue_views || []).map(cue => number(cue.end)),
      0.001
    );
  }

  function cueAtTime(cues, time) {
    let low = 0;
    let high = cues.length - 1;
    let candidate = -1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if (cues[middle].start <= time) {
        candidate = middle;
        low = middle + 1;
      } else high = middle - 1;
    }
    if (candidate < 0) return null;
    const cue = cues[candidate];
    return time < cue.end ? cue : null;
  }

  function rangesTouch(left, right, epsilon = SHARED_EPSILON) {
    return !!left && !!right && Math.abs(right.start - left.end) <= epsilon;
  }

  function sharedBoundary(cues, cueIndex, edge) {
    if (edge === "end" && cueIndex < cues.length - 1) {
      return {leftIndex:cueIndex, rightIndex:cueIndex + 1};
    }
    if (edge === "start" && cueIndex > 0) {
      return {leftIndex:cueIndex - 1, rightIndex:cueIndex};
    }
    return null;
  }

  function boundaryMode(cues, cueIndex, edge, selectedCueId = null, modeOverride = null) {
    if (modeOverride === "single") return "single";
    if (modeOverride === "shared" && sharedBoundary(cues, cueIndex, edge)) return "shared";
    if (selectedCueId !== null && cues[cueIndex]?.cue_id === selectedCueId) return "single";
    const pair = sharedBoundary(cues, cueIndex, edge);
    if (!pair) return "single";
    return rangesTouch(cues[pair.leftIndex], cues[pair.rightIndex]) ? "shared" : "single";
  }

  function previewBoundaryChange(cues, cueIndex, edge, requestedTime, selectedCueId = null, modeOverride = null) {
    const cue = cues[cueIndex];
    if (!cue || !["start", "end"].includes(edge)) return null;
    const mode = boundaryMode(cues, cueIndex, edge, selectedCueId, modeOverride);
    if (mode === "shared") {
      const pair = sharedBoundary(cues, cueIndex, edge);
      const left = cues[pair.leftIndex];
      const right = cues[pair.rightIndex];
      const time = clamp(number(requestedTime), left.start + MIN_CUE_SECONDS, right.end - MIN_CUE_SECONDS);
      return {
        mode,
        time:Number(time.toFixed(3)),
        primary:{cue_id:cue.cue_id, edge},
        changes:[
          {cue_id:left.cue_id, edge:"end", time:Number(time.toFixed(3))},
          {cue_id:right.cue_id, edge:"start", time:Number(time.toFixed(3))}
        ]
      };
    }
    const previous = cues[cueIndex - 1];
    const next = cues[cueIndex + 1];
    const minimum = edge === "start" ? (previous?.end ?? 0) : cue.start + MIN_CUE_SECONDS;
    const maximum = edge === "start" ? cue.end - MIN_CUE_SECONDS : (next?.start ?? Infinity);
    const time = clamp(number(requestedTime), minimum, maximum);
    return {
      mode,
      time:Number(time.toFixed(3)),
      primary:{cue_id:cue.cue_id, edge},
      changes:[{cue_id:cue.cue_id, edge, time:Number(time.toFixed(3))}]
    };
  }

  function applyPreview(cues, preview) {
    if (!preview) return cues;
    const changes = new Map(preview.changes.map(change => [`${change.cue_id}:${change.edge}`, change.time]));
    return cues.map(cue => ({
      ...cue,
      start:changes.get(`${cue.cue_id}:start`) ?? cue.start,
      end:changes.get(`${cue.cue_id}:end`) ?? cue.end
    }));
  }

  function previewHasTimeChange(cues, preview, epsilon = 0.0005) {
    if (!preview?.changes?.length) return false;
    const byId = new Map(cues.map(cue => [cue.cue_id, cue]));
    return preview.changes.some(change => {
      const cue = byId.get(change.cue_id);
      if (!cue || !["start", "end"].includes(change.edge)) return false;
      return Math.abs(number(cue[change.edge]) - number(change.time)) > epsilon;
    });
  }

  function autoSnapPlan(view, options = {}) {
    const cues = activeCues(view);
    const thresholdMs = options.backwardThresholdMs;
    const threshold = thresholdMs === null || thresholdMs === undefined
      ? null : clamp(number(thresholdMs), 0, 2000) / 1000;
    const forwardStarts = options.forwardStarts || {};
    const starts = cues.map((cue, index) => {
      const requested = Object.prototype.hasOwnProperty.call(forwardStarts, cue.cue_id)
        ? number(forwardStarts[cue.cue_id], cue.start) : cue.start;
      const minimum = index ? cues[index - 1].end : 0;
      return clamp(requested, minimum, cue.start);
    });
    const ends = cues.map(cue => cue.end);
    if (threshold !== null) {
      for (let index = 0; index < cues.length - 1; index += 1) {
        const gap = cues[index + 1].start - cues[index].end;
        if (gap > 0.001 && gap <= threshold) ends[index] = Math.max(ends[index], starts[index + 1]);
      }
    }
    const changes = [];
    for (let index = 0; index < cues.length; index += 1) {
      if (Math.abs(starts[index] - cues[index].start) > 0.0005) changes.push({cue_id:cues[index].cue_id, edge:"start", time:Number(starts[index].toFixed(3))});
      if (Math.abs(ends[index] - cues[index].end) > 0.0005) changes.push({cue_id:cues[index].cue_id, edge:"end", time:Number(ends[index].toFixed(3))});
    }
    return {
      type:"auto_snap_once",
      backward_threshold_ms:threshold === null ? null : Math.round(threshold * 1000),
      changes,
      count:new Set(changes.map(change => change.cue_id)).size
    };
  }

  function manualCueIntent(view, time, defaultDuration = DEFAULT_MANUAL_SECONDS, mediaDuration = 0) {
    const at = number(time);
    const start = Math.max(0, at);
    const cues = activeCues(view);
    const occupied = cueAtTime(cues, start);
    if (occupied) {
      return {type:"manual_cue_occupied", cue_id:occupied.cue_id, time:start};
    }
    const following = cues.find(cue => cue.start >= start);
    const limit = number(mediaDuration) > 0 ? number(mediaDuration) : Infinity;
    const end = Math.min(start + Math.max(number(defaultDuration), 0), following?.start ?? Infinity, limit);
    if (!(end > start)) return {type:"manual_cue_unavailable", time:start};
    return {
      type:"create_manual_cue",
      start:Number(start.toFixed(3)),
      end:Number(end.toFixed(3)),
      text:"人工未对齐字幕",
      source_token_ids:[],
      time_status:"manual_unaligned",
      confidence:"degraded"
    };
  }

  function resolveOperation(factory, name, intent) {
    const builder = factory?.[name];
    return typeof builder === "function" ? builder(intent) : intent;
  }

  function canvasSize(canvas) {
    const rect = canvas.getBoundingClientRect();
    return {
      width:Math.max(1, rect.width || canvas.clientWidth || 800),
      height:Math.max(1, rect.height || canvas.clientHeight || 150)
    };
  }

  function cssColor(documentRef, name, fallback) {
    const root = documentRef?.documentElement;
    const value = root && documentRef.defaultView
      ? documentRef.defaultView.getComputedStyle(root).getPropertyValue(name).trim()
      : "";
    return value || fallback;
  }

  function createTimelineController(options) {
    if (!options?.canvas) throw new Error("timeline canvas is required");
    const canvas = options.canvas;
    const media = options.media || null;
    const eventTarget = options.keyboardTarget
      || (typeof document === "object" ? document : null);
    const requestFrame = options.requestFrame
      || (typeof requestAnimationFrame === "function" ? requestAnimationFrame : callback => setTimeout(callback, 0));
    const cancelFrame = options.cancelFrame
      || (typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : clearTimeout);
    const operations = options.operations || {};
    let view = null;
    let waveform = [];
    let waveformStart = 0;
    let waveformEnd = 0;
    let waveformRequestKey = "";
    let waveformRequestSequence = 0;
    let waveformStatsCache = null;
    let lastPlayheadDrawAt = 0;
    let duration = 0.001;
    let viewStart = 0;
    let viewEnd = DEFAULT_WINDOW_SECONDS;
    let activeCueId = null;
    let selectedCueId = null;
    let boundaryModeOverride = null;
    let hover = null;
    let drag = null;
    let suppressClick = false;
    let preview = null;
    let timelineCues = [];
    let animationFrame = 0;
    let playbackFrame = 0;
    let followEnabled = false;
    let edgePanFrame = 0;
    let edgePanPointer = null;
    let destroyed = false;
    let staticDirty = true;
    const backgroundCanvas = canvas.ownerDocument?.createElement?.("canvas") || null;
    const themeTarget = canvas.ownerDocument?.documentElement || null;

    function timelineTheme() {
      const documentRef = canvas.ownerDocument;
      return {
        background:cssColor(documentRef, "--theme-timeline-bg", "#0b0e14"),
        ruler:cssColor(documentRef, "--theme-timeline-ruler", "#080a0f"),
        grid:cssColor(documentRef, "--theme-timeline-grid", "#3a4150"),
        waveform:cssColor(documentRef, "--theme-timeline-waveform", "rgba(221,201,226,.78)"),
        cue:cssColor(documentRef, "--theme-timeline-cue", "rgba(45,38,52,.94)"),
        cueAlt:cssColor(documentRef, "--theme-timeline-cue-alt", "rgba(53,43,60,.94)"),
        text:cssColor(documentRef, "--theme-timeline-text", "#f0eaf2"),
        outline:cssColor(documentRef, "--theme-timeline-outline", "rgba(153,133,166,.72)"),
        selected:cssColor(documentRef, "--theme-timeline-selected", "#ffe06a"),
        labelShadow:cssColor(documentRef, "--theme-timeline-label-shadow", "rgba(0,0,0,.78)"),
        speaker0:cssColor(documentRef, "--theme-timeline-speaker-0", "rgba(28,65,111,.94)"),
        speaker1:cssColor(documentRef, "--theme-timeline-speaker-1", "rgba(94,38,69,.94)"),
        speaker2:cssColor(documentRef, "--theme-timeline-speaker-2", "rgba(22,82,68,.94)"),
        speaker3:cssColor(documentRef, "--theme-timeline-speaker-3", "rgba(96,57,22,.94)")
      };
    }

    function cues() {
      return timelineCues;
    }

    function visibleCues(cueRanges) {
      let low = 0;
      let high = cueRanges.length;
      while (low < high) {
        const middle = (low + high) >> 1;
        if (cueRanges[middle].end < viewStart) low = middle + 1;
        else high = middle;
      }
      let end = low;
      while (end < cueRanges.length && cueRanges[end].start <= viewEnd) end += 1;
      return cueRanges.slice(low, end);
    }

    function visibleSpan() {
      return Math.max(0.001, viewEnd - viewStart);
    }

    function xAt(time, width) {
      return (time - viewStart) / visibleSpan() * width;
    }

    function timeAt(x, width) {
      return clamp(viewStart + x / Math.max(1, width) * visibleSpan(), 0, duration);
    }

    function shiftVisibleWindow(delta) {
      const span = visibleSpan();
      viewStart = clamp(viewStart + delta, 0, Math.max(0, duration - span));
      viewEnd = viewStart + span;
    }

    function stopEdgePan() {
      if (edgePanFrame) cancelFrame(edgePanFrame);
      edgePanFrame = 0;
      edgePanPointer = null;
    }

    function edgePanStep() {
      edgePanFrame = 0;
      if (destroyed || drag?.kind !== "playhead" || !edgePanPointer) return;
      const {x, width} = edgePanPointer;
      const threshold = Math.min(54, Math.max(28, width * .07));
      const direction = x <= threshold ? -1 : x >= width - threshold ? 1 : 0;
      if (!direction) return;
      const before = viewStart;
      shiftVisibleWindow(direction * visibleSpan() * .018);
      if (viewStart !== before) {
        const time = timeAt(clamp(x, 0, width), width);
        if (media) media.currentTime = time;
        options.onSeek?.(time);
        draw();
      }
      edgePanFrame = requestFrame(edgePanStep);
    }

    function updateEdgePan(pointer) {
      if (drag?.kind !== "playhead") {
        stopEdgePan();
        return;
      }
      edgePanPointer = pointer;
      const threshold = Math.min(54, Math.max(28, pointer.width * .07));
      const atEdge = pointer.x <= threshold || pointer.x >= pointer.width - threshold;
      if (atEdge && !edgePanFrame) edgePanFrame = requestFrame(edgePanStep);
      if (!atEdge) stopEdgePan();
    }

    function prepareCanvas() {
      const size = canvasSize(canvas);
      const ratio = number(options.pixelRatio, typeof devicePixelRatio === "number" ? devicePixelRatio : 1);
      const pixelWidth = Math.round(size.width * ratio);
      const pixelHeight = Math.round(size.height * ratio);
      if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
      if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
      const context = canvas.getContext("2d");
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      return {...size, context};
    }

    function drawRuler(context, width) {
      const colors = timelineTheme();
      context.fillStyle = colors.ruler;
      context.fillRect(0, 0, width, 22);
      const step = visibleSpan() <= 8 ? 1 : visibleSpan() <= 30 ? 2 : visibleSpan() <= 90 ? 5 : 10;
      const first = Math.ceil(viewStart / step) * step;
      context.font = "9px Consolas, monospace";
      context.textBaseline = "top";
      for (let time = first; time <= viewEnd; time += step) {
        const x = xAt(time, width);
        context.strokeStyle = colors.grid;
        context.beginPath();
        context.moveTo(x, 14);
        context.lineTo(x, 22);
        context.stroke();
        context.fillStyle = colors.outline;
        context.fillText(`${Math.floor(time / 60)}:${String(Math.floor(time % 60)).padStart(2, "0")}`, x + 3, 2);
      }
    }

    function drawWaveform(context, width, height) {
      if (!waveform.length) return;
      const top = 36;
      const bottom = height - 10;
      const center = (top + bottom) / 2;
      const amplitude = (bottom - top) * 0.37;
      const statsKey = `${waveformStart}:${waveformEnd}:${viewStart.toFixed(3)}:${viewEnd.toFixed(3)}:${width}`;
      const visibleValues = [];
      const waveformSpan = Math.max(.001, waveformEnd - waveformStart);
      const indexAt = time => clamp(
        Math.floor((time - waveformStart) / waveformSpan * waveform.length),
        0,
        waveform.length - 1
      );
      const startIndex = indexAt(viewStart);
      const endIndex = Math.min(waveform.length, indexAt(viewEnd) + 1);
      const sampleStep = Math.max(1, Math.floor((endIndex - startIndex) / Math.max(200, width)));
      for (let index = startIndex; index < endIndex; index += sampleStep) {
        visibleValues.push(clamp(Math.abs(number(waveform[index])), 0, 1));
      }
      if (!waveformStatsCache || waveformStatsCache.key !== statsKey) {
        visibleValues.sort((left, right) => left - right);
        const percentile = ratio => visibleValues[Math.min(visibleValues.length - 1, Math.max(0, Math.floor((visibleValues.length - 1) * ratio)))] || 0;
        const noiseFloor = percentile(.1);
        waveformStatsCache = {key:statsKey, noiseFloor, voicePeak:Math.max(percentile(.95), noiseFloor + .0001)};
      }
      const {noiseFloor, voicePeak} = waveformStatsCache;
      context.strokeStyle = timelineTheme().waveform;
      context.lineWidth = WAVEFORM_BAR_WIDTH;
      context.lineCap = "round";
      context.beginPath();
      for (let x = 2; x < width; x += WAVEFORM_BAR_STEP) {
        const from = indexAt(timeAt(x - WAVEFORM_BAR_STEP / 2, width));
        const to = Math.min(waveform.length, indexAt(timeAt(x + WAVEFORM_BAR_STEP / 2, width)) + 1);
        let raw = 0;
        for (let index = from; index < to; index += 1) {
          raw = Math.max(raw, clamp(Math.abs(number(waveform[index])), 0, 1));
        }
        const normalized = clamp((raw - noiseFloor) / (voicePeak - noiseFloor), 0, 1);
        const expanded = normalized <= .03 ? 0 : Math.pow(normalized, .66);
        const bar = expanded <= 0 ? 1.5 : Math.max(4, expanded * amplitude);
        context.moveTo(x, center - bar);
        context.lineTo(x, center + bar);
      }
      context.stroke();
      context.lineCap = "butt";
    }

    function requestWaveformWindow(width) {
      if (!options.onWaveformWindow || destroyed || duration <= 0) return;
      const span = visibleSpan();
      const margin = Math.min(30, span * .35);
      const wantedStart = clamp(viewStart - margin, 0, duration);
      const wantedEnd = clamp(viewEnd + margin, wantedStart, duration);
      const resolution = waveform.length / Math.max(.001, waveformEnd - waveformStart);
      const neededResolution = Math.max(8, width / span * .75);
      if (waveform.length && waveformStart <= viewStart && waveformEnd >= viewEnd
          && resolution >= neededResolution) return;
      const points = Math.round(clamp(width * 2, 512, 4096));
      const key = `${wantedStart.toFixed(3)}:${wantedEnd.toFixed(3)}:${points}`;
      if (key === waveformRequestKey) return;
      waveformRequestKey = key;
      const sequence = ++waveformRequestSequence;
      Promise.resolve(options.onWaveformWindow({start:wantedStart, end:wantedEnd, points}))
        .then(payload => {
          if (destroyed || sequence !== waveformRequestSequence || !payload) return;
      waveform = Array.from(payload.peaks || [], value => clamp(number(value), -1, 1));
          waveformStatsCache = null;
          waveformStart = number(payload.window_start, wantedStart);
          waveformEnd = number(payload.window_end, wantedEnd);
          duration = Math.max(duration, number(payload.duration));
          draw();
        })
        .catch(error => options.onWaveformError?.(error));
    }

    function drawCues(context, width, height, cueRanges) {
      const colors = timelineTheme();
      const top = 28;
      const bottom = height - 8;
      cueRanges.forEach(cue => {
        if (cue.end < viewStart || cue.start > viewEnd) return;
        const left = xAt(cue.start, width);
        const right = xAt(cue.end, width);
        const selected = cue.cue_id === activeCueId;
        const speakerColors = {
          speaker_0:colors.speaker0, speaker_1:colors.speaker1,
          speaker_2:colors.speaker2, speaker_3:colors.speaker3
        };
        context.fillStyle = speakerColors[cue.speaker]
          || (cue.index % 2 ? colors.cue : colors.cueAlt);
        context.fillRect(left, top, Math.max(1, right - left), bottom - top);
        context.strokeStyle = selected ? colors.selected : colors.outline;
        context.lineWidth = selected ? 2 : 1;
        context.strokeRect(left + .5, top + .5, Math.max(0, right - left - 1), bottom - top - 1);
        context.lineWidth = 1;
      });
    }

    function drawCueLabels(context, width, height, cueRanges) {
      const colors = timelineTheme();
      const top = 28;
      context.fillStyle = colors.text;
      context.font = "600 13px 'Segoe UI', sans-serif";
      context.textBaseline = "top";
      cueRanges.forEach(cue => {
        if (cue.end < viewStart || cue.start > viewEnd) return;
        const left = xAt(cue.start, width);
        const right = xAt(cue.end, width);
        const available = Math.max(0, right - left - 12);
        if (available < 24) return;
        if (cue.cue_id === activeCueId) {
          context.strokeStyle = colors.selected;
          context.lineWidth = 2;
          context.strokeRect(left + 1, top + 1, Math.max(0, right - left - 2), height - top - 10);
          context.lineWidth = 1;
        }
        const words = `${cue.index + 1} ${cue.text || ""}`.split(/\s+/).filter(Boolean);
        const lines = [];
        let line = "";
        words.forEach(word => {
          const candidate = line ? `${line} ${word}` : word;
          if (line && context.measureText(candidate).width > available) {
            lines.push(line);
            line = word;
          } else line = candidate;
        });
        if (line) lines.push(line);
        lines.slice(0, Math.max(1, Math.floor((height - top - 12) / 17))).forEach((text, index) => {
          context.strokeStyle = colors.labelShadow;
          context.lineWidth = 2;
          context.lineJoin = "round";
          context.strokeText(text, left + 6, top + 6 + index * 17, available);
          context.fillStyle = colors.text;
          context.fillText(text, left + 6, top + 6 + index * 17, available);
        });
        context.lineWidth = 1;
      });
    }

    function boundaryModeFor(target) {
      const override = boundaryModeOverride
        && boundaryModeOverride.cueId === target?.cueId
        && boundaryModeOverride.edge === target?.edge
        ? boundaryModeOverride.mode : null;
      return boundaryMode(cues(), target?.cueIndex, target?.edge, selectedCueId, override);
    }

    function boundaryModeBadge(target, width) {
      if (!target || !["boundary", "mode-toggle"].includes(target.kind)) return null;
      const cue = applyPreview(cues(), preview)[target.cueIndex];
      if (!cue) return null;
      const time = target.edge === "start" ? cue.start : cue.end;
      const labelX = Math.min(width - 30, xAt(time, width) + 6);
      return {left:labelX - 3, right:labelX + 29, top:23, bottom:42, labelX};
    }

    function drawBoundaryHighlight(context, width, height) {
      const target = drag?.kind === "boundary"
        ? drag : ["boundary", "mode-toggle"].includes(hover?.kind) ? hover : null;
      if (!target) return;
      const colors = timelineTheme();
      const cue = applyPreview(cues(), preview)[target.cueIndex];
      if (!cue) return;
      const time = target.edge === "start" ? cue.start : cue.end;
      const x = xAt(time, width);
      context.strokeStyle = target.mode === "shared" ? "#4ee1d2" : colors.selected;
      context.lineWidth = drag ? 4 : 3;
      context.shadowColor = context.strokeStyle;
      context.shadowBlur = drag ? 10 : 5;
      context.beginPath();
      context.moveTo(x, 24);
      context.lineTo(x, height);
      context.stroke();
      context.shadowBlur = 0;
      context.fillStyle = context.strokeStyle;
      context.fillRect(x - 5, 24, 10, 4);
      context.fillRect(x - 5, height - 4, 10, 4);
      const badge = boundaryModeBadge(target, width);
      context.globalAlpha = .2;
      context.fillRect(badge.left, badge.top, badge.right - badge.left, badge.bottom - badge.top);
      context.globalAlpha = 1;
      context.strokeStyle = context.fillStyle;
      context.lineWidth = 1;
      context.strokeRect(badge.left + .5, badge.top + .5, badge.right - badge.left - 1, badge.bottom - badge.top - 1);
      context.font = "700 10px 'Segoe UI', sans-serif";
      context.textBaseline = "top";
      context.fillText(target.mode === "shared" ? "联动" : "单条", badge.labelX, 26);
    }

    function drawPlayhead(context, width, height) {
      const time = number(media?.currentTime);
      if (time < viewStart || time > viewEnd) return;
      const x = xAt(time, width);
      context.strokeStyle = "#ff5b22";
      context.lineWidth = 3;
      context.shadowColor = "rgba(255,91,34,.8)";
      context.shadowBlur = 8;
      context.beginPath();
      context.moveTo(x, 0);
      context.lineTo(x, height);
      context.stroke();
      context.shadowBlur = 0;
    }

    function drawStatic(context, width, height) {
      requestWaveformWindow(width);
      context.clearRect(0, 0, width, height);
      context.fillStyle = timelineTheme().background;
      context.fillRect(0, 0, width, height);
      drawRuler(context, width);
      const cueRanges = applyPreview(cues(), preview);
      const drawRanges = visibleCues(cueRanges);
      drawCues(context, width, height, drawRanges);
      drawWaveform(context, width, height);
      drawCueLabels(context, width, height, drawRanges);
      drawBoundaryHighlight(context, width, height);
    }

    function drawNow() {
      if (destroyed) return;
      const {context, width, height} = prepareCanvas();
      if (backgroundCanvas) {
        const ratio = number(options.pixelRatio, typeof devicePixelRatio === "number" ? devicePixelRatio : 1);
        const pixelWidth = Math.round(width * ratio);
        const pixelHeight = Math.round(height * ratio);
        if (backgroundCanvas.width !== pixelWidth || backgroundCanvas.height !== pixelHeight) {
          backgroundCanvas.width = pixelWidth;
          backgroundCanvas.height = pixelHeight;
          staticDirty = true;
        }
        if (staticDirty) {
          const background = backgroundCanvas.getContext("2d");
          background.setTransform(ratio, 0, 0, ratio, 0, 0);
          drawStatic(background, width, height);
          staticDirty = false;
        }
        context.save();
        context.setTransform(1, 0, 0, 1, 0, 0);
        context.clearRect(0, 0, canvas.width, canvas.height);
        context.drawImage(backgroundCanvas, 0, 0);
        context.restore();
      } else {
        drawStatic(context, width, height);
      }
      drawPlayhead(context, width, height);
    }

    const drawReasons = new Set();

    function draw(reason = "all") {
      if (destroyed) return;
      if (reason !== "playhead") staticDirty = true;
      drawReasons.add(reason);
      if (animationFrame) return;
      animationFrame = -1;
      const scheduled = requestFrame(() => {
        animationFrame = 0;
        const reasons = [...drawReasons];
        drawReasons.clear();
        drawNow();
        options.onDraw?.(reasons);
      });
      if (animationFrame === -1) animationFrame = scheduled;
    }

    function pointerPosition(event) {
      const rect = canvas.getBoundingClientRect();
      return {x:event.clientX - rect.left, y:event.clientY - rect.top, width:rect.width};
    }

    function pointerTarget(event) {
      const pointer = pointerPosition(event);
      const badgeTarget = ["boundary", "mode-toggle"].includes(hover?.kind) ? hover : null;
      const badge = boundaryModeBadge(badgeTarget, pointer.width);
      if (badge && pointer.x >= badge.left && pointer.x <= badge.right
        && pointer.y >= badge.top && pointer.y <= badge.bottom
        && sharedBoundary(cues(), badgeTarget.cueIndex, badgeTarget.edge)) {
        return {...badgeTarget, kind:"mode-toggle", mode:boundaryModeFor(badgeTarget)};
      }
      // The playhead is the primary timeline control.  Test its wider logical
      // hit area before cue bodies and boundary handles so an overlapping
      // resize/select target can never steal a playhead drag.
      const playheadDistance = Math.abs(xAt(number(media?.currentTime), pointer.width) - pointer.x);
      if (playheadDistance <= 8) {
        return {kind:"playhead", distance:playheadDistance};
      }
      const cueRanges = applyPreview(cues(), preview);
      let nearest = null;
      cueRanges.forEach((cue, cueIndex) => {
        [["start", cue.start], ["end", cue.end]].forEach(([edge, time]) => {
          const distance = Math.abs(xAt(time, pointer.width) - pointer.x);
          const selectedSideWinsTie = nearest
            && distance === nearest.distance
            && cue.cue_id === selectedCueId
            && nearest.cueId !== selectedCueId;
          if (distance <= 7 && (!nearest || distance < nearest.distance || selectedSideWinsTie)) {
            nearest = {kind:"boundary", cueIndex, cueId:cue.cue_id, edge, distance};
          }
        });
      });
      if (nearest) return nearest;
      const time = timeAt(pointer.x, pointer.width);
      const cue = cueAtTime(cueRanges, time);
      return cue ? {kind:"cue", cueId:cue.cue_id, time} : {kind:"blank", time};
    }

    function emitOperation(name, intent) {
      const operation = resolveOperation(operations, name, intent);
      if (operation !== null && operation !== undefined) options.onOperation?.(operation, intent);
      return operation;
    }

    function toggleBoundaryMode(target) {
      if (!sharedBoundary(cues(), target.cueIndex, target.edge)) return false;
      const mode = boundaryModeFor(target) === "shared" ? "single" : "shared";
      boundaryModeOverride = {cueId:target.cueId, edge:target.edge, mode};
      hover = {...target, mode:boundaryModeFor(target)};
      canvas.style.cursor = "pointer";
      draw();
      return true;
    }

    function handlePointerDown(event) {
      if (event.button !== 0) return;
      suppressClick = false;
      const target = pointerTarget(event);
      const pointer = pointerPosition(event);
      if (target.kind === "boundary") {
        target.mode = boundaryModeFor(target);
        drag = {...target, pointerStartX:pointer.x, moved:false};
        preview = previewBoundaryChange(cues(), target.cueIndex, target.edge, target.edge === "start"
          ? cues()[target.cueIndex].start : cues()[target.cueIndex].end, selectedCueId, target.mode);
        canvas.setPointerCapture?.(event.pointerId);
        canvas.style.cursor = target.mode === "shared" ? "col-resize" : "ew-resize";
        draw();
        event.preventDefault();
        return;
      }
      if (target.kind === "playhead") {
        drag = {...target, pointerStartX:pointer.x, moved:false};
        edgePanPointer = pointer;
        canvas.setPointerCapture?.(event.pointerId);
        canvas.style.cursor = "grabbing";
        event.preventDefault();
      }
    }

    function handlePointerMove(event) {
      const pointer = pointerPosition(event);
      if (!drag) {
        hover = pointerTarget(event);
        if (hover.kind === "boundary") {
          hover.mode = boundaryModeFor(hover);
          canvas.style.cursor = "ew-resize";
        } else if (hover.kind === "mode-toggle") {
          canvas.style.cursor = "pointer";
        } else if (hover.kind === "playhead") canvas.style.cursor = "grab";
        else canvas.style.cursor = "pointer";
        draw();
        return;
      }
      if (Math.abs(pointer.x - drag.pointerStartX) > 2) drag.moved = true;
      const time = timeAt(pointer.x, pointer.width);
      if (drag.kind === "playhead") {
        updateEdgePan(pointer);
        if (media) media.currentTime = time;
        options.onSeek?.(time);
        draw();
        return;
      }
      preview = previewBoundaryChange(cues(), drag.cueIndex, drag.edge, time, selectedCueId, drag.mode);
      options.onBoundaryPreview?.(preview);
      draw();
    }

    function handlePointerUp(event) {
      if (!drag) return;
      const finished = drag;
      drag = null;
      stopEdgePan();
      canvas.releasePointerCapture?.(event.pointerId);
      canvas.style.cursor = "pointer";
      if (finished.kind === "boundary" && preview && finished.moved
        && previewHasTimeChange(cues(), preview)) {
        emitOperation("adjustBoundary", preview);
      }
      suppressClick = finished.moved;
      preview = null;
      draw();
    }

    function handleClick(event) {
      if (drag || suppressClick) {
        suppressClick = false;
        return;
      }
      const target = pointerTarget(event);
      if (["boundary", "mode-toggle"].includes(target.kind) && toggleBoundaryMode(target)) return;
      if (target.kind !== "cue" && target.kind !== "blank") return;
      const time = target.time;
      if (media) media.currentTime = time;
      options.onSeek?.(time);
      if (target.kind === "cue") {
        if (selectedCueId !== target.cueId) boundaryModeOverride = null;
        selectedCueId = target.cueId;
        options.onSelectCue?.(target.cueId, time);
      } else {
        boundaryModeOverride = null;
        selectedCueId = null;
        options.onSelectCue?.(null, time);
      }
      draw();
    }

    function handleContextMenu(event) {
      event.preventDefault();
      const target = pointerTarget(event);
      if (target.kind !== "blank" && target.kind !== "cue") return;
      const intent = manualCueIntent(view, target.time, options.manualCueSeconds, duration);
      if (intent) emitOperation("createManualCue", intent);
    }

    function handleWheel(event) {
      if (!event.altKey || !view) return;
      event.preventDefault();
      const pointer = pointerPosition(event);
      const oldSpan = visibleSpan();
      const ratio = clamp(pointer.x / Math.max(1, pointer.width), 0, 1);
      const factor = event.deltaY > 0 ? 1.22 : 1 / 1.22;
      const minimum = Math.min(2, duration);
      const span = clamp(oldSpan * factor, minimum, duration);
      const anchor = viewStart + oldSpan * ratio;
      viewStart = clamp(anchor - span * ratio, 0, Math.max(0, duration - span));
      viewEnd = viewStart + span;
      draw();
    }

    function handleKeyDown(event) {
      if (event.key !== "Backspace" || !selectedCueId) return;
      const target = event.target;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      event.preventDefault();
      emitOperation("deleteCue", {type:"delete_cue", cue_id:selectedCueId});
    }

    function followPlayhead(timestamp = 0) {
      if (!followEnabled || destroyed || !media || media.paused || media.ended) return;
      const time = number(media.currentTime);
      const span = visibleSpan();
      let windowChanged = false;
      if (time < viewStart + span * .04 || time > viewEnd - span * .06) {
        viewStart = clamp(time - span * .12, 0, Math.max(0, duration - span));
        viewEnd = viewStart + span;
        windowChanged = true;
      }
      // A seek can move the visible window on a frame that would otherwise be
      // skipped by the playhead throttle.  Rebuild the static canvas on that
      // same frame so it can never retain the old window coordinate system.
      if (windowChanged) {
        lastPlayheadDrawAt = timestamp;
        draw("follow-window");
      } else if (!lastPlayheadDrawAt || timestamp - lastPlayheadDrawAt >= 30) {
        lastPlayheadDrawAt = timestamp;
        draw("playhead");
      }
      playbackFrame = requestFrame(followPlayhead);
    }

    function handlePlay() {
      if (!playbackFrame) playbackFrame = requestFrame(followPlayhead);
    }

    function handlePause() {
      if (playbackFrame) cancelFrame(playbackFrame);
      playbackFrame = 0;
      lastPlayheadDrawAt = 0;
      if (animationFrame) cancelFrame(animationFrame);
      animationFrame = 0;
      draw("playhead");
    }

    function handleThemeChange() {
      staticDirty = true;
      waveformStatsCache = null;
      draw("theme");
    }

    canvas.addEventListener("pointerdown", handlePointerDown);
    canvas.addEventListener("pointermove", handlePointerMove);
    canvas.addEventListener("pointerup", handlePointerUp);
    canvas.addEventListener("pointercancel", handlePointerUp);
    canvas.addEventListener("click", handleClick);
    canvas.addEventListener("contextmenu", handleContextMenu);
    canvas.addEventListener("wheel", handleWheel, {passive:false});
    eventTarget?.addEventListener("keydown", handleKeyDown);
    themeTarget?.addEventListener("substar:themechange", handleThemeChange);
    media?.addEventListener?.("play", handlePlay);
    media?.addEventListener?.("pause", handlePause);

    return Object.freeze({
      setView(nextView, {drawNow = true} = {}) {
        const hadView = view !== null;
        const previousSpan = visibleSpan();
        view = nextView || null;
        timelineCues = activeCues(view);
        duration = timelineDuration(view, media?.duration);
        const span = Math.min(duration, hadView ? previousSpan : DEFAULT_WINDOW_SECONDS);
        viewStart = clamp(viewStart, 0, Math.max(0, duration - span));
        viewEnd = viewStart + span;
        if (activeCueId && !cues().some(cue => cue.cue_id === activeCueId)) activeCueId = null;
        if (selectedCueId && !cues().some(cue => cue.cue_id === selectedCueId)) selectedCueId = null;
        preview = null;
        waveformStatsCache = null;
        if (drawNow) draw();
      },
      setWaveform(samples, waveformDuration = null, windowStart = 0, windowEnd = null) {
        waveform = Array.from(samples || [], value => clamp(number(value), -1, 1));
        duration = Math.max(duration, number(waveformDuration));
        waveformStart = number(windowStart);
        waveformEnd = number(windowEnd, duration);
        draw();
      },
      setSelectedCue(cueId, {drawNow = true} = {}) {
        const nextCueId = cueId === null ? null : String(cueId);
        if (selectedCueId !== nextCueId) boundaryModeOverride = null;
        selectedCueId = nextCueId;
        if (drawNow) draw();
      },
      setActiveCue(cueId, {drawNow = true} = {}) {
        activeCueId = cueId === null ? null : String(cueId);
        if (drawNow) draw();
      },
      setFollowPlayback(enabled) {
        followEnabled = !!enabled;
        if (followEnabled) handlePlay();
        else if (playbackFrame) {
          cancelFrame(playbackFrame);
          playbackFrame = 0;
        }
      },
      revealTime(time, center = false) {
        const value = clamp(number(time), 0, duration);
        const span = visibleSpan();
        if (center || value < viewStart || value > viewEnd) {
          viewStart = clamp(value - span * (center ? .5 : .12), 0, Math.max(0, duration - span));
          viewEnd = viewStart + span;
        }
        draw();
      },
      seek(time) {
        const value = clamp(number(time), 0, duration);
        if (media) media.currentTime = value;
        options.onSeek?.(value);
        draw();
      },
      autoSnapOnce(options) {
        const intent = autoSnapPlan(view, options);
        if (intent.count) emitOperation("autoSnap", intent);
        return intent;
      },
      getState() {
        return {
          active_cue_id:activeCueId,
          selected_cue_id:selectedCueId,
          view_start:viewStart,
          view_end:viewEnd,
          duration,
          preview:preview ? JSON.parse(JSON.stringify(preview)) : null
        };
      },
      redraw:draw,
      destroy() {
        destroyed = true;
        if (animationFrame) cancelFrame(animationFrame);
        if (playbackFrame) cancelFrame(playbackFrame);
        stopEdgePan();
        canvas.removeEventListener("pointerdown", handlePointerDown);
        canvas.removeEventListener("pointermove", handlePointerMove);
        canvas.removeEventListener("pointerup", handlePointerUp);
        canvas.removeEventListener("pointercancel", handlePointerUp);
        canvas.removeEventListener("click", handleClick);
        canvas.removeEventListener("contextmenu", handleContextMenu);
        canvas.removeEventListener("wheel", handleWheel);
        eventTarget?.removeEventListener("keydown", handleKeyDown);
        themeTarget?.removeEventListener("substar:themechange", handleThemeChange);
        media?.removeEventListener?.("play", handlePlay);
        media?.removeEventListener?.("pause", handlePause);
      }
    });
  }

  return Object.freeze({
    MIN_CUE_SECONDS,
    SHARED_EPSILON,
    activeCues,
    cueAtTime,
    rangesTouch,
    boundaryMode,
    previewBoundaryChange,
    applyPreview,
    previewHasTimeChange,
    autoSnapPlan,
    manualCueIntent,
    createTimelineController
  });
});
