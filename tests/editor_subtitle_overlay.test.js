const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const css = fs.readFileSync(path.join(__dirname, "..", "web", "editor.css"), "utf8");

test("both non-empty preview subtitle rows have a 50% black rectangle", () => {
  assert.match(css, /\.subtitle-overlay\s*>\s*div:not\(:empty\)/);
  assert.match(css, /background:\s*rgba\(0,\s*0,\s*0,\s*\.5\)/);
  assert.match(css, /width:\s*fit-content/);
});
