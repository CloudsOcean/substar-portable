const assert = require("assert");
const saveAs = require("../web/system_save_as.js");

async function run() {
  assert.deepStrictEqual(
    saveAs.subtitleSpec("project:one", "ab-double", "/export"),
    {
      url:"/export",
      suggestedName:"project_one_ab-double.srt",
      description:"SubRip 字幕",
      mimeType:"application/x-subrip",
      extension:".srt"
    }
  );
  assert.strictEqual(
    saveAs.exchangeSpec("demo", "subtitle-project", "/exchange").suggestedName,
    "demo_字幕工程.zip"
  );

  const calls = [];
  const handle = {
    name:"chosen.srt",
    async createWritable() {
      return {
        async write(blob) { calls.push(["write", await blob.text()]); },
        async close() { calls.push(["close"]); }
      };
    }
  };
  const result = await saveAs.saveUrl(
    saveAs.subtitleSpec("demo", "source", "/export"),
    {
      picker:async options => { calls.push(["picker", options.suggestedName]); return handle; },
      fetch:async url => {
        calls.push(["fetch", url]);
        return {ok:true, blob:async () => new Blob(["subtitle"])};
      }
    }
  );
  assert.deepStrictEqual(result, {cancelled:false, filename:"chosen.srt"});
  assert.deepStrictEqual(calls, [
    ["picker", "demo_source.srt"],
    ["fetch", "/export"],
    ["write", "subtitle"],
    ["close"]
  ]);

  const streamCalls = [];
  const streamHandle = {
    name:"package.zip",
    async createWritable() { streamCalls.push("writable"); return {kind:"file-stream"}; }
  };
  const streamed = await saveAs.saveUrl(
    saveAs.exchangeSpec("demo", "subtitle-project", "/package"),
    {
      picker:async () => streamHandle,
      fetch:async () => ({
        ok:true,
        body:{async pipeTo(destination) { streamCalls.push(["pipeTo", destination.kind]); }}
      })
    }
  );
  assert.deepStrictEqual(streamed, {cancelled:false, filename:"package.zip"});
  assert.deepStrictEqual(streamCalls, ["writable", ["pipeTo", "file-stream"]]);

  const cancelled = await saveAs.saveUrl(
    saveAs.subtitleSpec("demo", "source", "/export"),
    {picker:async () => { const error = new Error("cancel"); error.name = "AbortError"; throw error; }}
  );
  assert.deepStrictEqual(cancelled, {cancelled:true});
}

run().then(() => console.log("system_save_as.test.js: ok"));
