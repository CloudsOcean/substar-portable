const {test} = require('node:test');
const assert = require('node:assert/strict');
const T = require('../web/glossary_transfer.js');
const local = () => ({collections:[{id:'global',name:'未分配',kind:'global',injection_permissions:[]},{id:'a',name:'节目',kind:'project',injection_permissions:['asr']}],entries:[{id:'1',source:'Cyrus',target:'旧译文',glossary_id:'a'}],candidates:[{id:'2',source:'Ignore',target:'',status:'ignored'}]});
const incoming = () => ({schema_version:T.FORMAT,collections:[{id:'b',name:'节目',kind:'project',injection_permissions:['translation']}],entries:[{source:' cyrus ',target:'新译文',glossary_id:'b'},{source:'New',target:'新增',glossary_id:'b'}],candidates:[{source:'Ignore',status:'pending'},{source:'Review',target:'审核',status:'pending'}]});
test('roundtrip and repeated import are idempotent, conflict preserves local',()=>{
 const r=T.merge(local(),T.parse(T.serialize(incoming())));
 assert.equal(r.library.collections.length,2);assert.deepEqual(r.library.collections[1].injection_permissions,['asr']);
 assert.equal(r.library.entries.length,2);assert.equal(r.conflicts.length,1);assert.equal(r.library.entries[0].target,'旧译文');
 assert.equal(r.library.candidates[0].status,'ignored');
 assert.deepEqual(T.merge(r.library,incoming()).library,r.library);
 assert.deepEqual(T.parse(T.serialize(r.library)).entries,r.library.entries);
 const changed=T.merge(local(),incoming(),{replace:new Set([r.conflicts[0].id])});assert.equal(changed.library.entries[0].target,'新译文');
});
test('external output enters candidates and grants no injection',()=>{
 const value=incoming();value.collections[0].name='新项目';
 const r=T.merge(local(),value,{external:true});
 assert.equal(r.library.entries.length,1);assert.deepEqual(r.library.collections[2].injection_permissions,[]);
 assert.equal(r.library.candidates.find(x=>x.source==='New').status,'pending');
 assert.equal(r.library.candidates.find(x=>x.source==='New').glossary_id,'b');
});
test('same source in different project allowed, empty translation fills',()=>{
 const value=incoming();value.entries[0].glossary_id='global';
 const l=local();l.entries[0].target='';
 const r=T.merge(l,incoming());assert.equal(r.stats.filled,1);
 assert.equal(T.merge(local(),value).library.entries.length,3);
});
test('invalid schema and dangling project rejected',()=>{
 assert.throws(()=>T.parse('{}'));
 const v=incoming();v.entries[0].glossary_id='missing';assert.throws(()=>T.parse(JSON.stringify(v)));
});
