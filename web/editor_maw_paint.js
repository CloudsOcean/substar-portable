/* Continuous display envelope; source peaks and edit timestamps stay untouched. */
export function softenWaveEnvelope(values) {
  return values.map((value,i)=>Math.max(value*.8,
    (values[Math.max(0,i-1)]+value*3+values[Math.min(values.length-1,i+1)])/5));
}
export function installSubstarWaveformPaint(wave) {
  const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));
  wave.paintWaveRow=function(row,ctx,width,height,startMs,endMs,x0,span,{withChips=true}={}) {
    const colors=this._getWaveColors(),range=Math.max(1,endMs-startMs);
    ctx.fillStyle=colors.rowBg;ctx.fillRect(x0,0,span,height);
    ctx.strokeStyle=colors.rowBorder;ctx.lineWidth=1;ctx.beginPath();
    const step=range<=10000?1000:range<=30000?2000:5000;
    for(let t=Math.ceil(startMs/step)*step;t<endMs;t+=step){const x=(t-startMs)/range*width;if(x>=x0&&x<=x0+span){ctx.moveTo(x+.5,0);ctx.lineTo(x+.5,height);}}
    ctx.stroke();
    const shape=this.activeWaveShape();if(!shape)return;
    const {peaks,peaksPerSecond,peakCount}=shape;
    // Aggregate at CSS-pixel density, then soften only immediate neighbours.
    const columns=Math.max(1,Math.ceil(width));
    const waveTop=28;
    // The waveform is the row background, including the area behind cue blocks.
    const waveBottom=Math.max(waveTop+2,height-6),center=(waveTop+waveBottom)/2;
    const amplitude=Math.max(1,(waveBottom-waveTop)/2)*(this.settings.waveformScale||1);
    ctx.strokeStyle='#334149';ctx.lineWidth=.7;ctx.beginPath();ctx.moveTo(x0,center);ctx.lineTo(x0+span,center);ctx.stroke();
    const columnWidth=width/columns;
    const first=Math.max(0,Math.floor(x0/columnWidth)-2),last=Math.min(columns,Math.ceil((x0+span)/columnWidth)+2);
    const lows=[],highs=[];
    for(let x=first;x<last;x++){
      const left=startMs+x/columns*range,right=startMs+(x+1)/columns*range;
      if(left/1000*peaksPerSecond>=peakCount){lows.push(0);highs.push(0);continue;}
      const a=clamp(Math.floor(left/1000*peaksPerSecond),0,peakCount-1);
      const b=clamp(Math.ceil(right/1000*peaksPerSecond),a+1,peakCount);
      let low=0,high=0;for(let i=a;i<b;i++){low=Math.min(low,peaks[i*2]/127);high=Math.max(high,peaks[i*2+1]/127);}
      lows.push(clamp(-low,0,1));highs.push(clamp(high,0,1));
    }
    const softLow=softenWaveEnvelope(lows),softHigh=softenWaveEnvelope(highs);
    const top=softHigh.map(v=>Math.max(2,center-v*amplitude));
    const bottom=softLow.map(v=>Math.min(height-2,center+v*amplitude));
    if(top.length){
      ctx.save();ctx.beginPath();ctx.rect(x0,0,span,height);ctx.clip();
      ctx.fillStyle='#579580';ctx.beginPath();ctx.moveTo(first*columnWidth,top[0]);
      for(let i=0;i<top.length-1;i++)ctx.quadraticCurveTo((first+i+.5)*columnWidth,top[i],(first+i+1)*columnWidth,(top[i]+top[i+1])/2);
      ctx.lineTo(last*columnWidth,top[top.length-1]);
      ctx.lineTo(last*columnWidth,bottom[bottom.length-1]);
      for(let i=bottom.length-1;i>0;i--)ctx.quadraticCurveTo((first+i+.5)*columnWidth,bottom[i],(first+i)*columnWidth,(bottom[i]+bottom[i-1])/2);
      ctx.lineTo(first*columnWidth,bottom[0]);ctx.closePath();ctx.fill();ctx.restore();
    }
    if(withChips)this.drawWordChips(row,ctx,width,height,startMs,endMs);
  };
}
