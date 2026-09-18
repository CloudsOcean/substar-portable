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

  function createCueListView({container, pageSize = 160, renderCue, onWindowChange = null, positionSlider = null}) {
    if (!container || typeof renderCue !== "function") {
      throw new Error("Cue list view requires a container and cue renderer");
    }

    let context = null;
    let windowStart = 0;
    let windowEnd = 0;
    let windowLoading = false;
    let topGap = 0;
    let bottomGap = 0;
    let sliderDragging = false;
    const maxRows = pageSize * 3;
    const baseStyle = typeof getComputedStyle === "function" ? getComputedStyle(container) : {};
    const baseTop = parseFloat(baseStyle.paddingTop) || 0;
    const baseBottom = parseFloat(baseStyle.paddingBottom) || 0;
    const measuredHeights = new Map();
    const rangeHeight = (start, end) => context.cues.slice(start, end).reduce((sum, cue) => sum + (measuredHeights.get(cue.cue_id) || 80), 0);
    const gapStyle = () => {
      if (!container.style) return;
      container.style.paddingTop = `${baseTop + topGap}px`;
      container.style.paddingBottom = `${baseBottom + bottomGap}px`;
    };
    const rowHeight = node => Number(node.getBoundingClientRect?.().height || node.offsetHeight || 80)
      + (typeof getComputedStyle === "function" ? parseFloat(getComputedStyle(node).marginBottom) || 0 : 0);
    const trimWindow = direction => {
      const focus = document.activeElement;
      while (windowEnd - windowStart > maxRows) {
        const index = direction === "forward" ? windowStart++ : --windowEnd;
        const id = context.cues[index]?.cue_id;
        const node = [...container.children].find(row => row.dataset?.cueId === id);
        if (!node) continue;
        // One focused row may remain outside the window until blur.
        if (node.contains?.(focus)) continue;
        measuredHeights.set(id, rowHeight(node));
        if (direction === "forward") topGap += rowHeight(node);
        else bottomGap += rowHeight(node);
        node.remove();
      }
      const allowed = new Set(context.cues.slice(windowStart, windowEnd).map(cue => cue.cue_id));
      [...container.children].forEach(node => {
        if (!allowed.has(node.dataset?.cueId) && !node.contains?.(focus)) {
          const index = context.cues.findIndex(cue => cue.cue_id === node.dataset?.cueId);
          const height = rowHeight(node);
          measuredHeights.set(node.dataset?.cueId, height);
          if (index >= 0 && index < windowStart) topGap += height;
          else if (index >= windowEnd) bottomGap += height;
          node.remove();
        }
      });
      gapStyle();
    };

    const reuseOrPatchRow = (existing, next) => {
      if (!existing || existing.dataset.cueId !== next.dataset.cueId) return next;
      // A previous edit's asynchronous save can refresh the list while the user
      // is already typing in another row. Never replace that live textarea:
      // doing so loses focus, selection, composition and its unsaved draft.
      const focus = document.activeElement;
      const nextTarget = next.querySelector(focus?.matches?.("[data-source-edit]") ? "[data-source-edit]" : "[data-target-edit]");
      if ((focus?.matches?.("[data-target-edit]") || focus?.matches?.("[data-source-edit]")) && existing.contains?.(focus)
          && nextTarget && !nextTarget.disabled) {
        const number = existing.querySelector(".cue-meta strong");
        const nextNumber = next.querySelector(".cue-meta strong");
        if (number && nextNumber) number.textContent = nextNumber.textContent;
        return existing;
      }
      if (existing.isEqualNode(next)) return existing;
      const existingNumber = existing.querySelector(".cue-meta strong");
      const nextNumber = next.querySelector(".cue-meta strong");
      if (!existingNumber || !nextNumber) return next;
      const authoritativeNumber = nextNumber.textContent;
      nextNumber.textContent = existingNumber.textContent;
      const differsOnlyByNumber = existing.isEqualNode(next);
      nextNumber.textContent = authoritativeNumber;
      if (!differsOnlyByNumber) return next;
      existingNumber.textContent = authoritativeNumber;
      return existing;
    };

    const reconcileRows = desiredRows => {
      // One existing node may be reused for each authoritative Cue ID. Any
      // historical duplicate is intentionally left out of `kept` and removed.
      const existingById = new Map();
      [...container.children].forEach(node => {
        const cueId = node?.dataset?.cueId;
        if (cueId && !existingById.has(cueId)) existingById.set(cueId, node);
      });
      const finalRows = desiredRows.map(next =>
        reuseOrPatchRow(existingById.get(next.dataset.cueId), next)
      );
      finalRows.forEach((node, index) => {
        const current = container.children[index] || null;
        if (current === node) return;
        if (current?.dataset?.cueId === node.dataset.cueId) {
          container.replaceChild(node, current);
          return;
        }
        container.insertBefore(node, current);
      });
      const kept = new Set(finalRows);
      [...container.children].forEach(node => {
        if (!kept.has(node)) node.remove();
      });
    };

    const syncPosition = () => {
      if (!positionSlider || sliderDragging) return;
      const count = context?.cues.length || 0;
      positionSlider.disabled = count < 2;
      let percentage = 0;
      if (count > 1) {
        const top = container.getBoundingClientRect().top;
        const first = [...container.children].find(node => node.dataset?.cueId && node.getBoundingClientRect().bottom > top);
        if (first) {
          const index = context.cues.findIndex(cue => cue.cue_id === first.dataset.cueId);
          const rect = first.getBoundingClientRect();
          const fraction = Math.max(0, Math.min(1, (top - rect.top) / Math.max(1, rect.height)));
          percentage = 100 * (index + fraction) / (count - 1);
        }
        if (windowEnd === count && container.scrollTop + container.clientHeight >= container.scrollHeight - 2) percentage = 100;
        if (windowStart === 0 && container.scrollTop <= baseTop) percentage = 0;
      }
      positionSlider.value = String(Math.max(0, Math.min(100, percentage)));
      positionSlider.setAttribute("aria-valuetext", `${Math.round(percentage)}%`);
    };

    const notify = () => {
      if (typeof onWindowChange === "function") onWindowChange({start:windowStart, end:windowEnd});
      syncPosition();
    };

    function jumpToPercent(value) {
      if (!context?.cues.length) return;
      const percent = Math.max(0, Math.min(100, Number(value) || 0));
      const count = context.cues.length;
      const index = Math.round(percent / 100 * (count - 1));
      windowStart = Math.max(0, Math.min(count - pageSize, index - Math.floor(pageSize / 3)));
      windowEnd = Math.min(count, windowStart + pageSize);
      topGap = 0; bottomGap = 0; gapStyle();
      reconcileRows(context.cues.slice(windowStart, windowEnd).map((cue, offset) => renderCue(cue, windowStart + offset, context.tokenById)));
      const row = [...container.children].find(node => node.dataset?.cueId === context.cues[index].cue_id);
      if (percent === 100) container.scrollTop = container.scrollHeight;
      else if (row) container.scrollTop += row.getBoundingClientRect().top - container.getBoundingClientRect().top - baseTop;
      if (percent === 0) container.scrollTop = 0;
      notify();
    }

    if (positionSlider) {
      positionSlider.addEventListener("pointerdown", () => {
        // Commit the current editor field before replacing its rendered window.
        if (container.contains(document.activeElement)) document.activeElement.blur();
        sliderDragging = true;
      });
      positionSlider.addEventListener("input", () => jumpToPercent(positionSlider.value));
      positionSlider.addEventListener("keydown", event => {
        const last = (context?.cues.length || 0) - 1;
        if (last <= 0 || !["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const index = Math.round(Number(positionSlider.value) / 100 * last);
        const next = event.key === "Home" ? 0 : event.key === "End" ? last : index + (event.key === "ArrowDown" ? 1 : -1);
        jumpToPercent(100 * next / last);
      });
      const finishDrag = () => { sliderDragging = false; syncPosition(); };
      positionSlider.addEventListener("change", finishDrag);
      positionSlider.addEventListener("pointerup", finishDrag);
      positionSlider.addEventListener("pointercancel", finishDrag);
    }

    const appendRange = (start, end) => {
      if (!context || start >= end) return;
      const safeStart = Math.max(0, Math.min(start, context.cues.length));
      const safeEnd = Math.max(safeStart, Math.min(end, context.cues.length));
      const fragment = document.createDocumentFragment();
      const existing = new Set([...container.children].map(node => node.dataset?.cueId));
      for (let index = safeStart; index < safeEnd; index += 1) {
        const cue = context.cues[index];
        if (cue && !existing.has(cue.cue_id)) fragment.append(renderCue(cue, index, context.tokenById));
      }
      container.append(fragment);
    };

    const prependRange = (start, end, preserveGap = false) => {
      if (!context || start >= end) return;
      const safeStart = Math.max(0, Math.min(start, context.cues.length));
      const safeEnd = Math.max(safeStart, Math.min(end, context.cues.length));
      const previousHeight = container.scrollHeight;
      const fragment = document.createDocumentFragment();
      const existing = new Set([...container.children].map(node => node.dataset?.cueId));
      for (let index = safeStart; index < safeEnd; index += 1) {
        const cue = context.cues[index];
        if (cue && !existing.has(cue.cue_id)) fragment.append(renderCue(cue, index, context.tokenById));
      }
      container.prepend(fragment);
      if (!preserveGap) container.scrollTop += container.scrollHeight - previousHeight;
    };

    container.addEventListener("scroll", () => {
      if (!context || windowLoading) return;
      windowLoading = true;
      requestAnimationFrame(() => {
        if (container.scrollTop + container.clientHeight >= container.scrollHeight - bottomGap - 480 && windowEnd < context.cues.length) {
          const nextEnd = Math.min(context.cues.length, windowEnd + pageSize);
          bottomGap = Math.max(0, bottomGap - rangeHeight(windowEnd, nextEnd));
          appendRange(windowEnd, nextEnd);
          windowEnd = nextEnd;
          trimWindow("forward");
          notify();
        }
        if (container.scrollTop <= topGap + 320 && windowStart > 0) {
          const nextStart = Math.max(0, windowStart - pageSize);
          const preserveGap = topGap > 0;
          topGap = Math.max(0, topGap - rangeHeight(nextStart, windowStart));
          gapStyle();
          prependRange(nextStart, windowStart, preserveGap);
          windowStart = nextStart;
          trimWindow("backward");
          notify();
        }
        windowLoading = false;
        syncPosition();
      });
    }, {passive:true});

    function render({cues, tokenById, activeCueId, pageStart = 0, preservePage = false}) {
      context = {cues, tokenById};
      const activeIndex = Math.max(0, cues.findIndex(cue => cue.cue_id === activeCueId));
      const preserved = preservedWindow(cues.length, windowStart, windowEnd);
      const keepWindow = preservePage
        && preserved.end > preserved.start
        && preserved.start < cues.length;
      const preservedSize = Math.min(maxRows, Math.max(pageSize, preserved.end - preserved.start));
      const page = keepWindow
        ? pageWindow(cues.length, preserved.start, activeIndex, preservedSize, true)
        : pageWindow(cues.length, pageStart, activeIndex, pageSize, false);
      if (!keepWindow) { topGap = 0; bottomGap = 0; gapStyle(); }
      windowStart = page.start;
      windowEnd = page.end;
      const desiredRows = [];
      for (let index = page.start; index < page.end; index += 1) {
        const cue = cues[index];
        if (cue) desiredRows.push(renderCue(cue, index, tokenById));
      }
      // Preserve unchanged Cue DOM nodes. A split updates one row and inserts
      // one row instead of replacing the whole scrolling surface.
      reconcileRows(desiredRows);
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

    return {render, setActive, jumpToPercent};
  }

  return {createCueListView, pageWindow, preservedWindow};
});
