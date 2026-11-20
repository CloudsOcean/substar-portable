/* Original MAW ASS template and utilities, connected to Substar's revision boundary. */
import {createUtilsModule as styleUtils} from './vendor/maw/shared/utils/ass-style.js';
import {createUtilsModule as animationUtils} from './vendor/maw/shared/utils/ass-animation.js';
import {createUtilsModule as fontUtils} from './vendor/maw/shared/utils/fonts.js';
const clone=value=>JSON.parse(JSON.stringify(value));
const U=styleUtils({cloneJsonValue:clone,getNavigator:()=>navigator});
const A=animationUtils(U);
const {mergeFontFamilyOptions,filterFontFamilyOptions}=fontUtils();
const ROW_STYLES = [
  {id:'ass', layer:'upper', name:'上行', event:'Upper', fallback:U.ASS_DEFAULT_ASS_STYLE},
  {id:'ass-extension', layer:'lower', name:'下行', event:'Lower', fallback:U.ASS_DEFAULT_EXTENSION_STYLE},
];

function profileRows(profile) {
  const named=ROW_STYLES.map(row=>profile.layers.find(layer=>layer.id===row.layer));
  const remaining=profile.layers.filter(layer=>!named.includes(layer));
  return named.map(layer=>layer || remaining.shift());
}

