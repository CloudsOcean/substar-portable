"use strict";

const assert = require("node:assert/strict");
const {createCueListView} = require("../web/editor_cue_list_view.js");

function cue(cueId, text) {
  return {cue_id:cueId, text};
}

function row(value) {
  return {dataset:{cueId:value.cue_id}, text:value.text};
}

function fixture() {
  let scrollReads = 0;
  let scrollWrites = 0;
  const container = {
    children:[],
    addEventListener() {},
    replaceChildren(fragment) {
      this.children = [...fragment.children];
    }
  };
  Object.defineProperty(container, "scrollTop", {
    get() {
      scrollReads += 1;
      return 317;
    },
    set(_value) {
      scrollWrites += 1;
    }
  });
  global.document = {
    createDocumentFragment() {
      return {
        children:[],
        append(node) {
          this.children.push(node);
        }
      };
    }
  };
  return {
    container,
    scrollAccesses:() => ({reads:scrollReads, writes:scrollWrites})
  };
}

function ids(container) {
  return container.children.map(node => node.dataset.cueId);
}

{
  const {container, scrollAccesses} = fixture();
  const view = createCueListView({
    container,
    pageSize:160,
    renderCue:row
  });

  view.render({
    cues:[cue("cue_a", "A"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a"
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_b"]);

  // A split changes cue_a and inserts cue_right. The old cue_a row must not
  // survive alongside its authoritative replacement.
  view.render({
    cues:[cue("cue_a", "A-left"), cue("cue_right", "A-right"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a"
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_right", "cue_b"]);
  assert.equal(new Set(ids(container)).size, container.children.length);

  // Undo is another full authoritative redraw and must remove the split row.
  view.render({
    cues:[cue("cue_a", "A"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a"
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_b"]);

  // Even a DOM already polluted by the historical bug is healed in one pass.
  container.children.push(row(cue("cue_a", "stale")), row(cue("cue_a", "stale-2")));
  view.render({
    cues:[cue("cue_a", "A"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a"
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_b"]);
  assert.deepEqual(scrollAccesses(), {reads:0, writes:0});
}

console.log("editor_cue_list_view: ok");
