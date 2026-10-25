/* Original MAW ASS template and utilities, connected to Substar's revision boundary. */
import {createUtilsModule as styleUtils} from './vendor/maw/shared/utils/ass-style.js';
import {createUtilsModule as animationUtils} from './vendor/maw/shared/utils/ass-animation.js';
const clone=value=>JSON.parse(JSON.stringify(value));
const U=styleUtils({cloneJsonValue:clone,getNavigator:()=>navigator});
const A=animationUtils(U);
export function createMawAssPanel({bridge,state,api}) {
  const host=document.createElement('div'); host.className='maw-waveform-host maw-ass-host';document.body.append(host);
  let panel,library,base,kind='style',selected='ass',opening=false,saving=false,project='',drag=null;
  const $=s=>host.querySelector(s);
  const status=text=>{if($('#ass-style-library-status')) $('#ass-style-library-status').textContent=text;};
  function fromStyle(style,id,name) {
    return U.normalizeAssStyle({id,name,fontName:style.font,fontSize:style.size,primaryColor:style.color,secondaryColor:style.highlight,
      outlineColor:style.outline_color,outline:style.outline,shadow:style.shadow,bold:style.bold,italic:style.italic,underline:style.underline,strikeOut:style.strike_out,
      borderStyle:style.background?3:(style.border_style||1),backColor:style.back_color||'#000000',outlineOpacity:style.outline_opacity??100,backOpacity:style.back_opacity??50,
      alignment:style.alignment,marginL:style.margin_l??style.margin_x,marginR:style.margin_r??style.margin_x,marginV:style.margin_y,
      scaleX:style.scale_x??100,scaleY:style.scale_y??100,spacing:style.spacing||0,angle:style.angle||0});
  }
  function toStyle(style,profile) {
    return {font:style.fontName,size:style.fontSize,color:style.primaryColor,highlight:style.secondaryColor,outline_color:style.outlineColor,
      outline:style.outline,shadow:style.shadow,bold:style.bold,italic:style.italic,underline:style.underline,strike_out:style.strikeOut,
      background:false,border_style:style.borderStyle,back_color:style.backColor,outline_opacity:style.outlineOpacity,back_opacity:style.backOpacity,
      alignment:style.alignment,margin_l:style.marginL,margin_r:style.marginR,margin_y:style.marginV,margin_x:style.marginL,
      scale_x:style.scaleX,scale_y:style.scaleY,spacing:style.spacing,angle:style.angle,fade_ms:0,
      override_tags:A.assAnimationOverrideTags(profile,{style})};
  }
  function result() {
    const next=clone(base),profile=U.assProfileForId(library,library.assignments.assExportProfileId);
    const main=U.assStyleForId(library,profile.styleId),secondary=U.assStyleForId(library,library.assignments.assExtensionStyleId);
    next.name=profile.name; next.maw_library=clone(library);
    next.layers.slice(0,2).forEach((layer,index)=>{
      const id='maw_'+layer.id;
      next.styles[id]=toStyle(index===0?main:secondary,profile);layer.style_id=id;
    });
    const used=new Set(next.layers.map(x=>x.style_id));
    for(const key of Object.keys(next.styles))if(!used.has(key))delete next.styles[key];
    return next;
  }
  function preview(){if(project!==state.projectId)return;library=U.normalizeAssStyleLibrary(library);bridge.setDraft(result(),$('#maw-ass-scope').value);status('修改待应用 · 播放可预览');}
  function entry(){return (kind==='style'?library.styles:library.assProfiles).find(x=>x.id===selected);}
  function select(k,id){kind=k;selected=id;render();$('.ass-style-editor').scrollTop=0;}
  function options(select,items,value){select.replaceChildren(...items.map(item=>new Option(item.name,item.id)));select.value=value;}
  function renderList(selector,items,k){const list=$(selector);list.replaceChildren(...items.map(item=>{
    const b=document.createElement('button');b.type='button';b.setAttribute('role','option');b.dataset.assSelectionId=item.id;b.textContent=item.name;
    b.setAttribute('aria-selected',String(kind===k&&selected===item.id));b.onclick=()=>select(k,item.id);
    if(item.builtin){const badge=document.createElement('span');badge.className='ass-style-list-badge';badge.textContent='内置';b.append(badge);}return b;
  }));}
  function renderSample(style) {
    const canvas=$('#ass-style-preview-sample'),ctx=canvas.getContext('2d');canvas.width=740;canvas.height=180;
    ctx.clearRect(0,0,740,180);ctx.save();ctx.translate(370,90);ctx.rotate(-style.angle*Math.PI/180);ctx.scale(style.scaleX/100,style.scaleY/100);
    ctx.font=`${style.italic?'italic ':''}${style.bold?'bold ':''}${Math.min(90,style.fontSize)}px "${style.fontName.replaceAll('"','')}"`;
    ctx.textAlign='center';ctx.textBaseline='middle';const text='ASS 字幕预览 OwO';
    const width=ctx.measureText(text).width;
    if(style.borderStyle===3){ctx.fillStyle=U.assCssColorWithOpacity(style.outlineColor,style.outlineOpacity);ctx.fillRect(-width/2-style.outline,-style.fontSize/2-style.outline,width+style.outline*2,style.fontSize+style.outline*2);}
    ctx.shadowColor=U.assCssColorWithOpacity(style.backColor,style.backOpacity);ctx.shadowBlur=0;ctx.shadowOffsetX=style.shadow;ctx.shadowOffsetY=style.shadow;
    if(style.borderStyle===1&&style.outline){ctx.strokeStyle=U.assCssColorWithOpacity(style.outlineColor,style.outlineOpacity);ctx.lineWidth=style.outline*2;ctx.lineJoin='round';ctx.strokeText(text,0,0);}
    ctx.fillStyle=style.primaryColor;ctx.fillText(text,0,0);ctx.restore();
  }
  function render(){
    const current=entry();if(!current){kind='style';selected=library.styles[0].id;return render();}
    renderList('#ass-style-list',library.styles,'style');renderList('#ass-profile-list',library.assProfiles,'profile');
    $('#ass-style-count').textContent=library.styles.length;$('#ass-profile-count').textContent=library.assProfiles.length;
    options($('#ass-ass-default-profile'),library.assProfiles,library.assignments.assExportProfileId);
    options($('#ass-profile-style-id'),library.styles,kind==='profile'?current.styleId:library.assProfiles[0].styleId);
    options($('#ass-profile-extension-style'),library.styles,library.assignments.assExtensionStyleId);
    $('#ass-profile-extension-style-field').hidden=base.layers.length<2;
    $('#ass-style-form').hidden=kind!=='style';$('#ass-profile-form').hidden=kind!=='profile';
    const slot=kind==='style'?$('#ass-style-delete-slot'):$('#ass-profile-delete-slot');slot.append($('#ass-style-delete'),$('#ass-style-reset'));
    $('#ass-style-delete').hidden=!!current.builtin;$('#ass-style-reset').hidden=!current.builtin;
    $('#ass-style-form-title').textContent=kind==='style'?current.name:'样式';$('#ass-profile-form-title').textContent=kind==='profile'?current.name:'输出方案';
    $('#ass-style-builtin-badge').hidden=!current.builtin;$('#ass-profile-builtin-badge').hidden=!current.builtin;
    for(const input of panel.querySelectorAll('[data-ass-style-field],[data-ass-profile-field],[data-ass-animation]')){
      let value;
      if(input.dataset.assStyleField){if(kind!=='style')continue;value=current[input.dataset.assStyleField];}
      else if(input.dataset.assProfileField){if(kind!=='profile')continue;value=current[input.dataset.assProfileField];}
      else {if(kind!=='profile')continue;const [g,k]=input.dataset.assAnimation.split('.');value=current.animations[g][k];}
      if(document.activeElement!==input){if(input.type==='checkbox')input.checked=!!value;else if(input.type==='radio')input.checked=String(value)===input.value;else input.value=value??'';}
    }
    if(kind==='style')renderSample(current);
    else $('#ass-profile-preview-summary').textContent=A.assAnimationOverrideTags(current)||'无逐句动画';
    $('#maw-ass-scope option[value=selection]').textContent=`选中的字幕（${state.cueSelection.ids.size}）`;
    if(kind==='style'){
      $('#ass-style-outline-color-label').textContent=current.borderStyle===3?'底框颜色':'描边颜色';
      $('#ass-style-outline-label').textContent=current.borderStyle===3?'底框宽度':'描边宽度';
      $('#ass-style-outline-opacity-label').textContent=current.borderStyle===3?'底框不透明度':'描边不透明度';
    }
  }
  function changed(){library=U.normalizeAssStyleLibrary(library);render();preview();}
  function valid(){return [...panel.querySelectorAll('input,select')].filter(x=>!x.closest('[hidden]')).every(x=>x.reportValidity());}
  async function work(fn){if(saving)return;saving=true;panel.setAttribute('aria-busy','true');panel.querySelectorAll('button').forEach(b=>b.disabled=true);try{if(project!==state.projectId)throw Error('项目已切换，请重新打开面板');await fn();}catch(e){status(e.message||String(e));}finally{saving=false;panel.removeAttribute('aria-busy');panel.querySelectorAll('button').forEach(b=>b.disabled=false);}}
  function close(){if(saving)return;panel.classList.remove('show');panel.setAttribute('aria-hidden','true');bridge.cancelDraft();document.querySelector('#experimental-subtitle-panel-open')?.focus();}
  function add(k,duplicate=false){const collection=k==='style'?library.styles:library.assProfiles;if(collection.length>=64){status('最多支持 64 个条目');return;}
    const id=(k==='style'?'style-':'profile-')+crypto.randomUUID().slice(0,12),source=duplicate?entry():(k==='style'?U.ASS_DEFAULT_ASS_STYLE:U.ASS_DEFAULT_PROFILE);
    const item={...clone(source),id,builtin:false,name:duplicate?source.name+' 副本':k==='style'?'新样式':'新方案'};collection.push(item);kind=k;selected=id;changed();}
  async function loadPanel(){
    const response=await fetch('/assets/vendor/maw/ass-panel.html');if(!response.ok)throw Error('ASS 面板加载失败');host.innerHTML=await response.text();panel=$('#ass-style-window');
    $('#ass-style-new').onclick=()=>add('style');$('#ass-style-duplicate').onclick=()=>add('style',true);$('#ass-profile-new').onclick=()=>add('profile');
    $('#ass-style-delete').onclick=()=>{const item=entry();if(item.builtin)return;const key=kind==='style'?'styles':'assProfiles';library[key]=library[key].filter(x=>x.id!==selected);library=U.normalizeAssStyleLibrary(library);selected=library[key][0].id;changed();};
    $('#ass-style-reset').onclick=()=>{const defaults=U.defaultAssStyleLibrary(),key=kind==='style'?'styles':'assProfiles',original=defaults[key].find(x=>x.id===selected);if(original){library[key]=library[key].map(x=>x.id===selected?clone(original):x);changed();}};
    panel.addEventListener('input',event=>{const input=event.target,item=entry();if(!item||!input.validity?.valid)return;let value=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value;
      if(input.dataset.assStyleField&&kind==='style')item[input.dataset.assStyleField]=value;
      else if(input.dataset.assProfileField&&kind==='profile')item[input.dataset.assProfileField]=value;
      else if(input.dataset.assAnimation&&kind==='profile'){const [g,k]=input.dataset.assAnimation.split('.');item.animations[g][k]=value;}
      else return;changed();
    });
    $('#ass-ass-default-profile').onchange=()=>{library.assignments.assExportProfileId=$('#ass-ass-default-profile').value;changed();};
    $('#ass-profile-extension-style').onchange=()=>{library.assignments.assExtensionStyleId=$('#ass-profile-extension-style').value;changed();};
    $('#maw-ass-scope').onchange=preview;
    $('#ass-style-save').onclick=()=>work(async()=>{if(!valid())return;await bridge.saveLibrary(result());status('样式库已保存到本机');});
    const load=document.createElement('button');load.type='button';load.textContent='载入样式库';load.id='maw-ass-library-load';$('#ass-style-save').before(load);
    load.onclick=()=>work(async()=>{const saved=(await bridge.getLibrary())['MAW 样式库'];if(!saved?.maw_library)throw Error('尚未保存 MAW 样式库');library=U.normalizeAssStyleLibrary(saved.maw_library);kind='style';selected=U.assProfileForId(library,library.assignments.assExportProfileId).styleId;changed();status('已载入保存的样式库；应用后写入项目版本');});
    $('#maw-ass-apply').onclick=()=>work(async()=>{if(!valid())return;preview();await bridge.applyDraft();panel.classList.remove('show');panel.setAttribute('aria-hidden','true');});
    $('#maw-ass-export').onclick=()=>{if(valid()){preview();bridge.exportDraft();}};
    $('#ass-style-window-close').onclick=close;$('#ass-style-window-close-footer').onclick=close;
    panel.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close();}});
    $('#ass-style-local-font-scan').onclick=()=>work(async()=>{const fonts=await api('/api/ass/fonts');const names=Object.keys(fonts.available||{});const list=document.createElement('datalist');list.id='maw-ass-fonts';for(const name of names)list.append(new Option(name,name));$('#maw-ass-fonts')?.remove();panel.append(list);$('#ass-style-font-name').setAttribute('list',list.id);status(`已载入 ${names.length} 种可用字体`);});
    $('#ass-style-font-toggle').onclick=()=>$('#ass-style-local-font-scan').click();
    const handle=$('#ass-style-drag-handle');handle.onpointerdown=e=>{if(e.button||e.target.closest('button'))return;const r=panel.getBoundingClientRect();drag={id:e.pointerId,x:e.clientX-r.left,y:e.clientY-r.top};handle.setPointerCapture(e.pointerId);e.preventDefault();};
    handle.onpointermove=e=>{if(drag?.id!==e.pointerId)return;panel.style.left=Math.max(8,Math.min(innerWidth-panel.offsetWidth-8,e.clientX-drag.x))+'px';panel.style.top=Math.max(8,Math.min(innerHeight-panel.offsetHeight-8,e.clientY-drag.y))+'px';panel.style.transform='none';};
    handle.onpointerup=handle.onpointercancel=()=>{drag=null;};
  }
  const ready=loadPanel();ready.catch(e=>console.error('ASS panel:',e));
  return {async open(){if(opening||saving)return;opening=true;try{await ready;project=state.projectId;base=await bridge.openDraft();library=base.maw_library?U.normalizeAssStyleLibrary(base.maw_library):U.defaultAssStyleLibrary();
    if(!base.maw_library){base.layers.slice(0,2).forEach((layer,i)=>{const id=i?'ass-extension':'ass';const style=fromStyle(base.styles[layer.style_id],id,layer.name);library.styles=library.styles.map(x=>x.id===id?{...style,builtin:true}:x);});}
    kind='style';selected=U.assProfileForId(library,library.assignments.assExportProfileId).styleId;
    $('#maw-ass-scope').value=state.cueSelection.ids.size?'selection':'project';render();panel.classList.add('show');panel.setAttribute('aria-hidden','false');status('修改后播放预览；应用到字幕后写入版本');$('#ass-style-window-close').focus();
  }catch(e){bridge.cancelDraft();console.error(e);status(e.message);}finally{opening=false;}}};
}
