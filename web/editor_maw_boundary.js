/* Adapt the original Substar boundary rules to the MAW millisecond renderer. */
export function createBoundarySession({engine, segments, index, edge, selected, mode, durationMs}) {
  const segment = segments[index];
  if (!segment) return null;
  // Different speakers may overlap. Overlay captions have no shared neighbour.
  const track = segment.subtitle_track === 'overlay' ? [segment]
    : segments.filter(item => item.speaker === segment.speaker);
  const cues = track.map(item => ({cue_id:item.id, start:item.start / 1000, end:item.end / 1000}));
  const cueIndex = track.indexOf(segment);
  const adjustment = engine.boundaryMode(cues, cueIndex, edge, selected, mode);
  // Only the one/two affected cues need word snapshots, not the whole track.
  const byId = new Map(track.map((item, i) => [item.id, i]));
  const originals = new Map();
  let intent = null, range = {start:segment.start, end:segment.end};
  function restore() {
    for (const [i, original] of originals) {
      track[i].start = original.start; track[i].end = original.end;
      track[i].items = original.items;
    }
  }
  return {
    mode:adjustment,
    edgeTime:segment[edge],
    canToggle:!!engine.sharedBoundary(cues, cueIndex, edge),
    restore,
    affectedSegments:() => (engine.previewBoundaryChange(cues,cueIndex,edge,segment[edge]/1000,selected,adjustment)?.changes || []).map(change=>track[byId.get(change.cue_id)]),
    get previewRange() { return range; },
    changed:() => engine.previewHasTimeChange(cues, intent),
    update(timeMs) {
      // Include the preceding preview so crossing/backtracking across rows clears it.
      const previous = [...originals.keys()].map(i => ({start:track[i].start,end:track[i].end}));
      restore();
      intent = engine.previewBoundaryChange(cues, cueIndex, edge,
        Math.max(0, Math.min(durationMs, timeMs)) / 1000, selected, adjustment);
      for (const change of intent?.changes || []) {
        const i = byId.get(change.cue_id), item = track[i];
        if (!originals.has(i)) originals.set(i, {start:item.start,end:item.end,items:item.items || []});
        item[change.edge] = Math.round(change.time * 1000);
      }
      range = {start:segment.start,end:segment.end};
      for (const [i, original] of originals) {
        const item = track[i];
        range.start = Math.min(range.start, original.start, item.start);
        range.end = Math.max(range.end, original.end, item.end);
        if (item.start === original.start && item.end === original.end) continue;
        const scale = (item.end - item.start) / (original.end - original.start);
        item.items = original.items.map(token => ({...token,
          start:Math.round(item.start + (token.start - original.start) * scale),
          end:Math.round(item.start + (token.end - original.start) * scale)}));
      }
      for (const prior of previous) {
        range.start = Math.min(range.start, prior.start); range.end = Math.max(range.end, prior.end);
      }
      return intent;
    }
  };
}

export function createWordBoundarySession({engine,segment,itemIndex,edge,selected,mode,durationMs}){
  const originals=segment.items.map(item=>({...item}));
  const words=originals.map(item=>({...item,speaker:''}));
  const selectedId=selected.has(itemIndex)?words[itemIndex]?.id:null;
  const base=createBoundarySession({engine,segments:words,index:itemIndex,edge,selected:selectedId,mode,durationMs});
  if(!base)return null;
  return {...base,
    get previewRange(){return base.previewRange;},
    restore(){base.restore();segment.items=originals.map(item=>({...item}));},
    update(time){
      const intent=base.update(Math.max(segment.start,Math.min(segment.end,time)));
      segment.items=words.map((word,i)=>({...originals[i],start:word.start,end:word.end}));
      return intent;
    }
  };
}

