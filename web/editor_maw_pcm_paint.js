import {suppressLowEnergy} from './editor_maw_native_style.js?v=20261025-low-energy-1';
/* Pre-MAW PCM window data and physical-pixel max envelope, on current interaction rows. */
// Gaussian smoothing is applied to display pixels only, never the PCM or timings.
export function reducePcmSpikes(values){
  const weights=[1,2,1];
  return values.map((_,i)=>weights.reduce((sum,w,j)=>sum+w*values[Math.max(0,Math.min(values.length-1,i+j-1))],0)/4);
}
export function installPcmWaveformPaint(wave,{loadWindow,getSource,onError,signal,getSuppression=()=>0}){
  const nativePaint=wave.paintWaveRow;
  const windows=new Map(),pending=new Map();let disposed=false;
  signal.addEventListener('abort',()=>{disposed=true;windows.clear();},{once:true});
  wave.paintWaveRow=function(row,ctx,width,height,startMs,endMs,x0,span,flags={}){
    if(getSource()!=='pcm')return nativePaint.call(this,row,ctx,width,height,startMs,endMs,x0,span,flags);
    const start=startMs/1000,end=Math.min(this.durationMs,endMs)/1000,key=`${start}:${end}`;
    if(end>start&&!windows.has(key)&&!pending.has(key)){
      pending.set(key,true);
      Promise.resolve().then(()=>loadWindow({start,end,points:4096})).then(payload=>{
        if(disposed)return;
        windows.set(key,payload);
        while(windows.size>48)windows.delete(windows.keys().next().value);
        this.render();
      }).catch(error=>{if(!disposed)onError?.(error);}).finally(()=>pending.delete(key));
    }
    const colors=this._getWaveColors();
    ctx.fillStyle=colors.rowBg;ctx.fillRect(x0,0,span,height);
    const range=Math.max(1,endMs-startMs),step=range<=10000?1000:range<=30000?2000:5000;
    ctx.strokeStyle=colors.rowBorder;ctx.lineWidth=1;ctx.beginPath();
    for(let t=Math.ceil(startMs/step)*step;t<endMs;t+=step){const x=(t-startMs)/range*width;if(x>=x0&&x<=x0+span){ctx.moveTo(x+.5,0);ctx.lineTo(x+.5,height);}}
    ctx.stroke();
    // Original multiline geometry, with the source gain applied consistently to every row.
    const center=this.settings.mode==='basic'?height/2:83,amplitude=this.settings.mode==='basic'?Math.max(1,height/2-6):77;
    ctx.strokeStyle='#334149';ctx.lineWidth=.7;ctx.beginPath();ctx.moveTo(x0,center);ctx.lineTo(x0+span,center);ctx.stroke();
    const payload=windows.get(key);
    if(payload?.peaks?.length){
      const columns=Math.max(1,Math.ceil(width));
      const rawValues=globalThis.EditorMultilineTimeline.waveformEnvelope(payload.peaks,columns,startMs/1000,endMs/1000,payload.window_start??start,payload.window_end??end,1,0);
      const values=reducePcmSpikes(rawValues).map(v=>suppressLowEnergy(v,getSuppression()));
      const columnWidth=width/columns,gain=this.settings.waveformScale||1;
      const first=Math.max(0,Math.floor(x0/columnWidth)-2),last=Math.min(columns,Math.ceil((x0+span)/columnWidth)+2);
      const upper=i=>Math.max(2,center-values[i]*amplitude*gain);
      const lower=i=>Math.min(height-2,center+values[i]*amplitude*gain);
      if(last>first){
        ctx.save();ctx.beginPath();ctx.rect(x0,0,span,height);ctx.clip();
        ctx.fillStyle='#69b49c';ctx.beginPath();ctx.moveTo(first*columnWidth,upper(first));
        for(let i=first;i<last-1;i++)ctx.quadraticCurveTo((i+.5)*columnWidth,upper(i),(i+1)*columnWidth,(upper(i)+upper(i+1))/2);
        ctx.lineTo(last*columnWidth,upper(last-1));ctx.lineTo(last*columnWidth,lower(last-1));
        for(let i=last-1;i>first;i--)ctx.quadraticCurveTo((i+.5)*columnWidth,lower(i),i*columnWidth,(lower(i)+lower(i-1))/2);
        ctx.lineTo(first*columnWidth,lower(first));ctx.closePath();ctx.fill();ctx.restore();
      }
    }
    if(flags.withChips!==false)this.drawWordChips(row,ctx,width,height,startMs,endMs);
  };
}
