(function (root, factory) {
  const timeline = root.EditorTimeline || (typeof module === "object" ? require("./editor_timeline.js") : null);
  const selectionApi = root.EditorCueSelection || (typeof module === "object" ? require("./editor_cue_selection.js") : null);
  const api = factory(timeline, selectionApi);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.EditorMultilineTimeline = api;
})(typeof globalThis === "object" ? globalThis : this, function (timeline, selectionApi) {
  "use strict";
  const ROW_HEIGHT = 180;
  const ROW_GAP = 8;
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  function rowRanges(duration, seconds = 10) {
    const span = Math.max(2, Number(seconds) || 10);
    return Array.from({length:Math.ceil(Math.max(0, duration) / span)}, (_, index) => ({
      index, start:index * span, end:Math.min(duration, (index + 1) * span)
    }));
  }
  function timeLabel(time) {
    return `${String(Math.floor(time / 60)).padStart(2, "0")}:${(time % 60).toFixed(3).padStart(6, "0")}`;
  }
  // Keep the strongest PCM peak per drawing column; never average neighboring peaks.
  function waveformEnvelope(peaks, width, start, end, windowStart, windowEnd, scale = 1, noiseFloor = 0) {
    const values = [];
    const span = Math.max(.001, windowEnd - windowStart);
    for (let x = 0; x < Math.ceil(width); x++) {
      const left = start + x / width * (end - start);
      const right = start + (x + 1) / width * (end - start);
      if (left < windowStart || left >= windowEnd) { values.push(0); continue; }
      const from = clamp(Math.floor((left-windowStart)/span*peaks.length), 0, Math.max(0,peaks.length-1));
      const to = clamp(Math.ceil((right-windowStart)/span*peaks.length), from+1, peaks.length);
      let peak = 0;
      for (let i=from;i<to;i++) peak=Math.max(peak,Math.abs(Number(peaks[i])||0));
      const normalized = clamp((peak-noiseFloor)/Math.max(.001,scale-noiseFloor),0,1);
      values.push(normalized);
    }
    return values;
  }
  function cueHandles(cue, row, seconds, width) {
    const xAt = time => (time-row.start)/seconds*width;
    const left = Math.max(0,xAt(cue.start)), right = Math.min(width,xAt(cue.end));
    const cap = Math.min(10,Math.max(0,(right-left)/4));
    return {left,right,cap,start:cue.start>=row.start,end:cue.end<=row.end};
  }
  function movePreview(cues, cue, delta, duration) {
    const main=cues.filter(c=>c.subtitle_track!=="overlay");
    const index=main.findIndex(c=>c.cue_id===cue.cue_id);
    const low=cue.subtitle_track==="overlay"?0:(main[index-1]?.end ?? 0);
    const high=cue.subtitle_track==="overlay"?duration:(main[index+1]?.start ?? duration);
    const shift=clamp(delta,low-cue.start,high-cue.end);
    return {mode:"single",primary:{cue_id:cue.cue_id},changes:[
      {cue_id:cue.cue_id,edge:"start",time:Number((cue.start+shift).toFixed(3))},
      {cue_id:cue.cue_id,edge:"end",time:Number((cue.end+shift).toFixed(3))}
    ]};
  }
  function rowCueText(cue, row, target = false) {
    const full = String(target ? cue.target_text || "" : cue.text || "");
    if (cue.start >= row.start && cue.end <= row.end) return full;
    const tokens = cue.timed_tokens || [];
    if (!target && tokens.length && tokens.every(t => Number.isFinite(t.start) && Number.isFinite(t.end))) {
      const included = tokens.filter(t => t.end > row.start && t.start < row.end);
      if (!included.length) return "（间歇）";
      const layout = globalThis.SubstarLanguageLayout?.layoutTokens || (values => values.join(" "));
      return (tokens.indexOf(included[0]) > 0 ? "… " : "") + layout(included.map(t => t.text))
        + (tokens.indexOf(included[included.length-1]) < tokens.length-1 ? " …" : "");
    }
    return cue.start < row.start ? "续 · " + full : full;
  }
  function createTimelineController(options) {
    const doc = options.canvas.ownerDocument;
    const panel = options.canvas.closest(".cue-timeline-panel");
    panel.classList.add("multiline-waveform-module");
    const originalTrack = options.canvas.parentElement;
    originalTrack.hidden = true;
    const toolbar = doc.createElement("div");
    toolbar.className = "multiline-waveform-toolbar";
    const title = doc.createElement("strong");
    title.textContent = "波形";
    const label = doc.createElement("label");
    label.textContent = "每行 ";
    const rowSelect = doc.createElement("select");
    rowSelect.setAttribute("aria-label", "每行波形时长");
    for (const seconds of [5, 10, 15, 20]) {
      const option = doc.createElement("option");
      option.value = String(seconds); option.textContent = `${seconds} 秒`;
      option.selected = seconds === 10; rowSelect.append(option);
    }
    label.append(rowSelect);
    const settings = doc.createElement("details");
    settings.className = "multiline-waveform-settings";
    const gear = doc.createElement("summary");
    gear.setAttribute("aria-label", "波形设置");
    gear.title = "波形设置";
    gear.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 3-.6 2.2-2 .9-2-.9-2.5 4.3 1.8 1.5v2l-1.8 1.5 2.5 4.3 2-.9 2 .9.6 2.2h5l.6-2.2 2-.9 2 .9 2.5-4.3-1.8-1.5v-2l1.8-1.5-2.5-4.3-2 .9-2-.9L14.5 3Z"/><circle cx="12" cy="12" r="3"/></svg>';
    const settingsPanel = doc.createElement("div");
    settingsPanel.className = "multiline-waveform-settings-panel";
    const settingsTitle = doc.createElement("strong");
    settingsTitle.textContent = "波形设置";
    const gainLabel=doc.createElement("label"); gainLabel.textContent="振幅";
    const gainInput=doc.createElement("input"); gainInput.type="range"; gainInput.min="0.5"; gainInput.max="4"; gainInput.step="0.1"; gainInput.value="1.3";
    gainInput.setAttribute("aria-label","波形振幅"); gainLabel.append(gainInput);
    const displayLabel = doc.createElement("label"); displayLabel.textContent = "显示";
    const displaySelect = doc.createElement("select"); displaySelect.setAttribute("aria-label", "音频显示");
    for (const [value, text] of [["waveform", "波形"], ["spectrogram", "声谱图"]]) {
      const item = doc.createElement("option"); item.value = value; item.textContent = text; displaySelect.append(item);
    }
    displaySelect.value = "waveform";
    displayLabel.append(displaySelect);
    const gainValue = doc.createElement("output"); gainValue.textContent = "1.3×";
    const gainControls = doc.createElement("span"); gainControls.className = "waveform-gain-controls";
    gainControls.append(gainInput, gainValue); gainLabel.append(gainControls);
    settingsPanel.append(settingsTitle, displayLabel, label, gainLabel);
    settings.append(gear, settingsPanel);
    const modes = doc.createElement("div");
    modes.className = "waveform-mode-switch";
    modes.setAttribute("role", "group"); modes.setAttribute("aria-label", "波形模式");
    const basicButton = doc.createElement("button"), multiButton = doc.createElement("button");
    basicButton.type = multiButton.type = "button";
    basicButton.textContent = "基础"; multiButton.textContent = "多行";
    modes.append(basicButton, multiButton);
    toolbar.append(title, modes, settings);
    const viewport = doc.createElement("div");
    viewport.className = "multiline-waveform-viewport";
    viewport.setAttribute("aria-label", "多行波形与字幕时间轴");
    const body = doc.createElement("div");
    body.className = "multiline-waveform-body";
    const positionSlider = doc.createElement("input");
    positionSlider.type = "range";
    positionSlider.className = "waveform-position-slider";
    positionSlider.min = positionSlider.max = positionSlider.value = "0";
    positionSlider.step = "0.001";
    positionSlider.disabled = true;
    positionSlider.setAttribute("aria-label", "波形时间位置");
    body.append(viewport, positionSlider);
    panel.prepend(toolbar, body);
    let view = null, cues = [], duration = 0, seconds = 10, rows = [];
    let active = null, selected = null, preview = null, drag = null;
    const selection = options.selection || selectionApi.createSelection();
    let selectionHandled = false;
    let hover = null, boundaryOverride = null;
    let destroyed = false, following = true, frame = 0, playback = 0, generation = 0;
    let firstActive = true, suppressClick = false, mode = "multiline";
    displaySelect.onchange = () => {
      title.textContent = displaySelect.value === "spectrogram" ? "声谱图" : "波形";
      gainLabel.style.display = displaySelect.value === "spectrogram" ? "none" : "";
      clearSpectra(); redraw();
    };
    const spectra = new Map();
    function clearSpectra(keep = []) {
      for (const [row, image] of spectra) {
        if (keep.includes(row)) continue;
        image.onload = image.onerror = null; image.removeAttribute("src"); spectra.delete(row);
      }
    }
    function requestSpectrum(row) {
      if (spectra.has(row) || !options.spectrogramUrl) return;
      const image = doc.createElement("img"), token = generation;
      spectra.set(row, image);
      image.onload = () => { if (!destroyed && generation === token && spectra.get(row) === image) redraw(); };
      image.onerror = () => { image.failed = true; if (!destroyed && generation === token) redraw(); };
      image.src = options.spectrogramUrl({start:row.start, end:row.end});
    }
    function visibleStart() { return mode === "basic" ? viewport.scrollLeft / Math.max(1, viewport.clientWidth) * seconds : viewport.scrollTop / (ROW_HEIGHT + ROW_GAP) * seconds; }
    function visibleSpan() { return mode === "basic" ? seconds : viewport.clientHeight / (ROW_HEIGHT + ROW_GAP) * seconds; }
    function setMode(next) {
      const time = visibleStart(); mode = next;
      viewport.classList.toggle("basic-waveform-viewport", mode === "basic");
      panel.classList.toggle("basic-waveform-module", mode === "basic");
      viewport.setAttribute("aria-label", mode === "basic" ? "连续波形与字幕时间轴" : "多行波形与字幕时间轴");
      basicButton.setAttribute("aria-pressed", String(mode === "basic")); multiButton.setAttribute("aria-pressed", String(mode === "multiline"));
      label.firstChild && (label.firstChild.textContent = mode === "basic" ? "可视时长 " : "每行 ");
      rebuild(); viewport.scrollLeft = viewport.scrollTop = 0; revealTime(time, true);
    }
    function chooseMode(next) {
      setMode(next);
      if (panel.dispatchEvent) panel.dispatchEvent(new CustomEvent("workspace-waveform-mode-change", {detail:{mode:next}}));
    }
    function workspaceMode(event) {
      if (event.detail.mode !== mode) setMode(event.detail.mode);
    }
    panel.addEventListener("workspace-waveform-mode", workspaceMode);
    basicButton.onclick = () => chooseMode("basic"); multiButton.onclick = () => chooseMode("multiline");
    basicButton.setAttribute("aria-pressed", "false"); multiButton.setAttribute("aria-pressed", "true");
    function setFollow(enabled) {
      following = !!enabled;
    }
    function dismissSettings(event) { if (!settings.contains(event.target)) settings.open=false; }
    function settingsKey(event) { if(event.key==="Escape") { settings.open=false; gear.focus(); } }
    doc.addEventListener("pointerdown",dismissSettings);
    settings.addEventListener("keydown",settingsKey);
    function visibleRows() {
      const offset = mode === "basic" ? viewport.scrollLeft / Math.max(1, viewport.clientWidth) : viewport.scrollTop / (ROW_HEIGHT + ROW_GAP);
      const count = mode === "basic" ? 1 : viewport.clientHeight / (ROW_HEIGHT + ROW_GAP);
      const first = Math.max(0, Math.floor(offset) - 1);
      const last = Math.min(rows.length, Math.ceil(offset + count) + 1);
      return rows.slice(first, last);
    }
    function notifyViewport() {
      const start = visibleStart();
      const count = Math.max(1, visibleSpan() / seconds);
      const span = Math.min(duration, count * seconds);
      positionSlider.min = String(span / 2);
      positionSlider.max = String(Math.max(span / 2, duration - span / 2));
      positionSlider.value = String(start + span / 2);
      positionSlider.disabled = duration <= span;
      positionSlider.setAttribute("aria-valuetext", `${timeLabel(start)} 起`);
      options.onViewportChange?.({start, end:Math.min(duration, start + span), duration});
    }
    function creationIntent(start, end = Math.min(duration, start + 1.5)) {
      return timeline.manualCueIntent(view, start, end-start, duration);
    }
    function rebuild() {
      generation++;
      clearSpectra();
      viewport.replaceChildren();
      rows = rowRanges(duration, seconds).map(range => {
        const canvas = doc.createElement("canvas");
        canvas.className = "multiline-waveform-row";
        canvas.tabIndex = 0;
        canvas.setAttribute("aria-label", `${timeLabel(range.start)} 至 ${timeLabel(range.end)} 波形与字幕`);
        canvas.dataset.start = String(range.start); canvas.dataset.end = String(range.end);
        viewport.append(canvas);
        const row = {...range, canvas, peaks:null, loading:false};
        canvas.addEventListener("pointerdown", event => pointerDown(event, row));
        canvas.addEventListener("pointermove", event => pointerMove(event, row));
        canvas.addEventListener("pointerleave", () => { if (!drag) { hover = null; redraw(); } });
        canvas.addEventListener("pointerup", event => pointerUp(event, row));
        canvas.addEventListener("pointercancel", () => { drag = null; preview = null; redraw(); });
        canvas.addEventListener("click", event => click(event, row));
        canvas.addEventListener("contextmenu", event => {
          event.preventDefault();
          const intent = creationIntent(position(event, row).time);
          if (intent) options.onOperation?.("createManualCue", intent);
        });
        canvas.addEventListener("dblclick", () => {
          if (options.media?.paused) options.media.play(); else options.media?.pause();
        });
        canvas.addEventListener("keydown", event => {
          if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault(); const intent=creationIntent(row.pointerTime ?? row.start);
            if(intent) options.onOperation?.("createManualCue",intent); return;
          }
          const shortcut = typeof options.hideCueShortcut === "function" ? options.hideCueShortcut() : options.hideCueShortcut || "h";
          if (selected && event.key.toLowerCase() === shortcut.toLowerCase() && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault(); options.onOperation?.("hideCue", {type:"delete_cue", cue_id:selected});
          }
        });
        return row;
      });
      redraw();
    }
    function requestPeaks(row, width) {
      if (row.loading || row.peaks || !options.onWaveformWindow) return;
      row.loading = true;
      const token = generation;
      Promise.resolve(options.onWaveformWindow({start:row.start, end:row.end, points:4096}))
        .then(payload => {
          if (destroyed || generation !== token || !payload) return;
          row.peaks = payload.peaks || [];
          // The server supplies normalized peaks. A common scale keeps quiet rows quiet.
          row.peakScale = 1;
          row.noiseFloor = 0;
          row.waveStart = payload.window_start ?? row.start;
          row.waveEnd = payload.window_end ?? row.end;
          redraw();
        }).catch(error => { if (!destroyed && generation === token) options.onWaveformError?.(error); });
    }
    function drawRow(row) {
      const canvas = row.canvas, width = canvas.clientWidth, height = mode === "basic" ? canvas.clientHeight : ROW_HEIGHT;
      if (!width) return;
      const ratio = Math.min(2, doc.defaultView.devicePixelRatio || 1);
      if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
      if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#0e1419"; ctx.fillRect(0, 0, width, height);
      const xAt = time => (time - row.start) / seconds * width;
      const hasOverlay = cues.some(cue=>cue.subtitle_track === "overlay" && cue.end>row.start && cue.start<row.end);
      const center = mode === "basic" ? Math.max(hasOverlay ? 58 : 83, (hasOverlay ? 58 : 83) + (height - ROW_HEIGHT) / 2) : hasOverlay ? 58 : 83;
      const amplitude = mode === "basic" ? Math.max(8, center - 6) : hasOverlay ? 52 : 77;
      ctx.strokeStyle = "#334149"; ctx.lineWidth = .7;
      ctx.beginPath(); ctx.moveTo(0, center); ctx.lineTo(width, center); ctx.stroke();
      if (displaySelect.value !== "spectrogram") requestPeaks(row, width);
      const peaks = row.peaks || [];
      if (displaySelect.value !== "spectrogram" && peaks.length) {
        const columns = canvas.width;
        const envelopeKey = `${columns}:${row.waveStart}:${row.waveEnd}`;
        if (row.envelopeKey !== envelopeKey) {
          row.envelopeKey = envelopeKey;
          row.envelope = waveformEnvelope(peaks,columns,row.start,row.start+seconds,row.waveStart,row.waveEnd,row.peakScale||1,row.noiseFloor||0);
        }
        const values = row.envelope;
        ctx.fillStyle = "#69b49c";
        // Adjacent physical-pixel columns retain abrupt peaks without diagonal interpolation.
        // Integer vertical bounds also avoid antialiased softness along the envelope.
        const columnWidth = width / columns;
        const gain = Number(gainInput.value);
        for (let x = 0; x < values.length; x++) {
          if (!values[x]) continue;
          const top = Math.max(2, Math.floor((center-values[x]*amplitude*gain)*ratio)/ratio);
          const bottom = Math.min(height-2, Math.ceil((center+values[x]*amplitude*gain)*ratio)/ratio);
          ctx.fillRect(x*columnWidth, top, columnWidth, bottom-top);
        }
      }
      const ranges = timeline.applyPreview(cues, preview);
      const translations = options.getShowTranslations?.() !== false && ranges.some(cue => cue.target_text);
      const overlays = ranges.filter(cue=>cue.subtitle_track === "overlay" && cue.end>row.start && cue.start<row.end);
      const overlayEnds=[], overlayLanes=new Map();
      for(const cue of overlays) {
        let lane=overlayEnds.findIndex(end=>end<=cue.start);
        if(lane<0) lane=overlayEnds.length;
        overlayEnds[lane]=cue.end; overlayLanes.set(cue.cue_id,lane);
      }
      const mainLanes=translations?2:1;
      const laneHeight=hasOverlay?Math.min(36,106/(mainLanes+overlayEnds.length)-3):(translations?40:48);
      const top=height-6-(mainLanes+overlayEnds.length)*(laneHeight+3);
      if (displaySelect.value === "spectrogram") {
        requestSpectrum(row);
        const image = spectra.get(row), plotTop = 30, plotHeight = Math.max(16, top - plotTop - 4);
        if (image?.complete && image.naturalWidth) {
          ctx.drawImage(image, 0, plotTop, width * (row.end-row.start)/seconds, plotHeight);
          ctx.font = "10px sans-serif";
          for (const [fraction, text] of [[0,"8 kHz"],[.5,"4 kHz"],[1,"0 Hz"]]) {
            const y = plotTop + fraction * (plotHeight - 10);
            ctx.fillStyle = "rgba(6,12,17,.8)"; ctx.fillRect(2, y, 39, 12);
            ctx.fillStyle = "#e6eaf0"; ctx.fillText(text, 4, y+10);
          }
        } else {
          ctx.fillStyle = "#9ba9b8"; ctx.font = "12px sans-serif";
          ctx.fillText(image?.failed ? "声谱图加载失败，切换显示可重试" : "声谱图加载中…", 12, 48);
        }
      }
      row.lanes=new Map();
      for (const cue of ranges) {
        if (cue.end <= row.start || cue.start >= row.end) continue;
        // In the continuous strip, draw the complete cue through canvas seams.
        // Canvas clipping preserves one label and only the real end handles.
        const handles = mode === "basic"
          ? {left:xAt(cue.start), right:xAt(cue.end), cap:Math.min(10, (cue.end-cue.start)/seconds*width/4), start:true, end:true}
          : cueHandles(cue,row,seconds,width);
        const {left,right,cap} = handles;
        const isSelected = selection.ids.has(cue.cue_id);
        const overlay=cue.subtitle_track === "overlay";
        const firstLane=overlay?mainLanes+overlayLanes.get(cue.cue_id):0;
        const count=overlay?1:mainLanes;
        row.lanes.set(cue.cue_id,{top:top+firstLane*(laneHeight+3),bottom:top+(firstLane+count)*(laneHeight+3)});
        for (let lane = 0; lane < count; lane++) {
          const y = top + (firstLane+lane) * (laneHeight + 3);
          ctx.fillStyle = overlay ? "rgba(74,67,94,.94)" : "rgba(66,78,89,.86)";
          ctx.beginPath(); ctx.roundRect(left + .5, y, Math.max(1, right - left - 1), laneHeight, 4); ctx.fill();
          ctx.save(); ctx.clip();
          ctx.fillStyle = "rgba(184,195,204,.27)";
          if(handles.start) ctx.fillRect(left,y,cap,laneHeight);
          if(handles.end) ctx.fillRect(right-cap,y,cap,laneHeight);
          ctx.restore();
          ctx.strokeStyle = isSelected ? "#ffe16b" : "#76838e";
          ctx.lineWidth = isSelected ? 2 : 1; ctx.stroke();
          const textLeft=left+(handles.start?cap:0)+5;
          const available = Math.max(0,right-(handles.end?cap:0)-textLeft-5);
          ctx.save(); ctx.beginPath(); ctx.rect(textLeft, y, available, laneHeight); ctx.clip();
          ctx.font = "14px 'Segoe UI', 'Microsoft YaHei', sans-serif"; ctx.fillStyle = "#edf1f4"; ctx.textBaseline = "middle";
          let text = mode === "multiline" ? rowCueText(cue, row, !!lane) : (lane ? cue.target_text : cue.text);
          if (ctx.measureText(text).width > available) {
            while (text.length && ctx.measureText(text + "…").width > available) text = text.slice(0,-1);
            text += "…";
          }
          ctx.fillText(text, textLeft, y + laneHeight / 2); ctx.restore();
        }
      }
      drawBoundaryMode(ctx, row, width, height, xAt);
      if(drag?.kind==="create") {
        const end=row.pointerTime ?? drag.time, left=xAt(Math.min(drag.time,end)),right=xAt(Math.max(drag.time,end));
        ctx.fillStyle="rgba(174,153,215,.18)"; ctx.fillRect(left,0,right-left,height);
        ctx.strokeStyle="#b9a0e2"; ctx.strokeRect(left+.5,.5,right-left,height-1);
      }
      const time = Number(options.media?.currentTime || 0);
      if (time >= row.start && time < row.end) {
        ctx.strokeStyle = "#ff6464"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(xAt(time), 0); ctx.lineTo(xAt(time), height); ctx.stroke();
      }
      ctx.font = "12px Consolas, monospace"; ctx.textBaseline = "middle";
      if (mode === "basic") {
        const step = seconds <= 10 ? 1 : seconds <= 30 ? 5 : 10;
        ctx.strokeStyle = "#27323b"; ctx.lineWidth = .5;
        for (let t = Math.ceil(row.start / step) * step; t < row.end; t += step) {
          const x = xAt(t); ctx.beginPath(); ctx.moveTo(x, 26); ctx.lineTo(x, height); ctx.stroke();
          ctx.fillStyle = "#8dc9f5"; ctx.fillText(timeLabel(t), x + 5, 14);
        }
        return;
      }
      const rangeText = `${timeLabel(row.start)} → ${timeLabel(row.end)}`;
      ctx.fillStyle = "rgba(6,12,17,.9)"; ctx.fillRect(6, 5, ctx.measureText(rangeText).width + 12, 22);
      ctx.fillStyle = "#8dc9f5"; ctx.fillText(rangeText, 12, 16);
    }
    function redraw() {
      if (destroyed || frame) return;
      frame = requestAnimationFrame(() => { frame = 0; const visible = visibleRows(); clearSpectra(visible); visible.forEach(drawRow); notifyViewport(); });
    }
    function revealTime(time, center = false) {
      if (mode === "basic") {
        const x = time / seconds * viewport.clientWidth;
        if (center || x < viewport.scrollLeft || x > viewport.scrollLeft + viewport.clientWidth)
          viewport.scrollLeft = Math.max(0, x - (center ? viewport.clientWidth / 2 : 0));
        redraw(); return;
      }
      const index = clamp(Math.floor(time / seconds), 0, Math.max(0, rows.length - 1));
      const top = index * (ROW_HEIGHT + ROW_GAP);
      if (center || top < viewport.scrollTop || top + ROW_HEIGHT > viewport.scrollTop + viewport.clientHeight)
        viewport.scrollTop = Math.max(0, top - (center ? (viewport.clientHeight - ROW_HEIGHT) / 2 : 0));
      redraw();
    }
    function scrollToTime(time) {
      setFollow(false);
      const span = Math.min(duration, visibleSpan());
      const start = clamp(Number(time) - span / 2, 0, Math.max(0, duration - span));
      if (mode === "basic") viewport.scrollLeft = start / seconds * viewport.clientWidth;
      else viewport.scrollTop = start / seconds * (ROW_HEIGHT + ROW_GAP);
      redraw();
    }
    positionSlider.oninput = () => scrollToTime(positionSlider.value);
    function seek(time) {
      const value = clamp(time, 0, duration);
      if (options.media) options.media.currentTime = value;
      options.onSeek?.(value); redraw();
    }
    function position(event, row) {
      const rect = row.canvas.getBoundingClientRect();
      return {x:event.clientX - rect.left, y:event.clientY - rect.top,
        time:clamp(row.start + (event.clientX - rect.left) / rect.width * seconds, 0, duration), width:rect.width};
    }
    function boundaryTrack(target) {
      const track = target.cue.subtitle_track === "overlay" ? [target.cue] : cues.filter(c => c.subtitle_track !== "overlay");
      return {track, index:track.findIndex(c => c.cue_id === target.cue.cue_id)};
    }
    function boundaryModeFor(target) {
      const {track,index} = boundaryTrack(target);
      const override = boundaryOverride?.cueId === target.cue.cue_id && boundaryOverride.edge === target.edge
        ? boundaryOverride.mode : null;
      return timeline.boundaryMode(track,index,target.edge,selected,override);
    }
    function boundaryBadge(target, row, width) {
      if (!target || !["boundary", "mode-toggle"].includes(target.kind)) return null;
      const cue = timeline.applyPreview(cues,preview).find(c => c.cue_id === target.cue.cue_id);
      if (!cue || cue[target.edge] < row.start || cue[target.edge] > row.end) return null;
      const x = (cue[target.edge]-row.start)/seconds*width;
      return {x,left:clamp(x+8,4,Math.max(4,width-54)),top:32,width:50,height:24};
    }
    function drawBoundaryMode(ctx,row,width,height,xAt) {
      const target = drag?.kind === "boundary" ? drag : hover?.row === row ? hover : null;
      const badge = boundaryBadge(target,row,width);
      if (!badge) return;
      const adjustment = drag?.kind === "boundary" ? drag.mode : boundaryModeFor(target);
      const color = adjustment === "shared" ? "#70bfff" : "#ffe16b";
      ctx.save(); ctx.strokeStyle=color; ctx.fillStyle=color; ctx.lineWidth=drag?4:3;
      ctx.beginPath(); ctx.moveTo(badge.x,28); ctx.lineTo(badge.x,height); ctx.stroke();
      ctx.fillRect(badge.x-4,height-6,8,5);
      ctx.fillStyle="#111c26"; ctx.fillRect(badge.left,badge.top,badge.width,badge.height);
      ctx.lineWidth=1; ctx.strokeRect(badge.left+.5,badge.top+.5,badge.width-1,badge.height-1);
      ctx.fillStyle=color; ctx.font="bold 12px 'Segoe UI', 'Microsoft YaHei', sans-serif"; ctx.textBaseline="middle";
      ctx.fillText(adjustment === "shared" ? "联动" : "单条",badge.left+12,badge.top+12);
      ctx.restore();
    }
    function toggleBoundaryMode(target) {
      const {track,index}=boundaryTrack(target);
      if (!timeline.sharedBoundary(track,index,target.edge)) return false;
      boundaryOverride={cueId:target.cue.cue_id,edge:target.edge,mode:boundaryModeFor(target)==="shared"?"single":"shared"};
      hover=target; redraw(); return true;
    }
    function hit(event, row) {
      const p = position(event, row);
      const badge = hover?.row === row ? boundaryBadge(hover,row,p.width) : null;
      if (badge && p.x >= badge.left && p.x <= badge.left+badge.width && p.y >= badge.top && p.y <= badge.top+badge.height)
        return {...hover,kind:"mode-toggle",...p};
      const laneCues=timeline.applyPreview(cues,preview).filter(cue=>{
        const lane=row.lanes?.get(cue.cue_id);
        return lane ? p.y>=lane.top && p.y<=lane.bottom : p.y>=99;
      });
      const cue = timeline.cueAtTime(laneCues, p.time);
      if (p.y >= 70) {
        for (let index = 0; index < cues.length; index++) {
          const lane=row.lanes?.get(cues[index].cue_id);
          if(lane && (p.y<lane.top || p.y>lane.bottom)) continue;
          for (const edge of ["start", "end"]) {
            const time = cues[index][edge];
            if (time < row.start || time > row.end) continue;
            const boundaryX=(time-row.start)/seconds*p.width;
            const insideHandle=edge==="start" ? p.x>=boundaryX&&p.x<=boundaryX+10 : p.x>=boundaryX-10&&p.x<=boundaryX;
            if (insideHandle || Math.abs(boundaryX-p.x)<4)
              return {kind:"boundary", cueIndex:index, edge, cue:cues[index], row, ...p};
          }
        }
      }
      return {kind:cue ? "cue" : "blank", cue, ...p};
    }
    function selectTarget(target,event) {
      if (selected !== (target.cue?.cue_id || null)) boundaryOverride=null;
      selected=target.cue?.cue_id || null;
      if (selected) selection.select(selected,cues.map(c=>c.cue_id),{range:event.shiftKey,toggle:event.ctrlKey || event.metaKey});
      else selection.clear();
      options.onSelectCue?.(selected,target.time,{range:!!event.shiftKey,toggle:!!(event.ctrlKey || event.metaKey)});
      redraw();
    }
    function pointerDown(event, row) {
      if (event.button !== 0) return;
      selectionHandled=false;
      row.canvas.focus({preventScroll:true});
      const target = hit(event, row);
      row.pointerTime=target.time;
      if (target.kind === "mode-toggle") { event.preventDefault(); return; }
      if((event.ctrlKey || event.metaKey) && target.kind === "blank") {
        event.preventDefault(); drag={kind:"create",time:target.time,startX:event.clientX,moved:false};
        row.canvas.setPointerCapture(event.pointerId); setFollow(false); return;
      }
      if(target.kind==="cue") {
        event.preventDefault(); setFollow(false);
        selectTarget(target,event); selectionHandled=true;
        if (event.shiftKey || event.ctrlKey || event.metaKey) return;
        drag={...target,kind:"move",startX:event.clientX,moved:false};
        row.canvas.setPointerCapture(event.pointerId); return;
      }
      if (target.kind !== "boundary") return;
      event.preventDefault();
      setFollow(false);
      drag = {...target, mode:event.shiftKey?"single":boundaryModeFor(target), startX:event.clientX, moved:false, offset:target.time-target.cue[target.edge]};
      row.canvas.setPointerCapture(event.pointerId);
      redraw();
    }
    function pointerMove(event, row) {
      row.pointerTime=position(event,row).time;
      if (!drag) {
        const target=hit(event,row);
        row.canvas.title = target.cue ? [target.cue.text, target.cue.target_text].filter(Boolean).join("\n") : "";
        const badge=hover?.row===row?boundaryBadge(hover,row,target.width):null;
        const inBadgePath=badge && target.x>=Math.min(badge.x-12,badge.left-8)
          && target.x<=Math.max(badge.x+12,badge.left+badge.width+8)
          && target.y>=badge.top-8 && target.y<=row.canvas.getBoundingClientRect().height;
        hover=["boundary","mode-toggle"].includes(target.kind)?target:inBadgePath?hover:null;
        row.canvas.style.cursor=target.kind==="mode-toggle"?"pointer":target.kind==="boundary"?(boundaryModeFor(target)==="shared"?"col-resize":"ew-resize"):"crosshair";
        redraw(); return;
      }
      if (Math.abs(event.clientX - drag.startX) > 2) drag.moved = true;
      if(drag.kind==="create") { row.canvas.style.cursor="crosshair"; redraw(); return; }
      if(drag.kind==="move") {
        preview=movePreview(cues,drag.cue,position(event,row).time-drag.time,duration); redraw(); return;
      }
      const track=drag.cue.subtitle_track === "overlay" ? [drag.cue] : cues.filter(c=>c.subtitle_track!=="overlay");
      const index=track.findIndex(c=>c.cue_id===drag.cue.cue_id);
      const adjustment = event.shiftKey ? "single" : boundaryModeFor(drag);
      drag.mode=adjustment;
      preview = timeline.previewBoundaryChange(track,index,drag.edge,position(event,row).time-drag.offset,selected,adjustment);
      redraw();
    }
    function pointerUp(event, row) {
      if (!drag) return;
      row.canvas.releasePointerCapture(event.pointerId);
      suppressClick = drag.moved;
      if(drag.kind==="create" && drag.moved) {
        const time=position(event,row).time, start=Math.min(drag.time,time),end=Math.max(drag.time,time);
        const intent=creationIntent(start,end);
        if(intent) options.onOperation?.("createManualCue",intent);
      }
      if (drag.moved && preview && timeline.previewHasTimeChange(cues, preview)) options.onOperation?.("adjustBoundary", preview);
      drag = null; preview = null; redraw();
    }
    function click(event, row) {
      if (suppressClick) { suppressClick = false; return; }
      const target = hit(event,row);
      if (["boundary","mode-toggle"].includes(target.kind) && toggleBoundaryMode(target)) return;
      hover=null;
      if (!selectionHandled) selectTarget(target,event);
      selectionHandled=false;
      if (!event.shiftKey && !event.ctrlKey && !event.metaKey) seek(target.time);
      redraw();
    }
    function tick() {
      playback = 0;
      if (destroyed) return;
      if (following) revealTime(options.media?.currentTime || 0);
      redraw();
      if (!options.media?.paused) playback = requestAnimationFrame(tick);
    }
    function mediaUpdate() { if (following) revealTime(options.media?.currentTime || 0); redraw(); }
    function mediaPlay() { if (!playback) playback = requestAnimationFrame(tick); }
    function onScroll() { redraw(); }
    function onWheel(event) {
      const modifier = String(typeof options.zoomModifier === "function" ? options.zoomModifier() : options.zoomModifier || "Alt").toLowerCase();
      if (!({alt:event.altKey,ctrl:event.ctrlKey,shift:event.shiftKey,meta:event.metaKey})[modifier]) { setFollow(false);
        if (mode === "basic" && Math.abs(event.deltaY) > Math.abs(event.deltaX)) { event.preventDefault(); viewport.scrollLeft += event.deltaY; }
        return;
      }
      event.preventDefault();
      const old = seconds, time = visibleStart();
      seconds = clamp(Math.round(seconds * (event.deltaY > 0 ? 1.25 : .8)), 2, 60);
      rebuild(); revealTime(time);
      if (!Array.from(rowSelect.options).some(option => Number(option.value) === seconds)) {
        rowSelect.querySelector('[data-custom-span]')?.remove();
        const option = doc.createElement("option"); option.dataset.customSpan="true"; option.value = String(seconds); option.textContent = `${seconds} 秒`; rowSelect.append(option);
      }
      rowSelect.value = String(seconds);
      options.onZoom?.({old_span:old,new_span:seconds,direction:seconds<old?"in":"out",anchor_time:time});
    }
    gainInput.oninput=() => { gainValue.textContent = `${Number(gainInput.value).toFixed(1)}×`; redraw(); };
    rowSelect.onchange = () => { const time = visibleStart(); seconds = Number(rowSelect.value); rebuild(); revealTime(time); };
    viewport.addEventListener("scroll", onScroll);
    viewport.addEventListener("wheel", onWheel, {passive:false});
    const observer = new ResizeObserver(redraw); observer.observe(viewport);
    options.media?.addEventListener("timeupdate", mediaUpdate);
    options.media?.addEventListener("seeked", mediaUpdate);
    options.media?.addEventListener("play", mediaPlay);
    if (panel.dataset.workspaceWaveformMode === "basic") setMode("basic");
    return Object.freeze({
      scrollToTime,
      setView(next) {
        view = next; cues = timeline.activeCues(view);
        selection.reconcile(cues.map(c=>c.cue_id));
        const nextDuration = Math.max(Number(options.media?.duration) || 0, ...cues.map(cue=>cue.end), .001);
        if (nextDuration !== duration || !rows.length) { duration = nextDuration; rebuild(); }
        else redraw();
      },
      setActiveCue(id) { active = id; if (firstActive && id) { firstActive=false; const cue=cues.find(c=>c.cue_id===id); if(cue) revealTime(cue.start); } redraw(); },
      setSelectedCue(id) { if (selected !== id) boundaryOverride=null; selected = id; redraw(); },
      setFollowPlayback:setFollow,
      setWaveform(samples, total, start=0, end=total) {
        for (const row of rows) { row.peaks=samples; row.peakScale=1; row.waveStart=start; row.waveEnd=end; row.envelopeKey=null; } redraw();
      },
      revealTime, revealRange:(start,end)=>revealTime((start+end)/2,true), seek, redraw,
      autoSnapOnce(config) { const intent=timeline.autoSnapPlan({...view,cue_views:view.cue_views.filter(c=>c.subtitle_track!=="overlay")},config); if(intent.count) options.onOperation?.("autoSnap",intent); return intent; },
      getState() { const start=visibleStart(); return {mode,active_cue_id:active,selected_cue_id:selected,selected_cue_ids:[...selection.ids],view_start:start,view_end:Math.min(duration,start+visibleSpan()),duration,playhead_time:options.media?.currentTime||0,preview}; },
      getTimeRect(start,end=start) {
        const row=rows[clamp(Math.floor(start/seconds),0,rows.length-1)]; if(!row) return null;
        const rect=row.canvas.getBoundingClientRect(); const left=rect.left+clamp((start-row.start)/seconds,0,1)*rect.width;
        const right=rect.left+clamp((end-row.start)/seconds,0,1)*rect.width;
        return {left,right,top:rect.top,bottom:rect.bottom,width:Math.max(0,right-left),height:rect.height};
      },
      destroy() {
        destroyed=true; generation++; clearSpectra(); cancelAnimationFrame(frame); cancelAnimationFrame(playback); observer.disconnect();
        panel.removeEventListener?.("workspace-waveform-mode", workspaceMode);
        doc.removeEventListener("pointerdown",dismissSettings);
        options.media?.removeEventListener("timeupdate",mediaUpdate); options.media?.removeEventListener("seeked",mediaUpdate); options.media?.removeEventListener("play",mediaPlay);
        toolbar.remove(); body.remove(); originalTrack.hidden=false; panel.classList.remove("multiline-waveform-module");
      }
    });
  }
  return Object.freeze({rowRanges,timeLabel,waveformEnvelope,cueHandles,movePreview,rowCueText,createTimelineController});
});
