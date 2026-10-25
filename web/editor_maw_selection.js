/* Independent word selection and consecutive selection runs. Times are milliseconds. */
export function selectionRuns(entries, selected) {
  const runs=[];let run=null;
  for(const entry of entries){
    if(!selected.has(entry.key)){run=null;continue;}
    if(run){run.start=Math.min(run.start,entry.start);run.end=Math.max(run.end,entry.end);}
    else {run={start:entry.start,end:entry.end};runs.push(run);}
  }
  return runs;
}
export function createWordSelection(){
  const keys=new Set();let anchor=null;
  return {keys,
    clear(){keys.clear();anchor=null;},
    select(key,order,{shiftKey=false,ctrlKey=false,metaKey=false}={}){
      if(shiftKey&&anchor!=null&&order.includes(anchor)){
        const a=order.indexOf(anchor),b=order.indexOf(key);if(b<0)return;
        if(!ctrlKey&&!metaKey)keys.clear();
        order.slice(Math.min(a,b),Math.max(a,b)+1).forEach(k=>keys.add(k));
      }else{
        if(ctrlKey||metaKey){if(keys.has(key))keys.delete(key);else keys.add(key);}
        else {keys.clear();keys.add(key);}
        anchor=key;
      }
    }
  };
}
