const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const saveAs = require("../web/system_save_as.js");
const {applyLocalOperation} = require("../web/editor_document_store.js");

function handler(context) {
  const source=fs.readFileSync(require.resolve("../web/editor.js"),"utf8");
  const start=source.indexOf('    const button = event.target.closest("[data-export-mode]");');
  const end=source.indexOf('  });',start);
  return vm.runInNewContext(`(async () => {${source.slice(start,end)}})`,context);
}

for (const scenario of ["saved","cancelled","failed","switched"]) {
  test(`actual SRT handler opens picker before waiting: ${scenario}`,async()=>{
    const events=[];let complete;
    const ack=new Promise(resolve=>{complete=resolve;});
    const context={state:{projectId:"p",revision:{},taskInfo:{}},projectEpoch:1,exportEpoch:1,
      event:{target:{closest:()=>({dataset:{exportMode:"source"}})}},
      $:()=>({open:true}),window:{SubstarProjectLabel:()=>"demo"},URLSearchParams,
      nextExportSequence:()=>({value:1}),commitExportSequence:()=>events.push("sequence"),
      ordinaryError:message=>events.push(message),observeEditorTutorialEvent:()=>{},
      projectPath:path=>"/api/projects/p"+path,
      flushActiveEditorInput:async()=>{events.push("wait");await ack;
        if(scenario==="failed")throw new Error("save failed");
        if(scenario==="switched")context.projectEpoch=2;
        return {revision_id:"saved-revision"};}};
    const deps={picker:async()=>{events.push("picker");
      if(scenario==="cancelled")throw Object.assign(new Error("cancel"),{name:"AbortError"});
      return {name:"demo.srt",createWritable:async()=>({write:async()=>events.push("write"),close:async()=>{}})};},
      fetch:async url=>{events.push(url);return {ok:true,blob:async()=>new Blob(["subtitle"])};}};
    context.systemSaveAs={...saveAs,saveUrl:spec=>saveAs.saveUrl(spec,deps)};
    const done=handler(context)();
    assert.equal(events[0],"picker");
    complete();await done;
    if(scenario==="saved") {
      assert.equal(events[1],"wait");
      assert.ok(events.some(e=>e.includes("revision_id=saved-revision")));
      assert.ok(events.includes("write"));
    } else assert.ok(!events.includes("write"));
    if(scenario==="cancelled")assert.deepEqual(events,["picker"]);
  });
}

test("ordinary SRT projected source edits invalidate translation immediately",()=>{
  const target={target_text:"old translation",translation_status:"translated",issue_code:null};
  const saved={document:{source_tokens:[],display_tokens:[],cues:[{cue_id:"c",source_text:"red",display_token_ids:[],state:"active",target}],properties:{},changes:[]}};
  const result=applyLocalOperation(saved,{operation_id:"edit",type:"set_source_text",payload:{cue_id:"c",text:"blue"}});
  assert.equal(result.document.cues[0].target.translation_status,"needs_review");
  assert.equal(target.translation_status,"translated");
});
