const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const languageLayout=require('../web/editor_language.js');
const M=require('../web/editor_maw_model.js');
const source=fs.readFileSync(require.resolve('../web/editor.js'),'utf8');
function setup(presentation){
 const context={state:{view:{presentation},trackLanguages:{target:'zh-CN'}},languageLayout,cueSourceText:c=>c.source_text};
 vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function projectPunctuation('),source.indexOf('  function timeLabel(')),context);return context;
}
test('punctuation projection removes Chinese commas and respects displayed line order',()=>{
 const c=setup({lower_remove:'，',upper_space:','});const cue={source_text:'Well, hello.',target:{target_text:'我很好，也很开心'}};
 const result=c.projectedCueLines(cue);assert.equal(result.source,'Well hello.');assert.equal(result.target,'我很好也很开心');
 assert.equal(cue.target.target_text,'我很好，也很开心');
 c.state.view.presentation={display_order:'target_above_source',upper_remove:'，',lower_remove:'.'};
 assert.equal(c.projectedCueLines(cue).source,'Well, hello');assert.equal(c.projectedCueLines(cue).target,'我很好也很开心');
});
test('word display uses its whole cue language route; remove wins over space',()=>{
 const c=setup({translation_layout:{enabled:true,lower_languages:['zh-CN']},lower_remove:'，',lower_space:'，'});
 assert.equal(c.projectedSourceTokenText('，',{source_text:'你好，世界'}),'');
 assert.equal(c.projectedSourceTokenText('hello,',{source_text:'hello, world'}),'hello,');
});
test('waveform projects both lines and words but retains raw edit text and identities',()=>{
 const c=setup({lower_remove:'，',upper_remove:','});
 const view={cue_views:[{cue_id:'c',source_text:'Hello,',display_token_ids:['w'],start:0,end:1,state:'active',target:{target_text:'你好，世界'}}],token_views:[{token_id:'w',text:'Hello,',start:0,end:1}]};
 const original=JSON.stringify(view);
 const [s]=M.segments(view,x=>x.source_text,c.projectedCueLines,c.projectedSourceTokenText);
 assert.equal(s.text,'Hello');assert.equal(s.translation,'你好世界');assert.equal(s.rawText,'Hello,');assert.equal(s.rawTranslation,'你好，世界');assert.equal(s.items[0].text,'Hello');assert.equal(s.items[0].id,'w');assert.equal(JSON.stringify(view),original);
});
