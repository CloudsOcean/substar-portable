"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "../web/settings.html"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "../web/settings.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "../app.py"), "utf8");
const providerSource = fs.readFileSync(path.join(__dirname, "../substar_core/model_providers.py"), "utf8");

test("settings exposes the official GLM OpenAI-compatible provider", () => {
  assert.match(html, /id="modelProviderList"/);
  assert.match(providerSource, /"id": "glm"/);
  assert.match(providerSource, /"base_url": "https:\/\/open\.bigmodel\.cn\/api\/paas\/v4"/);
  assert.match(providerSource, /"default_model": "glm-5\.3-flash"/);
  assert.match(
    html,
    /id="translation_api_key"[\s\S]*?class="secondary test-api[\s\S]*?测试连通性/,
    "the active provider API key must keep an adjacent connectivity test",
  );
});

test("connectivity testing also verifies thinking modes without probing every effort", () => {
  assert.match(source, /api\("\/api\/models\/reasoning-probe"/);
  assert.match(appSource, /probe_chat_thinking_modes/);
  assert.doesNotMatch(appSource, /probe_chat_reasoning_efforts/);
  assert.doesNotMatch(html, /id="probeReasoning"/);
  assert.match(html, /id="officialModelSelect"/);
  assert.doesNotMatch(html, /id="reasoningCapabilitySummary"/);
});

test("switching models updates inherited stages without clearing saved credentials", () => {
  assert.match(source, /if \(value\.includes\("bigmodel\.cn"\)\) return "glm"/);
  assert.match(source, /function captureModelProviderDraft\(providerId = selectedModelProvider\)/);
  assert.match(source, /function loadModelProviderDraft\(providerId, \{ forceStages = false \} = \{\}\)/);
  assert.match(source, /payload\.active_model_provider = selectedModelProvider/);
  assert.match(source, /payload\.model_provider_profiles = \{ \.\.\.modelProviderDrafts \}/);
  assert.match(source, /function setConnectionModel\(value, \{ force = false \} = \{\}\)/);
  assert.match(source, /loadModelProviderDraft\(provider, \{ forceStages: providerChanged \}\)/);
  assert.match(source, /force \|\| inheritedModelStages\.has\(stage\)/);
  assert.match(source, /inheritedModelStages\.has\(stage\)/);
  assert.match(source, /reasoningCapabilityCache\.clear\(\)/);
  assert.match(source, /capability\.supported_thinking_modes/);
  assert.match(source, /capability\.effort_selection_aliases/);
  assert.match(source, /思考（模型要求）/);
  assert.match(source, /thinking_mode: ""/);
  assert.match(source, /clear_translation_api_key\.checked = false/);
  assert.doesNotMatch(
    source,
    /clear_translation_api_key\.checked = provider !== savedModelProvider/,
  );
  assert.match(source, /!modelSettingsFields\.has\(event\.target\?\.name\)/);
  assert.match(source, /modelProviderDrafts\[providerId\] =/);
  assert.match(source, /settings\?\.model_provider_key_set\?\.\[provider\]/);
  assert.match(source, /provider_id: selectedModelProvider/);
});

test("cloud LLM catalog includes SmartSub-compatible API providers without local installers", () => {
  for (const provider of ["deepseek", "glm", "openai", "azure_openai", "deerapi", "gemini", "siliconflow", "qwen", "custom"]) {
    assert.match(providerSource, new RegExp(`"id": "${provider}"`));
  }
  assert.doesNotMatch(providerSource, /"id": "ollama"/);
  assert.doesNotMatch(providerSource, /download|model asset/i);
});

test("reasoning choices always expose five canonical levels and provider mapping", () => {
  assert.match(source, /const levels = allReasoningEfforts/);
  for (const value of ["low", "medium", "high", "xhigh", "max"]) {
    assert.match(source, new RegExp(`${value}:`));
  }
  assert.match(source, /effort_selection_aliases/);
});

test("normal stages default to thinking Low while fallback stages prefer non-thinking", () => {
  assert.match(source, /"segmentation_repair", "translation_repair", "audit_repair"/);
  assert.match(source, /select\.value = preserveEffort && levels\.includes\(current\) \? current : "low"/);
  assert.match(source, /explicitlyConfiguredThinkingStages\.has\(stage\)/);
  assert.match(source, /explicitlyConfiguredEffortStages\.has\(stage\)/);
});
