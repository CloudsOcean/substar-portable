"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "web", "editor.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "..", "web", "editor.js"), "utf8");

assert.match(html, /<details id="aiReviewMenu" class="editor-command-menu external-review-menu disabled">/);
assert.match(html, /class="editor-command-popover external-review-popover"/);
assert.doesNotMatch(html, /id="aiReviewFloat"|ai-review-float/);
assert.doesNotMatch(script, /aiReviewDrag|ai-review-position|clampAiReviewToViewport/);
assert.match(html, /data-review-scope="full"[^>]*>全部</);
assert.match(html, /data-review-scope="current"[^>]*>当前</);
assert.match(html, /data-review-scope="range"[^>]*>范围</);
assert.match(html, /id="externalReviewRange"/);
assert.doesNotMatch(html, /内容组成|externalReviewSummary/);

console.log("editor_review_popover_contract: ok");
