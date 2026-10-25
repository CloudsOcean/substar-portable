(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorCutTimeline = api;
})(typeof globalThis === "object" ? globalThis : this, function() {
  "use strict";
  const clone = value => JSON.parse(JSON.stringify(value));
  const id = () => globalThis.crypto.randomUUID();
  const clamp = (n,a,b) => Math.max(a,Math.min(b,n));
  function union(ranges, duration=Infinity) {
    const result=[];
    for (const r of [...ranges].sort((a,b)=>a.start-b.start)) {
      const a=Math.max(0,r.start), b=Math.min(duration,r.end);
      if (!(b>a)) continue;
      if (result.length && a<=result.at(-1).end) result.at(-1).end=Math.max(b,result.at(-1).end);
      else result.push({start:a,end:b});
    }
    return result;
  }
  function kept(plan,duration) {
    const cuts=union((plan?.cuts||[]).filter(c=>c.enabled!==false),duration), result=[];
    for (const r of union(plan?.keep ?? [{start:0,end:duration}],duration)) {
      let cursor=r.start;
      for(const cut of cuts) {
        if(cut.end<=cursor || cut.start>=r.end) continue;
        if(cut.start>cursor) result.push({start:cursor,end:Math.min(cut.start,r.end)});
        cursor=Math.min(r.end,Math.max(cursor,cut.end));
      }
      if(cursor<r.end) result.push({start:cursor,end:r.end});
    }
    return result;
  }
  function selectionRanges(units, selected) {
    const ranges=[]; let run=null;
    for(const unit of units) {
      if(!selected.has(unit.id)) { run=null; continue; }
      if(run && run.speaker===unit.speaker) {
        run.end=Math.max(run.end,unit.end);run.source_ids.push(unit.id);
      } else {
        run={start:unit.start,end:unit.end,speaker:unit.speaker,source_ids:[unit.id]};ranges.push(run);
      }
    }
    return ranges;
  }
  return {union,kept,selectionRanges};
});