function normalizeRowStyle(value, row) {
  return U.normalizeAssStyle({...value, name:row.name}, row.fallback, row.id);
}
export function createMawAssPanel({bridge,state}) {
  const host=document.createElement('div'); host.className='maw-waveform-host maw-ass-host';document.body.append(host);
  let panel,library,base,kind='style',selected='ass',opening=false,saving=false,project='',drag=null;
  let closeFontList=()=>{};
  const $=s=>host.querySelector(s);
  const status=text=>{if($('#ass-style-library-status')) $('#ass-style-library-status').textContent=text;};
  function fromStyle(style,row) {
    if (!style) return normalizeRowStyle(row.fallback, row);
    return normalizeRowStyle({fontName:style.font,fontSize:style.size,primaryColor:style.color,secondaryColor:style.highlight,
      outlineColor:style.outline_color,outline:style.outline,shadow:style.shadow,bold:style.bold,italic:style.italic,underline:style.underline,strikeOut:style.strike_out,
      borderStyle:style.background?3:(style.border_style||1),backColor:style.back_color||'#000000',outlineOpacity:style.outline_opacity??100,backOpacity:style.back_opacity??50,
      alignment:style.alignment,marginL:style.margin_l??style.margin_x,marginR:style.margin_r??style.margin_x,marginV:style.margin_y,
      scaleX:style.scale_x??100,scaleY:style.scale_y??100,spacing:style.spacing||0,angle:style.angle||0}, row);
  }
  function toStyle(style,profile) {
    return {font:style.fontName,size:style.fontSize,color:style.primaryColor,highlight:style.secondaryColor,outline_color:style.outlineColor,
      outline:style.outline,shadow:style.shadow,bold:style.bold,italic:style.italic,underline:style.underline,strike_out:style.strikeOut,
      background:false,border_style:style.borderStyle,back_color:style.backColor,outline_opacity:style.outlineOpacity,back_opacity:style.backOpacity,
      alignment:style.alignment,margin_l:style.marginL,margin_r:style.marginR,margin_y:style.marginV,margin_x:style.marginL,
      scale_x:style.scaleX,scale_y:style.scaleY,spacing:style.spacing,angle:style.angle,fade_ms:0,
      override_tags:A.assAnimationOverrideTags(profile,{style})};
  }
  function libraryForProfile(profile) {
    const result=U.normalizeAssStyleLibrary(profile.maw_library);
    result.styles=profileRows(profile).map((layer,index)=>fromStyle(profile.styles[layer?.style_id],ROW_STYLES[index]));
    for (const item of result.assProfiles) item.styleId=ROW_STYLES[0].id;
    result.assignments.assExtensionStyleId=ROW_STYLES[1].id;
    delete result.assignments.srtBurnStyleId;
    return result;
  }

  function activeProfile() {
    return library.assProfiles.find(item=>item.id===library.assignments.assExportProfileId) || library.assProfiles[0];
  }

  function result() {
    const next=clone(base), profile=activeProfile();
    next.name=profile.name;
    next.maw_library=clone(library);
    profileRows(next).forEach((layer,index)=>{
      if (!layer) return;
      const id='maw_'+layer.id;
      next.styles[id]=toStyle(library.styles[index],profile);
      layer.style_id=id;
    });
    const used=new Set(next.layers.map(layer=>layer.style_id));
    for (const key of Object.keys(next.styles)) if (!used.has(key)) delete next.styles[key];
    return next;
  }

  function preview() {
    if (project!==state.projectId) return;
    bridge.setDraft(result(),$('#maw-ass-scope').value);
    status('修改待应用 · 播放窗口实时更新');
  }
  function entry(){return (kind==='style'?library.styles:library.assProfiles).find(x=>x.id===selected);}
  function select(k,id) {
    closeFontList();
    kind=k;
    selected=id;
    render();
    $('.ass-style-editor').scrollTop=0;
    host.querySelector(`[data-ass-kind="${k}"][aria-selected="true"]`)?.focus();
  }
  function options(select,items,value){select.replaceChildren(...items.map(item=>new Option(item.name,item.id)));select.value=value;}
  function renderList(selector,items,entryKind) {
    const list=$(selector);
    list.replaceChildren(...items.map(item=>{
      const button=document.createElement('button');
      button.type='button';
      button.setAttribute('role','option');
      button.dataset.assKind=entryKind;
      button.setAttribute('aria-selected',String(kind===entryKind && selected===item.id));
      button.onclick=()=>select(entryKind,item.id);
      if (entryKind==='style') {
        const name=document.createElement('span');
        name.className='ass-row-name';
        name.textContent=item.name;
        const swatch=document.createElement('span');
        swatch.className='ass-row-swatch';
        swatch.style.backgroundColor=item.primaryColor;
        swatch.setAttribute('aria-hidden','true');
        const detail=document.createElement('small');
        detail.className='ass-row-detail';
        detail.textContent=`${item.fontName} · ${item.fontSize} px`;
        button.append(swatch,name,detail);
      } else {
        button.textContent=item.name;
      }
      return button;
    }));
  }

  let sampleRenderer=null,sampleFontKey='',sampleTimer=0,sampleEpoch=0;
  function stopSample() {
    ++sampleEpoch;
    clearTimeout(sampleTimer);
    sampleRenderer?.dispose();
    sampleRenderer=null;
    sampleFontKey='';
  }

  function sampleTrack(width) {
    const rows=library.styles.map((style,index)=>{
      const fontName=style.fontName.replace(/[,\r\n{}\\]/g,' ').trim() || ROW_STYLES[index].fallback.fontName;
      return {...style,fontName};
    });
    const content=[
      '[Script Info]','ScriptType: v4.00+',`PlayResX: ${width}`,'PlayResY: 1080',
      'ScaledBorderAndShadow: yes','WrapStyle: 0','','[V4+ Styles]',`Format: ${U.ASS_STYLE_FORMAT}`,
      ...rows.map((style,index)=>U.assStyleLine(style,ROW_STYLES[index].event)),
      '','[Events]',`Format: ${U.ASS_EVENT_FORMAT}`,
      ...ROW_STYLES.map(({event,name},index)=>`Dialogue: ${index},0:00:00.00,0:00:10.00,${event},,0,0,0,,${name}字幕预览`),'',
    ].join('\n');
    return {rows,content};
  }

  function scheduleSample() {
    clearTimeout(sampleTimer);
    const ticket=++sampleEpoch;
    sampleTimer=setTimeout(async()=>{
      try {
        const fonts=await bridge.getFonts();
        if (ticket!==sampleEpoch || project!==state.projectId || kind!=='style' || !panel.classList.contains('show')) return;
        const canvas=$('#ass-style-preview-sample');
        const video=document.querySelector('#projectVideo');
        const width=Math.round(video?.videoWidth && video?.videoHeight ? 1080*video.videoWidth/video.videoHeight : 1920);
        const stage=canvas.parentElement;
        stage.style.setProperty('--ass-sample-ratio',String(width/1080));
        const {rows,content}=sampleTrack(width);
        const fontUrls=new Set();
        for (const row of rows) {
          const family=row.fontName.toLowerCase();
          for (const name of [family,row.bold?family+' bold':'',row.italic?family+' italic':'']) {
            if (fonts.available[name]) fontUrls.add(fonts.available[name]);
          }
        }
        const nextKey=[...fontUrls].sort().join(',');
        if (sampleRenderer && nextKey!==sampleFontKey) {
          sampleRenderer.dispose();
          sampleRenderer=null;
        }
        sampleFontKey=nextKey;
        const size=stage.getBoundingClientRect();
        if (!size.width || !size.height) return;
        const pixels={width:Math.round(size.width),height:Math.round(size.height)};
        if (!sampleRenderer) {
          canvas.width=pixels.width;
          canvas.height=pixels.height;
          const renderer=new window.SubtitlesOctopus({
            canvas,subContent:content,fonts:[...fontUrls],
            ...(fonts.fallback?{fallbackFont:fonts.fallback}:{}),
            workerUrl:'/assets/vendor/libass/subtitles-octopus-worker.js',
            legacyWorkerUrl:'/assets/vendor/libass/subtitles-octopus-worker-legacy.js',
            onReady:()=>{if(sampleRenderer===renderer)renderer.setCurrentTime(1);},
            onError:()=>{if(sampleRenderer===renderer)status('样式预览暂不可用');},
          });
          sampleRenderer=renderer;
        } else {
          sampleRenderer.resize(pixels.width,pixels.height);
          sampleRenderer.setTrack(content);
          sampleRenderer.setCurrentTime(1);
        }
      } catch (error) {
        if (ticket===sampleEpoch) status(`样式预览暂不可用：${error.message}`);
      }
    },105);
  }

  function render(){
    const current=entry();if(!current){kind='style';selected=library.styles[0].id;return render();}
    renderList('#ass-style-list',library.styles,'style');renderList('#ass-profile-list',library.assProfiles,'profile');
    $('#ass-style-count').textContent=library.styles.length;$('#ass-profile-count').textContent=library.assProfiles.length;
    options($('#ass-ass-default-profile'),library.assProfiles,library.assignments.assExportProfileId);
    $('#ass-style-form').hidden=kind!=='style';$('#ass-profile-form').hidden=kind!=='profile';
    $('#ass-style-delete').hidden=kind!=='profile' || current.builtin;
    $('#ass-style-reset').hidden=kind!=='profile' || !current.builtin;
    $('#ass-profile-form-title').textContent=kind==='profile'?current.name:'输出方案';
    $('#ass-profile-builtin-badge').hidden=!current.builtin;
    for(const input of panel.querySelectorAll('[data-ass-style-field],[data-ass-profile-field],[data-ass-animation]')){
      let value;
      if(input.dataset.assStyleField){if(kind!=='style')continue;value=current[input.dataset.assStyleField];}
      else if(input.dataset.assProfileField){if(kind!=='profile')continue;value=current[input.dataset.assProfileField];}
      else {if(kind!=='profile')continue;const [g,k]=input.dataset.assAnimation.split('.');value=current.animations[g][k];}
      if(document.activeElement!==input){if(input.type==='checkbox')input.checked=!!value;else if(input.type==='radio')input.checked=String(value)===input.value;else input.value=value??'';}
    }
    if(kind==='style') scheduleSample();
    else {
      stopSample();
      $('#ass-profile-preview-summary').textContent=A.assAnimationOverrideTags(current)||'无逐句动画';
    }
    $('#maw-ass-scope option[value=selection]').textContent=`选中的字幕（${state.cueSelection.ids.size}）`;
    if(kind==='style'){
      $('#ass-style-outline-color-label').textContent=current.borderStyle===3?'底框颜色':'描边颜色';
      $('#ass-style-outline-label').textContent=current.borderStyle===3?'底框宽度':'描边宽度';
      $('#ass-style-outline-opacity-label').textContent=current.borderStyle===3?'底框不透明度':'描边不透明度';
    }
  }
  function changed() {
    library.styles=library.styles.map((style,index)=>normalizeRowStyle(style,ROW_STYLES[index]));
    library.assProfiles=library.assProfiles.map(item=>U.normalizeAssProfile(item,item));
    render();
    preview();
  }
  function valid(){return [...panel.querySelectorAll('input,select')].filter(x=>!x.closest('[hidden]')).every(x=>x.reportValidity());}
  async function work(fn){if(saving)return;saving=true;panel.setAttribute('aria-busy','true');panel.querySelectorAll('button').forEach(b=>b.disabled=true);try{if(project!==state.projectId)throw Error('项目已切换，请重新打开面板');await fn();}catch(e){status(e.message||String(e));}finally{saving=false;panel.removeAttribute('aria-busy');panel.querySelectorAll('button').forEach(b=>b.disabled=false);}}
  function hidePanel() {
    closeFontList();
    stopSample();
    panel.classList.remove('show');
    panel.setAttribute('aria-hidden','true');
    document.querySelector('#experimental-subtitle-panel-open')?.focus();
  }
  function close() {
    if (saving) return;
    hidePanel();
    bridge.cancelDraft();
  }
  function addProfile() {
    if (library.assProfiles.length>=64) {status('最多支持 64 个输出方案');return;}
    const id='profile-'+crypto.randomUUID().slice(0,12);
    library.assProfiles.push({...clone(U.ASS_DEFAULT_PROFILE),id,builtin:false,name:'新方案'});
    kind='profile';
    selected=id;
    changed();
  }
  function wireFontPicker() {
    const picker=$('#ass-style-font-picker');
    const input=$('#ass-style-font-name');
    const toggle=$('#ass-style-font-toggle');
    const list=$('#ass-font-name-options');
    const editor=$('.ass-style-editor');
    // Keep the list above the dialog's scrolling content and sticky preview.
    host.append(list);

    let catalog=null;
    let fontRequest=null;
    let query='';
    let activeOption=null;

    function highlight(option) {
      activeOption?.classList.remove('active');
      activeOption=option;
      if (option) {
        option.classList.add('active');
        input.setAttribute('aria-activedescendant',option.id);
        option.scrollIntoView({block:'nearest'});
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function setExpanded(expanded) {
      list.hidden=!expanded;
      input.setAttribute('aria-expanded',String(expanded));
      toggle.setAttribute('aria-expanded',String(expanded));
      toggle.setAttribute('aria-label',expanded?'收起字体列表':'展开字体列表');
      if (!expanded) highlight(null);
    }

    function positionList() {
      if (list.hidden) return;
      const anchor=picker.getBoundingClientRect();
      const scroller=editor.getBoundingClientRect();
      if (!anchor.width || anchor.bottom<=scroller.top || anchor.top>=scroller.bottom) {
        setExpanded(false);
        return;
      }
      const above=anchor.top-12;
      const below=innerHeight-anchor.bottom-12;
      const height=Math.min(280,innerHeight*.4,list.scrollHeight+2);
      const openAbove=below<height && above>below;
      list.style.left=anchor.left+'px';
      list.style.width=anchor.width+'px';
      list.style.maxHeight=Math.max(0,Math.min(height,openAbove?above:below))+'px';
      list.style.top=openAbove?'auto':(anchor.bottom+4)+'px';
      list.style.bottom=openAbove?(innerHeight-anchor.top+4)+'px':'auto';
    }

    function renderOptions(message) {
      highlight(null);
      const matches=filterFontFamilyOptions(catalog,query);
      const selection=input.value.trim().toLocaleLowerCase();
      const nodes=matches.map((entry,index)=>{
        const option=document.createElement('div');
        option.id=list.id+'-option-'+index;
        option.className='font-combobox-option';
        option.setAttribute('role','option');
        option.setAttribute('aria-selected',String(entry.value.toLocaleLowerCase()===selection));
        option.dataset.fontName=entry.value;
        option.textContent=entry.label;
        option.title=entry.label;
        return option;
      });
      if (!nodes.length) {
        const feedback=document.createElement('div');
        feedback.className='font-combobox-empty';
        feedback.textContent=message || (catalog?'未找到匹配的字体':'正在读取本机字体…');
        nodes.push(feedback);
      }
      // Reset the previous filtered list's height before measuring the new content.
      list.style.maxHeight='';
      list.replaceChildren(...nodes);
      list.scrollTop=0;
      positionList();
    }

    function getCatalog() {
      if (!fontRequest) {
        fontRequest=bridge.getFonts().then(fonts=>{
          const names=Object.keys(fonts.available||{});
          const preferredNames=['Arial',...ROW_STYLES.map(row=>row.fallback.fontName)];
          const labels=new Map(preferredNames.map(name=>[name.toLocaleLowerCase(),name]));
          catalog=mergeFontFamilyOptions(names.sort((a,b)=>a.localeCompare(b)).map(name=>({
            value:labels.get(name)||name
          })));
          if (!list.hidden) renderOptions();
          return catalog;
        }).catch(error=>{
          fontRequest=null;
          if (!list.hidden) renderOptions('读取失败，可点击「读取本机字体」重试');
          throw error;
        });
      }
      return fontRequest;
    }

    function showList(text='') {
      query=text;
      setExpanded(true);
      renderOptions();
      if (!catalog) void getCatalog().catch(()=>{});
    }

    function choose(option) {
      input.value=option.dataset.fontName;
      input.focus({preventScroll:true});
      input.dispatchEvent(new Event('input',{bubbles:true}));
      setExpanded(false);
    }

    toggle.onclick=()=>{
      if (!list.hidden) { setExpanded(false); return; }
      input.focus({preventScroll:true});
      showList();
    };
    $('#ass-style-local-font-scan').onclick=()=>work(async()=>{
      const entries=await getCatalog();
      status('已载入 '+entries.length+' 种可用字体');
    });
    input.addEventListener('input',event=>{
      if (!event.isComposing) showList(input.value);
    });
    input.addEventListener('keydown',event=>{
      if (event.isComposing) return;
      const {key}=event;
      if (key==='Tab') setExpanded(false);
      if (key==='Escape' && !list.hidden) {
        event.preventDefault();
        event.stopPropagation();
        setExpanded(false);
      }
      if (key==='Enter' && activeOption) {
        event.preventDefault();
        choose(activeOption);
      }
      if (key==='ArrowDown' || key==='ArrowUp') {
        event.preventDefault();
        if (list.hidden) showList();
        const options=[...list.querySelectorAll('[role="option"]')];
        if (!options.length) return;
        const index=options.indexOf(activeOption);
        const next=key==='ArrowDown'?index+1:(index<0?options.length-1:index-1);
        highlight(options[Math.max(0,Math.min(next,options.length-1))]);
      }
    });
    list.addEventListener('mousedown',event=>{
      if (event.target.closest('[role="option"]')) event.preventDefault();
    });
    list.addEventListener('click',event=>{
      const option=event.target.closest('[role="option"]');
      if (option) choose(option);
    });
    document.addEventListener('pointerdown',event=>{
      if (!list.hidden && !picker.contains(event.target) && !list.contains(event.target)) setExpanded(false);
    });
    editor.addEventListener('scroll',positionList);
    window.addEventListener('resize',positionList);
    return ()=>setExpanded(false);
  }

  async function loadPanel(){
    const response=await fetch('/assets/vendor/maw/ass-panel.html?v=20261118-panel-fit-ass-font-picker');
    if(!response.ok) throw Error('ASS 面板加载失败');
    host.innerHTML=await response.text();
    panel=$('#ass-style-window');
    new ResizeObserver(()=>{
      if (kind==='style' && panel.classList.contains('show')) scheduleSample();
    }).observe($('.ass-row-preview-stage'));
    $('#ass-profile-new').onclick=addProfile;
    $('#ass-style-delete').onclick=()=>{
      const item=entry();
      if(kind!=='profile'||item.builtin)return;
      library.assProfiles=library.assProfiles.filter(profile=>profile.id!==selected);
      if (library.assignments.assExportProfileId===selected) {
        library.assignments.assExportProfileId=library.assProfiles[0].id;
      }
      selected=library.assProfiles[0].id;
      changed();
    };
    $('#ass-style-reset').onclick=()=>{
      if(kind!=='profile'||!entry().builtin)return;
      library.assProfiles=library.assProfiles.map(item=>item.id===selected?clone(U.ASS_DEFAULT_PROFILE):item);
      changed();
    };
    panel.addEventListener('input',event=>{const input=event.target,item=entry();if(!item||!input.validity?.valid)return;let value=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value;
      if(input.dataset.assStyleField&&kind==='style')item[input.dataset.assStyleField]=value;
      else if(input.dataset.assProfileField&&kind==='profile')item[input.dataset.assProfileField]=value;
      else if(input.dataset.assAnimation&&kind==='profile'){const [g,k]=input.dataset.assAnimation.split('.');item.animations[g][k]=value;}
      else return;changed();
    });
    $('#ass-ass-default-profile').onchange=()=>{library.assignments.assExportProfileId=$('#ass-ass-default-profile').value;changed();};
    $('#maw-ass-scope').onchange=preview;
    $('#ass-style-save').onclick=()=>work(async()=>{if(!valid())return;await bridge.saveLibrary(result());status('样式库已保存到本机');});
    const load=document.createElement('button');load.type='button';load.textContent='载入样式库';load.id='maw-ass-library-load';$('#ass-style-save').before(load);
    load.onclick=()=>work(async()=>{
      const saved=(await bridge.getLibrary())['MAW 样式库'];
      if(!saved?.maw_library) throw Error('尚未保存 MAW 样式库');
      library=libraryForProfile(saved);
      kind='style';
      selected=ROW_STYLES[0].id;
      changed();
      status('已载入样式库 · 应用后写入项目版本');
    });
    $('#maw-ass-apply').onclick=()=>work(async()=>{if(!valid())return;preview();await bridge.applyDraft();hidePanel();});
    $('#maw-ass-export').onclick=()=>{if(valid()){preview();bridge.exportDraft();}};
    $('#ass-style-window-close').onclick=close;$('#ass-style-window-close-footer').onclick=close;
    panel.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close();}});
    closeFontList=wireFontPicker();
    const handle=$('#ass-style-drag-handle');handle.onpointerdown=e=>{if(e.button||e.target.closest('button'))return;const r=panel.getBoundingClientRect();drag={id:e.pointerId,x:e.clientX-r.left,y:e.clientY-r.top};handle.setPointerCapture(e.pointerId);e.preventDefault();};
    handle.onpointermove=e=>{if(drag?.id!==e.pointerId)return;panel.style.left=Math.max(8,Math.min(innerWidth-panel.offsetWidth-8,e.clientX-drag.x))+'px';panel.style.top=Math.max(8,Math.min(innerHeight-panel.offsetHeight-8,e.clientY-drag.y))+'px';panel.style.transform='none';};
    handle.onpointerup=handle.onpointercancel=()=>{drag=null;};
  }
  const ready=loadPanel();ready.catch(e=>console.error('ASS panel:',e));
  return {async open() {
    if(opening||saving)return;
    opening=true;
    try {
      await ready;
      project=state.projectId;
      const scope=state.cueSelection.ids.size?'selection':'project';
      base=await bridge.openDraft(scope);
      if (project!==state.projectId) throw Error('项目已切换，请重新打开样式库');
      library=libraryForProfile(base);
      kind='style';
      selected=ROW_STYLES[0].id;
      $('#maw-ass-scope').value=scope;
      panel.classList.add('show');
      panel.setAttribute('aria-hidden','false');
      render();
      status('修改后实时预览 · 应用到字幕后写入版本');
      $('#ass-style-window-close').focus();
    } catch(error) {
      bridge.cancelDraft();
      console.error(error);
      status(error.message);
    } finally {
      opening=false;
    }
  }};
}
