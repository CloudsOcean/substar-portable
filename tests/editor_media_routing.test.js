const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const editorJs = fs.readFileSync(path.join(root, "web", "editor.js"), "utf8");
const editorHtml = fs.readFileSync(path.join(root, "web", "editor.html"), "utf8");

test("editor routes audio and video through one active media clock", () => {
  assert.match(editorHtml, /<video id="projectVideo"/);
  assert.match(editorHtml, /<audio id="projectAudio"/);
  assert.match(editorJs, /function activeMedia\(\)/);
  assert.match(editorJs, /api\(projectPath\("\/media-info"\)\)/);
  assert.match(editorJs, /configureMedia\(mediaInfo\?\.kind\)/);
  assert.doesNotMatch(editorJs, /requestVideoFrameCallback|cancelVideoFrameCallback/);
});

test("playback UI uses animation frames with timeupdate as an unconditional fallback", () => {
  assert.match(editorJs, /state\.playbackFrameHandle = requestAnimationFrame\(tick\)/);
  assert.match(editorJs, /on\("timeupdate", event => \{\s*updatePlaybackUi\(/);
  assert.doesNotMatch(editorJs, /timeupdate[\s\S]{0,180}!state\.playbackFrameHandle/);
});
