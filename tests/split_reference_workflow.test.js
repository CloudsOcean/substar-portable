"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "../web/split.html"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "../web/split.js"), "utf8");

test("split page keeps three boundary strategies and names punctuation mode precisely", () => {
  assert.match(html, /value="disabled">仅使用听写结果/);
  assert.match(html, /value="one_step">AI 辅助切分/);
  assert.match(html, /value="reference_script">按参考稿标点切分/);
});

test("AI segmentation exposes an optional reference while punctuation mode requires it", () => {
  assert.match(source, /const referenceEnabled = workflow === "one_step" \|\| referenceMode/);
  assert.match(source, /referenceRequirement"\)\.textContent = referenceMode \? "必选" : "选填"/);
  assert.match(source, /workflow !== "disabled" && references\.length/);
  assert.match(source, /按参考稿标点切分必须选择参考文稿/);
});

test("reference break symbols remain exclusive to punctuation mode", () => {
  assert.match(source, /referenceBreakSymbolsField"\)\.classList\.toggle\("hidden", !referenceMode\)/);
  assert.match(source, /不替代 AI 语义切分/);
});
