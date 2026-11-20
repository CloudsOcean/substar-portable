const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {applyLocalOperation} = require("../web/editor_document_store.js");

function handler(context) {
  const source = fs.readFileSync(require.resolve("../web/editor.js"), "utf8");
  const registration = 'outputForm.addEventListener("submit", ';
  const start = source.indexOf(registration) + registration.length;
  const end = source.indexOf("\n  });", start);
  return vm.runInNewContext("(" + source.slice(start, end) + "})", context);
}

for (const scenario of ["saved", "disabled", "failed", "switched"]) {
  test("output jobs submit only after saving the current project: " + scenario, async () => {
    const events = [];
    let complete;
    const ack = new Promise(resolve => { complete = resolve; });
    const context = {
      state:{projectId:"p"}, projectEpoch:1,
      $:selector => ({
        checked:selector === "#outputSubtitles",
        disabled:selector === "#startOutput" && scenario === "disabled",
        value:"demo"
      }),
      projectPath:path => "/api/projects/p" + path,
      selectedOutputValues:name => name === "outputSubtitleFormat" ? ["srt"] : ["source"],
      outputPlan:() => null,
      outputSessions:new Map(),
      nextExportSequence:() => ({value:1}), commitExportSequence:() => events.push("sequence"),
      refreshOutputMenu(){}, observeEditorTutorialEvent(){},
      localStorage:{setItem(){}}, sessionStorage:{setItem(){}},
      api:async (url, options) => {
        events.push("start");
        assert.equal(url, "/api/projects/p/output-jobs");
        assert.equal(JSON.parse(options.body).revision_id, "saved-revision");
        assert.deepEqual(JSON.parse(options.body).subtitles, {formats:["srt"],modes:["source"]});
        return {id:"output"};
      },
      followOutput:async () => events.push("follow"),
      flushActiveEditorInput:async () => {
        events.push("wait");
        await ack;
        if (scenario === "failed") throw new Error("save failed");
        if (scenario === "switched") context.projectEpoch = 2;
        return {revision_id:"saved-revision"};
      }
    };
    const done = handler(context)({preventDefault(){}});
    assert.deepEqual(events, scenario === "disabled" ? [] : ["wait"]);
    complete();
    await done;
    if (scenario === "saved") assert.deepEqual(events, ["wait", "start", "sequence", "follow"]);
    else assert.ok(!events.includes("start"));
  });
}

test("ordinary SRT projected source edits invalidate translation immediately",()=>{
  const target={target_text:"old translation",translation_status:"translated",issue_code:null};
  const saved={document:{source_tokens:[],display_tokens:[],cues:[{cue_id:"c",source_text:"red",display_token_ids:[],state:"active",target}],properties:{},changes:[]}};
  const result=applyLocalOperation(saved,{operation_id:"edit",type:"set_source_text",payload:{cue_id:"c",text:"blue"}});
  assert.equal(result.document.cues[0].target.translation_status,"needs_review");
  assert.equal(target.translation_status,"translated");
});
