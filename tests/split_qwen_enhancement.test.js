"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "../web/split.html"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "../web/split.js"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "../app.py"), "utf8");

test("split page exposes direct Qwen prompt and multilingual temporary hotwords", () => {
  assert.match(html, /id="qwenAiBriefInput"/);
  assert.match(html, /id="qwenAssistButton"/);
  assert.match(html, /id="qwenPromptInput"/);
  assert.match(html, /id="qwenHotwordsInput"/);
  assert.match(source, /api\("\/api\/qwen-assist"/);
  assert.match(source, /context: \$\("#qwenPromptInput"\)\.value/);
  assert.match(source, /qwen_temporary_hotwords: parseTemporaryHotwords\(\)/);
});

test("split tasks freeze the active provider and authentication mode", () => {
  assert.match(source, /active_model_provider:\s*current\.active_model_provider/);
  assert.match(source, /translation_api_auth_mode:\s*current\.translation_api_auth_mode/);
});

test("AI replaces prompt and merges generated hotwords without erasing user terms", () => {
  assert.match(source, /new Map\(existing\.map/);
  assert.match(source, /if \(key && !merged\.has\(key\)\)/);
  assert.match(source, /qwenPromptInput"\)\.value = String\(generated\.prompt/);
});

test("AI brief and hotword guidance explain deterministic super weights", () => {
  assert.match(html, /节目内容与识别重点/);
  assert.match(html, /直接点名的词语会作为超级热词/);
  assert.match(html, /有助于结合上下文消歧，并提高专名识别准确率/);
  assert.match(html, /可提高对应词语的识别准确率/);
  assert.match(html, /:5 是普通热词最高权重/);
  assert.match(html, /:50 是必须重点命中的超级热词/);
  assert.doesNotMatch(html, /<small>描述节目内容/);
  assert.doesNotMatch(html, /<small>使用所选原文语言/);
  assert.doesNotMatch(html, /<small>可混合中文/);
  assert.match(html, /id="qwenAssistStatus" aria-live="polite"><\/span>/);
  assert.match(html, /class="primary-button qwen-assist-button"/);
  assert.match(app, /prioritize_generated_qwen_hotwords/);
});

test("upload drop zone uses the media upload icon", () => {
  assert.doesNotMatch(html, /<span class="drop-icon">↓<\/span>/);
  assert.match(html, /#upload-media/);
});

test("AI assist prefers non-thinking and falls back to thinking Low", () => {
  assert.match(app, /assist_model = str\(settings\["translation_api_model"\]\)/);
  assert.match(app, /assist_thinking_modes = \("disabled", "enabled"\)/);
  assert.match(app, /for thinking_mode in assist_thinking_modes/);
  assert.match(app, /thinking_mode=thinking_mode/);
  assert.match(app, /reasoning_effort="low"/);
});

test("Quick Start switches every LLM Stage atomically and preserves the reasoning probe", () => {
  for (const stage of ["segmentation", "segmentation_repair", "translation", "translation_repair", "calibration", "audit_repair"]) {
    assert.match(source, new RegExp(`stage_\\$\\{stage\\}_model`));
  }
  assert.match(source, /state\.settings = await api\("\/api\/settings"\);[\s\S]*?quickSettingsPayload\(kind, key\)/);
});

test("provider registration links appear in tutorials instead of quick cards", () => {
  assert.doesNotMatch(html, /智谱 API Key · <a/);
  assert.doesNotMatch(source, /百炼平台注册点此/);
  assert.equal((source.match(/label: "点击注册并获取 API Key"/g) || []).length, 2);
});

test("temporary hotword parser rejects unsupported numeric weights", () => {
  assert.match(source, /\[1, 2, 3, 4, 5, 50\]\.includes\(weight\)/);
  assert.match(source, /热词权重必须为 1–5 或 50/);
  assert.match(source, /只能指定一个热词权重/);
});

test("glossary hotwords are no longer injected invisibly into transcription", () => {
  assert.doesNotMatch(app, /hotwords = glossary_hotwords\(/);
  assert.match(html, /接口已预留，暂不自动注入本次听写/);
});
