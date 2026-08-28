const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "split.html"), "utf8");
const script = fs.readFileSync(path.join(root, "web", "split.js"), "utf8");

assert.doesNotMatch(html, /<span>项目词库<\/span>/);
assert.doesNotMatch(html, /仅全局词库/);
assert.doesNotMatch(html, /Qwen 热词接口暂未接入/);
assert.match(html, /<input id="glossaryInput" type="hidden" value="">/);
assert.match(script, /\$\("#glossaryInput"\)\.value = "";/);
assert.doesNotMatch(script, /api\("\/api\/glossary"\)/);

console.log("split_project_glossary_hidden: ok");
