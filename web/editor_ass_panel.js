(function(root) {
  'use strict';
  function create({state,api,projectPath,activeMedia,systemSaveAs,flush,setRevision,loadRevisionHistory,askText,onError}) {
    const $=s=>document.querySelector(s), form=$('#assForm'), menu=$('#assMenu');
    let config=null, draft=null, layerId=null, library={}, busy=false, dirty=false, wholeProfile=false;
    const dirtyLayers=new Set();
    let renderer=null, fonts=null, fontKey='', timer=0, epoch=0, project='', revision='', ready=false;
    const clone=value=>JSON.parse(JSON.stringify(value));
    const message=text=>{ $('#assStatus').textContent=text; };
    const selected=()=>[...state.cueSelection.ids];
    const field=name=>form.elements.namedItem(name);
    const currentLayer=()=>draft?.layers.find(x=>x.id===layerId);
    function options(select, entries, value) {
      select.replaceChildren(...entries.map(([id,label])=>{const o=document.createElement('option');o.value=id;o.textContent=label;return o;}));
      if (entries.some(x=>x[0]===value)) select.value=value;
    }
    function selectionChanged() {
      const count=selected().length;
      $('#assScope option[value="selection"]').textContent=`选中的字幕（${count}）`;
      $('#assReset').disabled=busy || !count || $('#assScope').value!=='selection';
    }
    function fillLayer() {
      if (!draft) return;
      if (!currentLayer()) layerId=draft.layers[0].id;
      options($('#assLayer'),draft.layers.map(x=>[x.id,x.name]),layerId);
      const layer=currentLayer(), style=draft.styles[layer.style_id];
      for (const [key,value] of Object.entries({...style,content:layer.content,enabled:layer.enabled,word_highlight:layer.word_highlight})) {
        const input=field(key); if (!input) continue;
        if (input.type==='checkbox') input.checked=!!value; else input.value=String(value);
      }
      field('word_highlight').disabled=layer.content!=='source';
      $('#assRemoveLayer').disabled=draft.layers.length===1;
    }
    function readLayer() {
      if (!draft) return;
      const layer=currentLayer(), style=clone(draft.styles[layer.style_id]);
      for (const key of ['font','color','highlight']) style[key]=field(key).value;
      for (const key of ['size','outline','shadow','margin_y','alignment']) style[key]=Number(field(key).value);
      for (const key of ['bold','background']) style[key]=field(key).checked;
      // Editing one layer cannot mutate another layer sharing its old style.
      layer.style_id='layer_'+layer.id; draft.styles[layer.style_id]=style;
      layer.content=field('content').value; layer.enabled=field('enabled').checked;
      layer.word_highlight=layer.content==='source' && field('word_highlight').checked;
      const used=new Set(draft.layers.map(x=>x.style_id));
      for (const key of Object.keys(draft.styles)) if (!used.has(key)) delete draft.styles[key];
    }
    function loadEffective() {
      if (!config) return;
      const cue=$('#assScope').value==='selection' ? selected()[0] : null;
      draft=clone(config.profiles[config.cue_profiles[cue] || config.default_profile]);
      layerId=draft.layers[0].id; dirty=false; wholeProfile=false; dirtyLayers.clear(); fillLayer(); selectionChanged();
      const mixed=$('#assScope').value==='selection' && new Set(selected().map(id=>config.cue_profiles[id] || config.default_profile)).size>1;
      message(mixed ? '所选字幕样式不同；显示首条样式，修改仅应用到当前层。' : '修改后直接播放预览；点击应用写入版本。');
    }
    function scopePayload() {
      const ids=$('#assScope').value==='selection' ? selected() : null;
      if (ids && !ids.length) throw new Error('请先选择字幕');
      return {profile:draft,cue_ids:ids,layer_ids:wholeProfile ? null : (dirtyLayers.size ? [...dirtyLayers] : [layerId])};
    }
    function renderPayload(useDraft=true) {
      const media=activeMedia();
      return {expected_revision_id:state.revision.revision_id,use_configuration:true,
        options:{width:media?.videoWidth || 1920,height:media?.videoHeight || 1080},
        ...(useDraft && menu.open && dirty ? scopePayload() : {})};
    }
    function closeFrame() { $('#assFramePreview').hidden=true; $('#assFrameImage').removeAttribute('src'); }
    async function live() {
      if (!state.revision || !config) return;
      const ticket=++epoch, thisProject=state.projectId;
      try {
        const payload=renderPayload();
        const response=await fetch(projectPath('/ass/export'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        if (!response.ok) {const error=await response.json();throw new Error(typeof error.detail==='string' ? error.detail : '字幕预览暂时不可用');}
        const content=await response.text();
        fonts ||= await api('/api/ass/fonts');
        if (ticket!==epoch || thisProject!==state.projectId) return;
        const fontUrls=new Set();
        for (const line of content.split('\n')) {
          if (!line.startsWith('Style: ')) continue;
          const values=line.split(','), name=values[1].toLowerCase();
          if (fonts.available[name]) fontUrls.add(fonts.available[name]);
          if (values[7]==='-1' && fonts.available[name+' bold']) fontUrls.add(fonts.available[name+' bold']);
        }
        const nextFontKey=[...fontUrls].sort().join(',');
        if (renderer && nextFontKey!==fontKey) {renderer.dispose();renderer=null;ready=false;}
        fontKey=nextFontKey;
        const canvas=$('#assLiveCanvas');
        if (!renderer) {
          const box=$('#mediaViewport').getBoundingClientRect();
          canvas.width=Math.max(1,Math.round(box.width)); canvas.height=Math.max(1,Math.round(box.height));
          renderer=new root.SubtitlesOctopus({canvas,subContent:content,
            workerUrl:'/assets/vendor/libass/subtitles-octopus-worker.js',
            legacyWorkerUrl:'/assets/vendor/libass/subtitles-octopus-worker-legacy.js',
            fonts:[...fontUrls], ...(fonts.fallback ? {fallbackFont:fonts.fallback} : {}),
            onReady:()=>{ready=true;canvas.hidden=false;$('.subtitle-overlay').hidden=true;resize();},
            onError:()=>{ready=false;canvas.hidden=true;$('.subtitle-overlay').hidden=false;message('实时字幕渲染未能启动，请重试。');}});
          renderer.worker?.addEventListener('error', event=>console.error('ASS renderer:',event.message,event.filename,event.lineno));
        } else {
          renderer.setTrack(content); ready=true; canvas.hidden=false; $(".subtitle-overlay").hidden=true;
        }
        renderer.setCurrentTime(Number(activeMedia()?.currentTime)||0);
      } catch(error) {
        if (ticket!==epoch) return;
        // Never leave an older track visible after a failed update.
        renderer?.freeTrack();
        ready=false; $("#assLiveCanvas").hidden=true; $(".subtitle-overlay").hidden=false;
        revision="";
        if (menu.open) message(error.message);
      }
    }
    function schedule() { clearTimeout(timer); timer=setTimeout(live,180); }
    function resize() {
      if (!renderer) return;
      const rect=$('#mediaViewport').getBoundingClientRect();
      renderer.resize(Math.max(1,Math.round(rect.width)),Math.max(1,Math.round(rect.height)));
      renderer.setCurrentTime(Number(activeMedia()?.currentTime)||0);
    }
    new ResizeObserver(resize).observe($('#mediaViewport'));
    let lastTime=-1;
    function tick() {
      if (ready) {
        const time=Number(activeMedia()?.currentTime)||0;
        if (time!==lastTime) {renderer.setCurrentTime(time);lastTime=time;}
      }
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    async function sync() {
      if (!state.revision) return;
      closeFrame();
      // Optimistic edits keep their base revision ID: do not show the saved ASS
      // track over newer text while the operation is still awaiting ACK.
      if (state.documentStore?.pending().length) {
        ++epoch; clearTimeout(timer); revision="";
        ready=false; $("#assLiveCanvas").hidden=true; $(".subtitle-overlay").hidden=false;
        return;
      }
      const currentProject=state.projectId, currentRevision=state.revision.revision_id;
      if (project===currentProject && revision===currentRevision) return;
      if (project!==currentProject) {
        ++epoch; renderer?.dispose();renderer=null;ready=false;config=null;draft=null;dirty=false;
        $('#assLiveCanvas').hidden=true;$('.subtitle-overlay').hidden=false;
      }
      ++epoch; clearTimeout(timer);
      project=currentProject;revision=currentRevision;
      try {
        const result=await api(projectPath('/ass/configuration'));
        if (project!==currentProject || revision!==currentRevision) return;
        config=result;
        if (!menu.open || !dirty) loadEffective();
        schedule();
      } catch(error) { if (menu.open) message(error.message); }
    }
    async function action(work) {
      if (busy) return;
      if (!form.reportValidity()) return;
      readLayer(); busy=true;
      const controls=[...form.querySelectorAll('input,select,button:not(#assClose)')];
      controls.forEach(x=>x.disabled=true);
      try { await work(); } catch(error) {message(error.message);}
      finally {busy=false;controls.forEach(x=>x.disabled=false);if(draft)fillLayer();selectionChanged();}
    }
    async function refreshLibrary() {
      library=await api('/api/ass/profiles');
      options($('#assProfile'),Object.keys(library).map(x=>[x,x]),$('#assProfile').value);
    }
    menu.addEventListener('toggle',async()=>{
      if (!menu.open) {dirty=false;wholeProfile=false;schedule();return;}
      if (busy) return;
      $('#assScope').value=selected().length ? 'selection' : 'project';
      try {await sync();loadEffective();await refreshLibrary();}catch(error){message(error.message);}
    });
    $('#assScope').onchange=()=>{loadEffective();schedule();};
    $('#assLayer').onchange=()=>{readLayer();layerId=$('#assLayer').value;fillLayer();};
    form.addEventListener('input',event=>{
      if (!event.target.name || !draft) return;
      readLayer();dirty=true;dirtyLayers.add(layerId);
      field('word_highlight').disabled=currentLayer().content!=='source';
      field('word_highlight').checked=currentLayer().word_highlight;
      closeFrame();schedule();
    });
    $('#assAddLayer').onclick=()=>{
      if (!draft || draft.layers.length>=16) return;
      readLayer();const id='layer_'+Date.now().toString(36), style=clone(draft.styles[currentLayer().style_id]);
      style.margin_y=Math.min(2000,style.margin_y+100);
      draft.styles[id]=style;draft.layers.push({id,name:`第 ${draft.layers.length+1} 层`,content:'source',style_id:id,enabled:true,word_highlight:false});
      layerId=id;dirty=true;wholeProfile=true;fillLayer();schedule();
    };
    $('#assRemoveLayer').onclick=()=>{
      if (!draft || draft.layers.length<=1) return;
      draft.layers=draft.layers.filter(x=>x.id!==layerId);layerId=draft.layers[0].id;
      dirty=true;wholeProfile=true;fillLayer();readLayer();schedule();
    };
    $('#assProfileLoad').onclick=()=>{const value=library[$('#assProfile').value];if(!value)return;draft=clone(value);layerId=draft.layers[0].id;dirty=true;wholeProfile=true;fillLayer();schedule();};
    $('#assProfileSave').onclick=()=>action(async()=>{
      const name=await askText('保存字幕方案','方案名称',draft.name==='双语字幕' ? '' : draft.name);
      if (!name?.trim()) return;
      await api('/api/ass/profiles/'+encodeURIComponent(name.trim()),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...draft,name:name.trim()})});
      await refreshLibrary();$('#assProfile').value=name.trim();message('方案已保存；已应用的历史版本保持原样。');
    });
    $('#assProfileDelete').onclick=()=>action(async()=>{await api('/api/ass/profiles/'+encodeURIComponent($('#assProfile').value),{method:'DELETE'});await refreshLibrary();message('方案已从库中删除，项目内的样式仍然保留。');});
    async function apply(reset=false) {
      const origin=state.projectId, payload={...scopePayload(),reset};
      state.restorePreparationPending=true;
      try {
        await flush();
        if (origin!==state.projectId) throw new Error('项目已切换');
        state.restoreWriting=true;$('#editorWorkbench').inert=true;
        const result=await api(projectPath('/ass/configuration'),{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({...payload,expected_revision_id:state.revision.revision_id})});
        dirty=false;menu.open=false;setRevision(result);await loadRevisionHistory({force:true});
      } finally {state.restorePreparationPending=false;state.restoreWriting=false;$('#editorWorkbench').inert=false;}
    }
    form.onsubmit=event=>{event.preventDefault();action(()=>apply());};
    $('#assReset').onclick=()=>action(()=>apply(true));
    $('#assCancel').onclick=()=>{menu.open=false;closeFrame();};
    $('#assFrameClose').onclick=closeFrame;
    async function exportResponse(payload) {
      const response=await fetch(projectPath('/ass/export'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if (!response.ok) {const error=await response.json();throw new Error(typeof error.detail==='string' ? error.detail : 'ASS 导出失败');}
      return response;
    }
    async function render(seconds) {
      const origin=state.projectId;
      const payload=renderPayload();
      message(seconds==null ? '正在烧录视频…' : '正在生成当前帧…');
      await flush();if(origin!==state.projectId)throw new Error('项目已切换');
      payload.expected_revision_id=state.revision.revision_id;
      const job=await api(projectPath('/ass/render'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,preview_seconds:seconds})});
      for (;;) {
        const status=await api('/api/ass/renders/'+job.job_id);
        if (status.status==='failed') throw new Error(status.error);
        if (status.status==='succeeded') return '/api/ass/renders/'+job.job_id+'/file';
        if(origin!==state.projectId) throw new Error('项目已切换，后台渲染仍会完成');
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
    }
    $('#assPreview').onclick=()=>action(async()=>{const origin=state.projectId;activeMedia()?.pause();const url=await render(Number(activeMedia()?.currentTime)||0);if(origin!==state.projectId)return;$('#assFrameImage').src=url;$('#assFramePreview').hidden=false;message('当前帧已生成；播放时返回实时字幕。');});
    $('#assExport').onclick=()=>action(async()=>{
      const origin=state.projectId;
      const result=await systemSaveAs.saveBlob({suggestedName:systemSaveAs.safeFilename(state.taskInfo?.display_name || origin)+'.ass',description:'ASS 字幕',mimeType:'text/plain',extension:'.ass'},async()=>{
        await flush();if(origin!==state.projectId)throw new Error('项目已切换');return (await exportResponse(renderPayload())).blob();
      });message(result.cancelled ? '已取消导出' : '字幕已导出');
    });
    $('#assBurn').onclick=()=>action(async()=>{const result=await systemSaveAs.saveUrl({suggestedName:systemSaveAs.safeFilename(state.taskInfo?.display_name || state.projectId)+'_字幕.mp4',description:'字幕视频',mimeType:'video/mp4',extension:'.mp4',url:()=>render(null)});message(result.cancelled ? '已取消烧录' : '视频已保存');});
    [$('#projectVideo'),$('#projectAudio')].forEach(media=>{
      ['play','seeking','emptied'].forEach(event=>media.addEventListener(event,closeFrame));
      media.addEventListener('loadedmetadata',schedule);
    });
    window.addEventListener('pagehide',()=>{renderer?.dispose();renderer=null;ready=false;});
    return {sync,selectionChanged};
  }
  root.EditorAssPanel={create};
})(window);
