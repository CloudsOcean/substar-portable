"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const languageLayout = require("../web/editor_language.js");

test("visible character counting treats Latin words as individual characters", () => {
  assert.equal(languageLayout.characterCount("data", "en", {
    countSpaces:true,
    countPunctuation:true
  }), 4);
  assert.equal(languageLayout.characterCount("data,", "en", {
    countSpaces:true,
    countPunctuation:true
  }), 5);
  assert.equal(languageLayout.characterCount("data test", "en", {
    countSpaces:true,
    countPunctuation:true
  }), 9);
});

test("space and punctuation always count for mixed Cues", () => {
  assert.equal(languageLayout.characterCount("中文 data。", "zh", {
    countSpaces:true,
    countPunctuation:true
  }), 8);
  assert.equal(languageLayout.characterCount("中文 data。", "zh", {
    countSpaces:false,
    countPunctuation:false
  }), 8);
});

test("the frozen project language remains authoritative", () => {
  assert.equal(
    languageLayout.resolveSourceLanguage("zh", "Scott, I want to jump in here."),
    "zh"
  );
  assert.equal(
    languageLayout.resolveSourceLanguage("en", "这是实际返回的中文字幕"),
    "en"
  );
});

test("Auto delegates to document language detection", () => {
  assert.equal(languageLayout.resolveSourceLanguage("Auto", "English source"), "en");
  assert.equal(languageLayout.resolveSourceLanguage("Auto", "这是中文字幕"), "zh");
});

test("virtual boundaries are continuous for CJK and spaced for word languages", () => {
  assert.equal(languageLayout.tokenUnitKind("重"), "character");
  assert.equal(languageLayout.virtualBoundaryKind("重", "庆"), "character");
  assert.equal(languageLayout.tokenUnitKind("Substar"), "word");
  assert.equal(languageLayout.virtualBoundaryKind("hello", "world", "en"), "word");
  assert.equal(languageLayout.virtualBoundaryKind("中", "Substar", "zh"), "character");
  assert.equal(languageLayout.virtualBoundaryKind("秦", "23", "zh"), "character");
  assert.equal(languageLayout.virtualBoundaryKind("Say", "Goodbye", "zh"), "word");
});
