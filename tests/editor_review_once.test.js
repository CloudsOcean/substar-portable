const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(){
  const nodes=new Map();
  const $=id=>{if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:'',open:true});return nodes.get(id);};
  let signature='cue-1',interval,finishPost;
  const calls=[];
  const context={window:{addEventListener(){}},document:{querySelector:$},MutationObserver:class{observe(){}},navigator:{clipboard:{writeText:async()=>{}}},setTimeout,clearTimeout,
    setInterval(fn){interval=fn;return 1;},clearInterval(){interval=null;}};
  vm.runInNewContext(fs.readFileSync('web/editor_review_chat.js','utf8'),context);
  const api=async(url,options)=>{
    calls.push([url,options?.method||'GET']);
    if(options?.method==='POST')return await new Promise(resolve=>finishPost=resolve);
    if(options?.method==='DELETE')return {};
    return {state:'done',answer:'review result'};
  };
  const review=context.window.SubstarReviewChat.init({api,project:()=> 'project',context:()=>signature,prepare:async()=>({material:'snapshot'})});
  return {$,review,calls,change(){signature='cue-2';interval?.();},resolve(){finishPost({id:'request'});}};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('clearing while POST is pending discards its late result',async()=>{
  const x=setup(),pending=x.review.start();await tick();x.review.clear();x.resolve();await pending;await tick();
  assert.equal(x.$('#reviewChatPanel').hidden,true);
  assert.equal(x.$('#reviewChatMessages').textContent,'');
  assert.ok(x.calls.some(([url,method])=>url.endsWith('/request')&&method==='DELETE'));
});
test('changing Cue clears an existing result and leaves send available',async()=>{
  const x=setup(),pending=x.review.start();await tick();x.resolve();await pending;await tick();
  assert.equal(x.$('#reviewChatMessages').textContent,'review result');
  x.change();assert.equal(x.$('#reviewChatMessages').textContent,'');
  assert.equal(x.$('#reviewChatPanel').hidden,true);
  assert.equal(x.$('#sendAiReview').disabled,false);
});
test('a new send cannot inherit a previous result',async()=>{
  const x=setup();let pending=x.review.start();await tick();x.resolve();await pending;await tick();
  pending=x.review.start();await tick();assert.equal(x.$('#reviewChatMessages').textContent,'');
  x.resolve();await pending;await tick();assert.equal(x.$('#reviewChatMessages').textContent,'review result');
});
