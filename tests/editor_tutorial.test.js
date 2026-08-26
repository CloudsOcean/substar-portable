"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  tokenAfterTextBoundary,
  resolveBeginnerAnchors
} = require("../web/editor_tutorial.js");

let tokenSequence = 0;
function tokens(texts) {
  return texts.map(text => ({token_id:`token_${++tokenSequence}`, text}));
}

function cue(cueId, index, start, end, cueTokens) {
  return {
    cue_id:cueId,
    index,
    start,
    end,
    state:"active",
    display_token_ids:cueTokens.map(token => token.token_id)
  };
}

const first = tokens(["开", "始"]);
const mergeLeft = tokens(["但，"]);
const mergeRight = tokens(["魏", "军", "会", "压", "缩", "章", "邯", "二十", "万"]);
const threat = tokens(["补", "给", "线", "被", "威", "胁", "是", "问", "题"]);
const reference = tokens(["由", "于", "章", "邯", "尚", "有", "兵", "力", "二十", "万，"]);
const gapLeft = tokens(["上一句。​"]);
const gapRight = tokens(["下一句，"]);
const tokenViews = [...first, ...mergeLeft, ...mergeRight, ...threat, ...reference, ...gapLeft, ...gapRight];
const cueViews = [
  cue("cue_first", 0, 0, 1, first),
  cue("cue_merge_left", 1, 1.1, 1.3, mergeLeft),
  cue("cue_merge_right", 2, 1.4, 3, mergeRight),
  cue("cue_threat", 3, 3.1, 4.5, threat),
  cue("cue_reference", 4, 4.8, 6, reference),
  cue("cue_gap_left", 5, 6.1, 7, gapLeft),
  cue("cue_gap_right", 6, 8, 9, gapRight)
];
const referenceTwenty = reference.find(token => token.text === "二十");
const result = resolveBeginnerAnchors({cue_views:cueViews, token_views:tokenViews}, [[
  referenceTwenty.token_id,
  {type:"replace", before:"20", after:"二十"}
]]);

assert.equal(result.ok, true);
assert.equal(result.anchors.mergeLeftCue, "cue_merge_left");
assert.equal(result.anchors.mergeRightCue, "cue_merge_right");
assert.equal(result.anchors.firstSplitAfter, mergeRight.find(token => token.text === "缩").token_id);
assert.equal(result.anchors.threatCue, "cue_threat");
assert.equal(result.anchors.threatSplitAfter, threat.find(token => token.text === "胁").token_id);
assert.equal(result.anchors.referenceCue, "cue_reference");
assert.equal(result.anchors.referenceTwenty, referenceTwenty.token_id);
assert.equal(result.anchors.referenceWan, reference.find(token => token.text === "万，").token_id);
assert.equal(result.anchors.manualGap.followingCueId, "cue_gap_right");
assert.equal(result.anchors.manualGap.gap, 1);
assert.deepEqual(result.anchors.baselineCueIds, cueViews.map(item => item.cue_id));

const joined = tokens(["压缩", "章邯"]);
const joinedCue = cue("cue_joined", 0, 0, 1, joined);
assert.equal(
  tokenAfterTextBoundary(joinedCue, new Map(joined.map(token => [token.token_id, token])), "压缩", "章邯"),
  joined[0].token_id
);
assert.equal(
  tokenAfterTextBoundary(joinedCue, new Map(joined.map(token => [token.token_id, token])), "压", "缩章邯"),
  null
);

const missing = resolveBeginnerAnchors({cue_views:[cueViews[0]], token_views:first}, []);
assert.equal(missing.ok, false);
assert.ok(missing.missing.includes("“压缩｜章邯”切分边界"));
assert.ok(missing.missing.includes("“20／二十万”参考差异"));

const editorSource = fs.readFileSync(path.join(__dirname, "../web/editor.js"), "utf8");
assert.match(
  editorSource,
  /#refreshDocumentBottom"\)\.onclick = \(\) => state\.projectId && loadProject\(state\.projectId\);/,
  "ordinary refresh must reload the latest revision without resetting a tutorial project"
);
assert.match(
  editorSource,
  /restartEditorTutorial[\s\S]*?loadProject\(state\.projectId, \{restoreTranslation:false, resetTutorial:true, showTutorialIntro:false\}\)/,
  "only the explicit tutorial restart path should request a tutorial reset"
);
assert.doesNotMatch(
  editorSource,
  /loadProject\((?:state\.projectId|button\.dataset\.projectId), \{resetTutorial:true\}\)/,
  "opening, switching, and refreshing projects must preserve tutorial revisions"
);

console.log("editor_tutorial: ok");
