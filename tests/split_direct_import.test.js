const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const text=fs.readFileSync('web/split.js','utf8');
const code=text.slice(text.indexOf('  async function runSubtitleImport()'),text.indexOf('  initializeSubtitleImport();'));
for(const mode of ['single','two-files','bilingual-lines','bilingual-inline']) {
  test(`${mode} creates directly without a confirmation dialog`,async()=>{
    const calls=[],messages=[]; let cleared=false, refreshed=false;
    const state={references:[{}],videos:[{token:'media-reference'}]};
    const window={location:{href:''}};
    const context=vm.createContext({state,window,FormData:class{append(){}},
      $:id=>({value:id==='#subtitleFormat'?mode:'24'}),validateForm(){},
      toast:message=>messages.push(message),clearSubmission:()=>{cleared=true;},refreshJobs:async()=>{refreshed=true;},errorMessage:e=>e.message,
      api:async url=>{calls.push(url);return url.endsWith('/preview')?{can_import:true,confirmation:'digest'}:{editor_url:'/editor?project=new'};}});
    vm.runInContext(code,context);
    await vm.runInContext('runSubtitleImport()',context);
    assert.deepEqual(calls,['/api/subtitle-projects/preview','/api/subtitle-projects']);
    assert.equal(window.location.href,'');
    assert.equal(cleared,true); assert.equal(refreshed,true);
    assert.match(messages[0],/可以继续配置下一个项目/);
    assert.equal(state.submitting,false);
  });
}
test('invalid subtitles do not proceed to project creation',async()=>{
  const calls=[],errors=[];
  const context=vm.createContext({state:{references:[{}],videos:[{token:'media'}]},FormData:class{append(){}},
    $:()=>({value:'single'}),validateForm(){},toast:m=>errors.push(m),errorMessage:e=>e.message,
    api:async url=>{calls.push(url);return {can_import:false,issues:[{entry:2,message:'双语格式无效'}]};}});
  vm.runInContext(code,context);
  await vm.runInContext('runSubtitleImport()',context);
  assert.equal(calls.length,1);
  assert.match(errors[0],/第 3 条/);
});
