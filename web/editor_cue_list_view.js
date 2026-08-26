(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorCueListView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function pageWindow(cueCount, requestedStart, activeIndex, pageSize, preservePage) {
    const size = Math.max(1, Number(pageSize) || 1);
    const count = Math.max(0, Number(cueCount) || 0);
    if (!count) return {start:0, end:0};
    let start = Math.max(0, Number(requestedStart) || 0);
    if (!preservePage && (activeIndex < start || activeIndex >= start + size)) {
      start = Math.max(0, Math.min(count - size, activeIndex - Math.floor(size / 3)));
    }
    start = Math.max(0, Math.min(start, Math.max(0, count - 1)));
    return {start, end:Math.min(count, start + size)};
  }

  function preservedWindow(cueCount, start, end) {
    const count = Math.max(0, Number(cueCount) || 0);
    if (!count) return {start:0, end:0};
    const safeStart = Math.max(0, Math.min(Number(start) || 0, count - 1));
    const safeEnd = Math.max(safeStart, Math.min(Number(end) || 0, count));
    return {start:safeStart, end:safeEnd};
  }

  function createCueListView({container, pageSize = 160, renderCue, onWindowChange = null}) {
    if (!container || typeof renderCue !== "function") {
      throw new Error("Cue list view requires a container and cue renderer");
    }

    let context = null;
    let windowStart = 0;
    let windowEnd = 0;
    let loading = false;

    function captureViewport() {
      const listRect = container.getBoundingClientRect();
      const anchor = [...container.querySelectorAll(".cue-row")]
        .find(row => row.getBoundingClientRect().bottom > listRect.top + 1);
      return {
        scrollTop:container.scrollTop,
        cueId:anchor?.dataset.cueId || null,
        offset:anchor ? anchor.getBoundingClientRect().top - listRect.top : 0
      };
    }

    function restoreViewport(viewport) {
      if (!viewport) return;
      const anchor = viewport.cueId
        ? [...container.querySelectorAll(".cue-row")].find(row => row.dataset.cueId === viewport.cueId)
        : null;
      if (!anchor) {
        container.scrollTop = viewport.scrollTop;
        return;
      }
      const listRect = container.getBoundingClientRect();
      container.scrollTop += anchor.getBoundingClientRect().top - listRect.top - viewport.offset;
    }

    const notify = () => {
      if (typeof onWindowChange === "function") onWindowChange({start:windowStart, end:windowEnd});
    };

    const appendRange = (start, end) => {
      if (!context || start >= end) return;
      const safeStart = Math.max(0, Math.min(start, context.cues.length));
      const safeEnd = Math.max(safeStart, Math.min(end, context.cues.length));
      const fragment = document.createDocumentFragment();
      for (let index = safeStart; index < safeEnd; index += 1) {
        const cue = context.cues[index];
        if (cue) fragment.append(renderCue(cue, index, context.tokenById));
      }
      container.append(fragment);
    };

    const prependRange = (start, end) => {
      if (!context || start >= end) return;
      const safeStart = Math.max(0, Math.min(start, context.cues.length));
      const safeEnd = Math.max(safeStart, Math.min(end, context.cues.length));
      const previousHeight = container.scrollHeight;
      const fragment = document.createDocumentFragment();
      for (let index = safeStart; index < safeEnd; index += 1) {
        const cue = context.cues[index];
        if (cue) fragment.append(renderCue(cue, index, context.tokenById));
      }
      container.prepend(fragment);
      container.scrollTop += container.scrollHeight - previousHeight;
    };

    container.addEventListener("scroll", () => {
      if (!context || loading) return;
      loading = true;
      requestAnimationFrame(() => {
        if (container.scrollTop + container.clientHeight >= container.scrollHeight - 480 && windowEnd < context.cues.length) {
          const nextEnd = Math.min(context.cues.length, windowEnd + pageSize);
          appendRange(windowEnd, nextEnd);
          windowEnd = nextEnd;
          notify();
        }
        if (container.scrollTop <= 320 && windowStart > 0) {
          const nextStart = Math.max(0, windowStart - pageSize);
          prependRange(nextStart, windowStart);
          windowStart = nextStart;
          notify();
        }
        loading = false;
      });
    }, {passive:true});

    function render({cues, tokenById, activeCueId, pageStart = 0, preservePage = false}) {
      const viewport = preservePage ? captureViewport() : null;
      context = {cues, tokenById};
      const activeIndex = Math.max(0, cues.findIndex(cue => cue.cue_id === activeCueId));
      const preserved = preservedWindow(cues.length, windowStart, windowEnd);
      const keepWindow = preservePage
        && preserved.end > preserved.start
        && preserved.start < cues.length;
      const page = keepWindow
        ? preserved
        : pageWindow(cues.length, pageStart, activeIndex, pageSize, false);
      windowStart = page.start;
      windowEnd = page.end;
      const fragment = document.createDocumentFragment();
      for (let index = page.start; index < page.end; index += 1) {
        const cue = cues[index];
        if (cue) fragment.append(renderCue(cue, index, tokenById));
      }
      // Rebuild the visible Cue window from the authoritative view in one DOM
      // replacement.  Never reuse a changed row with the same Cue ID: doing so
      // can leave an interactive stale duplicate after split/merge/undo.
      container.replaceChildren(fragment);
      restoreViewport(viewport);
      notify();
      return page;
    }

    function setActive(cueId) {
      container.querySelectorAll(".cue-row.current").forEach(row =>
        row.classList.remove("current")
      );
      if (!cueId) return;
      const row = [...container.querySelectorAll(".cue-row")]
        .find(item => item.dataset.cueId === cueId);
      row?.classList.add("current");
    }

    return {render, setActive};
  }

  return {createCueListView, pageWindow, preservedWindow};
});
