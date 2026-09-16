const {test}=require('node:test');const assert=require('node:assert/strict');const S=require('../web/subtitle_input.js');
const srt=text=>`1\n00:00:01,000 --> 00:00:03,000\n${text}\n`;
test('one English SRT becomes a bilingual pair when Chinese file is added',()=>{
 assert.deepEqual(S.infer([srt('Hello world')],'en','zh-CN'),{format:'single',separator:'|',firstTrack:'source',source:'en',target:''});
 const result=S.infer([srt('Hello world'),srt('你好世界')],'en','zh-CN');assert.equal(result.format,'two-files');assert.equal(result.source,'en');assert.equal(result.target,'zh-CN');
 assert.equal(S.infer([srt('你好世界'),srt('Hello world')],'en','zh-CN').firstTrack,'target');
 assert.equal(S.infer([srt('Hello world')],'en','zh-CN').format,'single');
});
test('multiline monolingual captions are not mistaken for bilingual captions',()=>{
 assert.equal(S.infer([srt('Hello world\nHow are you')],'en','zh-CN').format,'single');
 assert.equal(S.infer([srt('Hello world\n你好世界')],'en','zh-CN').format,'bilingual-lines');
 assert.equal(S.infer([srt('Hello world | 你好世界')],'en','zh-CN').format,'bilingual-inline');
});
test('reference extraction respects track selection and excludes translation',()=>{
 assert.equal(S.reference([srt('你好世界'),srt('Hello world')],'two-files','target','|'),'Hello world');
 assert.equal(S.reference([srt('你好世界\nHello world')],'bilingual-lines','target','|'),'Hello world');
 assert.equal(S.reference([srt('Hello world | 你好世界')],'bilingual-inline','source','|'),'Hello world ');
 assert.throws(()=>S.reference([srt('Hello')],'bilingual-lines','source','|'));
});
