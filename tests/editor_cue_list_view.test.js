'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {createCueListView,createHeightIndex}=require('../web/editor_cue_list_view.js');
function fixture(count=1405){
 const frames=new Map();let serial=0,observer;
 class Node {
  constructor(){this.children=[];this.dataset={};this.style={};this.attrs={};this.handlers={};this.classes=new Set();this.classList={contains:n=>this.classes.has(n),toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)};}
  get parentNode(){return this.parent;}
  get nextSibling(){return this.parent?.children[this.parent.children.indexOf(this)+1]||null;}
  append(node){this.insertBefore(node,null);}
  insertBefore(node,before){node.remove();const i=before?this.children.indexOf(before):this.children.length;this.children.splice(i,0,node);node.parent=this;}
  remove(){if(this.parent){this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null;}}
  replaceWith(node){const parent=this.parent;parent.insertBefore(node,this);this.remove();}
  replaceChildren(...nodes){for(const node of [...this.children])node.remove();for(const node of nodes)this.append(node);}
  contains(node){return node===this||this.children.some(c=>c.contains(node));}
  setAttribute(k,v){this.attrs[k]=v;}getAttribute(k){return this.attrs[k]??null;}
  addEventListener(n,f){(this.handlers[n]||=[]).push(f);}removeEventListener(){}
  fire(n,e={}){for(const f of this.handlers[n]||[])f(e);}
  querySelector(s){return s==='.cue-meta strong'?this.number:null;}
  isEqualNode(n){return this.text===n.text&&this.number.textContent===n.number.textContent&&this.height===n.height;}
  getBoundingClientRect(){if(this===container)return {top:0,bottom:600,height:600,width:500};const top=(parseFloat(this.style.top)||0)-container.scrollTop;return {top,bottom:top+this.height,height:this.height,width:500};}
 }
 const doc={activeElement:null,createElement:()=>new Node(),addEventListener(){},removeEventListener(){},getSelection:()=>null};
 const container=new Node();container.ownerDocument=doc;container.clientHeight=600;container.clientWidth=500;container.scrollTop=0;
 global.document=doc;global.getComputedStyle=n=>({paddingTop:'0',paddingBottom:'0',marginBottom:n.dataset.cueId?'10':'0'});
 global.requestAnimationFrame=fn=>{frames.set(++serial,fn);return serial;};global.cancelAnimationFrame=id=>frames.delete(id);
 global.ResizeObserver=class{constructor(fn){observer=fn;}observe(){}unobserve(){}disconnect(){}};
 const slider=new Node();slider.value='0';
 let renderCount=0;
 const renderCue=(cue,index)=>{renderCount++;const n=new Node();n.dataset.cueId=cue.cue_id;n.height=cue.height;n.text=cue.text;n.number={textContent:String(index+1)};return n;};
 const view=createCueListView({container,renderCue,positionSlider:slider});
 const cues=Array.from({length:count},(_,i)=>({cue_id:`c${i}`,text:`text ${i}`,height:50+(i%5)*31}));
 const render=(values=cues,preservePage=true)=>view.render({cues:values,tokenById:new Map(),activeCueId:values[0]?.cue_id,preservePage});
 const flush=()=>{let rounds=0;while(frames.size){if(rounds++>30)throw Error('unbounded animation loop');const callbacks=[...frames.values()];frames.clear();callbacks.forEach(fn=>fn());}};
 render();flush();
 return {container,doc,view,cues,render,flush,slider,resize:()=>observer(),rows:()=>container.children[0].children,get renderCount(){return renderCount;}};
}
test('height index matches variable prefix sums and point updates',()=>{
 const h=createHeightIndex([50,80,130]);assert.equal(h.total(),260);assert.equal(h.at(129),1);assert.equal(h.at(130),2);h.update(0,100);assert.equal(h.offset(2),180);assert.equal(h.at(179),1);
});
test('long document uses full internal height and bounded DOM for repeated end-to-end jumps',()=>{
 const f=fixture(10000);
 for(let n=0;n<4;n++)for(const p of [100,50,0]){
  f.view.jumpToPercent(p);f.flush();assert.ok(f.rows().length<70);assert.equal(f.container.getBoundingClientRect().height,600);assert.equal(f.container.style.paddingTop,undefined);assert.ok(parseFloat(f.container.children[0].style.height)>500000);
  const target=f.rows().find(r=>r.dataset.cueId===`c${Math.round(p/100*9999)}`);assert.ok(target);
 }
});
test('far navigation, last request wins and target centers using actual variable heights',()=>{
 const f=fixture();f.view.revealCue('c577');f.view.revealCue('c663');f.flush();
 const rect=f.rows().find(r=>r.dataset.cueId==='c663').getBoundingClientRect();assert.ok(Math.abs((rect.top+rect.bottom)/2-295)<6);
 assert.ok(f.rows().length<70);
});
test('user scrolling cancels deferred navigation corrections',()=>{
 const f=fixture();f.view.revealCue('c577');f.container.fire('wheel');f.container.scrollTop+=350;const wanted=f.container.scrollTop;f.flush();assert.ok(Math.abs(f.container.scrollTop-wanted)<1);
});
test('height changes above viewport preserve visible anchor',()=>{
 const f=fixture();f.view.revealCue('c577');f.flush();
 const anchor=f.rows().find(r=>r.getBoundingClientRect().bottom>0),before=anchor.getBoundingClientRect().top;
 const first=f.rows()[0];first.height+=100;f.resize();f.flush();assert.ok(Math.abs(anchor.getBoundingClientRect().top-before)<1);
});
test('editing row remains pinned across distant scrolling then is recycled on blur',()=>{
 const f=fixture(),row=f.rows()[1],input={matches:s=>s==='.whole-cue-editor'};
 row.children.push(input);input.parent=row;input.children=[];input.contains=n=>n===input;f.doc.activeElement=input;
 f.view.revealCue('c1000');f.flush();assert.ok(f.rows().includes(row));assert.equal(f.doc.activeElement,input);
 f.doc.activeElement=null;f.container.fire('focusout');f.flush();assert.ok(!f.rows().includes(row));
});
test('save refresh preserves focused editor and updates other visible rows',()=>{
 const f=fixture(3),row=f.rows()[1],input={matches:s=>s==='.inline-token-editor',value:'draft',selectionStart:2};input.contains=n=>n===input;input.children=[];row.children.push(input);f.doc.activeElement=input;
 f.render(f.cues.map(c=>({...c,text:'saved'})));f.flush();assert.equal(f.rows()[1],row);assert.equal(input.value,'draft');assert.equal(input.selectionStart,2);assert.equal(f.rows()[0].text,'saved');
 f.doc.activeElement=null;f.render(f.cues.map(c=>({...c,text:'saved'})));f.flush();assert.equal(f.rows()[1].text,'saved');
});
test('structural changes retain keyed rows, no duplicate IDs; empty list is supported',()=>{
 const f=fixture(3),last=f.rows()[2];
 f.render([f.cues[0],{cue_id:'new',text:'new',height:90},...f.cues.slice(1)]);f.flush();assert.equal(f.rows().at(-1),last);assert.equal(last.number.textContent,'4');
 f.render([f.cues[0],f.cues[2]]);f.flush();assert.equal(f.rows().at(-1),last);assert.equal(last.number.textContent,'2');
 f.render([]);f.flush();assert.equal(f.rows().length,0);assert.equal(f.slider.disabled,true);
});
test('scrolling a stable window does not recreate existing rows',()=>{
 const f=fixture(),before=f.renderCount;f.container.scrollTop+=10;f.container.fire('scroll');f.flush();assert.ok(f.renderCount-before<3);
});
