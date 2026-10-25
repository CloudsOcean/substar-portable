(function(root,factory){const api=factory();if(typeof module==='object')module.exports=api;root.EditorToolLayout=api;})(globalThis,function(){
  function dockBounds(layout,preview,cue,wave){
    if(layout==='waveform')return {left:preview.left,top:preview.bottom+4,width:preview.width};
    return {left:preview.left,top:wave.top-58,width:preview.width};
  }
  function clampPanel(position,viewport,size){return {left:Math.max(8,Math.min(position.left,viewport.width-size.width-8)),top:Math.max(8,Math.min(position.top,viewport.height-size.height-8))};}
  return {dockBounds,clampPanel};
});
