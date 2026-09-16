"use strict";

const assert = require("node:assert/strict");
const {createCueListView} = require("../web/editor_cue_list_view.js");

function cue(cueId, text) {
  return {cue_id:cueId, text};
}

function row(value, index = 0) {
  const number = {textContent:String(index + 1)};
  return {
    dataset:{cueId:value.cue_id},
    text:value.text,
    number,
    parent:null,
    querySelector(selector) {
      return selector === ".cue-meta strong" ? this.number : null;
    },
    isEqualNode(other) {
      return this.dataset.cueId === other?.dataset?.cueId
        && this.text === other.text
        && this.number.textContent === other.number.textContent;
    },
    remove() {
      if (!this.parent) return;
      const index = this.parent.children.indexOf(this);
      if (index >= 0) this.parent.children.splice(index, 1);
      this.parent = null;
    }
  };
}

function fixture({dynamic = false} = {}) {
  let scrollReads = 0;
  let scrollWrites = 0;
  let scrollTop = 317;
  const inserted = new Map();
  const frames = [];
  let scrollHandler = null;
  const container = {
    children:[],
    style:{},
    clientHeight:600,
    scrollHeight:2400,
    addEventListener(name, handler) {
      if (name === "scroll") scrollHandler = handler;
    },
    insertBefore(node, reference) {
      if (node.parent === this) {
        const currentIndex = this.children.indexOf(node);
        if (currentIndex >= 0) this.children.splice(currentIndex, 1);
      }
      const referenceIndex = reference ? this.children.indexOf(reference) : -1;
      const index = referenceIndex >= 0 ? referenceIndex : this.children.length;
      this.children.splice(index, 0, node);
      node.parent = this;
      inserted.set(node, (inserted.get(node) || 0) + 1);
    },
    replaceChild(node, current) {
      const index = this.children.indexOf(current);
      if (index < 0) throw new Error("replacement target missing");
      this.children[index] = node;
      current.parent = null;
      node.parent = this;
    },
    append(fragment) {
      fragment.children.forEach(node => this.insertBefore(node, null));
    },
    prepend(fragment) {
      [...fragment.children].reverse().forEach(node =>
        this.insertBefore(node, this.children[0] || null)
      );
    }
  };
  Object.defineProperty(container, "scrollTop", {
    get() {
      scrollReads += 1;
      return scrollTop;
    },
    set(value) {
      scrollWrites += 1;
      scrollTop = value;
    }
  });
  if (dynamic) Object.defineProperty(container, "scrollHeight", {get:() =>
    container.children.length * 80 + (parseFloat(container.style.paddingTop) || 0) + (parseFloat(container.style.paddingBottom) || 0)});
  global.document = {
    createDocumentFragment() {
      return {
        children:[],
        append(node) { this.children.push(node); }
      };
    }
  };
  global.requestAnimationFrame = callback => {
    frames.push(callback);
    return frames.length;
  };
  return {
    container,
    fireScroll:() => scrollHandler?.(),
    flushFrames:() => {
      while (frames.length) frames.shift()();
    },
    insertionCount:node => inserted.get(node) || 0,
    scrollAccesses:() => ({reads:scrollReads, writes:scrollWrites})
  };
}

function ids(container) {
  return container.children.map(node => node.dataset.cueId);
}

{
  const {container, fireScroll, flushFrames} = fixture({dynamic:true});
  let window;
  const view = createCueListView({container, pageSize:160, renderCue:row, onWindowChange:value => {window=value;}});
  const cues = Array.from({length:10000}, (_, index) => cue(`long_${index}`, `Cue ${index}`));
  view.render({cues, tokenById:new Map(), activeCueId:cues[0].cue_id});
  for (let i=0; i<70 && window.end<cues.length; i++) {
    container.scrollTop = container.scrollHeight - (parseFloat(container.style.paddingBottom) || 0) - container.clientHeight;
    fireScroll(); flushFrames();
    assert.ok(container.children.length <= 480, "forward scroll has a bounded DOM");
    assert.equal(new Set(ids(container)).size, container.children.length);
  }
  assert.equal(window.end,10000);
  for (let i=0; i<70 && window.start>0; i++) {
    container.scrollTop = parseFloat(container.style.paddingTop) || 0;
    fireScroll(); flushFrames();
    assert.ok(container.children.length <= 480, "backward scroll has a bounded DOM");
  }
  assert.equal(window.start,0);
  assert.equal(container.children[0].dataset.cueId,"long_0");
  assert.equal(parseFloat(container.style.paddingTop),0);
}

