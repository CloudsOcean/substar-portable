const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/editor_ass_panel.js','utf8');
const syncCode=source.slice(source.indexOf('    async function sync()'),source.indexOf('    async function action('));
test('optimistic edits hide stale ASS; saved revision refreshes it even after matching base ID',async()=>{
  const nodes=new Map();let pending=[{}],scheduled=0,closed=0,requests=0;
  const ctx={state:{projectId:'p',revision:{revision_id:'r1'},documentStore:{pending:()=>pending}},
    project:'p',revision:'r1',epoch:0,timer:0,ready:true,menu:{open:false},dirty:false,
    clearTimeout(){},closeFrame(){closed++;},loadEffective(){},schedule(){scheduled++;},
    projectPath:x=>x,api:async()=>{requests++;return {};},
    $:s=>{if(!nodes.has(s))nodes.set(s,{hidden:false});return nodes.get(s);}};
  vm.createContext(ctx);vm.runInContext(syncCode,ctx);
  await ctx.sync();assert.equal(ctx.ready,false);assert.equal(nodes.get('#assLiveCanvas').hidden,true);
  assert.equal(nodes.get('.subtitle-overlay').hidden,false);assert.equal(requests,0);
  pending=[];ctx.state.revision={revision_id:'r2'};await ctx.sync();
  assert.equal(requests,1);assert.equal(scheduled,1);assert.equal(closed,2);
});
