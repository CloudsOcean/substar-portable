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
    const presets = [["standard", "对齐布局右", "字幕在右，左上视频与工具并排，左下波形"],
      ["aligned-left", "对齐布局左", "字幕在左，右上工具与视频并排，右下波形"],
      ["classic", "经典布局", "工具在左、字幕居中、预览在右，波形横贯底部"],
      ["cinema", "大屏布局", "预览在左，工具与字幕在右，波形横贯底部"]];
    const key = "substar.editor.workspace-layouts-v3";
    const initialSizes = {
      standard: {left:54, first:39.42450055505476, second:56},
      "aligned-left": {left:54, first:39.42450055505476, second:56},
      classic: {left:62, first:78.2905982905983, second:56},
      cinema: {left:54, first:12, second:66}
    };
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch {}
    let preset = presets.some(([id]) => id === saved.preset) ? saved.preset : "standard";
    let sizes = saved.sizes || {};
    const waveformModes = saved.waveformModes || {};
    const menu = document.createElement("label"); menu.className = "workspace-layout-menu";
    const caption = document.createElement("span"); caption.textContent = "工作区";
    const layoutSelect = document.createElement("select"); layoutSelect.id = "workspaceLayoutSelect";
    layoutSelect.setAttribute("aria-label", "工作区布局");
    presets.forEach(([id, name]) => layoutSelect.add(new Option(name, id)));
    layoutSelect.onchange = () => { preset = layoutSelect.value; apply(); persist(); };
    menu.append(caption, layoutSelect); document.querySelector("#taskInfoMenu").after(menu);
    document.querySelector("#revisionLabel").hidden = true;
    function persist() { try { localStorage.setItem(key, JSON.stringify({preset, sizes, waveformModes})); } catch {} }
    function config() { return sizes[preset] ||= {...initialSizes[preset]}; }
    wave.addEventListener("workspace-waveform-mode-change", event => {
      waveformModes[preset] = event.detail.mode;
      wave.dataset.workspaceWaveformMode = event.detail.mode;
      persist();
    });
    function apply() {
      workbench.dataset.workspaceLayout = preset;
      const waveformMode = waveformModes[preset] || (preset === "classic" || preset === "cinema" ? "basic" : "multiline");
      if (wave.dataset.workspaceWaveformMode !== waveformMode) {
        wave.dataset.workspaceWaveformMode = waveformMode;
        wave.dispatchEvent(new CustomEvent("workspace-waveform-mode", {detail:{mode:waveformMode}}));
      }
      const size = config(), width = workbench.clientWidth - 24;
      const maxUpper = 100 - 254 / Math.max(1, workbench.clientHeight - 24) * 100;
      if (preset === "cinema") size.second = Math.min(size.second, Math.max(28, maxUpper - size.first));
      else size.first = Math.min(size.first, Math.max(preset === "classic" ? 50 : 30, maxUpper));
      const toolsWidth = Math.max(180, Math.min(240, width*.125));
      const minimum = preset === "classic" ? toolsWidth+360 : 460;
      const rightMinimum = preset === "classic" ? 300 : 380;
      const left = Math.max(Math.min(minimum, width*.58), Math.min(width-rightMinimum, width*size.left/100));
      workbench.style.setProperty("--workspace-tools", `${toolsWidth}px`);
      workbench.style.setProperty("--workspace-left", `${left}px`);
      workbench.style.setProperty("--workspace-first", `${size.first}%`);
      workbench.style.setProperty("--workspace-second", `${size.second}%`);
      layoutSelect.value = preset;
      requestAnimationFrame(positionHandles);
    }
    const handles = [makeHandle("左右调整工作区宽度", "vertical", "width"),
      makeHandle("上下调整模块高度", "horizontal", "first"), makeHandle("上下调整第二处分界", "horizontal", "second")];
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
        if (kind === "width" && !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        if (kind !== "width" && !["ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault(); resize(kind, kind === "width" ? directions[event.key] : 0, kind === "width" ? 0 : directions[event.key], {...config()}); persist();
      });
      workbench.append(handle); return handle;
    }
    function resize(kind, dx, dy, origin) {
      const size = config(), width = workbench.clientWidth-24, height = workbench.clientHeight-24;
      const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
      if (kind === "width") {
        const minimum = preset === "classic" ? Math.max(180, Math.min(240, width*.125))+360 : 460;
        const rightMinimum = preset === "classic" ? 300 : 380;
        size.left = clamp(origin.left+(preset === "aligned-left" ? -dx : dx)/width*100, Math.min(minimum/width*100,58), Math.min(72, (width-rightMinimum)/width*100));
      } else if (preset === "standard" || preset === "aligned-left") size.first = clamp(origin.first+dy/height*100, 30, Math.max(30, 100-254/height*100));
      else if (preset === "classic") size.first = clamp(origin.first+dy/height*100, 50, Math.max(50, 100-254/height*100));
      else {
        const delta = dy/height*100;
        if (kind === "first") { size.first = clamp(origin.first+delta, 12, origin.first+origin.second-28); size.second = origin.second+origin.first-size.first; }
        else size.second = clamp(origin.first+origin.second+delta, Math.max(50,origin.first+28),Math.max(50,100-254/height*100))-origin.first;
      }
      apply();
    }
    function positionHandles() {
      const box = workbench.getBoundingClientRect(), cueBox = cue.getBoundingClientRect();
      const previewBox = preview.getBoundingClientRect(), toolsBox = tools.getBoundingClientRect(), waveBox = wave.getBoundingClientRect();
      function place(handle, left, top, width, height) { Object.assign(handle.style, {left:`${left-box.left}px`, top:`${top-box.top}px`, width:`${width}px`, height:`${height}px`}); }
      handles[2].hidden = preset !== "cinema";
      if (preset === "standard") {
        place(handles[0], cueBox.left-8, box.top+8, 8, box.height-16);
        place(handles[1], box.left+8, waveBox.top-8, cueBox.left-box.left-16, 8);
      } else if (preset === "aligned-left") {
        place(handles[0], toolsBox.left-8, box.top+8, 8, box.height-16);
        place(handles[1], waveBox.left, waveBox.top-8, waveBox.width, 8);
      } else if (preset === "classic") {
        place(handles[0], previewBox.left-8, box.top+8, 8, waveBox.top-box.top-16);
        place(handles[1], box.left+8, waveBox.top-8, box.width-16, 8);
      } else {
        place(handles[0], toolsBox.left-8, box.top+8, 8, waveBox.top-box.top-16);
        place(handles[1], toolsBox.left, cueBox.top-8, toolsBox.width, 8);
        place(handles[2], box.left+8, waveBox.top-8, box.width-16, 8);
      }
    }
    new ResizeObserver(apply).observe(workbench);
    apply();
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
      const panels = Array.from(tools.querySelectorAll(":scope > .tool-accordion"));
      const tabLabels = ["定位", "查找", "标点", "吸附", "帮助"];
      const tabIcons = [
        '<circle cx="8" cy="8" r="4"/><path d="M8 1v3m0 8v3M1 8h3m8 0h3"/>',
        '<circle cx="6.5" cy="6.5" r="4.5"/><path d="m10 10 4 4"/>',
        '<path d="M4 4h3v4H4zm6 0h3v4h-3zM7 8l-2 4m8-4-2 4"/>',
        '<path d="M3 3v6a5 5 0 0 0 10 0V3M3 6h3m4 0h3M6 3v6m4-6v6"/>',
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
        button.onclick = () => activate(index);
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
        panels.forEach((panel, i) => {
          if (i === index) panel.open = true;
          panel.classList.toggle("experimental-tool-active", i === index);
          buttons[i].setAttribute("aria-selected", String(i === index));
          buttons[i].tabIndex = i === index ? 0 : -1;
        });
      }
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
      const punctuation = panels[2].querySelector(".tool-panel");
      const rules = Array.from(punctuation.children);
      for (const offset of [0, 3]) {
        const row = document.createElement("div"); row.className = "punctuation-rule-row";
        rules[offset].before(row);
        row.append(rules[offset], rules[offset+1], rules[offset+2]);
      }
      const replaceLabel = document.createElement("label");
      replaceLabel.htmlFor = "toolReplace"; replaceLabel.className = "wide-replace-label"; replaceLabel.textContent = "替换为";
      panels[1].querySelector("#toolReplace").before(replaceLabel);
      tools.prepend(tabs);
      tools.querySelectorAll(".tool-locator-row").forEach(row => {
        row.querySelectorAll(".tool-button-pair button").forEach((button, index) => {
          const name = `${button.textContent} · ${row.querySelector("strong").textContent}`;
          button.setAttribute("aria-label", name);
          button.title = name;
          button.textContent = index ? "›" : "‹";
        });
      });
      activate(0);
      // Review mode uses the locator panel for its review controls.
      new MutationObserver(() => {
        if (document.body.classList.contains("review-mode")) activate(0);
      }).observe(document.body, {attributes:true, attributeFilter:["class"]});
    }
  });
})();
