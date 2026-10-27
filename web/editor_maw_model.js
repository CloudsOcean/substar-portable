/* Substar source seconds <-> MAW source milliseconds. No editor state mutation. */
(function(root,factory){const api=factory();if(typeof module==='object')module.exports=api;root.SubstarMawModel=api;})(globalThis,function(){
  const clone=v=>JSON.parse(JSON.stringify(v));
  const ms=v=>Math.round(Number(v)*1000), seconds=v=>Number(v)/1000;
  function segments(view,sourceText){
    const tokens=new Map((view?.token_views||[]).map(t=>[t.token_id,t]));
    return (view?.cue_views||[]).map(c=>({
      id:c.cue_id,start:ms(c.start),end:ms(c.end),speaker:c.speaker||'',
      text:sourceText?.(c)??c.source_text??(c.display_token_ids||[]).map(id=>tokens.get(id)?.text||'').join(' '),
      translation:c.target_text??c.target?.target_text??'',disabled:c.state!=='active',subtitle_track:c.subtitle_track||'main',
      manual:c.source_text!=null,
      items:(c.active_display_token_ids||c.display_token_ids||[]).map(id=>tokens.get(id)).filter(t=>t&&Number.isFinite(t.start)&&Number.isFinite(t.end)).map(t=>({id:t.token_id,text:t.text,start:ms(t.start),end:ms(t.end)})),
    })).sort((a,b)=>a.start-b.start||a.end-b.end||a.id.localeCompare(b.id));
  }
  function timeline(value){return clone(value||{markers:[],plans:[],active_plan_id:null});}
  function markers(value){return (value?.markers||[]).map(m=>({...clone(m),start:ms(m.start),...(m.point==null?{}:{point:ms(m.point)}),...(m.end==null?{}:{end:ms(m.end)})})).map(m=>{if(m.end==null)delete m.end;return m;});}
  function storedMarkers(value){return value.map(m=>({...clone(m),start:seconds(m.start),...(m.point==null?{}:{point:seconds(m.point)}),...(m.end==null?{}:{end:seconds(m.end)})}));}
  function markerDrag(marker, mode, target, duration) {
    const next=clone(marker), region=marker.end!=null && marker.end-marker.start>1;
    const point=region?(marker.point??marker.start):marker.start;
    const limit=(v,a,b)=>Math.max(a,Math.min(b,Math.round(v)));
    if(mode==='move') {
      if(region) next.point=limit(target,marker.start,marker.end);
      else {next.start=limit(target,0,Math.max(0,duration-1));delete next.end;delete next.point;}
    } else if(mode==='resize-start') {
      const end=region?marker.end:marker.start;
      next.start=limit(target,0,Math.min(point,end));
      next.end=end;next.point=point;
    } else {
      const end=limit(target,Math.max(point,marker.start),duration);
      next.end=end;next.point=point;
    }
    if(mode!=='move' && next.end!=null && next.end-next.start<=1) {next.start=point;delete next.end;delete next.point;}
    return next;
  }
  function legacyGaps(value){const p=value?.plans?.find(p=>p.id===value.active_plan_id);return (p?.cuts||[]).map(c=>({start:ms(c.start),end:ms(c.end),removed:c.enabled!==false}));}
  function withGapState(value,gapState){
    const next=timeline(value);next.schema='substar.timeline.v2';next.gap_remove=clone(gapState);
    let plan=next.plans.find(p=>p.id===next.active_plan_id);
    if(!plan){plan={id:'maw-source-edit',name:'当前剪辑',keep:null,cuts:[]};next.plans.push(plan);next.active_plan_id=plan.id;}
    // The export projection remains compatible with the existing Substar exporters.
    plan.cuts=gapState.gaps.map((g,i)=>({id:`maw-gap-${i}`,start:seconds(g.start),end:seconds(g.end),enabled:g.removed!==false,source:'maw'}));
    plan.gap_remove=clone(gapState);
    return next;
  }
  function timeChanges(before,after){const old=new Map(before.map(s=>[s.id,s]));return after.filter(s=>{const b=old.get(s.id);return b&&(b.start!==s.start||b.end!==s.end);}).map(s=>({cue_id:s.id,start:seconds(s.start),end:seconds(s.end)}));}
  function wordTimeChanges(before,after){
    const byId=new Map(after.map(s=>[s.id,s])),cues=[],wordTimings={};
    for(const original of before){
      const segment=byId.get(original.id);if(!segment)continue;
      const words=new Map(original.items.map(item=>[item.id,item]));let changed=false;
      for(const item of segment.items){const old=words.get(item.id);
        if(old&&(old.start!==item.start||old.end!==item.end)){
          wordTimings[item.id]={start:Math.round(item.start),end:Math.round(item.end)};changed=true;
        }
      }
      if(changed)cues.push({cue_id:segment.id,start:seconds(segment.start),end:seconds(segment.end)});
    }
    return {cues,wordTimings};
  }
  function speakerLanes(items){const ends=[],result=new Map();for(const s of items){let lane=ends.findIndex(end=>end<=s.start);if(lane<0)lane=ends.length;ends[lane]=s.end;result.set(s.id,lane);}return {lanes:result,count:Math.max(1,ends.length)};}
  return {wordTimeChanges,markerDrag,clone,ms,seconds,segments,timeline,markers,storedMarkers,legacyGaps,withGapState,timeChanges,speakerLanes};
});
