(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorTutorial = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function cueText(cue, tokenById) {
    return (cue?.display_token_ids || []).map(id => tokenById.get(String(id))?.text || "").join("");
  }

  function tokenAfterTextBoundary(cue, tokenById, leftText, rightText) {
    const tokens = (cue?.display_token_ids || []).map(id => tokenById.get(String(id))).filter(Boolean);
    const text = tokens.map(token => token.text || "").join("");
    const pattern = `${leftText}${rightText}`;
    const match = text.indexOf(pattern);
    if (match < 0) return null;
    const boundaryOffset = match + String(leftText).length;
    let offset = 0;
    for (const token of tokens) {
      offset += String(token.text || "").length;
      if (offset === boundaryOffset) return token.token_id;
      if (offset > boundaryOffset) return null;
    }
    return null;
  }

  function findReferenceAnchor(activeCues, tokenById, referenceEntries) {
    const cueByTokenId = new Map();
    activeCues.forEach(cue => (cue.display_token_ids || []).forEach(id => cueByTokenId.set(String(id), cue)));
    for (const entry of referenceEntries || []) {
      const tokenId = String(Array.isArray(entry) ? entry[0] : entry?.tokenId || "");
      const change = Array.isArray(entry) ? entry[1] : entry?.change;
      if (!tokenId || String(change?.type || "") !== "replace") continue;
      if (String(change?.before || "") !== "20" || String(change?.after || "") !== "二十") continue;
      const cue = cueByTokenId.get(tokenId);
      if (!cue) continue;
      const text = cueText(cue, tokenById);
      if (!text.includes("章邯") || !text.includes("兵力")) continue;
      const position = cue.display_token_ids.map(String).indexOf(tokenId);
      const wan = cue.display_token_ids.slice(position + 1)
        .map(id => tokenById.get(String(id)))
        .find(token => String(token?.text || "").startsWith("万"));
      if (wan) return {cueId:cue.cue_id, twentyTokenId:tokenId, wanTokenId:wan.token_id};
    }
    return null;
  }

  function largestStableGap(activeCues, excludedCueIds = new Set()) {
    let best = null;
    for (let index = 1; index < activeCues.length; index += 1) {
      const previous = activeCues[index - 1];
      const following = activeCues[index];
      if (excludedCueIds.has(previous.cue_id) || excludedCueIds.has(following.cue_id)) continue;
      const gap = Number(following.start) - Number(previous.end);
      if (gap < 0.2 || (best && gap <= best.gap)) continue;
      best = {
        previousCueId:previous.cue_id,
        followingCueId:following.cue_id,
        start:Number(previous.end),
        end:Number(following.start),
        gap
      };
    }
    return best;
  }

  function resolveBeginnerAnchors(view, referenceEntries) {
    const activeCues = (view?.cue_views || []).filter(cue => cue.state === "active");
    const tokenById = new Map((view?.token_views || []).map(token => [String(token.token_id), token]));
    const textByCueId = new Map(activeCues.map(cue => [cue.cue_id, cueText(cue, tokenById)]));
    const mergeLeftIndex = activeCues.findIndex(cue => textByCueId.get(cue.cue_id).trim() === "但，");
    const mergeLeft = mergeLeftIndex >= 0 ? activeCues[mergeLeftIndex] : null;
    const mergeRight = mergeLeftIndex >= 0 ? activeCues[mergeLeftIndex + 1] : null;
    const firstSplitAfter = mergeRight && textByCueId.get(mergeRight.cue_id).includes("压缩章邯")
      ? tokenAfterTextBoundary(mergeRight, tokenById, "压缩", "章邯") : null;
    const threatCue = activeCues.find(cue => textByCueId.get(cue.cue_id).includes("威胁是")) || null;
    const threatSplitAfter = threatCue
      ? tokenAfterTextBoundary(threatCue, tokenById, "威胁", "是") : null;
    const reference = findReferenceAnchor(activeCues, tokenById, referenceEntries);
    const referenceCue = reference
      ? activeCues.find(cue => cue.cue_id === reference.cueId) : null;
    const excluded = new Set([
      mergeLeft?.cue_id, mergeRight?.cue_id, threatCue?.cue_id, reference?.cueId
    ].filter(Boolean));
    const manualGap = largestStableGap(activeCues, excluded);
    const missing = [];
    if (!activeCues[0]) missing.push("首条 Cue");
    if (!mergeLeft || !mergeRight) missing.push("“但，”及其下一条 Cue");
    if (!firstSplitAfter) missing.push("“压缩｜章邯”切分边界");
    if (!threatCue || !threatSplitAfter) missing.push("“威胁｜是”切分边界");
    if (!reference || !referenceCue) missing.push("“20／二十万”参考差异");
    if (!manualGap) missing.push("可新建 Cue 的时间空隙");
    if (missing.length) return {ok:false, missing};
    return {
      ok:true,
      anchors:{
        cue1:activeCues[0].cue_id,
        mergeLeftCue:mergeLeft.cue_id,
        mergeRightCue:mergeRight.cue_id,
        mergedCue:null,
        firstSplitAfter,
        threatCue:threatCue.cue_id,
        threatSplitAfter,
        referenceCue:reference.cueId,
        referenceTwenty:reference.twentyTokenId,
        referenceWan:reference.wanTokenId,
        referenceStart:Number(referenceCue.start),
        baselineCueIds:activeCues.map(cue => cue.cue_id),
        manualGap
      }
    };
  }

  return {cueText, tokenAfterTextBoundary, findReferenceAnchor, largestStableGap, resolveBeginnerAnchors};
});
