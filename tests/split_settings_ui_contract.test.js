const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const splitHtml = fs.readFileSync(path.join(root, "web", "split.html"), "utf8");
const splitJs = fs.readFileSync(path.join(root, "web", "split.js"), "utf8");
const splitCss = fs.readFileSync(path.join(root, "web", "split.css"), "utf8");
const settingsHtml = fs.readFileSync(path.join(root, "web", "settings.html"), "utf8");
const settingsJs = fs.readFileSync(path.join(root, "web", "settings.js"), "utf8");

test("reference upload is conditional and its break input stays contained", () => {
  assert.match(splitHtml, /class="[^"]*reference-row[^"]*hidden[^"]*" id="referenceRow"/);
  assert.match(splitJs, /#referenceRow"\)\.classList\.toggle\("hidden", !referenceMode\)/);
  assert.match(splitJs, /value === "reference_script" && references\.length/);
  assert.match(splitJs, /const matchedReferences = referenceMode/);
  assert.match(splitCss, /select, input\[type="number"\], #referenceBreakSymbolsInput \{[^}]*box-sizing: border-box;[^}]*width: 100%;/s);
  assert.doesNotMatch(splitHtml, /任务配置已保留；多个素材将共用此配置/);
});

test("reference break presets follow the selected source language", () => {
  assert.match(splitJs, /zh:"，。？！", en:"\.\?!", ja:"。！？", ko:"\.\?!"/);
  assert.match(splitJs, /mixed:"，。？！\.\?!", Auto:"，。？！\.\?!"/);
  assert.match(splitJs, /#languageInput"\)\.addEventListener\("change", syncReferenceBreakPreset\)/);
  assert.match(splitJs, /configuredBreakSymbols && !isReferenceBreakPreset\(configuredBreakSymbols\)/);
  assert.match(splitHtml, /按原文语言提供预设；可自定义/);
});

test("advanced settings expose real scheduler limits", () => {
  assert.match(settingsHtml, /data-panel="advanced"/);
  for (const name of [
    "runtime_worker_concurrency",
    "runtime_cloud_concurrency",
    "runtime_media_concurrency",
    "runtime_gpu_concurrency",
    "runtime_download_concurrency",
  ]) assert.match(settingsHtml, new RegExp(`name="${name}"`));
  assert.match(settingsHtml, /Finalizer 提交/);
  assert.match(settingsHtml, /按项目事务提交/);
  assert.match(settingsHtml, /id="recognitionBadge"/);
});

test("model and recognition category badges share configured wording", () => {
  assert.match(settingsJs, /apiBadge"\)\.textContent = configured \? "已配置" : "未配置"/);
  assert.match(settingsJs, /recognitionBadge\) recognitionBadge\.textContent = recognitionConfigured \? "已配置" : "未配置"/);
  assert.doesNotMatch(settingsJs, /recognitionBadge\) recognitionBadge\.textContent = recognitionConfigured \? "正在使用"/);
});

test("recent-task export menus open upward without card clipping", () => {
  assert.match(splitCss, /\.recent-item \{[^}]*overflow: visible;/s);
  assert.match(splitCss, /\.recent-item:has\(\.export-menu\[open\]\) \{[^}]*z-index: 20;/s);
  assert.match(splitCss, /\.export-menu-panel \{[^}]*bottom: calc\(100% \+ 7px\);[^}]*z-index: 30;/s);
});
