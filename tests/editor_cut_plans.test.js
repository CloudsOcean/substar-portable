const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
require('../web/vendor/maw/shared/gap-remove-core.js');
const C=globalThis.AsrGapRemoveCore,M=require('../web/editor_maw_model.js'),cut=require('../web/editor_cut_timeline.js');
const loader=import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(require.resolve('../web/editor_cut_plan_panel.js'),'utf8')).toString('base64'));
const original={markers:[{id:'manual',start:1,name:'手工标记'}],plans:[{id:'old',name:'旧方案',keep:null,cuts:[{id:'old-cut',start:1,end:2}]}],active_plan_id:'old'};
const proposal={plan:{id:'new',name:'精简',keep:[{start:2,end:8}],cuts:[{id:'pause',start:4,end:5,reason:'明显停顿'}]},markers:[{id:'review',start:6,end:7,note:'确认语义'}]};
test('AI plan gaps, source-clock preview and export select identical intervals',async()=>{
 const {proposalTimeline}=await loader,before=JSON.stringify(original),next=proposalTimeline(original,proposal,10000,C,M,cut);
 assert.equal(JSON.stringify(original),before);assert.deepEqual(next.plans[0],original.plans[0]);assert.equal(next.active_plan_id,'new');
 const plan=next.plans[1];assert.equal(plan.keep,null);
 assert.deepEqual(cut.kept(plan,10),[{start:2,end:4},{start:5,end:8}]);
 assert.deepEqual(C.getGapRemoveDisplayGaps(plan.gap_remove.gaps).map(g=>[g.start,g.end]),[[0,2000],[4000,5000],[8000,10000]]);
 assert.equal(next.markers[1].plan_id,'new');assert.equal(plan.ai_changes[1].reason,'明显停顿');
});
test('restoring an AI gap preserves the other scheme and makes preview/export retain it',async()=>{
 const {proposalTimeline}=await loader,next=proposalTimeline(original,proposal,10000,C,M,cut),state=next.gap_remove;
 const provenance=C.appendGapRemoveManualOverrides(state.provenance,[{start:4000,end:5000,removed:false}],state.gaps);
 const updated=M.withGapState(next,C.normalizeGapRemoveData({...state,provenance,gaps:C.gapRangesFromProvenance(provenance)}));
 assert.deepEqual(cut.kept(updated.plans[1],10),[{start:2,end:8}]);assert.deepEqual(updated.plans[0],original.plans[0]);
});
test('a generated scheme cannot reuse an existing ID or remove all content',async()=>{
 const {proposalTimeline}=await loader;
 assert.throws(()=>proposalTimeline(original,{plan:{id:'old',cuts:[]}},10000,C,M,cut));
 assert.throws(()=>proposalTimeline(original,{plan:{id:'empty',keep:[],cuts:[]}},10000,C,M,cut));
});

test('explicit head cut retains all later pauses and the tail beyond the last cue',async()=>{
 const {proposalTimeline}=await loader;
 const response={plan:{id:'head-only',keep:null,cuts:[{id:'head',start:0,end:8.53,reason:'删cue1之前'}]}};
 const next=proposalTimeline(original,response,177656,C,M,cut);
 assert.deepEqual(cut.kept(next.plans[1],177.656),[{start:8.53,end:177.656}]);
 assert.deepEqual(next.gap_remove.gaps.map(g=>[g.start,g.end]),[[0,8530]]);
 assert.deepEqual(next.plans[0],original.plans[0]);
});
