(function (root) {
  "use strict";
  const actions = [
    ["play_pause", "播放／暂停", "Space", "切换预览播放"],
    ["undo", "撤销", "Ctrl+Z", "撤销上一次项目编辑"],
    ["redo", "重做", "Ctrl+Y", "重做刚撤销的编辑"],
    ["create_cue", "创建字幕", "N", "在播放头处创建字幕"],
    ["speaker", "分配／取消说话人", "0–9", "仅处理选中的字幕，0 取消分配"],
    ["hide_cue", "隐藏字幕", "Backspace", "仅隐藏选中且可见的字幕"],
    ["restore_cue", "恢复字幕", "Shift+Backspace", "仅恢复选中且已隐藏的字幕"],
    ["point_marker", "添加点标记", "M", "在每段选区起点或播放头处添加"],
    ["range_marker", "添加线标记", "Shift+M", "按选区分段添加"],
    ["draw_gap", "精确绘制空隙", "G", "按键后用左键拖出范围，Esc 取消"],
    ["delete_gap", "删除空隙", "Alt+G", "删除选中的空隙"],
    ["retranscribe", "重新听写片段", "Alt+R", "重新听写选中的连续范围"],
    ["skip_gaps", "跳过空隙", "Alt", "单独按下并松开 Alt 时切换"],
    ["word_timing", "切换词级时间条", "Q", "显示或隐藏词级时间条"],
    ["previous_cue", "上一条字幕", "ArrowUp", "定位到上一条字幕"],
    ["next_cue", "下一条字幕", "ArrowDown", "定位到下一条字幕"],
    ["seek_back", "播放头后退", "ArrowLeft", "后退 1 秒"],
    ["seek_forward", "播放头前进", "ArrowRight", "前进 1 秒"],
    ["previous_marker", "上一个标记", "[", "定位到上一个标记"],
    ["next_marker", "下一个标记", "]", "定位到下一个标记"],
  ].map(([id, label, key, description]) => ({id, label, key, description}));
  const defaults = Object.fromEntries(actions.map(({id, key}) => [id, key]));
  const serverActions = new Set(["undo", "redo", "play_pause", "hide_cue"]);
  const fixedActions = new Set(["speaker", "skip_gaps"]);
  const storageKey = "substar.editor.shortcuts";
  const arrows = {ArrowUp:"↑", ArrowDown:"↓", ArrowLeft:"←", ArrowRight:"→"};

  function bindings(settings = {}) {
    let overrides = {};
    try { overrides = JSON.parse(localStorage.getItem(storageKey) || "{}") || {}; } catch (_) {}
    const result = {...defaults};
    for (const {id} of actions) {
      if (serverActions.has(id)) result[id] = settings["shortcut_" + id] || defaults[id];
      else if (!fixedActions.has(id) && typeof overrides[id] === "string" && overrides[id]) result[id] = overrides[id];
    }
    return result;
  }

  function saveLocal(id, value) {
    const config = bindings();
    const overrides = Object.fromEntries(actions.filter(action =>
      !serverActions.has(action.id) && !fixedActions.has(action.id)
    ).map(action => [action.id, config[action.id]]));
    if (value === defaults[id]) delete overrides[id];
    else overrides[id] = value;
    localStorage.setItem(storageKey, JSON.stringify(overrides));
  }

  function fromEvent(event) {
    if (event.isComposing || event.keyCode === 229 || !event.key ||
        ["Control","Alt","Shift","Meta","Dead","Unidentified","Process","AltGraph"].includes(event.key) ||
        event.getModifierState?.("AltGraph")) return "";
    const keys = [];
    if (event.ctrlKey) keys.push("Ctrl");
    if (event.altKey) keys.push("Alt");
    if (event.shiftKey) keys.push("Shift");
    if (event.metaKey) keys.push("Meta");
    const bracket = {BracketLeft:"[", BracketRight:"]"}[event.code];
    const key = event.key === " " ? "Space" : bracket || (event.key.length === 1 ? event.key.toUpperCase() : event.key);
    return [...keys, key].join("+");
  }

  const textFields = 'textarea,[role="textbox"],[contenteditable]:not([contenteditable="false"]),input:not([type]),input:is([type="text"],[type="search"],[type="number"],[type="email"],[type="url"],[type="password"])';
  const formControls = 'input,select,button,summary,[role="combobox"],[role="slider"],dialog[open],[aria-modal="true"]:not(.hidden)';
  function inInput(event, {textOnly = false} = {}) {
    return event.isComposing || event.keyCode === 229 || event.composedPath().some(node =>
      node instanceof Element && (node.matches(textFields) ||
        (!textOnly && node.matches(formControls) && !node.matches(".display-token,[data-cue-select]")))
    );
  }

  const display = value => value?.split("+").map(key => arrows[key] || key).join("+") || "";
  const speakerForDigit = (digit, ids) => digit === "0" ? null : ids.find(id => id === "speaker_" + (Number(digit) - 1));
  const digitForSpeaker = id => id == null ? "0" : /^speaker_[0-8]$/.test(id) ? String(Number(id.at(-1)) + 1) : "";
  root.SubstarShortcuts = {actions, defaults, serverActions, fixedActions, storageKey,
    bindings, saveLocal, fromEvent, inInput, display, speakerForDigit, digitForSpeaker};
})(globalThis);
