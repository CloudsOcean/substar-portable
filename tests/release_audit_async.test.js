const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");

function isolatedFunction(file, name, scope) {
  const source = readFileSync(resolve(__dirname, "../web", file), "utf8");
  const match = source.match(new RegExp("^([ ]*)async function " + name + "\\([^]*?^\\1}", "m"));
  assert.ok(match, name);
  return vm.runInNewContext(match[0] + "\n" + name, scope);
}

function delayed() {
  let finish;
  const promise = new Promise(resolve => { finish = resolve; });
  return {promise, finish};
}

test("a project switch during translation startup or polling ignores the obsolete response", async () => {
  const gate = delayed();
  let posts = 0;
  const rendered = [];
  const scope = {state:{projectId:"A",revision:{revision_id:"r"}}, projectEpoch:1,
    isAdvancedTutorial:()=>false, ordinaryError:()=>{}, projectPath:s=>"/A"+s,
    $:()=>({value:"",checked:false}), renderTranslationTask:t=>rendered.push(t),
    loadTranslationGlossaries:()=>gate.promise, api:()=>gate.promise, withSavedDocument:()=>{posts++;}};
  const start = isolatedFunction("editor.js","startTranslation",scope)();
  const poll = isolatedFunction("editor.js","refreshTranslationTask",scope)();
  scope.projectEpoch++;
  scope.state.projectId = "B";
  gate.finish({state:"running"});
  await Promise.all([start,poll]);
  assert.equal(posts,0);
  assert.deepEqual(rendered.map(t=>t.state),["queued"]);
});

test("glossary save keeps edits typed during the request", async () => {
  const gate = delayed();
  const headers = [];
  let redraws = 0;
  const scope = {candidateImports:[],supportsTransfer:true,$:()=>({}),draftVersion:3,dirty:true,
    collections:[],collectEntries:()=>[],api:()=>gate.promise,setHeader:(...x)=>headers.push(x),
    renderLibrary:()=>redraws++};
  const saving = isolatedFunction("glossary.js","saveGlossary",scope)();
  scope.draftVersion++;
  gate.finish({entries:[]});
  assert.equal(await saving,false);
  assert.equal(scope.dirty,true);
  assert.equal(redraws,0);
  assert.equal(headers.at(-1)[1],"dirty");
});

test("model discovery drops stale responses after changing providers", async () => {
  const gate = delayed();
  const scope = {selectedModelProvider:"one",discoveryGeneration:0,providerModelCatalogs:new Map(),
    form:{elements:{translation_api_base_url:{value:"one"},translation_api_auth_mode:{},translation_api_key:{}}},
    $:()=>({replaceChildren:()=>{}}),api:()=>gate.promise};
  const loading = isolatedFunction("settings.js","discoverModels",scope)();
  scope.selectedModelProvider = "two";
  gate.finish({models:["old"]});
  await loading;
  assert.equal(scope.providerModelCatalogs.size,0);
});