(function(root){
  const language = text => /[\u3040-\u30ff]/u.test(text) ? 'ja' : /[\uac00-\ud7af]/u.test(text) ? 'ko' : /[\u4e00-\u9fff]/u.test(text) ? 'zh-CN' : /[a-z]{2,}/i.test(text) ? 'en' : '';
  function blocks(text) {
    return text.replace(/^\uFEFF/,'').replace(/\r/g,'').trim().split(/\n\s*\n/).map(block=>{
      const lines=block.split('\n');const time=lines.findIndex(line=>/\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->/.test(line));
      return time < 0 ? [] : lines.slice(time+1).filter(s=>s.trim());
    }).filter(lines=>lines.length);
  }
  function infer(texts, source, target) {
    const samples=texts.map(blocks); const first=language(samples[0]?.flat().join(' ') || '');
    let format=texts.length===2?'two-files':'single', separator='|', left=first, right=texts.length===2?language(samples[1].flat().join(' ')):'';
    if(texts.length===1 && samples[0].length) {
      const rows=samples[0];
      const inline=rows.filter(r=>r.length===1 && r[0].split('|').length===2 && language(r[0].split('|')[0]) && language(r[0].split('|')[1]) && language(r[0].split('|')[0])!==language(r[0].split('|')[1]));
      const bilingual=rows.filter(r=>r.length===2 && language(r[0]) && language(r[1]) && language(r[0])!==language(r[1]));
      if(inline.length/rows.length>=0.8) {format='bilingual-inline';left=language(inline[0][0].split('|')[0]);right=language(inline[0][0].split('|')[1]);}
      else if(bilingual.length/rows.length>=0.8) {format='bilingual-lines';left=language(bilingual[0][0]);right=language(bilingual[0][1]);}
    }
    const firstTrack=left && left===target && left!==source?'target':'source';
    return {format,separator,firstTrack,source:firstTrack==='source'?left:right,target:firstTrack==='target'?left:right};
  }
  function reference(texts, format, firstTrack, separator) {
    if(format==='two-files') return blocks(texts[firstTrack==='source'?0:1]).flat().join('\n');
    return blocks(texts[0]).map(lines=>{
      if(format==='single') return lines.join('\n');
      const parts=format==='bilingual-lines'?lines:lines.join('\n').split(separator);
      if(parts.length!==2) throw new Error('双语字幕应包含明确的原文和译文，请检查字幕格式');
      return parts[firstTrack==='source'?0:1];
    }).join('\n');
  }
  const api={blocks,infer,reference};if(typeof module!=='undefined')module.exports=api;else root.SubstarSubtitleInput=api;
})(globalThis);
