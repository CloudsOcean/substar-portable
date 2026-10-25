/* Pure conversion: every AI edit becomes a source-clock gap in a separate plan. */
export function proposalTimeline(value,response,durationMs,C,M,cut){
  if(!(durationMs>0))throw Error('媒体时长尚未加载');
  const plan=M.clone(response.plan),duration=durationMs/1000;
  if(!plan?.id||!Array.isArray(plan.cuts))throw Error('AI 返回的方案格式无效');
  if(value.plans.some(p=>p.id===plan.id))throw Error('AI 方案编号已存在');
  const kept=cut.kept(plan,duration);if(!kept.length)throw Error('生成方案没有保留内容');
  const ranges=[];let cursor=0;
  for(const item of kept){if(item.start>cursor)ranges.push({start:Math.round(cursor*1000),end:Math.round(item.start*1000),removed:true});cursor=Math.max(cursor,item.end);}
  if(cursor<duration)ranges.push({start:Math.round(cursor*1000),end:durationMs,removed:true});
  const changes=ranges.map(g=>({start:g.start/1000,end:g.end/1000,reason:plan.cuts.filter(c=>c.start<g.end/1000&&c.end>g.start/1000).map(c=>c.reason).filter(Boolean).join('；')||'未纳入本方案的保留内容'}));
  const provenance=C.replaceGapRemoveProvenanceSource(null,'ai_cleanup',ranges,[]);
  const gaps=C.normalizeGapRemoveData({provenance,gaps:C.gapRangesFromProvenance(provenance)});
  const next=M.timeline(value);plan.source_revision_id=response.revision_id;plan.keep=null;plan.ai_changes=changes;next.plans.push(plan);next.active_plan_id=plan.id;
  for(const marker of response.markers||[])next.markers.push({...M.clone(marker),plan_id:plan.id});
  return M.withGapState(next,gaps);
}

export function createCutPlanPanel(h){
  const {root,signal,options}=h;if(!root)return null;
  const doc=root.ownerDocument,C=globalThis.AsrGapRemoveCore,M=globalThis.SubstarMawModel,cut=globalThis.EditorCutTimeline;
  const element=(tag,text,cls)=>{const el=doc.createElement(tag);if(text)el.textContent=text;if(cls)el.className=cls;return el;};
  const group=element('section',null,'tool-section cut-ai-section');
  const title=element('h4','生成剪辑方案');
  const label=element('label','剪辑要求');const goal=element('textarea');goal.id='cut-ai-goal';goal.rows=4;goal.maxLength=4000;goal.placeholder='例如：保留核心观点，删去重复和明显停顿，保持原顺序。';label.append(goal);
  const reference=element('label',null,'tool-check');const useCurrent=element('input');useCurrent.type='checkbox';useCurrent.checked=true;reference.append(useCurrent,doc.createTextNode('参考当前方案继续调整'));
  const generate=element('button','生成新方案','tool-primary');generate.type='button';generate.id='cut-ai-generate';
  const message=element('p',null,'tool-feedback');message.id='cut-ai-status';message.setAttribute('role','status');message.setAttribute('aria-live','polite');
  group.append(title,label,reference,generate,message);root.querySelector('h3').after(group);
  const review=element('section',null,'tool-section cut-plan-review');const summary=element('p');summary.id='cut-plan-summary';const note=element('p',null,'muted');
  const preview=element('button','按方案试听');preview.type='button';preview.id='cut-plan-preview';preview.addEventListener('click',h.preview,{signal});
  const list=element('div',null,'cut-change-list');review.append(element('h4','方案内容'),summary,note,preview,list);root.append(review);
  let running=false,stopped=false;signal.addEventListener('abort',()=>{stopped=true;},{once:true});
  generate.addEventListener('click',async()=>{
    if(running)return;const instruction=goal.value.trim();if(!instruction){message.textContent='请先填写剪辑要求';goal.focus();return;}
    running=true;generate.disabled=true;generate.textContent='正在生成…';message.textContent='正在分析字幕，生成独立方案…';
    try{
      await h.waitQueue();if(stopped)return;
      const revision=options.getRevisionId(),before=JSON.stringify(h.getTimeline()),base=useCurrent.checked?h.getTimeline().active_plan_id:null;
      const result=await options.onAiPlan(instruction,base);if(stopped)return;
      if(options.getRevisionId()!==revision||result.revision_id!==revision||JSON.stringify(h.getTimeline())!==before)throw Error('生成期间工程已变化，请重新生成；当前方案已保留');
      const next=proposalTimeline(h.getTimeline(),result,h.getDuration(),C,M,cut);
      await h.persist(next);if(stopped)return;h.refresh();message.textContent='新方案已保存，可试听、调整或撤销。';
    }catch(error){if(!stopped){message.textContent=error.message||'生成失败，请重试';h.show(message.textContent);}}
    finally{running=false;if(!stopped){generate.disabled=false;generate.textContent='生成新方案';}}
  },{signal});
  function render(){
    const timeline=h.getTimeline(),plan=timeline.plans.find(p=>p.id===timeline.active_plan_id),duration=h.getDuration()/1000;
    const spans=cut.kept(plan,duration),remaining=spans.reduce((total,s)=>total+s.end-s.start,0);
    summary.textContent=`原片 ${duration.toFixed(1)} 秒 · 预计保留 ${remaining.toFixed(1)} 秒`;
    note.textContent=plan?.summary||'保持原顺序；空隙用于跳过内容，原字幕与素材保留。';
    preview.disabled=!duration||!spans.length;list.replaceChildren();
    const gaps=C.getGapRemoveDisplayGaps(h.getGapState().gaps);
    if(!gaps.length){list.append(element('p','当前方案没有空隙。','muted'));return;}
    for(const gap of gaps){
      const row=element('div',null,'cut-change-row'),time=`${(gap.start/1000).toFixed(2)}–${(gap.end/1000).toFixed(2)} 秒`;
      const locate=element('button',time);locate.type='button';locate.title='定位此区段';locate.onclick=()=>h.locate(gap.start/1000);
      const reason=(plan?.ai_changes||[]).filter(c=>c.start<gap.end/1000&&c.end>gap.start/1000).map(c=>c.reason).join('；');
      const toggle=element('button',gap.removed===false?'重新移除':'恢复内容');toggle.type='button';toggle.onclick=async()=>{toggle.disabled=true;try{await h.manualGap(gap.start,gap.end,gap.removed===false);}catch(error){message.textContent=error.message;}finally{if(!stopped)render();}};
      row.append(locate,toggle,element('p',reason||'手工调整或空隙检测'));list.append(row);
    }
  }
  return {render};
}
