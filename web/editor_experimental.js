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
      const maxUpper = 100 - 260 / Math.max(1, workbench.clientHeight - 24) * 100;
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
      } else if (preset === "standard" || preset === "aligned-left" || preset === "waveform") size.first = clamp(origin.first+dy/height*100, 30, Math.max(30, 100-260/height*100));
      else if (preset === "classic") size.first = clamp(origin.first+dy/height*100, 50, Math.max(50, 100-260/height*100));
      else {
        const delta = dy/height*100;
        if (kind === "first") { size.first = clamp(origin.first+delta, 12, origin.first+origin.second-28); size.second = origin.second+origin.first-size.first; }
        else size.second = clamp(origin.first+origin.second+delta, Math.max(50,origin.first+28),Math.max(50,100-260/height*100))-origin.first;
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
    const reviewToggle = document.querySelector("#reviewModeToggle");
    if (reviewToggle) group.before(reviewToggle);
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
    const displayToggle = document.createElement("summary");
    displayToggle.textContent = '⚙️'; displayToggle.classList.add('workspace-settings-gear');
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
    const settingsHeader = document.createElement("div"); settingsHeader.className = "cue-settings-header";
    const settingsClose = document.createElement("button"); settingsClose.type = "button"; settingsClose.textContent = "×";
    settingsClose.setAttribute("aria-label", "关闭字幕显示设置"); settingsClose.onclick = () => { displayMenu.open = false; };
    settingsHeader.append(settingsTitle, settingsClose);
    const settingsBody = document.createElement("div"); settingsBody.className = "cue-settings-body";
    fontRow.append(fontLabel, sliderControls); settingsBody.append(fontRow, bilingual, fontGroup);
    displayContent.append(settingsHeader, settingsBody);
    displayMenu.append(displayToggle, displayContent);
    document.querySelector(".pane-live-controls").append(displayMenu);
  }
  function installSpeakerMenu() {
    document.querySelector('#detectSpeakers').hidden=true;
    const form=document.querySelector('#speakerDialogForm');
    form.classList.remove('text-dialog','speaker-dialog');form.classList.add('speaker-tool-form');
    form.querySelector('p').textContent='管理当前工程中的说话人名称；点击字幕右侧人形按钮分配说话人。';
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
      const cueSwitch=document.querySelector('.cue-view-switch');
      cueSwitch.after(...['translationMenu','aiOrganizeMenu','aiReviewMenu','aiCalibrationMenu','scriptProjectionMenu','referenceManuscript'].map(id=>document.getElementById(id)));
      for(const id of ['translationMenu','aiOrganizeMenu','aiReviewMenu','aiCalibrationMenu']) {
        const menu=document.getElementById(id);
        const position=()=>{
          if(!menu.open)return;
          const popup=menu.querySelector('.editor-command-popover'),anchor=menu.querySelector('summary').getBoundingClientRect();
          popup.style.position='fixed';popup.style.transform='none';
          popup.style.left=Math.max(8,Math.min(anchor.left,innerWidth-popup.offsetWidth-8))+'px';
          popup.style.top=Math.max(8,Math.min(anchor.bottom+6,innerHeight-popup.offsetHeight-8))+'px';
        };
        menu.addEventListener('toggle',position);
        window.addEventListener('resize',position);
      }
      const projection=document.querySelector('#scriptProjectionMenu');
      projection.addEventListener('toggle',()=>{
        if(!projection.open)return;const popup=projection.querySelector('.editor-command-popover'),r=projection.querySelector('summary').getBoundingClientRect();
        popup.style.position='fixed';popup.style.left=Math.min(r.left,innerWidth-popup.offsetWidth-8)+'px';popup.style.top=r.bottom+6+'px';
      });
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
      for (const [key,title] of [['cut','剪辑'],['gaps','空隙'],['markers','标记'],['dubbing','配音']]) {
        const section=document.createElement('details');section.className='tool-accordion';section.dataset.toolPanel=key;
        const heading=document.createElement('summary');heading.textContent=title;
        const content=document.createElement('div');content.className='tool-panel';content.id='editor-'+key+'-tool-content';
        section.append(heading,content);tools.append(section);
      }
      tools.querySelector('#editor-dubbing-tool-content').append(document.querySelector('#speakerDialogForm'));
      const styleMenu=document.querySelector('#assMenu');
      styleMenu.hidden=true;

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
      const toolDock=document.createElement('div');toolDock.className='workspace-tool-dock';
      document.querySelector('#editorWorkbench').append(toolDock);
      const dockPrevious = document.createElement('button');
      const dockNext = document.createElement('button');
      for (const [button, label, glyph, direction] of [
        [dockPrevious, '向左查看更多工具', '‹', -1],
        [dockNext, '向右查看更多工具', '›', 1],
      ]) {
        button.type = 'button';
        button.className = 'workspace-tool-scroll';
        button.setAttribute('aria-label', label);
        button.textContent = glyph;
        button.hidden = true;
        button.addEventListener('click', () => tabs.scrollBy({left:direction * Math.max(120, tabs.clientWidth * .7), behavior:'smooth'}));
      }
      toolDock.append(dockPrevious, dockNext);
      function syncDockArrows() {
        if (toolDock.hidden || tabs.parentElement !== toolDock) return;
        const overflow = tabs.scrollWidth > toolDock.clientWidth - 12 + 1;
        dockPrevious.hidden = dockNext.hidden = !overflow;
        dockPrevious.disabled = tabs.scrollLeft <= 1;
        dockNext.disabled = tabs.scrollLeft + tabs.clientWidth >= tabs.scrollWidth - 1;
      }
      tabs.addEventListener('scroll', syncDockArrows, {passive:true});
      new ResizeObserver(syncDockArrows).observe(toolDock);
      const floatingPositions={};let panelDrag=null;
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
          const preview=document.querySelector('.editor-preview-pane').getBoundingClientRect();
          const cue=document.querySelector('.editor-document-pane').getBoundingClientRect();
          const wave=document.querySelector('.cue-timeline-panel').getBoundingClientRect();
          const dock=window.EditorToolLayout.dockBounds(workbench.dataset.workspaceLayout,preview,cue,wave);
          Object.assign(toolDock.style,{left:dock.left+'px',top:dock.top+'px',width:dock.width+'px'});
          syncDockArrows();
          const width=Math.min(360,innerWidth-24),bodyHeight=Math.min(520,innerHeight-100);
          const requested=floatingPositions[workbench.dataset.workspaceLayout]||{left:dock.left+8,top:dock.top+62};
          const position=window.EditorToolLayout.clampPanel(requested,{width:innerWidth,height:innerHeight},{width,height:bodyHeight+36});
          Object.assign(tools.style,{left:position.left+'px',top:position.top+'px',width:width+'px'});
          tools.style.setProperty('--cinema-window-height',bodyHeight+'px');
          activePanel.style.left='0px';toolsScroll.style.left=(width-16)+'px';
        } else {
          ['left','top','width'].forEach(key=>tools.style.removeProperty(key));
          panels.forEach(panel=>panel.style.removeProperty('left'));toolsScroll.style.removeProperty('left');
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
      const tabLabels = ["查找", "标点", "吸附", "合并", "帮助", "剪辑", "空隙", "标记", "配音"];
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
        button.innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true">${(index===8?'<rect x="5" y="1" width="6" height="9" rx="3"/><path d="M2 7a6 6 0 0 0 12 0M8 13v2M5 15h6"/>':index===6?'<path d="M2 4v8m12-8v8M5 8h6"/>':index===7?'<path d="M4 14V2h8l-2 3 2 3H4"/>':tabIcons[index]) || (index===5 ? '<path d="M3 3l10 10M3 13L13 3"/><circle cx="3" cy="3" r="2"/><circle cx="3" cy="13" r="2"/>' : '<path d="M2 3h12M8 3v11M5 14h6"/>')}</svg><span>${tabLabels[index]}</span>`;
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
      const subtitleButton = document.createElement('button');
      subtitleButton.type = 'button';
      subtitleButton.id = 'experimental-subtitle-panel-open';
      subtitleButton.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1.5" y="3" width="13" height="10" rx="2"/><path d="M4 7h3m2 0h3M4 10h8"/></svg><span>字幕</span>';
      subtitleButton.setAttribute('aria-label', '字幕面板');
      subtitleButton.setAttribute('aria-haspopup', 'dialog');
      subtitleButton.setAttribute('aria-controls', 'ass-style-window');
      subtitleButton.title = '打开 ASS 字幕面板';
      subtitleButton.onclick = () => window.SubstarMawAssPanel?.open();
      tabs.append(subtitleButton);
      for(let slot=tabs.children.length;slot<10;slot++){const empty=document.createElement('span');empty.className='tool-slot-placeholder';empty.setAttribute('aria-hidden','true');tabs.append(empty);}
      document.addEventListener('substar:activate-tool',event=>{
        const index=panels.findIndex(panel=>panel.dataset.toolPanel===event.detail);
        if(index>=0)activate(index);else if(event.detail==='locator')activate(0);
      });
      function activate(index) {
        const changed=activePanel!==panels[index];
        activePanel = panels[index];
        if(changed)activePanel.scrollTop=0;
        document.dispatchEvent(new CustomEvent("substar:tool-activated",{detail:activePanel.dataset.toolPanel}));

        floatingTitle.textContent = tabLabels[index];
        if (isCinema()) {
          if(index!==6) document.querySelector("#assMenu").open = false;
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
          toolDock.insertBefore(tabs,dockNext);toolDock.hidden=false;
          if (document.body.classList.contains("review-mode")) document.querySelector("#reviewModeToggle")?.click();
        } else {tools.prepend(tabs);toolDock.hidden=true;}
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

      document.addEventListener("keydown", event => {
        if (isCinema() && event.key === "Escape") { closeTools(); buttons[panels.indexOf(activePanel)].focus(); event.stopPropagation(); }
      });
      floatingHeading.addEventListener('pointerdown',event=>{
        if(!isCinema()||event.button!==0||event.target.closest('button'))return;
        const box=tools.getBoundingClientRect();panelDrag={id:event.pointerId,x:event.clientX,y:event.clientY,left:box.left,top:box.top};
        floatingHeading.setPointerCapture(event.pointerId);event.preventDefault();
      });
      floatingHeading.addEventListener('pointermove',event=>{
        if(!panelDrag||panelDrag.id!==event.pointerId)return;
        floatingPositions[workbench.dataset.workspaceLayout]={left:panelDrag.left+event.clientX-panelDrag.x,top:panelDrag.top+event.clientY-panelDrag.y};scheduleToolsScroll();
      });
      for(const name of ['pointerup','pointercancel','lostpointercapture'])floatingHeading.addEventListener(name,()=>{panelDrag=null;});
      window.addEventListener("resize", scheduleToolsScroll);
      toolsResize.observe(document.querySelector('.editor-document-pane'));
      toolsResize.observe(document.querySelector('.cue-timeline-panel'));
      toolsResize.observe(document.querySelector(".editor-preview-pane"));
      const help = panels[4].querySelector(".tool-panel");
      const legend = help.cloneNode(true);
      legend.removeAttribute("id"); legend.removeAttribute("role"); legend.removeAttribute("aria-labelledby");
      legend.querySelector(".sidebar-usage")?.remove();
      help.replaceChildren();
      help.classList.add("experimental-help");
      help.innerHTML = `
        <section><h3>字幕编辑</h3><p>点击字幕定位并选中，双击词元修改文字。右键词间连接点拆分；右键句末连接点合并下一条。</p><p>双击句末紫点编辑整句，点击外部保存。新建字幕会直接进入整句编辑。</p></section>
        <section><h3>波形与选区</h3><dl>
          <dt>单击字幕块</dt><dd>选择字幕、定位播放头</dd>
          <dt>拖动字幕块</dt><dd>移动字幕时间；拖动两侧浅色边缘调整起止时间</dd>
          <dt>字词按钮 / <kbd>Q</kbd></dt><dd>切换词级时间条</dd>
          <dt><kbd>Shift</kbd> + 单击</dt><dd>按顺序连续选择，可跨句选词</dd>
          <dt><kbd>Ctrl</kbd> + 单击</dt><dd>追加或取消单项选择</dd>
        </dl><p>词级与句级选区独立保存，互不干扰。右键词条操作词选区，右键句条操作句选区。</p><p>右键波形空白处可创建字幕；波形齿轮可设置每行时长、行高与振幅。</p></section>
        <section><h3>标记与空隙</h3><p>右键选中的词条或句条，可添加点标记、线标记或空隙。顺序相邻的选中项合为一段（包含中间停顿），不连续的选区分别创建；点标记位于每段起点。</p><p>蓝标是定位点，白点是范围边界。未展开时拖蓝标移动整个点标记，拖白点展开范围；展开后蓝标只在范围内移动，不带动边界。两侧收拢后恢复点标记。</p><p>蓝标和白点按当前模式吸附词边界或句边界，按住 Alt 暂时取消吸附。双击或右键标记可编辑名称等属性，标记面板可编辑、删除和定位。</p><p>空隙操作决定鼠标编辑方式：拖动边界调整起止，中键拖动圈选；可同时开启或关闭编辑。启用“跳过静音空隙”后，播放跳过已移除区间。</p></section>
        <section><h3>波形设置</h3><p>低能量抑制使用加减按钮调节，每次 5%；只改变弱波形的显示，不改变音量和空隙检测结果。</p><p>“隐藏字幕”决定已隐藏字幕在波形中的显示方式：变淡或完全隐藏，并同步句条、译文条和词条。显示画面预览图可开启已有的视频帧导航。</p></section>
        <section><h3>导出与剪辑导出</h3><p>“导出”保留原有字幕、外部 AI 材料和字幕工程导出。</p><p>“剪辑导出”按当前剪辑方案输出 MP4 视频、WAV 音频，或上行、下行、上下行单行、上下行双行四种 SRT 字幕；字幕时间随剪辑结果重新计算。</p><p>标记与选区创建操作支持撤销；批量创建可整批撤销。关闭页面前确认保存完成。</p></section>
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
      document.addEventListener("substar:open-style-tool",()=>window.SubstarMawAssPanel?.open());
      activate(0);
      syncToolMode();
      // Review mode uses the locator panel for its review controls.
      new MutationObserver(() => {
        if (document.body.classList.contains("review-mode")) activate(0);
      }).observe(document.body, {attributes:true, attributeFilter:["class"]});
    }
  });
})();