{
  const {container, insertionCount, scrollAccesses} = fixture();
  const view = createCueListView({
    container,
    pageSize:160,
    renderCue:row
  });

  view.render({
    cues:[cue("cue_a", "A"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a",
    preservePage:true
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_b"]);
  const originalB = container.children[1];
  const originalBInsertions = insertionCount(originalB);

  // A split changes cue_a and inserts cue_right. The old cue_a row must not
  // survive alongside its authoritative replacement.
  view.render({
    cues:[cue("cue_a", "A-left"), cue("cue_right", "A-right"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a",
    preservePage:true
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_right", "cue_b"]);
  assert.equal(new Set(ids(container)).size, container.children.length);
  assert.equal(container.children[2], originalB, "unchanged Cue DOM must be retained");
  assert.equal(insertionCount(originalB), originalBInsertions, "unchanged Cue DOM must not be moved");
  assert.equal(originalB.number.textContent, "3", "retained Cue number must be patched");

  // Undo removes only the split row and retains the unchanged tail Cue.
  view.render({
    cues:[cue("cue_a", "A"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a",
    preservePage:true
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_b"]);
  assert.equal(container.children[1], originalB);

  // Even a DOM already polluted by the historical bug is healed in one pass.
  const staleRows = [row(cue("cue_a", "stale")), row(cue("cue_a", "stale-2"))];
  staleRows.forEach(node => { node.parent = container; container.children.push(node); });
  view.render({
    cues:[cue("cue_a", "A"), cue("cue_b", "B")],
    tokenById:new Map(),
    activeCueId:"cue_a",
    preservePage:true
  });
  assert.deepEqual(ids(container), ["cue_a", "cue_b"]);
  assert.deepEqual(scrollAccesses(), {reads:0, writes:0});
  const repairedBInsertions = insertionCount(originalB);

  // Repeated splits perform keyed inserts only. The unchanged tail never gets
  // replaced and no redraw reads or writes the list's scrollbar.
  const rights = [];
  for (let index = 0; index < 12; index += 1) {
    rights.push(cue(`cue_right_${index}`, `R-${index}`));
    view.render({
      cues:[cue("cue_a", `A-${index}`), ...rights, cue("cue_b", "B")],
      tokenById:new Map(),
      activeCueId:"cue_a",
      preservePage:true
    });
    assert.equal(container.children.at(-1), originalB);
    assert.equal(insertionCount(originalB), repairedBInsertions);
  }
  assert.deepEqual(scrollAccesses(), {reads:0, writes:0});
}

{
  const {container, fireScroll, flushFrames, scrollAccesses} = fixture();
  const view = createCueListView({container, pageSize:160, renderCue:row});
  const cues = Array.from({length:1000}, (_, index) => cue(`cue_${index}`, `Cue ${index}`));
  view.render({
    cues,
    tokenById:new Map(),
    activeCueId:"cue_0",
    preservePage:false
  });
  assert.equal(container.children.length, 160, "initial long document render stays windowed");
  assert.deepEqual(scrollAccesses(), {reads:0, writes:0});

  // User scrolling near the bottom loads one more segment, not the whole file.
  container.scrollHeight = 800;
  fireScroll();
  flushFrames();
  assert.equal(container.children.length, 320);
  assert.ok(container.children.length < cues.length);
  const afterUserScroll = scrollAccesses();

  // A topology redraw retains exactly the loaded window and does not touch the
  // scrollbar or materialize the remaining 680 Cues.
  const splitCues = [cue("cue_0", "Cue 0 left"), cue("cue_split", "Cue 0 right"), ...cues.slice(1)];
  view.render({
    cues:splitCues,
    tokenById:new Map(),
    activeCueId:"cue_0",
    preservePage:true
  });
  assert.equal(container.children.length, 320);
  assert.ok(container.children.length < splitCues.length);
  assert.deepEqual(scrollAccesses(), afterUserScroll);
}

console.log("editor_cue_list_view: ok");

{
  const {container} = fixture();
  const renderCue = (value, index) => {
    const node = row(value, index);
    node.target = {value:value.text, disabled:!!value.disabled, selectionStart:2, selectionEnd:4, matches:selector=>selector === "[data-target-edit]"};
    const query = node.querySelector.bind(node);
    node.querySelector = selector => selector === "[data-target-edit]" ? node.target : query(selector);
    node.contains = target => target === node.target;
    const equal = node.isEqualNode.bind(node);
    node.isEqualNode = other => equal(other) && node.target.disabled === other.target.disabled;
    return node;
  };
  const view = createCueListView({container, renderCue});
  const render = cues => view.render({cues, tokenById:new Map(), activeCueId:"b", preservePage:true});
  render([cue("a","A"),cue("b","B")]);
  const editing = container.children[1];
  document.activeElement = editing.target;
  editing.target.value = "unsaved composition";
  render([cue("a","A saved"),cue("b","B server refreshed")]);
  assert.equal(container.children[0].text,"A saved", "other rows update normally");
  assert.equal(container.children[1],editing,"saving previous cue keeps the active row");
  assert.equal(editing.target.value,"unsaved composition");
  assert.equal(editing.target.selectionStart,2);
  assert.equal(editing.target.selectionEnd,4);
  document.activeElement = null;
  render([cue("a","A saved"),cue("b","B saved")]);
  assert.notEqual(container.children[1],editing,"after blur authoritative content updates");
  document.activeElement = container.children[1].target;
  const unlocked = container.children[1];
  render([cue("a","A saved"),{...cue("b","B saved"),disabled:true}]);
  assert.notEqual(container.children[1],unlocked,"task locks still replace active edits");
  render([cue("a","A saved")]);
  assert.deepEqual(ids(container),["a"],"deleted cues are not retained");
}
