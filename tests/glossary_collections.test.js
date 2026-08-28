const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "glossary.html"), "utf8");
const script = fs.readFileSync(path.join(root, "web", "glossary.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web", "glossary.css"), "utf8");

assert.match(html, /id="addCollection" class="collection-create"/);
assert.match(html, /id="deleteCollectionDialog"/);
assert.match(html, /id="confirmDeleteCollection"/);
assert.match(script, /function requestDeleteCollection\(id\)/);
assert.match(script, /function deleteCollection\(event\)/);
assert.match(script, /glossary_id.*\.value === id\) card\.remove\(\)/);
assert.match(css, /\.collection-row:hover \.collection-delete/);
assert.match(css, /dialog\.collection-modal/);

console.log("glossary_collections: ok");
