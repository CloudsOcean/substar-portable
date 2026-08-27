"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "../web/settings.html"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "../web/settings.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "../app.py"), "utf8");

test("settings exposes the official GLM OpenAI-compatible provider", () => {
  assert.match(html, /data-model-provider="glm"/);
  assert.match(html, /data-base-url="https:\/\/open\.bigmodel\.cn\/api\/paas\/v4"/);
  assert.match(html, /data-default-model="glm-5\.3"/);
  assert.match(
    html,
    /id="translation_api_key"[\s\S]*?class="secondary test-api[\s\S]*?测试连通性/,
    "the active provider API key must keep an adjacent connectivity test",
  );
});

test("live probing verifies declared efforts without redefining the dropdown", () => {
  assert.match(appSource, /"verified_efforts": accepted/);
  assert.doesNotMatch(appSource, /"supported_efforts": accepted/);
});

test("switching to GLM updates the connection model and every stage model", () => {
  assert.match(source, /if \(value\.includes\("bigmodel\.cn"\)\) return "glm"/);
  assert.match(source, /form\.elements\.translation_api_model\.value = defaultModel/);
  assert.match(source, /form\.elements\[`stage_\$\{stage\}_model`\]/);
  assert.match(source, /reasoningCapabilityCache\.clear\(\)/);
  assert.match(source, /capability\.supported_thinking_modes/);
  assert.match(source, /capability\.effort_selection_aliases/);
  assert.match(source, /思考（模型要求）/);
  assert.match(source, /thinking_mode: thinkingMode/);
  assert.doesNotMatch(source, /thinking_mode: "disabled"/);
});
