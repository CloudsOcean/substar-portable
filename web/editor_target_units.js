(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.SubstarTargetUnits=api;
})(typeof globalThis==='object'?globalThis:this,function(){
  'use strict';
  const copy=value=>JSON.parse(JSON.stringify(value));
  function all(document){
    const found=new Map();
    for(const cue of document?.cues||[])for(const unit of cue.mapping?.target_units||[])found.set(unit.unit_id,copy(unit));
    return [...found.values()].sort((a,b)=>a.start-b.start||a.unit_id.localeCompare(b.unit_id));
  }
  function install(document,units,provenance){
    const owners=new Map(),linked=new Map();
    for(const unit of units){
      const owner=unit.source_cue_ids.find(id=>document.cues.some(c=>c.cue_id===id&&c.state==='active'))||unit.source_cue_ids[0];
      owners.set(owner,[...(owners.get(owner)||[]),unit]);
      for(const id of unit.source_cue_ids)linked.set(id,[...(linked.get(id)||[]),unit]);
    }
    document.cues=document.cues.map(cue=>{
      const mapping={...cue.mapping},wasLinked=Boolean(mapping.target_unit_ids?.length);
      delete mapping.target_units;delete mapping.target_unit_ids;
      const entries=(linked.get(cue.cue_id)||[]).sort((a,b)=>a.start-b.start||a.unit_id.localeCompare(b.unit_id));
      let target=cue.target;
      if(entries.length){
        mapping.target_unit_ids=entries.map(u=>u.unit_id);
        if(owners.has(cue.cue_id))mapping.target_units=copy(owners.get(cue.cue_id));
        const previous=target;
        target={...target,target_text:entries.map(u=>u.text).join('\n'),language:entries[0].language||'zh-CN',
          original_text:target?.original_text??null,provenance,
          translation_status:entries.some(u=>u.needs_review)?'needs_review':'translated',
          issue_code:entries.some(u=>u.needs_review)?'source_changed':null,editable:true};
        if(previous&&['target_text','language','translation_status','issue_code'].every(key=>previous[key]===target[key]))target=previous;
      }else if(wasLinked)target=null;
      return {...cue,mapping,target};
    });
    return document;
  }
  function edit(document,payload,provenance){
    let units=all(document),unit=units.find(u=>u.unit_id===payload.unit_id);
    if(!unit)throw Error('译文单元已不存在');
    if(payload.expected_text!==undefined&&unit.text!==payload.expected_text)throw Error('译文已被修改，请刷新');
    if(payload.text!==undefined&&!String(payload.text).trim())units=units.filter(u=>u!==unit);
    else{
      if(payload.text!==undefined)unit.text=String(payload.text).trim();
      for(const edge of ['start','end'])if(payload[edge]!==undefined)unit[edge]=Number(payload[edge]);
      if(unit.start<0||unit.end<=unit.start)throw Error('译文时间范围无效');
      Object.assign(unit,{provenance});if(payload.text!==undefined)unit.needs_review=false;
    }
    return install(document,units,provenance);
  }
  return {all,install,edit};
});
