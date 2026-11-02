// Retain the waveform canvas and subtitle nodes when only gaps change.
const rowSignatures=new WeakMap();
export function refreshGapRows(wave,gaps,mode){
  for(const row of wave.renderedRows){
    const start=Number(row.dataset.startMs),end=Number(row.dataset.endMs);
    let lo=0,hi=gaps.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(gaps[mid].end<=start)lo=mid+1;else hi=mid;}
    const first=lo,visible=[];
    for(let i=first;i<gaps.length&&gaps[i].start<end;i++)visible.push(gaps[i]);
    const signature=JSON.stringify([mode,visible.length?first:0,visible]);
    if(rowSignatures.get(row)===signature)continue;
    row.querySelectorAll('.waveform-gap-block').forEach(node=>node.remove());
    wave.appendGapBlocks(row,start,end);
    rowSignatures.set(row,signature);
  }
  wave.positionPlayheads();
}
