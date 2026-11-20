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

export function createCutPlanPanel(h) {
  const {root, signal} = h;
  if (!root) return null;
  const doc = root.ownerDocument;
  const gapsCore = globalThis.AsrGapRemoveCore;
  const cut = globalThis.EditorCutTimeline;

  function element(tag, text, className) {
    const node = doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  const planLabel = element('label', '剪辑方案', 'cut-plan-label');
  const planSelect = h.planSelect;
  planSelect.id = 'cut-plan-select';
  planLabel.htmlFor = planSelect.id;
  planLabel.append(planSelect);
  root.prepend(planLabel);

  const review = element('section', null, 'tool-section cut-plan-review');
  const summary = element('p');
  summary.id = 'cut-plan-summary';
  const message = element('p', null, 'tool-feedback');
  message.hidden = true;
  message.setAttribute('role', 'status');
  message.setAttribute('aria-live', 'polite');
  const list = element('div', null, 'cut-change-list');
  review.append(summary, message, list);
  root.append(review);
  let stopped = false;
  signal.addEventListener('abort', () => { stopped = true; }, {once:true});

  function render() {
    const timeline = h.getTimeline();
    const plan = timeline.plans.find(item => item.id === timeline.active_plan_id);
    const entries = timeline.plans.map(item => [item.id, item.name || '未命名方案']);
    if (!plan) entries.unshift(['', '未选择方案']);
    if (planSelect.options.length !== entries.length || entries.some(([id, name], i) =>
      planSelect.options[i].value !== id || planSelect.options[i].textContent !== name
    )) {
      planSelect.replaceChildren(...entries.map(([id, name]) => {
        const option = element('option', name);
        option.value = id;
        option.disabled = !id;
        return option;
      }));
    }
    planSelect.value = plan?.id || '';
    planSelect.disabled = !timeline.plans.length;
    const duration = h.getDuration() / 1000;
    const spans = cut.kept(plan, duration);
    const remaining = spans.reduce((total, span) => total + span.end - span.start, 0);
    summary.textContent = `原片 ${duration.toFixed(1)} 秒 · 预计保留 ${remaining.toFixed(1)} 秒`;
    list.replaceChildren();

    const gaps = gapsCore.getGapRemoveDisplayGaps(h.getGapState().gaps);
    if (!gaps.length) {
      list.append(element('p', '当前方案没有空隙。', 'muted'));
      return;
    }
    for (const gap of gaps) {
      const row = element('div', null, 'cut-change-row');
      const time = `${(gap.start / 1000).toFixed(2)}–${(gap.end / 1000).toFixed(2)}`;
      const locate = element('button', time, 'cut-change-locate');
      locate.type = 'button';
      locate.title = `定位此区段（${time} 秒）`;
      locate.setAttribute('aria-label', locate.title);
      locate.addEventListener('click', () => h.locate(gap.start / 1000), {signal});
      const toggle = element('button', gap.removed === false ? '重新移除' : '恢复内容');
      toggle.type = 'button';
      toggle.addEventListener('click', async () => {
        toggle.disabled = true;
        message.textContent = '';
        message.hidden = true;
        try {
          await h.manualGap(gap.start, gap.end, gap.removed === false);
        } catch (error) {
          if (!stopped) {
            message.textContent = error.message || '保存失败，请重试';
            message.hidden = false;
            h.show(message.textContent);
          }
        } finally {
          if (!stopped) render();
        }
      }, {signal});
      row.append(locate, toggle);
      list.append(row);
    }
  }

  return {render};
}
