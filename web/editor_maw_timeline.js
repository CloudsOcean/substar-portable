import {installSubstarBoundaries} from './editor_maw_boundary.js?v=20261025-unified-word-boundaries-1';
import {installNativeWaveformStyle} from './editor_maw_native_style.js?v=20261025-low-energy-1';
import {installPcmWaveformPaint} from './editor_maw_pcm_paint.js?v=20261025-low-energy-1';
import {createWaveformLoader} from './editor_maw_waveform_loader.js?v=20261025-recovery-1';
import {createGapBoundarySnapper} from './editor_maw_gap_snap.js?v=20261024-visible-boundaries-1';
/* MAW waveform host adapter. All persistent edits use Substar document operations. */
import {createMawMenus} from './editor_maw_menus.js?v=20261024-word-menu-removed-1';
import {createCutPlanPanel} from './editor_cut_plan_panel.js?v=2';
import {shell} from './editor_maw_shell.js?v=20261024-no-test-menu-1';
import {initSubstarMarkersPanel} from './vendor/maw/editor/markers/editor-markers-panel.js';
const M=globalThis.SubstarMawModel;
const C=globalThis.AsrGapRemoveCore;
const U=()=>globalThis.AsrEditorUtils;

export function createTimelineController(options){
  const doc=options.canvas.ownerDocument,panel=options.canvas.closest('.cue-timeline-panel');
  const original=options.canvas.parentElement;original.hidden=true;
  const host=doc.createElement('div');host.className='maw-waveform-host';panel.append(host);
  host.innerHTML=shell;
  const $=s=>host.querySelector(s)||doc.querySelector('#editor-gaps-tool-content')?.querySelector(s)||doc.querySelector('#editor-markers-tool-content')?.querySelector(s),field=n=>$(`[data-field="${n}"]`)||doc.querySelector(`#editor-cut-tool-content [data-field="${n}"]`),status=field('save-status');
  let view=null,timeline=M.timeline(),markers=[],gapState=C.normalizeGapRemoveData(null),all=[],main=[],overlay=[],extension=[],beforeEdit=null;
  let cutPlanPanel=null;
  let dead=false,queue=Promise.resolve(),pending=0,selected=null,markerEnabled=true,wordEnabled=false,overlayEnabled=false,speaker='',newSpeaker='',payload=null,menu=null,frame=0,audition=null,wordSelection=new Set();
  const abort=new AbortController(),signal=abort.signal;
  let lowEnergySuppression=0;try{lowEnergySuppression=Math.max(0,Math.min(100,Number(localStorage.getItem('substar.maw.low-energy-suppression'))||0));}catch(_){}
  const getSuppression=()=>lowEnergySuppression/100;
  const show=text=>{status.textContent=text?' · '+text:'';};
  const refresh=()=>{if(!dead){wave.refreshCueOverlay();wave.refreshGapOverlay?.();wave.refreshMarkerOverlay?.();decorate();}};
  function enqueue(task){pending++;host.dataset.saving='true';show('正在保存…');const done=queue.then(async()=>{if(dead)return;try{await task();if(!dead){if(pending===1){readView(view);refreshControls();refresh();}show('');}}catch(e){if(!dead){readView(view);refresh();show('保存失败：'+(e.message||e));}throw e;}finally{pending--;host.dataset.saving=String(pending>0);}});queue=done.catch(()=>{});return done;}
  const run=fn=>Promise.resolve().then(fn).catch(e=>show(e.message||e));
  function persist(next){const snapshot=M.clone(next);return enqueue(async()=>{await options.onTimelineChange(snapshot);timeline=M.clone(snapshot);});}
  function commitMarkers(mutator){const next=U().normalizeMarkers(M.clone(markers));mutator(next);markers=U().normalizeMarkers(next);const t=M.timeline(timeline);t.schema='substar.timeline.v2';t.markers=[...(t.markers||[]).filter(m=>m.plan_id&&m.plan_id!==t.active_plan_id),...M.storedMarkers(markers)];timeline=t;refresh();globalThis.MaweMarkersPanel?.render();return persist(t);}
  function commitGaps(next){gapState=C.normalizeGapRemoveData(next);timeline=M.withGapState(timeline,gapState);refresh();renderGapPanel();cutPlanPanel?.render();return persist(timeline);}
  function manualGap(start,end,removed=true){const provenance=C.appendGapRemoveManualOverrides(gapState.provenance,[{start:Math.round(start),end:Math.round(end),removed}],gapState.gaps);return commitGaps({...gapState,provenance,gaps:C.gapRangesFromProvenance(provenance)});}
  function moveGap(index,delta,copy=false){if(copy){const gaps=C.copyGapRemoveRange(gapState.gaps,index,delta,wave.durationMs);return commitGaps({...gapState,provenance:C.normalizeGapRemoveProvenance(null,gaps),gaps});}const result=C.moveGapRemoveProvenance(gapState.provenance,gapState.gaps,index,delta,wave.durationMs,gapState.gaps);return commitGaps({...gapState,provenance:result.provenance,gaps:result.gaps});}
  function markerMove(id,delta){return commitMarkers(items=>{const item=items.find(m=>m.id===id);if(!item)return;const span=item.end==null?0:item.end-item.start;item.start=Math.max(0,Math.min(wave.durationMs-span,item.start+Math.round(delta)));if(item.end!=null)item.end=item.start+span;});}
  function markerFields(id,values){return commitMarkers(items=>{const item=items.find(m=>m.id===id);if(!item)return;for(const [key,value] of Object.entries(values)){if(key==='review')item.review=value?{status:value,reason:item.review?.reason||''}:undefined;else if(value===null)delete item[key];else item[key]=value;}});}
  function markerAdd(start,end){return commitMarkers(items=>items.push({id:crypto.randomUUID(),start:Math.round(start),...(end>start?{end:Math.round(end)}:{}),name:'',note:'',color:U().MARKER_DEFAULT_COLOR}));}
  function locateMarker(id,{play=false}={}){const item=markers.find(m=>m.id===id);if(!item)return false;seek(item.start/1000);revealTime(item.start/1000,true);if(play)options.media?.play().catch(e=>show(e.message));return true;}
  globalThis.MaweMarkerEditing={isEditingEnabled:()=>markerEnabled,getMarkers:()=>markers,findMarker:id=>markers.find(m=>m.id===id),addMarkerAtCurrentTime:()=>run(()=>markerAdd((options.media?.currentTime||0)*1000)),updateMarkerFields:(id,v)=>run(()=>markerFields(id,v)),deleteMarker:id=>run(()=>commitMarkers(items=>{const i=items.findIndex(m=>m.id===id);if(i>=0)items.splice(i,1);})),deleteMarkers:ids=>run(()=>commitMarkers(items=>{for(let i=items.length-1;i>=0;i--)if(ids.includes(items[i].id))items.splice(i,1);})),locateMarker};
  globalThis.MaweHint={flashHint:show};
  globalThis.MaweMediaPlayback={seekToTime:seconds=>seek(seconds)};
  globalThis.MaweGapRemoveData={getGapRemoveData:()=>gapState};
  globalThis.MaweGapRemoveUi={toggleAiCleanupReviewMute:marker=>{if(!Number.isFinite(marker.start)||!Number.isFinite(marker.end)||marker.end<=marker.start)return {changed:false};const ranges=gapState.provenance.sources.ai_cleanup_review||[],existing=ranges.some(r=>r.review_marker_id===marker.id),next=existing?ranges.filter(r=>r.review_marker_id!==marker.id):[...ranges,{id:marker.id,review_marker_id:marker.id,start:marker.start,end:marker.end,removed:true}];const provenance=C.replaceGapRemoveProvenanceSource(gapState.provenance,'ai_cleanup_review',next,gapState.gaps);run(()=>commitGaps({...gapState,provenance,gaps:C.gapRangesFromProvenance(provenance)}));return {changed:true,muted:!existing};}};
  globalThis.MaweBoot={DATA:{},SERVER_CONFIG:{}};
  function floatingPanel({panel:element,dragHandle,onOpen,manageButton}){
    const dockKey=()=>element.closest('[data-tool-panel]')?.dataset.toolPanel;
    const open=()=>{if(dockKey())doc.dispatchEvent(new CustomEvent('substar:activate-tool',{detail:dockKey()}));element.classList.add('show');element.setAttribute('aria-hidden','false');onOpen?.();};const close=()=>{if(dockKey()){if(element.closest('.experimental-tool-active'))doc.dispatchEvent(new CustomEvent('substar:activate-tool',{detail:'locator'}));return;}element.classList.remove('show');element.setAttribute('aria-hidden','true');};const toggle=()=>dockKey()?open():element.classList.contains('show')?close():open();
    manageButton?.addEventListener('click',toggle,{signal});
    dragHandle?.addEventListener('pointerdown',e=>{if(dockKey()||e.target.closest('button'))return;const r=element.getBoundingClientRect(),x=e.clientX,y=e.clientY;dragHandle.setPointerCapture(e.pointerId);const move=n=>{element.style.left=Math.max(0,Math.min(innerWidth-element.offsetWidth,r.left+n.clientX-x))+'px';element.style.top=Math.max(0,Math.min(innerHeight-40,r.top+n.clientY-y))+'px';element.style.right='auto';};const end=()=>{dragHandle.removeEventListener('pointermove',move);};dragHandle.addEventListener('pointermove',move,{signal});dragHandle.addEventListener('pointerup',end,{once:true,signal});dragHandle.addEventListener('pointercancel',end,{once:true,signal});},{signal});return {open,close,toggle,isOpen:()=>element.classList.contains('show')};
  }
  globalThis.MaweFloatingPanel={createFloatingPanel:floatingPanel};
  const gapPanel=floatingPanel({panel:$('[data-panel="gaps"]'),dragHandle:$('[data-panel="gaps"] header'),onOpen:renderGapPanel});
  function dockPanel(element,key){
    const target=doc.querySelector('#editor-'+key+'-tool-content');if(!target)return;
    target.classList.add('maw-waveform-host','docked-wave-tool');target.replaceChildren(element);
    element.classList.add('show','docked-tool-panel');element.setAttribute('aria-hidden','false');
  }
  dockPanel($('#gap-remove-panel'),'gaps');
  doc.addEventListener('substar:tool-activated',event=>{if(event.detail==='markers')globalThis.MaweMarkersPanel?.render();if(event.detail==='gaps')renderGapPanel();},{signal});
  const auxiliaryPanels=[];
  for(const [id,button,close] of [['auto-merge-panel','auto-merge-manage','auto-merge-close'],['subtitle-extend-panel','subtitle-extend-manage','subtitle-extend-close']]){const ctrl=floatingPanel({panel:$('#'+id),dragHandle:$('#'+id+' header'),manageButton:$('#'+button)});$('#'+close).addEventListener('click',ctrl.close,{signal});auxiliaryPanels.push(ctrl);}
  function segments(track='main'){return track==='extension'?extension:track==='overlay'?overlay:main;}
  function selection(track='main'){const ids=new Set(options.selection?.ids||[]);if(selected)ids.add(selected);return new Set(segments(track).flatMap((s,i)=>ids.has(s.id)?[i]:[]));}
  function select(index,track='main',modifiers={}){const s=segments(track)[index];if(!s)return;selected=s.id;options.selection?.select?.(s.id,all.map(s=>s.id),modifiers);options.onSelectCue?.(s.id,s.start/1000,modifiers);refresh();}
  function rangeSelect(index,track='main'){select(index,track,{range:true});}
  function selectToggle(index,track='main'){select(index,track,{toggle:true});}
  function seek(seconds){if(options.media)options.media.currentTime=seconds;options.onSeek?.(seconds);}
  function togglePlayback(){const media=options.media;if(media?.paused)media.play().catch(e=>show(e.message));else media?.pause();}
  function commitTimes(){const changes=M.timeChanges(beforeEdit||[],[...main,...overlay]);beforeEdit=null;if(!changes.length)return;const ids=new Set(changes.map(c=>c.cue_id)),wordTimes={};for(const s of [...main,...overlay])if(ids.has(s.id))for(const item of s.items)if(item.end>item.start&&item.start>=s.start&&item.end<=s.end)wordTimes[item.id]={start:Math.round(item.start),end:Math.round(item.end)};run(()=>enqueue(()=>options.onCommitTimelineTimes(changes,wordTimes)));}
  function editText(index,track='main'){
    const s=segments(track)[index];if(!s)return;
    const box=doc.createElement('dialog');box.className='cut-dialog';const title=doc.createElement('h3');title.textContent=track==='extension'?'编辑译文':'编辑字幕';const input=doc.createElement('textarea');input.value=s.text;const save=doc.createElement('button');save.textContent='保存';const close=doc.createElement('button');close.textContent='取消';box.append(title,input,save,close);host.append(box);box.showModal();input.focus();
    close.onclick=()=>box.close();box.onclose=()=>box.remove();save.onclick=()=>run(async()=>{await options.onTimelineTextEdit({cueId:s.id,tokenId:null,text:input.value,track});box.close();});
  }
  function split(index,time,track='main'){const s=segments(track)[index];if(!s)return;const cue=view.cue_views.find(c=>c.cue_id===s.id);const tokenIds=new Set(cue.display_token_ids||[]);const tokens=(view.token_views||[]).filter(t=>tokenIds.has(t.token_id)&&t.end*1000<=time);const after=tokens.at(-1);if(!after){show('此处没有可用字词边界');return;}options.onOperation?.('split_cue',{type:'split_cue',cue_id:s.id,after_token_id:after.token_id});}
  const snapGapBoundary=createGapBoundarySnapper(()=>main,()=>wordEnabled);
  const menus=createMawMenus({host,signal,run,segments,selection,select,split,editText,snapGapBoundary,
    getView:()=>view,getTimeline:()=>timeline,persist,enqueue,options,show,
    getGapState:()=>gapState,commitGaps,manualGap,markerAdd,getDuration:()=>wave.durationMs,
    overlayEnabled:()=>overlayEnabled,newSpeaker:()=>newSpeaker,
    audition:(start,end)=>{audition={end:end/1000};seek(start/1000);return options.media.play();},
    getWordSelection:()=>wordSelection,setWordSelection:(index,items)=>{selected=main[index].id;wordSelection=new Set(items);refresh();},
  });
  const wave=globalThis.AsrWaveform.create({host,getSegments:segments,getExtensionSegments:()=>extension,getSelection:selection,getExtensionSelection:()=>selection('extension'),getOverlaySelection:()=>selection('overlay'),
    multiSubtitleVisible:()=>options.getShowTranslations?.()!==false&&extension.some(s=>s.text),showTrackBadges:()=>false,
    getMarkers:()=>markers,getGapRemoveGaps:()=>C.getGapRemoveDisplayGaps(gapState.gaps),getGapOperationMode:()=>field('gap-mode').value,snapGapBoundary,
    getAutoSnapAdjacentCues:()=>true,getAdjacentBoundaryMode:()=> 'dual',getClickBehavior:()=> 'select-and-seek',getClickTarget:()=> 'pointer',getHoverSeekPreview:()=>false,getSnapToFrame:()=>false,
    getWaveShapeSource:()=>$('#waveform-shape-source').value,
    getOverlayCreateEnabled:()=>overlayEnabled,getHideDisabled:()=>false,isPlaybackActive:()=>!options.media?.paused,
    selectCue:i=>select(i),selectExtensionCue:i=>select(i,'extension'),selectOverlayCue:i=>select(i,'overlay'),activateCue:i=>select(i),activateExtensionCue:i=>select(i,'extension'),activateOverlayCue:i=>select(i,'overlay'),
    toggleCueSelection:i=>selectToggle(i),toggleExtensionSelection:i=>selectToggle(i,'extension'),toggleOverlaySelection:i=>selectToggle(i,'overlay'),selectCueRange:i=>rangeSelect(i),selectExtensionRange:i=>rangeSelect(i,'extension'),selectOverlayRange:i=>rangeSelect(i,'overlay'),
    addCueSelection:i=>select(i,'main',{toggle:true}),addExtensionSelection:i=>select(i,'extension',{toggle:true}),clearSelection:()=>{selected=null;options.selection?.clear();},
    // MAW seek callbacks already supply seconds (cue and gap ranges use milliseconds).
    seek:seconds=>seek(seconds),togglePlayback,
    onBeginEdit:()=>{beforeEdit=M.clone([...main,...overlay]);},onCancelEdit:()=>{beforeEdit=null;readView(view);refresh();},onCommitEdit:commitTimes,
    enterCueEditor:i=>editText(i),enterExtensionCueEditor:i=>editText(i,'extension'),enterOverlayCueEditor:i=>editText(i,'overlay'),
    splitCueAtTime:(i,t)=>split(i,t),splitOverlayCueAtTime:(i,t)=>split(i,t,'overlay'),
    showContextMenu:(x,y,i,t,mode)=>menus.cue(x,y,i,t,'main',mode),showExtensionContextMenu:(x,y,i,t)=>menus.cue(x,y,i,t,'extension'),showOverlayContextMenu:(x,y,i,t)=>menus.cue(x,y,i,t,'overlay'),
    showBlankWaveformMenu:(time,x,y,track)=>menus.blank(time,x,y,track),
    addCueRange:(start,end,_x,_y,track)=>run(()=>enqueue(()=>options.onCreateTimelineCue(start/1000,end/1000,newSpeaker||null,track==='overlay'))),onCueCreateRejected:()=>show('此处已有字幕，可在叠加轨创建'),
    onMarkerAdd:start=>run(()=>markerAdd(start)),onMarkerCreateRegion:(start,end)=>run(()=>markerAdd(start,end)),onMarkerMove:(id,delta)=>run(()=>markerMove(id,delta)),onMarkerResize:(id,edge,value)=>run(()=>markerFields(id,{[edge]:value==null?null:Math.round(value)})),onMarkerQuickEditFields:(id,v)=>run(()=>markerFields(id,v)),onMarkerQuickEditDelete:id=>globalThis.MaweMarkerEditing.deleteMarker(id),
    applyGapRange:(start,end,removed)=>run(()=>manualGap(start,end,removed)),toggleGapRemoved:index=>{const g=C.getGapRemoveDisplayGaps(gapState.gaps)[index];if(g)run(()=>manualGap(g.start,g.end,g.removed===false));},
    resizeGapBoundary:(index,edge,value)=>run(()=>{const result=C.resizeGapRemoveProvenanceBoundary(gapState.provenance,gapState.gaps,index,edge,value,gapState.gaps);return commitGaps({...gapState,provenance:result.provenance,gaps:result.gaps});}),
    moveGap:(index,delta)=>run(()=>moveGap(index,delta)),copyGap:(index,delta)=>run(()=>moveGap(index,delta,true)),
    previewGapAt:time=>{audition={end:Math.min(wave.durationMs,time+2000)/1000};seek(time/1000);options.media?.play().catch(e=>show(e.message));},
    showGapContextMenu:(x,y,index)=>menus.gap(x,y,index),
    wordTiming:{get enabled(){return wordEnabled;},getSelection:segment=>segment?.id===selected?wordSelection:new Set(),select:(index,item,event)=>{if(selected!==main[index]?.id||(!event.ctrlKey&&!event.metaKey&&!event.shiftKey))wordSelection.clear();wordSelection.add(item);select(index);},clearSelection:()=>wordSelection.clear(),previewItems:(segment,items)=>{segment.items=items;},showMenu:(x,y,index,item)=>menus.word(x,y,index,item)},
    beginWordEdit:()=>({before:M.clone(main),cancel:()=>{readView(view);refresh();}}),commitWordEdit:command=>{if(!command)return false;const t=M.timeline(timeline);t.word_timings={...(t.word_timings||{})};for(const s of main)for(const item of s.items)t.word_timings[item.id]={start:item.start,end:item.end};timeline=t;run(()=>persist(t));return true;},
  });
  installSubstarBoundaries({wave,host,signal,getSelected:()=>selected,refresh});
  installNativeWaveformStyle(wave,{getSuppression});
  installPcmWaveformPaint(wave,{loadWindow:request=>options.onWaveformWindow(request),getSource:()=>$('#waveform-shape-source').value,getSuppression,onError:error=>show('PCM 波形读取失败：'+error.message),signal});
  host.addEventListener('contextmenu',event=>{if(!event.target.matches('.waveform-marker-track'))return;const row=event.target.closest('.waveform-row');event.preventDefault();event.stopPropagation();menus.blank(wave.pointerTimeMs(event,row),event.clientX,event.clientY);},{capture:true,signal});
  const cutTool=doc.querySelector('#editor-cut-tool-content');
  if(cutTool){
    cutTool.replaceChildren();
    const heading=doc.createElement('h3');heading.textContent='剪辑';cutTool.append(heading);
    for(const name of ['plan','export']){const input=field(name);cutTool.append(input.closest('label'));if(name==='plan'){const button=$('[data-action="new-plan"]');cutTool.append(button);button.addEventListener('click',()=>{const name=prompt('剪辑方案名称','新方案');if(name===null)return;run(async()=>{const next=M.timeline(timeline),id=crypto.randomUUID();next.plans.push({id,name:name||'新方案',keep:null,cuts:[]});next.active_plan_id=id;delete next.gap_remove;await persist(next);readView({...view,properties:{...view.properties,timeline:next}});refresh();refreshControls();});},{signal});}}
    const gapButton=doc.createElement('button');gapButton.type='button';gapButton.textContent='空隙设置';gapButton.addEventListener('click',()=>gapPanel.open(),{signal});cutTool.append(gapButton);
    const hint=doc.createElement('p');hint.className='muted';hint.textContent='在波形中添加、调整空隙，按当前剪辑方案导出。';cutTool.append(hint);
  }
  if(cutTool)host.querySelector('.maw-plan-controls-staging')?.remove();
  cutPlanPanel=createCutPlanPanel({root:cutTool,signal,options,getTimeline:()=>timeline,getGapState:()=>gapState,
    getDuration:()=>wave.durationMs,waitQueue:()=>queue,persist,manualGap,refresh:()=>{readView(view);refreshControls();refresh();},
    preview:()=>{const first=globalThis.EditorCutTimeline.kept(timeline.plans.find(p=>p.id===timeline.active_plan_id),wave.durationMs/1000)[0];if(!first)return;field('skip').checked=true;seek(first.start);options.media?.play().catch(e=>show(e.message));},
    locate:start=>{seek(start);revealTime(start,true);},show});
  wave.attachPlayer(options.media);wave.setMediaAvailable(Boolean(options.media));
  globalThis.MaweCoreState={waveformEditor:wave,player:options.media};
  // MAW's main track is sequential. Substar permits different speakers to overlap:
  // run neighbour constraints on each speaker's sequence, then map indices back.
  const sharedBoundary=wave.isSharedBoundary.bind(wave);
  const reindexTracks=wave.reindexSelectedMoveTracks.bind(wave);
  wave.reindexSelectedMoveTracks=function(drag){for(const move of drag.moveTracks){const list=wave.options.getSegments(move.track),byId=new Map(list.map(s=>[s.id,s])),old=[...move.moveOriginals];move.moveOriginals=new Map(old.filter(([s])=>byId.has(s.id)).map(([s,t])=>[byId.get(s.id),t]));move.moveAnchorSegment=byId.get(move.moveAnchorSegment?.id);move.moveCommitSegments=new Set([...move.moveCommitSegments].map(s=>byId.get(s.id)).filter(Boolean));}return reindexTracks(drag);};
  wave.isSharedBoundary=function(event,left,right,row,track){const list=segments(track);return list[left]?.speaker===list[right]?.speaker&&sharedBoundary(event,left,right,row,track);};
  const moveDelta=wave.moveDragDelta.bind(wave);
  wave.moveDragDelta=function(drag,...args){const full=wave.options.getSegments(drag.track);const sid=full[drag.index]?.speaker;if(new Set(full.map(s=>s.speaker)).size<2)return moveDelta(drag,...args);const indices=full.flatMap((s,i)=>s.speaker===sid?[i]:[]),reverse=new Map(indices.map((i,j)=>[i,j]));const oldGet=wave.options.getSegments;wave.options.getSegments=track=>track===drag.track?indices.map(i=>full[i]):oldGet(track);const remap=map=>map?new Map([...map].filter(([i])=>reverse.has(i)).map(([i,v])=>[reverse.get(i),v])):map;try{return moveDelta({...drag,index:reverse.get(drag.index),indices:drag.indices.filter(i=>reverse.has(i)).map(i=>reverse.get(i)),originals:remap(drag.originals),squeezeOriginals:remap(drag.squeezeOriginals)},...args);}finally{wave.options.getSegments=oldGet;}};
  for(const method of ['applyMoveDrag','applyResizeDrag','applyBoundaryDrag','applyIndependentBoundaryDrag']){
    if(typeof wave[method]!=='function')continue;const originalMethod=wave[method].bind(wave);
    wave[method]=function(drag,...args){
      const full=segments(drag.track),speakers=new Set(drag.indices.map(i=>full[i]?.speaker));
      if(new Set(full.map(s=>s.speaker)).size<2)return originalMethod(drag,...args);
      const oldGet=wave.options.getSegments,commits=new Set();
      for(const sid of speakers){
        const indices=full.flatMap((s,i)=>s.speaker===sid?[i]:[]),reverse=new Map(indices.map((i,j)=>[i,j]));
        const remap=map=>map?new Map([...map].filter(([i])=>reverse.has(i)).map(([i,v])=>[reverse.get(i),v])):map;
        const local={...drag,index:reverse.get(drag.index),indices:drag.indices.filter(i=>reverse.has(i)).map(i=>reverse.get(i)),originals:remap(drag.originals),allOriginals:remap(drag.allOriginals),cancelOriginals:remap(drag.cancelOriginals),squeezeOriginals:remap(drag.squeezeOriginals),commitIndices:new Set([...drag.commitIndices||[]].filter(i=>reverse.has(i)).map(i=>reverse.get(i)))};
        if(local.index==null)local.index=local.indices[0];
        wave.options.getSegments=track=>track===drag.track?indices.map(i=>full[i]):oldGet(track);
        try{originalMethod(local,...args);}finally{wave.options.getSegments=oldGet;}
        for(const i of local.commitIndices||local.indices)commits.add(indices[i]);
      }
      drag.commitIndices=commits;
    };
  }
  const cueHeight=36,cueStep=40,wordHeight=cueHeight,wordStep=cueStep;
  const baseRowHeight=wave.getTimelineRowHeight.bind(wave);
  const wordStripHeight=()=>wordEnabled?wordStep*M.speakerLanes(main).count:0;
  wave.getTimelineRowHeight=()=>baseRowHeight()+wordStripHeight();
  const createMultiRow=wave.createMultiRow.bind(wave);
  wave.createMultiRow=(...args)=>{const row=createMultiRow(...args);row.style.height=(wave.settings.rowHeight+wordStripHeight())+'px';return row;};
  function toggleWords(value){wordEnabled=value;host.classList.toggle('maw-separated-words',value);field('layer').value=value?'word':'sentence';$('#word-timing-quick-toggle').setAttribute('aria-pressed',String(value));wave.multiRange=[-1,-1];wave.render();decorate();}
  // Host seam: preserve MAW geometry; add lanes only where Substar speakers overlap.
  const originalAppend=wave.appendCueBlocks.bind(wave);wave.appendCueBlocks=function(...args){originalAppend(...args);decorateRow(args[0]);};
  function decorateRow(row){
    const packed=M.speakerLanes(main.filter(s=>s.end>Number(row.dataset.startMs)&&s.start<Number(row.dataset.endMs)));
    const bilingual=options.getShowTranslations?.()!==false&&extension.some(s=>s.text);const laneHeight=cueHeight;
    const bandBottom=7;
    for(const block of row.querySelectorAll('.waveform-cue-block')){
      const track=block.dataset.track||'main',index=Number(block.dataset.idx??block.dataset.extIdx??block.dataset.overlayIdx),s=segments(track)[index];if(!s)continue;
      block.style.height=cueHeight+'px';
      block.dataset.substarSpeaker=s.speaker;const color=globalThis.EditorSpeakers?.color(s.speaker)||'#9cc9be';block.style.setProperty('--substar-speaker-color',color);
      if(track!=='overlay'){const lane=packed.lanes.get(s.id)||0;block.style.bottom=(bandBottom+lane*(bilingual?2:1)*cueStep+(bilingual&&((track==='main')===s.sourceUpper)?cueStep:0))+'px';block.style.height=laneHeight+'px';}
      if(wordEnabled&&track!=='overlay'){const lane=packed.lanes.get(s.id)||0;const offset=bandBottom+lane*((bilingual?2:1)*cueStep+wordStep);block.style.bottom=(offset+(bilingual&&((track==='main')===s.sourceUpper)?cueStep:0))+'px';block.style.height=cueHeight+'px';block.classList.remove('word-timing-background','word-timing-compact');}
      block.title=(globalThis.EditorSpeakers?.label(s.speaker,view?.properties?.speaker_names)||'未分配')+' · '+s.text;
    }
    // Shared seam hit areas follow the same resized lanes as their subtitle blocks.
    for(const zone of row.querySelectorAll('.waveform-cue-boundary')){
      const track=zone.dataset.track||'main',index=Number(zone.dataset.leftIdx);
      const block=[...row.querySelectorAll('.waveform-cue-block')].find(block=>(block.dataset.track||'main')===track&&Number(block.dataset.idx??block.dataset.extIdx??block.dataset.overlayIdx)===index);
      if(block){zone.style.height=block.style.height;zone.style.bottom=block.style.bottom||'7px';}
    }
    if(wordEnabled)for(const block of row.querySelectorAll('.waveform-word-block,.waveform-word-boundary')){const segment=main[Number(block.dataset.segmentIdx)];if(!segment)continue;const lane=packed.lanes.get(segment.id)||0;block.style.bottom=(bandBottom+lane*((bilingual?2:1)*cueStep+wordStep)+(bilingual?2:1)*cueStep)+'px';block.style.height=wordHeight+'px';block.classList.remove('word-timing-compact');block.classList.add('word-timing-full');}
  }
  const nativeWordRefresh=wave.refreshWordBlocks.bind(wave);wave.refreshWordBlocks=function(...args){nativeWordRefresh(...args);decorate();};
  function decorate(){wave.renderedRows.forEach(decorateRow);}
  function readView(next){
    view=next;timeline=M.timeline(next?.properties?.timeline);markers=U().normalizeMarkers(M.markers(timeline).filter(m=>!m.plan_id||m.plan_id===timeline.active_plan_id));
    const plan=timeline.plans.find(p=>p.id===timeline.active_plan_id);gapState=C.normalizeGapRemoveData(plan?.gap_remove||timeline.gap_remove||{gaps:M.legacyGaps(timeline)});
    all=M.segments(next,options.getTimelineCueText);for(const s of all){s.sourceUpper=globalThis.SubstarLanguageLayout?.routedSourceUpper(s.text,view?.presentation||{})!==false;s.color={name:s.speaker,value:globalThis.EditorSpeakers?.color(s.speaker)||'#87bfae'};for(const item of s.items){const timing=timeline.word_timings?.[item.id];if(timing)Object.assign(item,timing);}}
    const visible=all.filter(s=>!speaker||s.speaker===speaker);main=visible.filter(s=>s.subtitle_track!=='overlay');overlay=visible.filter(s=>s.subtitle_track==='overlay');extension=main.map(s=>{const translation={...s,text:s.translation,items:[],bound_to:s.id};for(const edge of ['start','end'])Object.defineProperty(translation,edge,{enumerable:true,get:()=>s[edge],set:value=>{s[edge]=value;}});return translation;});
    globalThis.MaweBoot.DATA.gap_remove=gapState;globalThis.MaweBoot.DATA.markers=markers;
  }
  function refreshControls(){
    const laneCount=M.speakerLanes(main).count,bilingual=options.getShowTranslations?.()!==false&&extension.some(s=>s.text);
    const needed=66+laneCount*(bilingual?2:1)*cueStep;
    if(wave.settings.rowHeight<needed){wave.settings.rowHeight=Math.ceil(needed/24)*24;if(![...wave.rowHeightSelect.options].some(o=>Number(o.value)===wave.settings.rowHeight))wave.rowHeightSelect.add(new Option(wave.settings.rowHeight+' px',String(wave.settings.rowHeight)));wave.multiRange=[-1,-1];wave.render();}
    const plan=field('plan');plan.replaceChildren();for(const p of timeline.plans)plan.add(new Option(p.name,p.id));plan.value=timeline.active_plan_id||'';$('#multi-subtitle-quick-toggle').setAttribute('aria-pressed',String(options.getShowTranslations?.()!==false));renderGapPanel();cutPlanPanel?.render();
  }
  function renderGapPanel(){if(!wave)return;for(const input of $('#gap-remove-panel').querySelectorAll('[data-gap]'))if(doc.activeElement!==input)input.value=gapState[input.dataset.gap]??({minimum_ms:400,threshold_db:-28,hysteresis_db:2,lead_in_ms:120,lead_out_ms:80})[input.dataset.gap];
    const gaps=gapState.gaps||[],removed=gaps.filter(g=>g.removed!==false);field('gap-summary').textContent=gaps.length?`已移除 ${removed.length}/${gaps.length} 段，共 ${(removed.reduce((n,g)=>n+g.end-g.start,0)/1000).toFixed(3)} 秒；左键定位，Alt+点击切换，空白处 Alt+左键拖动增加，空隙块左键拖动偏移，Ctrl/Cmd+拖动复制。`:'未能找到符合门限的静音空隙；尝试提高「音量阈值」来检测更多静音。';
    const list=field('gap-list');list.replaceChildren();for(const g of gaps){const line=doc.createElement('div');line.className='maw-gap-row';const label=doc.createElement('span');label.textContent=`${(g.start/1000).toFixed(3)}—${(g.end/1000).toFixed(3)}`;const locate=doc.createElement('button');locate.textContent='定位';locate.onclick=()=>{seek(g.start/1000);revealTime(g.start/1000,true);};const toggle=doc.createElement('button');toggle.textContent=g.removed===false?'移除':'恢复';toggle.onclick=()=>run(()=>manualGap(g.start,g.end,g.removed===false));line.append(label,locate,toggle);list.append(line);}
  }
  async function scan(){if(!payload)throw Error('波形尚未加载');const values={...gapState};for(const input of $('#gap-remove-panel').querySelectorAll('[data-gap]'))values[input.dataset.gap]=Number(input.value);const ranges=C.detectAudioGapRemoveGaps({...wave.activeWaveShape().payload,peaks:wave.activeWaveShape().peaks},{minimumMs:values.minimum_ms,thresholdDb:values.threshold_db,hysteresisDb:values.hysteresis_db,leadInMs:values.lead_in_ms,leadOutMs:values.lead_out_ms});const provenance=C.replaceGapRemoveProvenanceSource(gapState.provenance,'audio_gate',ranges,gapState.gaps);await commitGaps({...values,provenance,gaps:C.gapRangesFromProvenance(provenance)});}
  function revealTime(time,center=true){if(!wave.payload)return;if(wave.settings.mode==='basic'){wave.basicWindowStartMs=Math.max(0,time*1000-(center?wave.settings.visibleSeconds*500:0));wave.renderBasic();}else{wave.scroll.scrollTop=Math.max(0,Math.floor(time/wave.settings.secondsPerRow)*(wave.getTimelineRowHeight()+10)-(center?wave.scroll.clientHeight/2:0));wave.renderMultiVisible();}}
  const handlePanelAction=e=>{const action=e.target.closest('[data-action]')?.dataset.action;if(action==='gaps')gapPanel.toggle();if(action==='close-gaps')gapPanel.close();if(action==='scan')run(scan);};host.addEventListener('click',handlePanelAction,{signal});doc.querySelector('#editor-gaps-tool-content')?.addEventListener('click',handlePanelAction,{signal});
  $('#gap-remove-manage').textContent='空隙';$('#gap-remove-manage').hidden=true;
  $('#gap-remove-panel-title').textContent='空隙';
  $('#gap-remove-manage').title='打开空隙面板';$('#gap-remove-close').setAttribute('aria-label','关闭空隙面板');
  doc.addEventListener('keydown',event=>{if(event.key==='Escape'&&!host.contains(event.target)){gapPanel.close();globalThis.MaweMarkersPanel?.closePanel();menus.close();}},{signal});
  $('#markers-quick-toggle').hidden=true;
  doc.addEventListener('substar:open-wave-tool',event=>{
    if(event.detail==='gaps')gapPanel.toggle();
    if(event.detail==='markers')globalThis.MaweMarkersPanel?.togglePanel();
  },{signal});
  $('#markers-quick-toggle').removeAttribute('aria-pressed');
  $('#markers-quick-toggle').title='打开标记面板（M）；右键添加点或区段标记';
  $('#markers-manage').hidden=true;
  $('#auto-merge-manage').hidden=true;
  $('#subtitle-extend-manage').hidden=true;
  const settingsPanel=$('#waveform-settings-panel'),settingsToggle=$('#waveform-settings-toggle');
  const settingsHeader=doc.createElement('div');settingsHeader.className='waveform-settings-header';
  const settingsTitle=doc.createElement('strong');settingsTitle.id='waveform-settings-title';settingsTitle.textContent='波形设置';
  const settingsActions=doc.createElement('div');settingsActions.className='waveform-settings-actions';
  const settingsClose=doc.createElement('button');settingsClose.type='button';settingsClose.className='waveform-settings-close';settingsClose.textContent='×';settingsClose.setAttribute('aria-label','关闭波形设置');
  $('#waveform-settings-help')?.remove();settingsActions.append(settingsClose);settingsHeader.append(settingsTitle,settingsActions);
  const settingsBody=doc.createElement('div');settingsBody.className='waveform-settings-body';
  const appearance=$('#waveform-settings-appearance'),display=$('#waveform-settings-display');
  appearance.querySelector('.settings-panel-title-row').replaceWith($('#waveform-settings-appearance-title'));
  const suppressionRow=doc.createElement('div');suppressionRow.className='editor-settings-item waveform-suppression-row';
  const suppressionLabel=doc.createElement('label');suppressionLabel.htmlFor='waveform-low-energy';suppressionLabel.textContent='低能量抑制';
  const suppressionControls=doc.createElement('span');suppressionControls.className='waveform-suppression-controls';
  const suppressionInput=doc.createElement('input');suppressionInput.id='waveform-low-energy';suppressionInput.type='range';suppressionInput.min='0';suppressionInput.max='100';suppressionInput.step='5';suppressionInput.value=String(lowEnergySuppression);
  const suppressionValue=doc.createElement('output');suppressionValue.htmlFor=suppressionInput.id;
  const suppressionReset=doc.createElement('button');suppressionReset.type='button';suppressionReset.textContent='重置';suppressionReset.setAttribute('aria-label','重置低能量抑制');
  function updateSuppression(){lowEnergySuppression=Number(suppressionInput.value);suppressionValue.textContent=lowEnergySuppression?`${lowEnergySuppression}%`:'关闭';suppressionInput.setAttribute('aria-valuetext',suppressionValue.textContent);try{localStorage.setItem('substar.maw.low-energy-suppression',String(lowEnergySuppression));}catch(_){}wave.render();decorate();}
  suppressionValue.textContent=lowEnergySuppression?`${lowEnergySuppression}%`:'关闭';suppressionInput.setAttribute('aria-valuetext',suppressionValue.textContent);
  suppressionInput.addEventListener('input',updateSuppression,{signal});suppressionReset.addEventListener('click',()=>{suppressionInput.value='0';updateSuppression();},{signal});
  suppressionControls.append(suppressionInput,suppressionValue,suppressionReset);suppressionRow.append(suppressionLabel,suppressionControls);
  const suppressionHint=doc.createElement('small');suppressionHint.className='settings-panel-hint';suppressionHint.textContent='仅压低弱波形显示，不改变音量或空隙检测。';
  $('#waveform-scale-label').closest('.editor-settings-item').after(suppressionRow,suppressionHint);

  const playback=doc.createElement('section');playback.className='settings-panel-section waveform-settings-section';playback.setAttribute('role','group');playback.setAttribute('aria-label','播放与空隙');
  const playbackTitle=doc.createElement('span');playbackTitle.className='settings-panel-title waveform-settings-title';playbackTitle.textContent='播放与空隙';
  playback.append(playbackTitle,field('gap-mode').closest('label'),field('skip').closest('label'));
  settingsBody.append(appearance,display,playback);

  // Reuse MAW's row shells and pointer navigation with Substar's project cache.
  wave.thumbnailController?.close();
  let filmstrip;
  const thumbnailSection=doc.createElement('section');thumbnailSection.className='settings-panel-section waveform-settings-section';
  const thumbnailTitle=doc.createElement('span');thumbnailTitle.className='settings-panel-title';thumbnailTitle.textContent='画面预览图';thumbnailSection.append(thumbnailTitle);settingsBody.append(thumbnailSection);
  const repaintThumbnails=()=>{if(dead)return;for(const row of wave.renderedRows)wave.drawThumbnails(row);};
  filmstrip=globalThis.EditorThumbnails.create({doc,settingsPanel:thumbnailSection,url:options.thumbnailsUrl,
    onLayout(){if(dead)return;const stride=wave.settings.rowHeight+wave.thumbnailLayoutHeight+8,anchor=wave.scroll.scrollTop/Math.max(1,stride);wave.thumbnailLayoutHeight=filmstrip.enabled?48:0;wave.render();if(wave.isMultiMode()){wave.scroll.scrollTop=anchor*(wave.getTimelineRowHeight()+8);wave.renderMultiVisible();}decorate();},
    onPaint:repaintThumbnails});
  wave.thumbnailController={get visible(){return filmstrip.enabled;},get enabled(){return filmstrip.enabled;},mode:'strip',decodeEnabled:false,state:'ready',persisted:true,interval:500,
    setView(){},setPlayhead(){},setPlaying(){},setBinding(){},setMode(){},setDecodeEnabled(){},retry(){},close(){filmstrip.close();},picture(){return null;},keyframesInRange(){return [];}};
  wave.thumbnailLayoutHeight=filmstrip.enabled?48:0;
  wave.updateThumbnailStatus=()=>{};
  wave.drawThumbnails=row=>{if(!row._thumbnailCanvas||!filmstrip.enabled)return;const start=Number(row.dataset.startMs)/1000,end=Number(row.dataset.endMs)/1000;filmstrip.draw(row._thumbnailCanvas,start,end-start);};
  wave.shiftThumbnailStrip=row=>wave.drawThumbnails(row);
  for(const id of ['waveform-thumbnail-mode','waveform-thumbnail-decode'])settingsBody.querySelector('#'+id)?.closest('.editor-settings-item')?.remove();

  settingsPanel.prepend(settingsHeader,settingsBody);settingsPanel.setAttribute('aria-labelledby',settingsTitle.id);
  // Match the editor's terminology for hidden subtitles.
  const hiddenDisplay=$('#waveform-disabled-display').closest('label');
  for(const node of hiddenDisplay.childNodes)if(node.nodeType===3&&node.textContent.includes('禁用项'))node.textContent='隐藏字幕';
  function positionWaveformSettings(){
    if(settingsPanel.hidden)return;
    const r=settingsToggle.getBoundingClientRect(),margin=12,gap=8;
    const width=settingsPanel.offsetWidth,vw=doc.documentElement.clientWidth,vh=doc.documentElement.clientHeight;
    const below=vh-r.bottom-gap-margin,above=r.top-gap-margin;
    let left=Math.max(margin,Math.min(vw-width-margin,r.right-width)),top,maxHeight;
    if(below>=320){top=r.bottom+gap;maxHeight=below;}
    else if(above>=320){maxHeight=above;settingsPanel.style.maxHeight=Math.min(640,maxHeight)+'px';top=r.top-gap-settingsPanel.offsetHeight;}
    else if(vw-r.right-gap-margin>=width||r.left-gap-margin>=width){
      left=vw-r.right-gap-margin>=width?r.right+gap:r.left-gap-width;
      maxHeight=vh-2*margin;top=Math.max(margin,Math.min(r.top,vh-Math.min(640,maxHeight)-margin));
    }else{maxHeight=Math.max(below,above);top=below>=above?r.bottom+gap:margin;}
    settingsPanel.style.maxHeight=Math.max(80,Math.min(640,maxHeight))+'px';
    settingsPanel.style.left=left+'px';settingsPanel.style.top=Math.max(margin,top)+'px';
  }
  function closeWaveformSettings({focus=false}={}){settingsPanel.hidden=true;settingsToggle.setAttribute('aria-expanded','false');if(focus)settingsToggle.focus();}
  settingsToggle.addEventListener('click',()=>{if(!settingsPanel.hidden)return closeWaveformSettings();settingsPanel.hidden=false;settingsToggle.setAttribute('aria-expanded','true');positionWaveformSettings();},{signal});
  settingsClose.addEventListener('click',()=>closeWaveformSettings({focus:true}),{signal});
  window.addEventListener('resize',positionWaveformSettings,{signal});
  doc.addEventListener('scroll',positionWaveformSettings,{capture:true,signal});
  doc.addEventListener('pointerup',positionWaveformSettings,{signal});
  doc.addEventListener('keydown',e=>{if(e.key==='Escape'&&!settingsPanel.hidden){e.preventDefault();closeWaveformSettings({focus:true});}},{signal});
  $('#word-timing-quick-toggle').addEventListener('click',()=>toggleWords(!wordEnabled),{signal});
  $('#overlay-quick-toggle').addEventListener('click',()=>{overlayEnabled=!overlayEnabled;$('#overlay-quick-toggle').setAttribute('aria-pressed',String(overlayEnabled));},{signal});
  $('#multi-subtitle-quick-toggle').addEventListener('click',()=>{const toggle=doc.querySelector('#showTranslations');if(toggle){toggle.checked=!toggle.checked;toggle.dispatchEvent(new Event('change',{bubbles:true}));$('#multi-subtitle-quick-toggle').setAttribute('aria-pressed',String(toggle.checked));refresh();}},{signal});
  $('#waveform-settings-toggle').hidden=false;
  $('#waveform-settings-toggle').classList.add('workspace-settings-gear');
  $('#ass-style-quick-open')?.remove();
  $('#ass-mode-quick-toggle')?.remove();
  host.querySelectorAll('.waveform-toolbar .sep').forEach(separator=>separator.remove());
  for(const [toggle,body] of [['gap-remove-advanced-toggle','gap-remove-advanced-body'],['gap-remove-disable-toggle','gap-remove-disable-body']])$('#'+toggle).addEventListener('click',()=>{const target=$('#'+body);target.hidden=!target.hidden;$('#'+toggle).setAttribute('aria-expanded',String(!target.hidden));},{signal});
  $('#gap-remove-clear-all').addEventListener('click',()=>run(()=>commitGaps({...gapState,provenance:C.normalizeGapRemoveProvenance(null,[]),gaps:[]})),{signal});
  $('#gap-remove-shrink').addEventListener('click',()=>run(()=>{const audioGaps=C.normalizeGapRemoveGaps(gapState.provenance?.sources?.audio_gate);if(!audioGaps.length)throw Error('当前没有可收缩的静音空隙');const gaps=C.shrinkGapRemoveGaps(audioGaps,Number($('#gap-remove-lead-in').value),Number($('#gap-remove-lead-out').value));const provenance=C.replaceGapRemoveProvenanceSource(gapState.provenance,'audio_gate',gaps,gapState.gaps);return commitGaps({...gapState,provenance,gaps:C.gapRangesFromProvenance(provenance)});}),{signal});
  $('#gap-remove-disable-button').addEventListener('click',()=>run(async()=>{const matches=C.findGapRemoveDisableMatches(main,gapState.gaps,{coveragePercent:Number($('#gap-remove-disable-coverage').value),remainingMs:Number($('#gap-remove-disable-remaining').value)});const cue_ids=matches.filter(({index})=>!main[index].disabled).map(({index})=>main[index].id);if(!cue_ids.length){show('没有符合条件的字幕');return;}await enqueue(()=>options.onNativeMawOperation('hide_cues',{cue_ids}));}),{signal});
  $('#auto-merge-run').addEventListener('click',()=>run(()=>enqueue(()=>options.onNativeMawOperation('auto_merge_cues',{gap_ms:Number($('#auto-merge-gap-ms').value),snap_direction:$('#auto-merge-snap-direction').value==='forward'?'next':'previous',absorb:$('#auto-merge-absorb-short').checked,short_count:Number($('#auto-merge-short-count').value),absorb_direction:$('#auto-merge-absorb-direction').value}))),{signal});
  $('#subtitle-extend-run').addEventListener('click',()=>run(async()=>{const changes=[];for(const sid of new Set(main.map(s=>s.speaker))){const list=main.filter(s=>s.speaker===sid),ids=options.selection?.ids||new Set();const chosen=list.flatMap((s,i)=>ids.has(s.id)?[i]:[]);if(ids.size&&!chosen.length)continue;const plan=U().planSubtitleExtension(list,chosen,{forwardMs:Number($('#subtitle-extend-forward-ms').value),backwardMs:Number($('#subtitle-extend-backward-ms').value),durationMs:wave.durationMs});for(const change of plan.changes)if(change.changed)changes.push({cue_id:list[change.index].id,start:change.start/1000,end:change.end/1000});}if(changes.length)await enqueue(()=>options.onCommitTimelineTimes(changes));else show('没有需要延长的字幕');}),{signal});
  field('layer').onchange=()=>{toggleWords(field('layer').value==='word');};
  field('plan').onchange=()=>run(async()=>{const t=M.timeline(timeline);t.active_plan_id=field('plan').value;delete t.gap_remove;await persist(t);readView({...view,properties:{...view.properties,timeline:t}});refresh();});
  field('export').onchange=()=>run(async()=>{const format=field('export').value;field('export').value='';if(!format)return;await queue;if(!timeline.active_plan_id)throw Error('请先添加移除区间或建立方案');await options.onCutExport(timeline.active_plan_id,format,'ab-double');});
  doc.addEventListener('pointerdown',e=>{if(menu&&!menu.contains(e.target)){menu.remove();menu=null;}if(!e.target.closest('#waveform-settings'))closeWaveformSettings();},{signal});
  host.addEventListener('keydown',e=>{if(e.target.matches('input,textarea,select,[contenteditable=true]'))return;if(e.key==='Escape'){gapPanel.close();globalThis.MaweMarkersPanel?.closePanel();menus.close();}if(e.key.toLowerCase()==='q'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();toggleWords(!wordEnabled);}if(e.key.toLowerCase()==='m'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();$('#markers-quick-toggle').click();}},{signal});
  function tick(){if(dead)return;const media=options.media;if(media&&!media.paused){const time=media.currentTime;if(audition){if(time>=audition.end){media.pause();audition=null;}}else if(field('skip').checked){const g=gapState.gaps.find(g=>g.removed!==false&&time*1000>=g.start&&time*1000<g.end);if(g){if(g.end>=wave.durationMs)media.pause();else media.currentTime=g.end/1000;}}wave.updatePlayback();}frame=requestAnimationFrame(tick);}
  frame=requestAnimationFrame(tick);
  // Import the original marker manager and bind its host services once per controller.
  run(async()=>{const response=await fetch('/assets/vendor/maw/markers-panel.html');if(!response.ok)throw Error('标记面板读取失败');const html=await response.text();if(dead)return;const template=doc.createElement('template');template.innerHTML=html;host.append(template.content);
    const names={markersPanel:'markers-panel',markersDragHandle:'markers-drag-handle',markersManageButton:'markers-quick-toggle',markersSearchInput:'markers-search',markersFilterKind:'markers-filter-kind',markersFilterColor:'markers-filter-color',markersFilterReview:'markers-filter-review',markersList:'markers-list',markersSummary:'markers-summary',markersAddCurrentButton:'markers-add-current',markersBatchSelectButton:'markers-batch-select',markersBatchActions:'markers-batch-actions',markersDeleteSelectedButton:'markers-delete-selected',markersSelectAllButton:'markers-select-all',markersSelectionSummary:'markers-selection-summary'};
    globalThis.MaweDom=Object.fromEntries(Object.entries(names).map(([k,id])=>[k,$('#'+id)]));initSubstarMarkersPanel(window);dockPanel($('#markers-panel'),'markers');$('#markers-panel-title').textContent='标记';$('#markers-filter-review')?.closest('label')?.setAttribute('hidden','');$('#markers-close')?.addEventListener('click',()=>globalThis.MaweMarkersPanel.closePanel(),{signal});
  });
  const shapeSource=$('#waveform-shape-source');
  shapeSource.options[1].textContent='精细波形（ReaPeaks）';
  const pcmOption=doc.createElement('option');pcmOption.value='pcm';pcmOption.textContent='原版 PCM 波形';shapeSource.prepend(pcmOption);shapeSource.value='reapeaks';
  const shapeStatus=doc.createElement('small');shapeStatus.id='waveform-detail-status';shapeStatus.setAttribute('role','status');shapeStatus.textContent='原版 PCM · 按区间读取';shapeStatus.style.cssText='display:block;margin:4px 0 8px;color:var(--theme-muted);font-size:12px';
  shapeSource.closest('label').after(shapeStatus);
  shapeSource.addEventListener('change',()=>{if(shapeSource.value==='reapeaks')detailLoader.start();else shapeStatus.textContent=shapeSource.value==='pcm'?'原版 PCM · 按区间读取':'内置波形';wave.render();decorate();},{signal});
  const detailLoader=createWaveformLoader({
    load:request=>options.onDetailWaveformPayload(request),timeoutMs:660000,retries:1,
    apply(detail){if(dead||!wave.setReapeaksWaveform(detail))return false;shapeStatus.textContent=`精细波形已就绪 · ${Math.round(detail.sample_rate/detail.division)} 点/秒`;decorate();},
    onState({state,error}){if(dead)return;if(state==='loading')shapeStatus.textContent='精细波形生成中，暂用内置波形…';if(state==='error')shapeStatus.textContent=`精细波形不可用，使用内置波形：${error.message}`;}
  });
  const waveformLoader=createWaveformLoader({
    load:request=>options.onWaveformPayload(request),
    apply(loaded){if(!wave.setPayload(loaded))return false;payload=loaded;decorate();if(shapeSource.value==='reapeaks')detailLoader.start();},
    onState({state,attempt,error}){
      if(dead)return;
      const empty=wave.empty;
      if(state==='ready'){empty.replaceChildren();empty.classList.add('hidden');return;}
      empty.classList.remove('hidden');
      if(state==='error'){
        const message=doc.createElement('span');message.textContent=`波形读取失败：${error.message} `;
        const retry=doc.createElement('button');retry.type='button';retry.textContent='重新读取波形';
        retry.addEventListener('click',()=>waveformLoader.start(),{signal});
        empty.replaceChildren(message,retry);
      }else empty.textContent=state==='retrying'?`波形读取失败，正在重试（${attempt}/3）…`:'正在读取波形…';
    }
  });
  waveformLoader.start();
  return {setView(next){if(dead)return;filmstrip.setUrl(options.getThumbnailsUrl?.());if(!pending&&next?.revision_id!==view?.revision_id){readView(next);refreshControls();}else view=next;wave.refreshCueOverlay();wave.refreshGapOverlay?.();wave.refreshMarkerOverlay?.();decorate();if(doc.querySelector('[data-tool-panel=markers].experimental-tool-active'))globalThis.MaweMarkersPanel?.render();},setActiveCue(id){selected=id;},setSelectedCue(id){selected=id;wave.refreshCueBlocks();decorate();},redraw(){if(!wave.drag&&!wave.wordDrag&&!wave.substarBoundaryDrag)wave.render();decorate();},revealTime,revealRange:(start)=>revealTime(start,true),scrollToTime:revealTime,seek,
    setFollowPlayback(value){wave.manualFollowUntil=value?0:Date.now()+3600000;},setMode(mode){wave.setMode(mode==='multiline'?'multi':mode);},getState(){return {duration:wave.durationMs/1000,selected_cue_ids:[...selection()].map(i=>main[i]?.id).filter(Boolean),mode:wave.settings.mode==='multi'?'multiline':'basic'};},
    getTimeRect(start,end){const row=wave.renderedRows.find(r=>Number(r.dataset.startMs)<=start*1000&&Number(r.dataset.endMs)>start*1000);if(!row)return null;const r=row.getBoundingClientRect(),span=Number(row.dataset.endMs)-Number(row.dataset.startMs);return {left:r.left+(start*1000-Number(row.dataset.startMs))/span*r.width,top:r.top,width:(end-start)*1000/span*r.width,height:r.height};},
    autoSnapOnce:settings=>{const intent=globalThis.EditorTimeline?.autoSnapPlan?.(view,settings);if(intent?.count)options.onOperation?.('autoSnap',intent);return intent;},
    destroy(){dead=true;detailLoader.dispose();waveformLoader.dispose();abort.abort();cancelAnimationFrame(frame);wave.resizeObserver?.disconnect();wave.attachPlayer(null);wave.cancelCueDrag?.();wave.cancelWordDrag?.();wave.thumbnailController?.close();for(const name of ['resizeFrame','multiVisibleFrame','basicRenderFrame','cueRefreshFrame','gapMovePreviewFrame'])cancelAnimationFrame(wave[name]);menus.close();for(const key of ['gaps','markers'])doc.querySelector('#editor-'+key+'-tool-content')?.replaceChildren();host.remove();original.hidden=false;},
  };
}
