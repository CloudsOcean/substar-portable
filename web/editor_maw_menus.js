import {selectionRuns} from './editor_maw_selection.js?v=20261025-selection-1';
/* MAW context menu interaction and labels, with Substar's operation/save boundary. */
export function createMawMenus(h){
  const {host,signal,run,segments,selection,select,options}=h,doc=host.ownerDocument;
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
  function create(time,track){const list=segments(track);const next=list.find(s=>s.start>time);const end=Math.min(h.getDuration(),time+1500,next?.start??Infinity);if(end<=time)return;return h.enqueue(()=>options.onCreateTimelineCue(time/1000,end/1000,h.newSpeaker()||null,track==='overlay'));}
  function addMarkerItems(time,end){
    sep();item('添加点标记','',()=>h.markerAdd(time));

  }
  function blank(time,x,y,track='main'){
    reset();const index=at(time),ext=at(time,'extension');
    if(track==='extension')item('创建副字幕','N',()=>h.editText(ext,'extension'),{disabled:ext<0||Boolean(segments('extension')[ext]?.text)});
    else {item('创建字幕','N',()=>create(time,'main'),{disabled:index>=0});if(h.overlayEnabled())item('创建叠加字幕','',()=>create(time,'overlay'),{disabled:at(time,'overlay')>=0});}
    sep();item('添加空隙','',()=>{const duration=h.getDuration(),length=Math.min(duration,Math.max(10,Number(h.getGapState().minimum_ms)||400)),start=Math.min(Math.max(0,Math.round(time/10)*10),duration-length);const a=h.snapGapBoundary(start),b=h.snapGapBoundary(start+length);return b-a>=10?h.manualGap(a,b):h.manualGap(start,start+length);});
    if(h.getGapState().gaps.some(g=>g.removed!==false))item('填充区间空隙','',()=>{const gaps=h.getGapState().gaps.filter(g=>g.removed!==false).sort((a,b)=>a.start-b.start);if(gaps.some(g=>g.start<=time&&time<g.end))return;const start=gaps.filter(g=>g.end<=time).at(-1)?.end||0,end=gaps.find(g=>g.start>time)?.start??h.getDuration();return h.manualGap(start,end);}).title='将点击位置两侧已移除区间之间的整段内容也设为移除；缺少一侧时延伸到媒体开头或结尾，导出剪辑时会剪掉该段。';addMarkerItems(time);open(x,y);
  }
  function gap(x,y,index){const state=h.getGapState(),g=globalThis.AsrGapRemoveCore.getGapRemoveDisplayGaps(state.gaps)[index];if(!g)return;reset();item(g.removed===false?'移除区段':'恢复区段','',()=>h.manualGap(g.start,g.end,g.removed===false));sep();item('清理空隙','',()=>{const core=globalThis.AsrGapRemoveCore,provenance=core.removeGapRemoveProvenanceRange(state.provenance,g.start,g.end,state.gaps);return h.commitGaps({...state,provenance,gaps:core.gapRangesFromProvenance(provenance)});},{danger:true});addMarkerItems(g.start,g.end);open(x,y);}
  function selectionItems(ranges){
    sep();
    item('添加点标记','',()=>h.createSelectionItems('point',ranges));
    item('添加线标记','',()=>h.createSelectionItems('range',ranges));
    item('添加空隙','',()=>h.createSelectionItems('gap',ranges));
  }
  function word(x,y,index,itemIndex){
    const s=segments('main')[index];if(!s)return;
    if(!h.getWordSelection(index).has(itemIndex))h.setWordSelection(index,[itemIndex]);
    reset();selectionItems(h.getWordRanges());open(x,y);
  }

  async function editMany(ids,transform,track){for(const id of ids){const s=segments(track).find(s=>s.id===id);if(s)await options.onTimelineTextEdit({cueId:id,text:transform(s.text),track});}}
  async function merge(ids){for(let i=1;i<ids.length;i++)await op('merge_cues',{cue_ids:[ids[0],ids[i]]});}
  function cue(x,y,index,time,track='main',mode={}){
    const s=segments(track)[index];if(!s)return;const multi=selection(track).size>1&&selection(track).has(index);if(!multi)select(index,track);
    const selected=multi?[...selection(track)].sort((a,b)=>a-b):[index],targets=selected.map(i=>segments(track)[i]),ids=targets.map(s=>s.id);reset();
    if(track==='extension'){
      const bound=(label,key)=>{const entry=item(label,key,()=>{},{disabled:true});entry.title='当前译文与主字幕共用时间范围，尚不支持独立副字幕';};
      bound('在鼠标位置拆分','B');bound('合并副字幕块','C');bound('隐藏副字幕','Alt+点击');
      item('删除副字幕','',()=>editMany(ids,()=>'',track),{danger:true});
      bound('对齐主字幕时间范围','H');bound('解绑','Shift+G');open(x,y);return;
    }
    if(multi)item(`合并 ${ids.length} 条字幕`,'C',()=>merge(ids));
    if(multi)item('批量替换选中字幕…','',()=>{const from=prompt('查找文字');if(!from)return;const to=prompt('替换为','');if(to!==null)return editMany(ids,text=>text.split(from).join(to),track);});
    sep();
    // A cue's color is its speaker identity; both controls save the same operation.
    const colorRow=doc.createElement('div');colorRow.className='speaker-color-options';
    const label=doc.createElement('div');label.textContent='说话人 / 颜色';colorRow.append(label);
    const names=h.getView()?.properties?.speaker_names||{};
    for(const [i,speaker] of [null,...globalThis.EditorSpeakers.ids(h.getView())].entries()){
      const name=globalThis.EditorSpeakers.label(speaker,names),button=doc.createElement('button');
      button.type='button';button.className='item speaker-color-option';button.setAttribute('role','menuitemradio');
      button.setAttribute('aria-checked',String(targets.every(target=>(target.speaker||null)===speaker)));
      button.dataset.speakerChoice=speaker||'';button.title=name;
      const swatch=doc.createElement('i');swatch.className='speaker-color-swatch';swatch.style.background=globalThis.EditorSpeakers.color(speaker);
      const text=doc.createElement('span');text.textContent=name;const key=doc.createElement('kbd');key.textContent=i<10?String(i):'';
      button.append(swatch,text,key);button.onclick=()=>{close();run(()=>h.enqueue(()=>options.onTimelineSpeakerChange(ids,speaker)));};colorRow.append(button);
    }
    menu.append(colorRow);sep();
    if(track==='extension')item('删除副字幕','Delete',()=>editMany(ids,()=>'',track),{danger:true});
    else {item(targets.every(s=>s.disabled)?(multi?'显示选中字幕':'显示字幕'):(multi?'隐藏选中字幕':'隐藏字幕'),'Alt+点击',()=>op(targets.every(s=>s.disabled)?'restore':'hide_cues',{cue_ids:ids}));item(multi?`删除 ${ids.length} 条字幕`:'删除字幕','Delete',()=>op('purge_cue',{cue_ids:ids}),{danger:true});}
    if(multi)item('取消选择','Ctrl/Cmd+D',()=>{options.selection?.clear();options.onSelectCue?.(null);});
    selectionItems(selectionRuns(segments(track).map((s,i)=>({...s,key:i})),new Set(selected)));open(x,y);
  }
  doc.addEventListener('pointerdown',e=>{if(!menu.contains(e.target))close();},{signal});
  doc.addEventListener('keydown',e=>{if(!menu.classList.contains('show'))return;if(e.key==='Escape'){e.preventDefault();close();return;}if(e.ctrlKey||e.metaKey||e.altKey||e.target.matches('input,textarea,[contenteditable=true]'))return;const entry=[...menu.querySelectorAll('button:not(:disabled)')].find(el=>el.querySelector('kbd')?.textContent.toLowerCase()===e.key.toLowerCase());if(entry){e.preventDefault();e.stopPropagation();entry.click();}},{capture:true,signal});
  doc.addEventListener('scroll',e=>{if(!menu.contains(e.target))close();},{capture:true,signal});window.addEventListener('resize',close,{signal});
  menu.addEventListener('keydown',e=>{if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;const list=[...menu.querySelectorAll('button:not(:disabled)')].filter(el=>el.getClientRects().length),i=list.indexOf(doc.activeElement);if(!list.length)return;e.preventDefault();list[e.key==='Home'?0:e.key==='End'?list.length-1:(i+(e.key==='ArrowDown'?1:-1)+list.length)%list.length].focus();});
  return {blank,gap,word,cue,close};
}
