(() => {
  "use strict";
  // This workspace is the production editor. Retain the internal CSS marker
  // for compatibility with existing modules and saved workspace preferences.
  const experimental = true;
  if (experimental) {
    document.documentElement.dataset.editorLayout = "experimental";
    document.title = "Substar · 字幕编辑器";
    const url = new URL(location.href);
    if (url.searchParams.get("layout") === "experimental") {
      url.searchParams.delete("layout");
      history.replaceState(null, "", url);
    }
  }
  function installWorkspaceLayouts() {
    const workbench = document.querySelector("#editorWorkbench");
    const preview = workbench.querySelector(".editor-preview-pane");
    const tools = workbench.querySelector("#editorTools");
    const wave = workbench.querySelector(".cue-timeline-panel");
    const cue = workbench.querySelector(".editor-document-pane");
    const presets = [["standard", "字幕布局", "字幕在右，左上视频与工具并排，左下波形"],
      ["waveform", "波形布局", "左上预览、左下字幕，右侧多行波形"],
      ["classic", "审阅布局", "工具在左、字幕居中、预览在右，波形横贯底部"],
      ["cinema", "大屏布局", "预览在左，工具与字幕在右，波形横贯底部"]];
    const key = "substar.editor.workspace-layouts-v3";
    const initialSizes = {
      standard: {left:60, cue:897.3228759765625, tools:365, first:48.6, second:56},
      classic: {left:62, cue:843.8750305175781, tools:293.3854675292969, first:79.48717948717949, second:56},
      waveform: {left:31.666850779855533, first:40, second:56},
      cinema: {left:57.5, first:12, second:67.48717948717949}
    };
    const initialSwaps = {standard:true, waveform:false, classic:false, cinema:false};
    const initialModes = {standard:"multiline", waveform:"multiline", classic:"basic", cinema:"basic"};
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch {}
    let preset = presets.some(([id]) => id === saved.preset) ? saved.preset : "standard";
    let sizes = saved.sizes || {};
    const waveformModes = saved.waveformModes || {};
    const swaps = saved.swaps || {};
    // Apply the requested defaults once; subsequent resizing remains personal.
    const defaultsVersion = 8;
    if (saved.defaultsVersion !== defaultsVersion) {
      for (const [id] of presets) {
        sizes[id] = {...initialSizes[id]};
        waveformModes[id] = initialModes[id];
        swaps[id] = initialSwaps[id];
      }
    }
    const menu = document.createElement("label"); menu.className = "workspace-layout-menu";
    const caption = document.createElement("span"); caption.textContent = "工作区";
    const layoutSelect = document.createElement("select"); layoutSelect.id = "workspaceLayoutSelect";
    layoutSelect.setAttribute("aria-label", "工作区布局");
    presets.forEach(([id, name]) => layoutSelect.add(new Option(name, id)));
    layoutSelect.onchange = () => { preset = layoutSelect.value; apply(); persist(); };
    menu.append(caption, layoutSelect); document.querySelector("#taskInfoMenu").after(menu);
    const reset = document.createElement("button"); reset.type="button"; reset.textContent="恢复布局";
    reset.title="恢复当前布局的默认大小和位置";
    reset.onclick=()=>{ sizes[preset]={...initialSizes[preset]}; waveformModes[preset]=initialModes[preset]; swaps[preset]=initialSwaps[preset]; apply(); persist(); };
    menu.append(reset);
    document.querySelector("#revisionLabel").hidden = true;
    function persist() { try { localStorage.setItem(key, JSON.stringify({preset, sizes, waveformModes, swaps, defaultsVersion})); } catch {} }
    function config() { return sizes[preset] ||= {...initialSizes[preset]}; }
    wave.addEventListener("workspace-waveform-mode-change", event => {
      waveformModes[preset] = event.detail.mode;
      wave.dataset.workspaceWaveformMode = event.detail.mode;
      persist();
    });
    function apply() {
      workbench.dataset.workspaceLayout = preset;
      workbench.dataset.workspaceSwapped = String(!!swaps[preset]);
      const waveformMode = waveformModes[preset] || initialModes[preset];
      if (wave.dataset.workspaceWaveformMode !== waveformMode) {
        wave.dataset.workspaceWaveformMode = waveformMode;
        wave.dispatchEvent(new CustomEvent("workspace-waveform-mode", {detail:{mode:waveformMode}}));
      }
      const size = config(), width = workbench.clientWidth - 24;
      const maxUpper = 100 - 240 / Math.max(1, workbench.clientHeight - 24) * 100;
      if (preset === "cinema") size.second = Math.min(size.second, Math.max(28, maxUpper - size.first));
      else size.first = Math.min(size.first, Math.max(preset === "classic" ? 50 : 30, maxUpper));
      const toolsWidth = Math.max(180, Math.min(size.tools || 250, Math.max(180, width-660)));
      const cueWidth = Math.max(320, Math.min(size.cue || 710, width-toolsWidth-300));
      const left = preset === "waveform" ? Math.max(460, Math.min(width-400, width*size.left/100)) : preset === "cinema"
        ? Math.max(380, Math.min(width-540, width*size.left/100))
        : preset === "classic" ? toolsWidth+cueWidth : width-cueWidth;
      workbench.style.setProperty("--workspace-tools", `${toolsWidth}px`);
      workbench.style.setProperty("--workspace-cue", `${cueWidth}px`);
      workbench.style.setProperty("--workspace-left", `${left}px`);
      workbench.style.setProperty("--workspace-first", `${size.first}%`);
      workbench.style.setProperty("--workspace-second", `${size.second}%`);
      layoutSelect.value = preset;
      requestAnimationFrame(positionHandles);
    }
    const handles = [makeHandle("左右调整工作区宽度", "vertical", "width"),
      makeHandle("上下调整模块高度", "horizontal", "first"), makeHandle("上下调整第二处分界", "horizontal", "second"),
      makeHandle("调整工具栏宽度", "vertical", "tools")];
    let dragging = null;
    function makeHandle(name, orientation, kind) {
      const handle = document.createElement("div"); handle.className = `workspace-divider ${orientation}`;
      handle.tabIndex = 0; handle.setAttribute("role", "separator"); handle.setAttribute("aria-label", name);
      handle.setAttribute("aria-orientation", orientation);
      handle.title = name;
      handle.addEventListener("pointerdown", event => {
        if (event.button !== 0) return;
        event.preventDefault(); handle.setPointerCapture(event.pointerId);
        dragging = {kind, x:event.clientX, y:event.clientY, size:{...config()}};
        document.body.classList.add("workspace-resizing");
      });
      handle.addEventListener("pointermove", event => {
        if (!dragging || dragging.kind !== kind) return;
        resize(kind, event.clientX-dragging.x, event.clientY-dragging.y, dragging.size);
      });
      const finish = () => { if (!dragging) return; dragging = null; document.body.classList.remove("workspace-resizing"); persist(); };
      handle.addEventListener("pointerup", finish); handle.addEventListener("pointercancel", finish);
      handle.addEventListener("keydown", event => {
        const directions = {ArrowLeft:-12, ArrowRight:12, ArrowUp:-12, ArrowDown:12};
        if (!(event.key in directions)) return;
        const horizontal = kind === "width" || kind === "tools";
        if (horizontal && !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        if (!horizontal && !["ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault(); resize(kind, horizontal ? directions[event.key] : 0, horizontal ? 0 : directions[event.key], {...config()}); persist();
      });
      workbench.append(handle); return handle;
    }
    function resize(kind, dx, dy, origin) {
      const size = config(), width = workbench.clientWidth-24, height = workbench.clientHeight-24;
      const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
      if (kind === "tools") {
        const total = tools.getBoundingClientRect().width + cue.getBoundingClientRect().width;
        size.tools = clamp((origin.tools || 250) + (swaps[preset] ? -dx : dx), 180, total-320);
        size.cue = total-size.tools;
      } else if (kind === "width") {
        if (preset === "waveform") size.left = clamp(origin.left+dx/width*100, 460/width*100, (width-400)/width*100);
        else if (preset === "cinema") size.left = clamp(origin.left+dx/width*100, 380/width*100, (width-540)/width*100);
        else size.cue = clamp(origin.cue+(preset === "standard" || swaps[preset] ? -dx : dx), 320, width-(size.tools || 250)-300);
      } else if (preset === "standard" || preset === "aligned-left" || preset === "waveform") size.first = clamp(origin.first+dy/height*100, 30, Math.max(30, 100-240/height*100));
      else if (preset === "classic") size.first = clamp(origin.first+dy/height*100, 50, Math.max(50, 100-240/height*100));
      else {
        const delta = dy/height*100;
        if (kind === "first") { size.first = clamp(origin.first+delta, 12, origin.first+origin.second-28); size.second = origin.second+origin.first-size.first; }
        else size.second = clamp(origin.first+origin.second+delta, Math.max(50,origin.first+28),Math.max(50,100-240/height*100))-origin.first;
      }
      apply();
    }
    function positionHandles() {
      const box = workbench.getBoundingClientRect(), cueBox = cue.getBoundingClientRect();
      const previewBox = preview.getBoundingClientRect(), toolsBox = tools.getBoundingClientRect(), waveBox = wave.getBoundingClientRect();
      function place(handle, left, top, width, height) { Object.assign(handle.style, {left:`${left-box.left}px`, top:`${top-box.top}px`, width:`${width}px`, height:`${height}px`}); }
      handles[2].hidden = preset !== "cinema";
      handles[1].hidden = preset === "cinema";
      handles[3].hidden = preset !== "classic";
      if (preset === "standard") {
        place(handles[0], cueBox.left-8, box.top+8, 8, box.height-16);
        place(handles[1], box.left+8, waveBox.top-8, cueBox.left-box.left-16, 8);
      } else if (preset === "waveform") {
        place(handles[0], waveBox.left-8, box.top+8, 8, box.height-16);
        place(handles[1], previewBox.left, cueBox.top-8, previewBox.width, 8);
      } else if (preset === "aligned-left") {
        place(handles[0], toolsBox.left-8, box.top+8, 8, box.height-16);
        place(handles[1], waveBox.left, waveBox.top-8, waveBox.width, 8);
      } else if (preset === "classic") {
        place(handles[3], swaps[preset] ? toolsBox.left-8 : cueBox.left-8, box.top+8, 8, waveBox.top-box.top-16);
        place(handles[0], swaps[preset] ? cueBox.left-8 : previewBox.left-8, box.top+8, 8, waveBox.top-box.top-16);
        place(handles[1], box.left+8, waveBox.top-8, box.width-16, 8);
      } else {
        place(handles[0], cueBox.left-8, box.top+8, 8, waveBox.top-box.top-16);
        place(handles[1], toolsBox.left, cueBox.top-8, toolsBox.width, 8);
        place(handles[2], box.left+8, waveBox.top-8, box.width-16, 8);
      }
    }
    let swapDrag=null;
    function finishSwap(commit=false) {
      if (!swapDrag) return;
      const {source,target,ghost,pointerId}=swapDrag;
      source.releasePointerCapture?.(pointerId); ghost?.remove(); target.classList.remove("workspace-drop-target");
      document.body.classList.remove("workspace-swapping"); swapDrag=null;
      if (commit) { swaps[preset]=!swaps[preset]; apply(); persist(); }
    }
    for (const source of [preview,tools]) {
      source.addEventListener("pointerdown",event=>{
        if (event.button!==0 || !["standard","classic"].includes(preset)) return;
        if (event.target.closest("button, input, select, summary, a, label")) return;
        const box=source.getBoundingClientRect();
        if (event.clientY>box.top+60) return;
        event.preventDefault();
        swapDrag={source,target:source===preview?tools:preview,x:event.clientX,y:event.clientY,pointerId:event.pointerId};
        source.setPointerCapture(event.pointerId);
      });
      source.addEventListener("pointermove",event=>{
        if (!swapDrag || swapDrag.source!==source) return;
        if (!swapDrag.ghost && Math.hypot(event.clientX-swapDrag.x,event.clientY-swapDrag.y)<6) return;
        if (!swapDrag.ghost) {
          const ghost=document.createElement("div"); ghost.className="workspace-drag-ghost";
          ghost.textContent=source===preview?"预览":"工具箱"; document.body.append(ghost); swapDrag.ghost=ghost;
          document.body.classList.add("workspace-swapping");
        }
        Object.assign(swapDrag.ghost.style,{left:`${event.clientX+14}px`,top:`${event.clientY+14}px`});
        const box=swapDrag.target.getBoundingClientRect();
        swapDrag.valid=event.clientX>=box.left && event.clientX<=box.right && event.clientY>=box.top && event.clientY<=box.bottom;
        swapDrag.target.classList.toggle("workspace-drop-target",swapDrag.valid);
      });
      source.addEventListener("pointerup",()=>finishSwap(!!swapDrag?.ghost && swapDrag.valid));
      source.addEventListener("pointercancel",()=>finishSwap());
    }
    document.addEventListener("keydown",event=>{ if(event.key==="Escape") finishSwap(); });
    layoutSelect.addEventListener("change",()=>finishSwap());
    new ResizeObserver(apply).observe(workbench);
    apply();
    persist();
  }
  function installTaskIsland() {
    const island = document.querySelector("#cueTaskIsland");
    const task = island.querySelector("#translationTaskPanel");
    const error = island.querySelector("#ordinaryError");
    const naming = island.querySelector("#checkpointNaming");
    let previous = "", previousSize = null;
    const sync = () => {
      const state = `${task.className}:${error.className}:${naming.hidden}`;
      island.classList.toggle("island-compact", task.classList.contains("hidden") && error.classList.contains("hidden") && naming.hidden);

      island.classList.toggle("task-active", !task.classList.contains("hidden") && !task.classList.contains("completed") && !task.classList.contains("failed"));
      if (state !== previous) {
        previous = state;
        island.getAnimations().forEach(animation => animation.cancel());
        const nextSize = island.getBoundingClientRect();
        if (previousSize && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
          island.animate([
            {width:`${previousSize.width}px`, height:`${previousSize.height}px`},
            {width:`${nextSize.width}px`, height:`${nextSize.height}px`}
          ], {duration:240, easing:"cubic-bezier(.2,.8,.2,1)"});
        }
        previousSize = {width:nextSize.width, height:nextSize.height};
      }
    };
    new MutationObserver(sync).observe(task, {attributes:true, attributeFilter:["class"]});
    new MutationObserver(sync).observe(error, {attributes:true, attributeFilter:["class"]});
    new MutationObserver(sync).observe(naming, {attributes:true, attributeFilter:["hidden"]});
    sync();
  }
  function installCueViewSwitch() {
    const label = document.querySelector("#cueSplitViewLabel");
    const select = label.querySelector("select");
    const group = document.createElement("div");
    group.id = label.id; group.className = "cue-view-switch";
    group.setAttribute("role", "group"); group.setAttribute("aria-label", "字幕视图");
    select.hidden = true;
    group.append(select);
    for (const option of select.options) {
      const button = document.createElement("button");
      button.type = "button"; button.textContent = option.textContent;
      button.dataset.cueView = option.value;
      button.setAttribute("aria-pressed", String(select.value === option.value));
      button.addEventListener("click", () => {
        if (select.disabled || select.value === option.value) return;
        select.value = option.value;
        select.dispatchEvent(new Event("change", {bubbles:true}));
      });
      group.append(button);
    }
    label.replaceWith(group);
    document.querySelector(".editor-document-pane .pane-heading > div:first-child").append(group);
    group.before(document.querySelector("#reviewModeToggle"));
    const fontGroup = document.createElement("div"); fontGroup.className = "cue-font-control";
    fontGroup.setAttribute("role", "group"); fontGroup.setAttribute("aria-label", "字幕字号");
    const decrease = document.querySelector("#fontDecrease"), value = document.querySelector("#fontValue"), increase = document.querySelector("#fontIncrease");
    decrease.before(fontGroup); fontGroup.append(decrease, value, increase);
    decrease.setAttribute("aria-label", "减小字幕字号"); increase.setAttribute("aria-label", "增大字幕字号");
    value.setAttribute("aria-label", "当前字幕字号");
    const bilingual = document.querySelector("#showTranslations").parentElement;
    bilingual.classList.add("bilingual-toggle");
    [...bilingual.childNodes].filter(node => node.nodeType === Node.TEXT_NODE)
      .forEach(node => { node.textContent = "双语字幕"; });
    const displayMenu = document.createElement("details"); displayMenu.className = "cue-display-settings multiline-waveform-settings";
    const displayToggle = document.createElement("summary"); displayToggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 3-.6 2.2-2 .9-2-.9-2.5 4.3 1.8 1.5v2l-1.8 1.5 2.5 4.3 2-.9 2 .9.6 2.2h5l.6-2.2 2-.9 2 .9 2.5-4.3-1.8-1.5v-2l1.8-1.5-2.5-4.3-2 .9-2-.9L14.5 3Z"/><circle cx="12" cy="12" r="3"/></svg>';
    displayToggle.setAttribute("aria-label", "字幕显示设置"); displayToggle.title = "字幕显示设置";
    const displayContent = document.createElement("div"); displayContent.className = "cue-display-settings-content multiline-waveform-settings-panel";
    const fontLabel = document.createElement("span"); fontLabel.textContent = "字号";
    const fontRow = document.createElement("div"); fontRow.className = "cue-settings-font-row";
    const fontSlider = document.createElement("input"); fontSlider.type = "range"; fontSlider.min = "11"; fontSlider.max = "22"; fontSlider.step = "1"; fontSlider.value = value.textContent || "18";
    fontSlider.setAttribute("aria-label", "字幕字号");
    const fontOutput = document.createElement("output"); fontOutput.textContent = fontSlider.value;
    const syncFont = () => { fontSlider.value = value.textContent; fontOutput.textContent = value.textContent; };
    new MutationObserver(syncFont).observe(value, {childList:true, characterData:true, subtree:true});
    fontSlider.oninput = () => {
      const target = Number(fontSlider.value), current = Number(value.textContent);
      for (let i = 0; i < Math.abs(target-current); i++) (target > current ? increase : decrease).click();
      syncFont();
    };
    fontGroup.hidden = true;
    const sliderControls = document.createElement("span"); sliderControls.className = "waveform-gain-controls"; sliderControls.append(fontSlider, fontOutput);
    const settingsTitle = document.createElement("strong"); settingsTitle.textContent = "字幕显示设置";
    fontRow.append(fontLabel, sliderControls); displayContent.append(settingsTitle, fontRow, bilingual, fontGroup);
    displayMenu.append(displayToggle, displayContent);
    document.querySelector(".pane-live-controls").append(displayMenu);
  }
  function installSpeakerMenu() {
    const button = document.querySelector("#detectSpeakers");
    const menu = document.createElement("details"); menu.id = "speakerMenu"; menu.className = "editor-command-menu speaker-command-menu";
    const summary = document.createElement("summary"); summary.id = button.id; summary.textContent = "说话人设置";
    summary.disabled = button.disabled; summary.setAttribute("aria-disabled", String(button.disabled));
    const form = document.querySelector("#speakerDialogForm");
    form.classList.remove("text-dialog", "speaker-dialog"); form.classList.add("editor-command-popover", "speaker-popover");
    form.querySelector("p").textContent = "设置显示名称；点击字幕右侧人形按钮切换说话人。";
    menu.append(summary, form); button.replaceWith(menu);
  }
  document.addEventListener("DOMContentLoaded", () => {
    if (experimental) {
      document.querySelector("#taskInfo").textContent = "项目配置";
      document.querySelector("#taskInfoTitle").textContent = "项目配置";
      document.querySelector("#projectMenu > summary").textContent = "选择项目";
      document.querySelector("#exportMenu").before(document.querySelector("#importMenu"));
      const versions = document.querySelector("#versionMenu"), reset = document.querySelector("#resetDocument");
      reset.hidden = true; reset.before(versions);
      versions.classList.add("island-version-menu");
      versions.querySelector("summary").id = "checkpointVersionsToggle";
      installWorkspaceLayouts();
      document.querySelector(".editor-document-pane .pane-heading strong").textContent = "字幕";
      installTaskIsland();
      installSpeakerMenu();
      installCueViewSwitch();
      const overlayPreview=document.createElement("div"); overlayPreview.id="previewOverlay";
      overlayPreview.className="preview-overlay-subtitle";
      document.querySelector("#mediaViewport").append(overlayPreview);
      const tools = document.querySelector("#editorTools");
      const tabs = document.createElement("div");
      tabs.className = "experimental-tool-tabs";
      tabs.setAttribute("role", "tablist");
      tabs.setAttribute("aria-label", "编辑工具");
      const locator = tools.querySelector('[data-tool-panel="locator"]');
      const search = tools.querySelector('[data-tool-panel="search"]');
      const searchContent = search.querySelector('.tool-panel');
      searchContent.id = 'experimental-search-panel';
      search.open = true;
      search.classList.add('combined-search');
      const locatorGroup = locator.querySelector('.tool-panel');
      const combined = document.createElement('div');
      combined.className = 'tool-panel combined-find-panel';
      combined.append(search, locatorGroup);
      locator.append(combined);
      locator.querySelector('summary').textContent = '查找与定位';
      const panels = Array.from(tools.querySelectorAll(":scope > .tool-accordion"));
      const toolsScroll = document.createElement("input");
      toolsScroll.type = "range";
      toolsScroll.className = "tools-scroll-slider";
      toolsScroll.setAttribute("aria-label", "编辑工具内容位置");
      toolsScroll.min = "0";
      toolsScroll.step = "1";
      toolsScroll.value = "0";
      toolsScroll.hidden = true;
      let activePanel = panels[0];
      const workbench = document.querySelector("#editorWorkbench");
      const isCinema = () => ["cinema", "waveform"].includes(workbench.dataset.workspaceLayout);
      let scrollFrame = 0;
      const floatingHeading = document.createElement("div"); floatingHeading.className = "cinema-tool-heading";
      const floatingTitle = document.createElement("strong");
      const floatingClose = document.createElement("button"); floatingClose.type = "button"; floatingClose.textContent = "×";
      floatingClose.setAttribute("aria-label", "关闭工具窗口"); floatingClose.onclick = closeTools;
      floatingHeading.append(floatingTitle, floatingClose); tools.append(floatingHeading);
      function syncToolsScroll() {
        scrollFrame = 0;
        if (isCinema()) {
          const preview = document.querySelector(".editor-preview-pane").getBoundingClientRect();
          const anchor = document.querySelector("#assMenu > summary").getBoundingClientRect();
          const width = Math.min(340, preview.width - 24);
          tools.style.left = `${preview.right - width - 12}px`;
          tools.style.top = `${anchor.bottom + 8}px`;
          tools.style.width = `${width}px`;
          tools.style.setProperty("--cinema-window-height", `${Math.max(120, Math.min(560, preview.bottom - anchor.bottom - 60))}px`);
          activePanel.style.left = "0px";
          toolsScroll.style.left = `${width - 16}px`;
        } else {
          ["left", "top", "width"].forEach(key => tools.style.removeProperty(key));
          panels.forEach(panel => panel.style.removeProperty("left"));
          toolsScroll.style.removeProperty("left");
        }
        const maximum = Math.max(0, activePanel.scrollHeight - activePanel.clientHeight);
        toolsScroll.hidden = maximum <= 1 || (isCinema() && !tools.classList.contains("cinema-tools-open"));
        toolsScroll.disabled = maximum <= 1;
        toolsScroll.max = String(maximum);
        toolsScroll.value = String(activePanel.scrollTop);
        toolsScroll.style.top = isCinema() ? "36px" : `${tabs.offsetTop + tabs.offsetHeight}px`;
        toolsScroll.style.height = `${activePanel.clientHeight}px`;
        toolsScroll.setAttribute("aria-controls", activePanel.querySelector(".tool-panel").id);
      }
      function scheduleToolsScroll() {
        if (!scrollFrame) scrollFrame = requestAnimationFrame(syncToolsScroll);
      }
      toolsScroll.addEventListener("input", () => {
        activePanel.scrollTop = Number(toolsScroll.value);
      });
      const toolsResize = new ResizeObserver(scheduleToolsScroll);
      toolsResize.observe(tools);
      panels.forEach(panel => {
        panel.addEventListener("scroll", scheduleToolsScroll, {passive:true});
        toolsResize.observe(panel);
        toolsResize.observe(panel.querySelector(".tool-panel"));
      });
      new MutationObserver(scheduleToolsScroll).observe(tools, {subtree:true, childList:true, characterData:true});
      const tabLabels = ["查找", "标点", "吸附", "合并", "帮助"];
      const tabIcons = [
        '<circle cx="6.5" cy="6.5" r="4.5"/><path d="m10 10 4 4"/>',
        '<path d="M4 4h3v4H4zm6 0h3v4h-3zM7 8l-2 4m8-4-2 4"/>',
        '<path d="M3 3v6a5 5 0 0 0 10 0V3M3 6h3m4 0h3M6 3v6m4-6v6"/>',
        '<path d="M2 3h3l3 5m6-5h-3L8 8v6m-3-3 3 3 3-3"/>',
        '<circle cx="8" cy="8" r="6"/><path d="M6.3 6a1.8 1.8 0 0 1 3.4.8C9.7 8 8 8.2 8 9.5M8 12v.1"/>'
      ];
      const buttons = panels.map((panel, index) => {
        panel.open = true;
        const button = document.createElement("button");
        button.type = "button";
        const name = index === 4 ? "帮助" : panel.querySelector("summary").textContent;
        button.innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true">${tabIcons[index]}</svg><span>${tabLabels[index]}</span>`;
        button.setAttribute("aria-label", name);
        button.title = name;
        button.id = `experimental-tool-tab-${index}`;
        const content = panel.querySelector(".tool-panel");
        content.id ||= `experimental-tool-panel-${index}`;
        content.setAttribute("role", "tabpanel");
        content.setAttribute("aria-labelledby", button.id);
        button.setAttribute("role", "tab");
        button.setAttribute("aria-controls", content.id);
        button.onclick = () => {
          if (isCinema() && activePanel === panel && tools.classList.contains("cinema-tools-open")) closeTools();
          else activate(index);
        };
        button.onkeydown = event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? 0 : event.key === "End" ? panels.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + panels.length) % panels.length;
          activate(next);
          buttons[next].focus();
        };
        tabs.append(button);
        return button;
      });
      function activate(index) {
        activePanel = panels[index];
        floatingTitle.textContent = tabLabels[index];
        if (isCinema()) {
          document.querySelector("#assMenu").open = false;
          tools.classList.add("cinema-tools-open");
        }
        panels.forEach((panel, i) => {
          if (i === index) panel.open = true;
          panel.classList.toggle("experimental-tool-active", i === index);
          if (!isCinema()) buttons[i].setAttribute("aria-selected", String(i === index));
          buttons[i].tabIndex = i === index ? 0 : -1;
          if (isCinema()) { buttons[i].setAttribute("aria-expanded", String(i === index)); buttons[i].tabIndex = 0; }
        });
        scheduleToolsScroll();
      }
      function closeTools() {
        tools.classList.remove("cinema-tools-open");
        buttons.forEach(button => { if (isCinema()) button.setAttribute("aria-expanded", "false"); });
        scheduleToolsScroll();
      }
      function syncToolMode() {
        closeTools();
        if (isCinema()) {
          document.querySelector(".cue-view-switch").after(tabs);
          if (document.body.classList.contains("review-mode")) document.querySelector("#reviewModeToggle")?.click();
        } else tools.prepend(tabs);
        tabs.setAttribute("role", isCinema() ? "toolbar" : "tablist");
        buttons.forEach((button, index) => {
          button.setAttribute("role", isCinema() ? "button" : "tab");
          button.tabIndex = isCinema() || activePanel === panels[index] ? 0 : -1;
          if (isCinema()) { button.setAttribute("aria-haspopup", "dialog"); button.setAttribute("aria-expanded", "false"); button.removeAttribute("aria-selected"); }
          else { button.removeAttribute("aria-haspopup"); button.removeAttribute("aria-expanded"); button.setAttribute("aria-selected", String(activePanel === panels[index])); }
          panels[index].querySelector(".tool-panel").setAttribute("role", isCinema() ? "dialog" : "tabpanel");
        });
      }
      new MutationObserver(syncToolMode).observe(workbench, {attributes:true, attributeFilter:["data-workspace-layout"]});
      document.querySelector("#assMenu").addEventListener("toggle", event => {
        if (isCinema() && event.currentTarget.open) closeTools();
      });
      document.addEventListener("keydown", event => {
        if (isCinema() && event.key === "Escape") { closeTools(); buttons[panels.indexOf(activePanel)].focus(); event.stopPropagation(); }
      });
      window.addEventListener("resize", scheduleToolsScroll);
      toolsResize.observe(document.querySelector(".editor-preview-pane"));
      const help = panels[4].querySelector(".tool-panel");
      const legend = help.cloneNode(true);
      legend.removeAttribute("id"); legend.removeAttribute("role"); legend.removeAttribute("aria-labelledby");
      legend.querySelector(".sidebar-usage")?.remove();
      help.replaceChildren();
      help.classList.add("experimental-help");
      help.innerHTML = `
        <section><h3>字幕编辑</h3><p>点击字幕定位并选中，双击词元修改文字。右键词间连接点拆分；右键句末连接点合并下一条。</p><p>双击句末紫点编辑整句，点击外部保存。新建字幕会直接进入整句编辑。</p></section>
        <section><h3>波形操作</h3><dl>
          <dt>单击</dt><dd>定位播放头、选择字幕</dd>
          <dt>双击</dt><dd>播放 / 暂停</dd>
          <dt>拖拽字幕块</dt><dd>整体移动字幕时间</dd>
          <dt>拖拽浅色边缘</dt><dd>调整起点或终点</dd>
          <dt><kbd>Shift</kbd> + 拖边缘</dt><dd>仅调整当前字幕</dd>
          <dt>右键 / <kbd>N</kbd></dt><dd>在指针位置添加字幕</dd>
          <dt><kbd>Ctrl</kbd> + 拖拽</dt><dd>创建指定时长的字幕</dd>
        </dl><p>右键波形空隙直接创建字幕，并在 Cue 编辑区输入文字。</p><p>缩放修饰键 + 滚轮调整每行时长；也可在波形齿轮内设置。</p></section>
`;
      if (legend) {
        const section = document.createElement("section");
        const heading = document.createElement("h3"); heading.textContent = "变更标记说明";
        section.append(heading, legend); help.append(section);
      }
      // Group punctuation rules by language without replacing the existing inputs.
      const punctuation = tools.querySelector('[data-tool-panel="punctuation"] .tool-panel');
      const rules = Array.from(punctuation.children);
      for (const offset of [0, 3]) {
        const row = document.createElement("fieldset"); row.className = "punctuation-rule-row";
        rules[offset].before(row);
        const heading = document.createElement("legend"); heading.textContent = rules[offset].textContent;
        row.append(heading);
        rules[offset].remove();
        ["移除符号", "替换为空格"].forEach((name, index) => {
          const label = document.createElement("label");
          const text = document.createElement("span"); text.textContent = name;
          const input = rules[offset + index + 1];
          input.placeholder = index ? "例如：，、" : "例如：。！？";
          label.append(text, input); row.append(label);
        });
      }
      const punctuationActions = document.createElement("div"); punctuationActions.className = "panel-actions";
      punctuationActions.append(punctuation.querySelector("#applyPunctuation"));
      punctuation.append(punctuationActions);
      function searchGroup(name, nodes) {
        const group = document.createElement("fieldset");
        const legend = document.createElement("legend"); legend.textContent = name;
        group.append(legend, ...nodes); return group;
      }
      const findActions = document.createElement("div"); findActions.className = "panel-actions";
      findActions.append(searchContent.querySelector("#toolSearchNext"));
      const replaceActions = document.createElement("div"); replaceActions.className = "panel-actions replace-actions";
      replaceActions.append(findActions.querySelector("#toolSearchNext"), ...["#toolReplaceCurrent", "#toolReplaceAll", "#toolUndoReplace"].map(id => searchContent.querySelector(id)));
      const queryRow = document.createElement("div"); queryRow.className = "search-query-row";
      queryRow.append(searchContent.querySelector("#toolSearch"), findActions);
      const findGroup = searchGroup("查找与替换", [searchContent.querySelector(".search-scope-toggle"), queryRow, searchContent.querySelector("#toolReplace"), replaceActions]);
      const searchStatus = searchContent.querySelector("#toolSearchStatus");
      searchContent.replaceChildren(findGroup, searchStatus);
      tools.prepend(tabs);
      tools.append(toolsScroll);
      tools.querySelectorAll(".tool-locator-row").forEach(row => {
        row.querySelectorAll(".tool-button-pair button").forEach((button, index) => {
          const name = `${button.textContent} · ${row.querySelector("strong").textContent}`;
          button.setAttribute("aria-label", name);
          button.title = name;
          button.textContent = index ? "›" : "‹";
        });
      });
      activate(0);
      syncToolMode();
      // Review mode uses the locator panel for its review controls.
      new MutationObserver(() => {
        if (document.body.classList.contains("review-mode")) activate(0);
      }).observe(document.body, {attributes:true, attributeFilter:["class"]});
    }
  });
})();
