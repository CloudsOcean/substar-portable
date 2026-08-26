const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "settings.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "settings.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web", "settings.css"), "utf8");

test("settings edit registered prompt components while keeping routes read-only", () => {
  assert.match(html, /data-panel="prompts"/);
  assert.match(html, /data-settings-panel="prompts"/);
  assert.match(html, /生产提示词/);
  assert.match(html, /路由只读/);
  assert.match(html, /id="promptSaveButton"/);
  assert.match(html, /textarea id="promptSourceView"/);
  assert.match(html, /创建项目时冻结快照/);
  assert.match(html, /执行时读取当前注册表/);
});

test("prompt UI renders catalog routes and lazily reads registered components", () => {
  assert.match(js, /api\("\/api\/prompts"\)/);
  assert.match(js, /\/api\/prompts\/content\?path=/);
  assert.match(js, /method: "PUT"/);
  assert.match(js, /expected_sha256/);
  assert.match(js, /promptVariantLabel/);
  assert.match(js, /prompt-route-chain/);
  assert.match(js, /\["environment", "prompts"\]\.includes\(name\)/);
  assert.match(css, /\.prompt-lifecycle/);
  assert.match(css, /\.prompt-workspace/);
  assert.match(css, /#promptSourceView/);
});
