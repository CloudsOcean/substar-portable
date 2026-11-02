const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../web/split.js'),'utf8');
test('retry sends selected profile without changing global settings and clears choice only on success',async()=>{
 const calls=[];const state={retryModelChoices:new Map([['job','glm']])};
 const context={state,api:async(url,request)=>calls.push({url,request}),toast(){},refreshJobs:async()=>{},errorMessage:e=>e.message};
 vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  async function retryJob('),source.indexOf('  function renderQueue(')),context);
 const button={};await context.retryJob('job',button,'glm');
 assert.equal(calls[0].url,'/api/project-creations/job/retry');
 assert.deepEqual(JSON.parse(calls[0].request.body),{provider_id:'glm'});
 assert.equal(state.retryModelChoices.has('job'),false);
 state.retryModelChoices.set('job','glm');context.api=async()=>{throw Error('offline');};
 await context.retryJob('job',button,'glm');assert.equal(state.retryModelChoices.get('job'),'glm');assert.equal(button.disabled,false);
});
