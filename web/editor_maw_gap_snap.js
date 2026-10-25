/* Gap snapping follows the visible editing granularity. All values are milliseconds. */
export function gapSnapCandidates(segments, wordsVisible) {
  const values=[];
  for(const cue of segments || []) {
    if(cue.disabled)continue;
    values.push(cue.start,cue.end);
    if(wordsVisible)for(const word of cue.items || [])values.push(word.start,word.end);
  }
  return [...new Set(values.filter(Number.isFinite))].sort((a,b)=>a-b);
}
export function snapGapBoundary(value, boundaries, threshold=80, bypass=false) {
  if(bypass || !Number.isFinite(value) || !boundaries.length)return value;
  let lo=0,hi=boundaries.length;
  while(lo<hi){const mid=(lo+hi)>>>1;if(boundaries[mid]<value)lo=mid+1;else hi=mid;}
  let nearest=value,distance=Infinity;
  for(const point of [boundaries[lo-1],boundaries[lo]])if(Number.isFinite(point)&&Math.abs(point-value)<distance){nearest=point;distance=Math.abs(point-value);}
  return distance<=threshold?nearest:value;
}
export function createGapBoundarySnapper(getSegments,getWordsVisible) {
  let previousSegments,previousWords,boundaries=[];
  return (value,{event,row,altIsGesture=false}={})=>{
    const segments=getSegments(),words=Boolean(getWordsVisible());
    if(segments!==previousSegments||words!==previousWords){boundaries=gapSnapCandidates(segments,words);previousSegments=segments;previousWords=words;}
    const span=Number(row?.dataset?.endMs)-Number(row?.dataset?.startMs),width=row?.getBoundingClientRect?.().width;
    const threshold=span>0&&width>0?Math.min(120,span/width*8):80;
    return snapGapBoundary(value,boundaries,threshold,Boolean(event?.altKey)&&!altIsGesture);
  };
}

export function createMarkerBoundarySnapper(getSegments,getWordsVisible){
  return (value,{event,row}={})=>{
    const entries=getSegments().filter(s=>!s.disabled).flatMap(s=>getWordsVisible()&&s.items?.length?s.items:[s]);
    const boundaries=[...new Set(entries.flatMap(s=>[s.start,s.end]).filter(Number.isFinite))].sort((a,b)=>a-b);
    const span=Number(row?.dataset?.endMs)-Number(row?.dataset?.startMs),width=row?.getBoundingClientRect?.().width;
    const threshold=span>0&&width>0?Math.min(120,span/width*8):80;
    return snapGapBoundary(value,boundaries,threshold,Boolean(event?.altKey));
  };
}
