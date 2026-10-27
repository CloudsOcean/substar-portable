(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;if(root)root.EditorCueListView=api;})(typeof globalThis==='object'?globalThis:this,function(){
  'use strict';
  // Fenwick sums: scroll lookup and measured-height changes are logarithmic.
  function createHeightIndex(values){
    const heights=[...values],tree=new Float64Array(values.length+1);
    const add=(i,delta)=>{for(let n=i+1;n<tree.length;n+=n&-n)tree[n]+=delta;};
    heights.forEach((h,i)=>add(i,h));
    const offset=end=>{let sum=0;for(let i=Math.min(end,heights.length);i>0;i-=i&-i)sum+=tree[i];return sum;};
    return {offset,total:()=>offset(heights.length),height:i=>heights[i]||0,
      update(i,height){const delta=height-heights[i];heights[i]=height;add(i,delta);},
      at(y){let i=0,sum=0,step=1;while(step*2<tree.length)step*=2;for(;step;step=Math.floor(step/2)){const next=i+step;if(next<tree.length&&sum+tree[next]<=y){i=next;sum+=tree[next];}}return Math.min(i,Math.max(0,heights.length-1));}
    };
  }
  function pageWindow(count,start,active,size,preserve){start=Math.max(0,start||0);if(!preserve&&(active<start||active>=start+size))start=Math.max(0,active-Math.floor(size/3));return {start:Math.min(start,Math.max(0,count-1)),end:Math.min(count,start+size)};}
  function preservedWindow(count,start,end){return {start:Math.max(0,Math.min(start,count-1)),end:Math.min(count,end)};}
  function createCueListView({container,renderCue,onWindowChange=null,positionSlider=null}){
    if(!container||typeof renderCue!=='function')throw Error('Cue list view requires a container and cue renderer');
    const doc=container.ownerDocument||document;
    const content=doc.createElement('div');content.className='cue-virtual-content';
    content.style.position='relative';content.style.width='100%';
    container.replaceChildren(content);
    // Padding belongs to the viewport, never to offscreen row compensation.
    const css=typeof getComputedStyle==='function'?getComputedStyle(container):{};
    const baseTop=parseFloat(css.paddingTop)||0,baseBottom=parseFloat(css.paddingBottom)||0;
    let context=null,indexById=new Map(),heights=createHeightIndex([]),frame=0,generation=0,dead=false,active=null;
    let range={start:0,end:0},lastWidth=container.clientWidth,sliderDragging=false;
    const measured=new Map(),nodes=new Map();
    const request=fn=>requestAnimationFrame(fn);
    const viewport=()=>Math.max(1,container.clientHeight-baseTop-baseBottom);
    const localTop=()=>Math.max(0,container.scrollTop-baseTop);
    const anchor=()=>{
      if(!context?.cues.length)return null;
      const i=heights.at(localTop());
      return {id:context.cues[i].cue_id,index:i,offset:heights.offset(i)-localTop()};
    };
    function restoreAnchor(a){
      if(!a||!context.cues.length)return;
      const i=indexById.get(a.id)??Math.min(a.index,context.cues.length-1);
      const next=Math.max(0,heights.offset(i)-a.offset+baseTop);
      if(Math.abs(container.scrollTop-next)>.5)container.scrollTop=next;
    }
    function syncPosition(){
      if(!positionSlider||sliderDragging)return;
      const count=context?.cues.length||0;positionSlider.disabled=count<2;
      const i=count?heights.at(localTop()):0;
      let percent=count>1?100*(i+Math.max(0,(localTop()-heights.offset(i))/Math.max(1,heights.height(i))))/(count-1):0;
      if(container.scrollTop<=baseTop)percent=0;
      else if(container.scrollTop+container.clientHeight>=heights.total()+baseTop+baseBottom-2)percent=100;
      positionSlider.value=String(Math.max(0,Math.min(100,percent)));positionSlider.setAttribute('aria-valuetext',`${Math.round(Number(positionSlider.value))}%`);
    }
    function positionRows(){
      content.style.height=`${heights.total()}px`;
      for(const [id,node] of nodes){const i=indexById.get(id);if(i!==undefined){node.style.position='absolute';node.style.top=`${heights.offset(i)}px`;node.style.left='0';node.style.right='0';}}
    }
    function measure(){
      const a=anchor();let changed=false;
      for(const [id,node] of nodes){
        const i=indexById.get(id);if(i===undefined)continue;
        const rect=node.getBoundingClientRect();
        const margin=typeof getComputedStyle==='function'?parseFloat(getComputedStyle(node).marginBottom)||0:0;
        const height=rect.height+margin;if(height<=0)continue;
        measured.set(id,height);
        if(Math.abs(height-heights.height(i))>.5){heights.update(i,height);changed=true;}
      }
      if(changed){positionRows();restoreAnchor(a);}
      return changed;
    }
    const observer=typeof ResizeObserver==='function'?new ResizeObserver(()=>schedule()):null;
    observer?.observe(container);
    function pinned(node){
      if(node.contains(doc.activeElement))return true;
      const selection=doc.getSelection?.();
      return !!selection&&!selection.isCollapsed&&(node.contains(selection.anchorNode)||node.contains(selection.focusNode));
    }
    const reuseOrPatchRow = (existing, next) => {
      if (!existing || existing.dataset.cueId !== next.dataset.cueId) return next;
      next.style.cssText = existing.style.cssText;
      // Selection is applied after rendering, not by the cue renderer. Ignore
      // that presentation-only difference or every save replaces every row.
      next.classList.toggle("cue-selected", existing.classList.contains("cue-selected"));
      const selected = existing.getAttribute("aria-selected");
      if (selected !== null) next.setAttribute("aria-selected", selected);
      // A previous edit's asynchronous save can refresh the list while the user
      // is already typing in another row. Never replace that live textarea:
      // doing so loses focus, selection, composition and its unsaved draft.
      const focus = document.activeElement;
      if ((focus?.matches?.(".inline-token-editor") || focus?.matches?.(".whole-cue-editor")) && existing.contains?.(focus)) {
        // Keep the actual node, including selection and IME composition. A
        // save acknowledgement must never turn DOM removal into a user blur.
        existing.dataset.deferredRefresh = "true";
        return existing;
      }
      const nextTarget = next.querySelector(focus?.matches?.("[data-source-edit]") ? "[data-source-edit]" : "[data-target-edit]");
      if ((focus?.matches?.("[data-target-edit]") || focus?.matches?.("[data-source-edit]")) && existing.contains?.(focus)
          && nextTarget && !nextTarget.disabled) {
        const number = existing.querySelector(".cue-meta strong");
        const nextNumber = next.querySelector(".cue-meta strong");
        if (number && nextNumber) number.textContent = nextNumber.textContent;
        return existing;
      }
      if (existing.isEqualNode(next)) return existing;
      const existingNumber = existing.querySelector(".cue-meta strong");
      const nextNumber = next.querySelector(".cue-meta strong");
      if (!existingNumber || !nextNumber) return next;
      const authoritativeNumber = nextNumber.textContent;
      nextNumber.textContent = existingNumber.textContent;
      const differsOnlyByNumber = existing.isEqualNode(next);
      nextNumber.textContent = authoritativeNumber;
      if (!differsOnlyByNumber) return next;
      existingNumber.textContent = authoritativeNumber;
      return existing;
    };

    function update(force=false){
      if(dead||!context)return;
      const width=container.clientWidth;
      if(width!==lastWidth){lastWidth=width;const a=anchor();measured.clear();heights=createHeightIndex(context.cues.map(()=>104));restoreAnchor(a);}
      const overscan=Math.max(240,viewport());
      range={start:context.cues.length?heights.at(Math.max(0,localTop()-overscan)):0,end:context.cues.length?Math.min(context.cues.length,heights.at(localTop()+viewport()+overscan)+1):0};
      const wanted=new Set(context.cues.slice(range.start,range.end).map(c=>c.cue_id));
      for(const [id,node] of nodes){if(!indexById.has(id)||(!wanted.has(id)&&!pinned(node))){observer?.unobserve(node);node.remove();nodes.delete(id);}}
      for(let i=range.start;i<range.end;i++){
        const cue=context.cues[i],old=nodes.get(cue.cue_id);
        if(old&&!force)continue;
        const next=renderCue(cue,i,context.tokenById),node=reuseOrPatchRow(old,next);
        if(node!==old){if(old){observer?.unobserve(old);old.replaceWith(node);}else content.append(node);nodes.set(cue.cue_id,node);observer?.observe(node);}
      }
      // Keep DOM reading order without removing/reinserting a focused editor.
      const ordered=[...nodes].sort((a,b)=>indexById.get(a[0])-indexById.get(b[0]));
      let before=null;
      for(let i=ordered.length-1;i>=0;i--){const node=ordered[i][1];if(node.nextSibling!==before)content.insertBefore(node,before);before=node;}
      for(const [id,node] of nodes)node.classList.toggle('current',id===active);
      positionRows();const changed=measure();
      onWindowChange?.(range);syncPosition();
      if(changed)schedule();
    }
    function schedule(){if(!frame&&!dead)frame=request(()=>{frame=0;update();});}
    function revealCue(id,{align='center'}={}){
      const i=indexById.get(id);if(i===undefined)return false;
      const ticket=++generation;
      function locate(){
        const height=heights.height(i),offset=align==='start'?0:Math.max(0,(viewport()-height)/2);
        container.scrollTop=Math.max(0,heights.offset(i)-offset+baseTop);
        update();
      }
      locate();let expected=container.scrollTop;
      const settle=remaining=>request(()=>{
        if(dead||ticket!==generation||Math.abs(container.scrollTop-expected)>1)return;
        const node=nodes.get(id);if(!node)return;
        measure();locate();expected=container.scrollTop;
        if(remaining>1)settle(remaining-1);
      });
      settle(2);return true;
    }
    function jumpToPercent(value){
      if(!context?.cues.length)return;
      const percent=Math.max(0,Math.min(100,Number(value)||0));
      revealCue(context.cues[Math.round(percent/100*(context.cues.length-1))].cue_id,{align:'start'});
      if(percent===0)container.scrollTop=0;
      if(percent===100)container.scrollTop=heights.total();
      update();
    }
    const userScroll=()=>{generation++;};
    container.addEventListener('wheel',userScroll,{passive:true});
    container.addEventListener('pointerdown',userScroll);
    container.addEventListener('keydown',userScroll);
    container.addEventListener('scroll',schedule,{passive:true});
    container.addEventListener('focusout',schedule);
    doc.addEventListener?.('selectionchange',schedule);
    if(positionSlider){
      positionSlider.addEventListener('pointerdown',()=>{sliderDragging=true;generation++;});
      positionSlider.addEventListener('input',()=>jumpToPercent(positionSlider.value));
      positionSlider.addEventListener('keydown',event=>{
        const last=(context?.cues.length||0)-1;if(last<=0||!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
        event.preventDefault();const i=Math.round(Number(positionSlider.value)/100*last);
        jumpToPercent(100*(event.key==='Home'?0:event.key==='End'?last:i+(event.key==='ArrowDown'?1:-1))/last);
      });
      for(const name of ['change','pointerup','pointercancel'])positionSlider.addEventListener(name,()=>{sliderDragging=false;syncPosition();});
    }
    function render({cues,tokenById,activeCueId,preservePage=false}){
      if(content.parentNode!==container)container.replaceChildren(content);
      generation++;const a=preservePage?anchor():null;
      // If the anchor was removed, retain the next surviving visible row's offset.
      const ids=new Set(cues.map(c=>c.cue_id));
      if(a&&!ids.has(a.id)&&context){for(let i=a.index+1;i<context.cues.length;i++){const id=context.cues[i].cue_id;if(ids.has(id)){a.offset+=heights.offset(i)-heights.offset(a.index);a.id=id;break;}}}
      context={cues,tokenById};active=activeCueId;indexById=new Map(cues.map((c,i)=>[c.cue_id,i]));
      for(const id of measured.keys())if(!ids.has(id))measured.delete(id);
      heights=createHeightIndex(cues.map(c=>measured.get(c.cue_id)||104));
      positionRows();
      if(a)restoreAnchor(a);else container.scrollTop=0;
      update(true);
      if(!preservePage&&activeCueId)revealCue(activeCueId);
      return range;
    }
    function setActive(id){active=id;for(const [cueId,node] of nodes)node.classList.toggle('current',cueId===id);}
    function destroy(){dead=true;generation++;if(frame)cancelAnimationFrame(frame);observer?.disconnect();container.removeEventListener('scroll',schedule);doc.removeEventListener?.('selectionchange',schedule);}
    return {render,setActive,jumpToPercent,revealCue,destroy};
  }
  return {createCueListView,createHeightIndex,pageWindow,preservedWindow};
});
