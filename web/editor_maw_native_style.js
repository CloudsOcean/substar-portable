// Display contrast only: silence and full-scale peaks remain fixed.
export function suppressLowEnergy(value,strength=0){
  const amount=Math.max(0,Math.min(1,Number(strength)||0));
  return Math.pow(Math.max(0,Math.min(1,value)),1+amount*2);
}
/* Native MAW geometry and drawing, with green peaks and restrained display smoothing. */
export function lightSmoothColumns(values){
  return Float32Array.from(values,(v,i)=>.875*v+.0625*values[Math.max(0,i-1)]+.0625*values[Math.min(values.length-1,i+1)]);
}
export function installNativeWaveformStyle(wave,{getSuppression=()=>0}={}){
  const colors=wave._getWaveColors;
  wave._getWaveColors=function(){return {...colors.call(this),peak:'#69b49c',peakDim:'#477b6b'};};
  const envelope=wave.getWaveformColumnEnvelope;
  const styled=new WeakMap();
  wave.getWaveformColumnEnvelope=function(...args){
    const original=envelope.apply(this,args);if(!original)return original;
    const strength=getSuppression();
    if(styled.get(original)?.strength!==strength){
      const absolute=Float32Array.from(original.high,(v,i)=>Math.max(Math.abs(v),Math.abs(original.low[i])));
      const high=Float32Array.from(lightSmoothColumns(absolute),v=>127*suppressLowEnergy(v/127,strength)),low=Float32Array.from(high,v=>-v);
      styled.set(original,{strength,result:{...original,low,high}});
    }
    return styled.get(original).result;
  };
}