export function installSubstarBoundaries({wave, host, signal, getSelected, refresh, previewRefresh = refresh,
  engine = globalThis.EditorTimeline}) {
  const doc = host.ownerDocument, win = doc.defaultView;
  let override = null, hover = null, drag = null, suppressClick = false;
  // The original seam is only a hit area now; Substar owns its visual feedback.
  const legacyStyle = doc.createElement('style');
  legacyStyle.textContent = `
    .maw-waveform-host .waveform-cue-boundary { cursor: default; }
    .maw-waveform-host .waveform-cue-boundary::before { display: none; }
    .maw-waveform-host .waveform-pointer-line { display: none !important; }
    .maw-waveform-host .waveform-pane { --wave-shared-boundary-cursor: default; }
  `;
  host.append(legacyStyle);
  // Word edges now use the same Substar boundary hit test and feedback as cues.
  // Remove MAW's separate resize handles and seam widgets; retain body move/select.
  const appendWords=wave.appendWordBlocks;
  wave.appendWordBlocks=function(row,...args){
    const result=appendWords.call(this,row,...args);
    row.querySelectorAll('.waveform-word-boundary,.waveform-word-block .waveform-cue-handle').forEach(el=>el.remove());
    return result;
  };

  const appendZones = wave.appendSharedBoundaryZones;
  if (appendZones) {
    wave.appendSharedBoundaryZones = function(row, ...args) {
      const result = appendZones.call(this, row, ...args);
      for (const zone of row.querySelectorAll('.waveform-cue-boundary')) zone.removeAttribute('title');
      return result;
    };
  }
  const badge = doc.createElement('button');
  badge.type = 'button'; badge.className = 'substar-boundary-mode'; badge.hidden = true;
  badge.style.cssText = 'position:fixed;z-index:1400;padding:3px 10px;min-width:50px;font-size:12px;background:#111c26;border:1px solid currentColor;pointer-events:auto';
  host.append(badge);
  const line = doc.createElement('div');
  line.className = 'substar-boundary-line'; line.hidden = true;
  line.setAttribute('aria-hidden', 'true');
  line.style.cssText = 'position:absolute;top:28px;bottom:0;width:3px;transform:translateX(-50%);pointer-events:auto;z-index:14;background:currentColor;box-shadow:0 0 5px currentColor';
  for (const edge of ['top', 'bottom']) {
    const cap = doc.createElement('span');
    cap.style.cssText = `position:absolute;left:50%;transform:translateX(-50%);${edge}:0;width:10px;height:4px;background:currentColor;pointer-events:auto`;
    line.append(cap);
  }

  function target(event) {
    if (badge.contains(event.target)) return hover;
    if (line.contains(event.target)) return hover;
    const word=event.target.closest?.('.waveform-word-block');
    if(word){
      const rect=word.getBoundingClientRect(),left=event.clientX-rect.left,right=rect.right-event.clientX;
      const edge=left<=right?'start':'end';if(Math.min(left,right)>8)return null;
      if(edge==='start'&&word.classList.contains('continues-from-previous-row')||edge==='end'&&word.classList.contains('continues-to-next-row'))return null;
      const index=Number(word.dataset.segmentIdx),itemIndex=Number(word.dataset.itemIdx);
      const parent=wave.options.getSegments('main')[index],item=parent?.items[itemIndex];
      return item?{kind:'word',track:'main',index,itemIndex,edge,row:word.closest('.waveform-row'),parent,segment:item}:null;
    }
    const handle = event.target.closest?.('.waveform-cue-handle');
    const zone = event.target.closest?.('.waveform-cue-boundary');
    const block = handle?.closest('.waveform-cue-block');
    const row = (block || zone)?.closest('.waveform-row');
    if (!row) return null;
    const track = (block || zone).dataset.track || 'main';
    let index = zone ? Number(zone.dataset.leftIdx)
      : Number(block.dataset.idx ?? block.dataset.extIdx ?? block.dataset.overlayIdx);
    let edge = zone || handle.classList.contains('right') ? 'end' : 'start';
    // At a shared seam the selected side wins, matching the old hit test.
    if (zone && wave.options.getSegments(track)[index + 1]?.id === getSelected()) {
      index++; edge = 'start';
    }
    const segment = wave.options.getSegments(track)[index];
    return segment ? {track, index, edge, row, segment} : null;
  }
  function session(t, forceSingle = false) {
    const selected = getSelected();
    if (override?.selected !== selected) override = null;
    if(t.kind==='word')return createWordBoundarySession({engine,segment:t.parent,itemIndex:t.itemIndex,edge:t.edge,
      selected:wave.options.wordTiming.getSelection(t.parent),
      mode:forceSingle?'single':override?.id===t.segment.id&&override.edge===t.edge?override.mode:null,durationMs:wave.durationMs});
    // Translation captions share source timing; resize source word timings too.
    const track = t.track === 'extension' ? 'main' : t.track;
    return createBoundarySession({engine, segments:track === 'overlay' ? [t.segment] : wave.options.getSegments(track),
      index:t.track === 'overlay' ? 0 : t.index, edge:t.edge, selected,
      mode:forceSingle ? 'single' : override?.id === t.segment.id && override.edge === t.edge ? override.mode : null,
      durationMs:wave.durationMs});
  }
  function show(t, current = null) {
    hover = t;
    if (!t) { badge.hidden = true; line.hidden = true; return; }
    const time = t.kind==='word'?t.parent.items[t.itemIndex][t.edge]:t.segment[t.edge];
    const row = wave.findVisibleWaveformRowForTime?.(time) || t.row;
    const s = current || session(t), rect = row.getBoundingClientRect();
    const start = Number(row.dataset.startMs), end = Number(row.dataset.endMs);
    if (time < start || time > end) { badge.hidden = true; line.hidden = true; return; }
    const ratio = (time - start) / (end - start);
    const x = rect.left + ratio * rect.width;
    badge.hidden = false; badge.textContent = s.mode === 'shared' ? '联动' : '单条';
    badge.style.color = s.mode === 'shared' ? '#70bfff' : '#ffe16b';
    row.append(line); line.hidden = false;
    line.style.left = `${ratio * 100}%`; line.style.color = badge.style.color;
    line.style.cursor = s.mode === 'shared' ? 'col-resize' : 'ew-resize';
    line.style.width = drag?.started ? '4px' : '3px';
    line.style.boxShadow = `0 0 ${drag?.started ? 10 : 5}px currentColor`;
    badge.style.left = Math.max(4, Math.min(win.innerWidth - 62, x + 8)) + 'px';
    badge.style.top = Math.max(4, rect.top + 30) + 'px';
    badge.title = s.canToggle ? '点击切换联动／单条；Shift 拖动临时单条' : '单条调整边界';
    badge.setAttribute('aria-label', badge.textContent + (t.kind==='word'?'调整字词边界':'调整字幕边界'));
  }
  function toggle(t) {
    const s = session(t);
    if (s.canToggle) override = {id:t.segment.id, edge:t.edge,
      mode:s.mode === 'shared' ? 'single' : 'shared', selected:getSelected()};
    show(t);
  }
  function stop(event) { event.preventDefault(); event.stopImmediatePropagation(); }
  function down(event) {
    if (event.button !== 0 || wave.drag || wave.wordDrag) return;
    suppressClick = false;
    const t = target(event);
    if (!t || wave.tool === 'razor') return;
    if(t.kind==='word' && (event.ctrlKey||event.metaKey||event.shiftKey))return;
    stop(event);
    if (badge.contains(event.target)) { toggle(t); return; }
    wave.focusWaveform(); wave.cancelHoverSeekPreview();
    const geometry = wave.captureRowGeometry(t.row), s = session(t, event.shiftKey);
    drag = {target:t, session:s, geometry, pointerId:event.pointerId,
      x:event.clientX, time:wave.timeFromPointerUnbounded(event, t.row, geometry), started:false};
    wave.substarBoundaryDrag = true;
    show(t, s);
  }
  let previewFrame = 0, pendingTime = null;
  function flushPreview() {
    if (previewFrame) win.cancelAnimationFrame(previewFrame);
    previewFrame = 0;
    if (!drag || pendingTime === null) return;
    const time = pendingTime; pendingTime = null;
    drag.session.update(time);
    previewRefresh(drag.session.previewRange); show(drag.target, drag.session);
  }
  function move(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    stop(event);
    if (!drag.started && Math.abs(event.clientX - drag.x) <= 2) return;
    if (!drag.started) {
      drag.started = true;
      if(drag.target.kind==='word')drag.command=wave.options.beginWordEdit(drag.target.parent);
      else wave.options.onBeginEdit?.('调整字幕边界',drag.session.affectedSegments());
      try { wave.pane.setPointerCapture(event.pointerId); } catch (_) {}
    }
    const time = wave.timeFromPointerUnbounded(event, drag.target.row, drag.geometry);
    pendingTime = drag.session.edgeTime + time - drag.time;
    if (!previewFrame) previewFrame = win.requestAnimationFrame(flushPreview);
  }
  function finish(event, cancel = false) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    stop(event);
    if (!cancel) { move(event); flushPreview(); }
    if (previewFrame) win.cancelAnimationFrame(previewFrame);
    previewFrame = 0; pendingTime = null;
    const finished = drag; drag = null; wave.substarBoundaryDrag = false;
    suppressClick = true;
    try { wave.pane.releasePointerCapture(event.pointerId); } catch (_) {}
    if (cancel) {
      finished.session.restore();
      if(finished.started){if(finished.target.kind==='word')finished.command?.cancel?.();else wave.options.onCancelEdit?.();}
    } else if (finished.started) {
      if(finished.target.kind==='word'){if(finished.session.changed())wave.options.commitWordEdit(finished.command);else finished.command?.cancel?.();}
      else if (finished.session.changed()) wave.options.onCommitEdit?.();
      else wave.options.onCancelEdit?.();
    } else toggle(finished.target);
    refresh(); show(!cancel && !finished.started ? finished.target : null);
  }
  host.addEventListener('pointerdown', down, {capture:true, signal});
  host.addEventListener('pointermove', event => {
    if (drag || wave.drag || wave.wordDrag || badge.contains(event.target)) return;
    const next = target(event), rect = badge.getBoundingClientRect();
    const inBadgePath = hover && event.clientX >= rect.left - 22 && event.clientX <= rect.right + 8
      && event.clientY >= rect.top - 8 && event.clientY <= hover.row.getBoundingClientRect().bottom;
    if (next || !inBadgePath) show(next);
  }, {signal});
  host.addEventListener('pointerleave', () => { if (!drag) show(null); }, {signal});
  host.addEventListener('scroll', () => {
    if (drag) show(drag.target, drag.session); else show(null);
  }, {capture:true, signal});
  win.addEventListener('resize', () => {
    if (drag) show(drag.target, drag.session); else show(null);
  }, {signal});
  host.addEventListener('click', event => {
    if (suppressClick || badge.contains(event.target)) {
      if (!suppressClick && badge.contains(event.target) && event.detail === 0 && hover) toggle(hover);
      stop(event); suppressClick = false;
    }
  }, {capture:true, signal});
  win.addEventListener('pointermove', move, {capture:true, signal});
  win.addEventListener('pointerup', event => finish(event), {capture:true, signal});
  win.addEventListener('pointercancel', event => finish(event, true), {capture:true, signal});
  win.addEventListener('keydown', event => {
    if (event.key === 'Escape' && drag) finish({pointerId:drag.pointerId,
      preventDefault:() => event.preventDefault(), stopImmediatePropagation:() => event.stopImmediatePropagation()}, true);
  }, {capture:true, signal});
  signal.addEventListener('abort', () => {
    if (previewFrame) win.cancelAnimationFrame(previewFrame);
    previewFrame = 0; pendingTime = null;
    if (drag) drag.session.restore();
    drag = null; wave.substarBoundaryDrag = false; badge.remove(); line.remove(); legacyStyle.remove();
    if (appendZones) wave.appendSharedBoundaryZones = appendZones;
    wave.appendWordBlocks=appendWords;
  }, {once:true});
}
