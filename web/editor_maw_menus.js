import {selectionRuns} from './editor_maw_selection.js?v=20261025-selection-1';
/* MAW context menu interaction and labels, with Substar's operation/save boundary. */
export function createMawMenus(h){
  const {host,signal,run,segments,selection,select,options}=h,doc=host.ownerDocument;
  const shortcut = id => options.shortcutLabel?.(id) || "";
  const menu=doc.createElement('div');menu.className='ctxmenu';menu.setAttribute('role','menu');host.append(menu);
  const close=()=>menu.classList.remove('show');
  function open(x,y){menu.classList.add('show');const rect=menu.getBoundingClientRect();menu.style.left=Math.max(4,Math.min(x,innerWidth-rect.width-4))+'px';menu.style.top=Math.max(4,Math.min(y,innerHeight-rect.height-4))+'px';}
  function reset(){menu.replaceChildren();}
  function item(label,key,fn,{disabled=false,danger=false,parent=menu}={}){
    const el=doc.createElement('button');el.type='button';el.className='item'+(disabled?' disabled':'')+(danger?' danger':'');el.setAttribute('role','menuitem');el.disabled=disabled;el.setAttribute('aria-disabled',String(disabled));
    const name=doc.createElement('span'),kbd=doc.createElement('kbd');name.textContent=label;kbd.textContent=key||'';if(!key)kbd.style.visibility='hidden';el.append(name,kbd);parent.append(el);
    el.addEventListener('click',()=>{close();run(fn);});return el;
  }
  function sep(parent=menu){const el=doc.createElement('div');el.className='sep';el.setAttribute('role','separator');parent.append(el);}
  const op=(type,payload)=>h.enqueue(()=>options.onNativeMawOperation(type,payload));
  const at=(time,track='main')=>segments(track).findIndex(s=>s.start<time&&time<s.end);
  function addMarkerItems(time){
    sep();item('添加点标记',shortcut('point_marker'),()=>h.markerAdd(time));

  }
  function blank(time,x,y,track='main'){
    reset();const ext=at(time,'extension');
    if(track==='extension')item('创建副字幕','',()=>h.editText(ext,'extension'),{disabled:ext<0||Boolean(segments('extension')[ext]?.text)});
    else {
      item('创建字幕',shortcut('create_cue'),()=>h.createCueAt(time),{disabled:!h.canCreateCue(time)});
      if(h.overlayEnabled())item('创建叠加字幕','',()=>h.createCueAt(time,'overlay'),{disabled:!h.canCreateCue(time,'overlay')});
    }
    sep();item('添加空隙','',()=>{const duration=h.getDuration(),length=Math.min(duration,Math.max(10,Number(h.getGapState().minimum_ms)||400)),start=Math.min(Math.max(0,Math.round(time/10)*10),duration-length);const a=h.snapGapBoundary(start),b=h.snapGapBoundary(start+length);return b-a>=10?h.manualGap(a,b):h.manualGap(start,start+length);});
    item('精确绘制空隙',shortcut('draw_gap'),h.armGapDraw);
    if(h.hasHiddenGaps())item('显示隐藏空隙','',h.showHiddenGaps);
    addMarkerItems(time);open(x,y);
  }
  function gap(x,y,index){
    const g=globalThis.AsrGapRemoveCore.getGapRemoveDisplayGaps(h.getGapState().gaps)[index];
    if(!g)return;
    h.selectGap(g);
    reset();
    item('重新听写此片段',shortcut('retranscribe'),()=>options.onRetranscribeGap(g.start/1000,g.end/1000),{disabled:!options.onRetranscribeGap});
    sep();
    item('恢复部分片段','',()=>h.startPartialGapRestore(g),{disabled:g.removed===false});
    sep();
    item(h.isGapHidden(g)?'显示空隙':'隐藏空隙','',()=>h.toggleGapVisibility(g));
    item('删除空隙',shortcut('delete_gap'),()=>h.clearGapRange(g.start,g.end),{danger:true});
    addMarkerItems(g.start);
    open(x,y);
  }
  function selectionItems(ranges){
    sep();
    item('添加点标记',shortcut('point_marker'),()=>h.createSelectionItems('point',ranges));
    item('添加线标记',shortcut('range_marker'),()=>h.createSelectionItems('range',ranges));
    item('添加空隙','',()=>h.createSelectionItems('gap',ranges));
    if(ranges.length===1 && ranges[0].end>ranges[0].start) {
      item('重新听写此片段',shortcut('retranscribe'),()=>options.onRetranscribeGap(ranges[0].start/1000,ranges[0].end/1000),{disabled:!options.onRetranscribeGap});
    }
  }
  function word(x,y,index,itemIndex){
    const s=segments('main')[index];if(!s)return;
    if(!h.getWordSelection(index).has(itemIndex))h.setWordSelection(index,[itemIndex]);
    reset();selectionItems(h.getWordRanges());open(x,y);
  }

  async function merge(ids){for(let i=1;i<ids.length;i++)await op('merge_cues',{cue_ids:[ids[0],ids[i]]});}
  function cue(x,y,index,time,track='main',mode={}){
    const s=segments(track)[index];if(!s)return;const multi=selection(track).size>1&&selection(track).has(index);if(!multi)select(index,track);
    const selected=multi?[...selection(track)].sort((a,b)=>a-b):[index],targets=selected.map(i=>segments(track)[i]),ids=targets.map(s=>s.id);reset();
    // A cue's color is its speaker identity; both controls save the same operation.
    const colorRow=doc.createElement('div');colorRow.className='speaker-color-options';
    const label=doc.createElement('div');label.textContent='说话人 / 颜色';colorRow.append(label);
    const names=h.getView()?.properties?.speaker_names||{};
    for(const speaker of [null,...globalThis.EditorSpeakers.ids(h.getView())]){
      const name=globalThis.EditorSpeakers.label(speaker,names),button=doc.createElement('button');
      button.type='button';button.className='item speaker-color-option';button.setAttribute('role','menuitemradio');
      button.setAttribute('aria-checked',String(targets.every(target=>(target.speaker||null)===speaker)));
      button.dataset.speakerChoice=speaker||'';button.title=name;
      const swatch=doc.createElement('i');swatch.className='speaker-color-swatch';swatch.style.background=globalThis.EditorSpeakers.color(speaker);
      const text=doc.createElement('span');text.textContent=name;const key=doc.createElement('kbd');key.textContent=globalThis.SubstarShortcuts.digitForSpeaker(speaker);
      button.append(swatch,text,key);button.onclick=()=>{close();run(()=>h.enqueue(()=>options.onTimelineSpeakerChange(ids,speaker)));};colorRow.append(button);
    }
    menu.append(colorRow);sep();
    const hiddenIds=targets.filter(target=>target.disabled).map(target=>target.id);
    const visibleIds=targets.filter(target=>!target.disabled).map(target=>target.id);
    if(multi)item('合并选中字幕','',()=>merge(ids));
    item('恢复选中字幕',shortcut('restore_cue'),()=>op('restore',{cue_ids:hiddenIds}),{disabled:!hiddenIds.length});
    item('隐藏选中字幕',shortcut('hide_cue'),()=>op('hide_cues',{cue_ids:visibleIds}),{disabled:!visibleIds.length});
    item('删除选中字幕','',()=>op('purge_cue',{cue_ids:ids}),{danger:true});
    selectionItems(selectionRuns(segments(track).map((s,i)=>({...s,key:i})),new Set(selected)));open(x,y);
  }
  doc.addEventListener('pointerdown',e=>{if(!menu.contains(e.target))close();},{signal});
  doc.addEventListener('keydown',e=>{if(e.key==='Escape'&&menu.classList.contains('show')){e.preventDefault();close();}},{capture:true,signal});
  function runShortcut(key, {closeUnmatched = true, activate = true} = {}) {
    if (!menu.classList.contains("show")) return false;
    const label = globalThis.SubstarShortcuts.display(key);
    const item = [...menu.querySelectorAll("button")].find(button => button.querySelector("kbd")?.textContent === label);
    if (!item) {
      if (closeUnmatched) close();
      return false;
    }
    if (activate && !item.disabled && options.canEdit?.()) item.click();
    return true;
  }
  // Selecting a waveform cue scrolls the separate subtitle list as the menu opens.
  host.addEventListener('scroll',e=>{if(!menu.contains(e.target))close();},{capture:true,signal});
  window.addEventListener('resize',close,{signal});
  menu.addEventListener('keydown',e=>{if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;const list=[...menu.querySelectorAll('button:not(:disabled)')].filter(el=>el.getClientRects().length),i=list.indexOf(doc.activeElement);if(!list.length)return;e.preventDefault();list[e.key==='Home'?0:e.key==='End'?list.length-1:(i+(e.key==='ArrowDown'?1:-1)+list.length)%list.length].focus();});
  return {blank,gap,word,cue,close,runShortcut};
}
