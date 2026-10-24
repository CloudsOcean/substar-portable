(function (root) {
  "use strict";
  const languages = [["zh-CN", "汉语"], ["en", "英语"], ["ja", "日语"], ["ko", "韩语"], ["th", "泰语"], ["ar", "阿语"], ["fr", "法语"], ["de", "德语"], ["es", "西语"], ["pt", "葡语"], ["ru", "俄语"], ["it", "意语"], ["vi", "越语"], ["id", "印尼"], ["ms", "马语"], ["tl", "菲语"], ["hi", "印语"], ["nl", "荷语"], ["sv", "瑞语"], ["da", "丹语"], ["fi", "芬语"], ["no", "挪语"], ["el", "希语"], ["pl", "波语"], ["cs", "捷语"], ["hu", "匈语"], ["ro", "罗语"], ["bg", "保语"], ["hr", "克语"], ["sk", "斯语"]];
  const defaults = languages.slice(0,12).map(([code]) => code);
  const valid = code => languages.some(([id]) => id === code);
  const nameOf = code => languages.find(([id]) => id === code)?.[1] || code;
  const models = new WeakMap();
  let added = [];
  try { added = JSON.parse(localStorage.getItem("substar.extraLanguages") || "[]").filter(valid); } catch {}
  const instances = new WeakMap();
  function controls(host) {
    const ids = {translationLanguageLayout:["translationSourceLanguage","translationTargetLanguage"], taskInfoLanguageLayout:["taskInfoSourceLanguage","taskInfoTargetLanguage"], splitLanguageLayout:["languageInput","targetLanguageInput"]}[host.id] || [];
    return ids.map(id => document.getElementById(id));
  }
  function sync(host) {
    const state = instances.get(host), [source,target] = controls(host);
    if (source) source.value = state.auto_detect || state.source_languages.length !== 1 ? "Auto" : (host.id === "translationLanguageLayout" ? state.source_languages[0] : state.source_languages[0].replace("zh-CN","zh"));
    if (target) target.value = state.lower_target;
  }
  function render(host) {
    const state = instances.get(host);
    const slot = host.querySelector("[data-source-languages]");
    const visible = [...new Set([...defaults, ...added, ...state.source_languages, state.lower_target, state.upper_target])].filter(valid);
    slot.replaceChildren(...visible.map(code => {
      const name = nameOf(code);
      const button = document.createElement("button");
      const selected = !state.auto_detect && state.source_languages.includes(code);
      button.type = "button";
      button.className = "language-block";
      button.textContent = name + (selected && state.bidirectional && state.upper_target === code ? " ★" : "");
      button.disabled = false;
      button.setAttribute("aria-label", `${name}原始语言`);
      button.setAttribute("aria-pressed", String(selected));
      button.addEventListener("click", () => {
        if (host.disabled) return;
        if (selected && state.source_languages.length === 1) {
          host.querySelector("[data-layout-status]").textContent = "至少选择一种原始语言。"; return;
        }
        if (!selected && !state.auto_detect && state.source_languages.length >= 4) {
          host.querySelector("[data-layout-status]").textContent = "最多选择四种原始语言。"; return;
        }
        if (state.auto_detect) state.source_languages = [];
        state.auto_detect = false;
        state.source_languages = selected ? state.source_languages.filter(id => id !== code) : [...state.source_languages,code];
        render(host);
        host.dispatchEvent(new Event("change", {bubbles:true}));
      });
      return button;
    }));
    const add = document.createElement("button"); add.type = "button"; add.className = "language-block";
    add.textContent = "＋"; add.setAttribute("aria-label", "添加语言");
    add.addEventListener("click", () => { host.querySelector("[data-add-language]").hidden = !host.querySelector("[data-add-language]").hidden; });
    slot.append(add);
    const picker = host.querySelector("[data-extra-language]");
    picker.replaceChildren(...languages.filter(([code]) => !visible.includes(code)).map(([code,name]) => {
      const option = document.createElement("option"); option.value = code; option.textContent = name; return option;
    }));
    add.disabled = picker.options.length === 0;
    const target = host.querySelector("[data-single-target]");
    target.replaceChildren(...visible.map(code => { const option = document.createElement("option"); option.value=code; option.textContent=nameOf(code); return option; }));
    target.value = state.lower_target;
    const dialect = host.querySelector("[data-keep-dialect]");
    const supported = (models.get(host) || "").startsWith("qwen-audio-3.1-asr-flash-filetrans");
    dialect.disabled = !supported;
    dialect.checked = supported && state.keep_dialect;
    host.querySelector("[data-dialect-help]").textContent = supported ? "保留粤语等方言的原有表达。" : "仅 Qwen Audio 3.1 文件听写支持，可在设置中切换模型。";
    const reverse = host.querySelector("[data-reverse-target]");
    reverse.replaceChildren(...(state.auto_detect ? visible : state.source_languages).filter(code => code !== state.lower_target).map(code => {
      const option = document.createElement("option"); option.value = code;
      option.textContent = languages.find(([id]) => id === code)[1] + " ★"; return option;
    }));
    if (![...reverse.options].some(option => option.value === state.upper_target)) state.upper_target = reverse.options[0]?.value || languages.find(([code]) => code !== state.lower_target)[0];
    if (!reverse.options.length) {
      const option = document.createElement("option"); option.value = state.upper_target;
      option.textContent = languages.find(([code]) => code === state.upper_target)[1]; reverse.append(option);
    }
    reverse.value = state.upper_target;
    host.querySelector("[data-auto-detect]").checked = state.auto_detect;
    sync(host);
    host.querySelector("[data-reverse-field]").hidden = !state.bidirectional;
    host.querySelector("[data-layout-status]").textContent = "";
  }
  function mount(id) {
    const host = document.getElementById(id);
    if (!host || instances.has(host)) return;
    host.innerHTML = `<div data-layout-fields>
        <section class="language-slot"><strong>原始语言（多选）</strong><label><input type="checkbox" data-auto-detect> 自动识别</label><div data-source-languages role="group" aria-label="原始语言多选"></div></section>
        <div data-add-language hidden><select data-extra-language aria-label="其他语言"></select><button type="button" data-add-confirm>添加</button></div>
        <label>目标语言（下行）<select data-single-target aria-label="下行目标语言">${languages.map(([code,name]) => `<option value="${code}">${name}</option>`).join("")}</select></label>
        <label class="language-bidirectional"><input type="checkbox" data-keep-dialect> 保留方言表达</label><small data-dialect-help></small>
        <label class="language-bidirectional"><input type="checkbox" data-bidirectional> 双向翻译</label>
        <label data-reverse-field hidden>反向译入 ★<select data-reverse-target aria-label="反向译入语言"></select></label>
        <small>原始语言译入唯一目标语言。勾选双向后，目标语言原文译入 ★ 语言；否则保留在下行，不反向翻译。混合原文默认上行。</small>
        <small data-layout-status role="status"></small>
      </div>`;
    instances.set(host, {});
    host.querySelector("[data-add-confirm]").addEventListener("click", () => {
      const code = host.querySelector("[data-extra-language]").value;
      if (!valid(code)) return;
      added = [...new Set([...added, code])];
      try { localStorage.setItem("substar.extraLanguages", JSON.stringify(added)); } catch {}
      host.querySelector("[data-add-language]").hidden = true;
      document.querySelectorAll(".translation-language-layout").forEach(node => { if(instances.has(node)) render(node); });
      host.dispatchEvent(new Event("change", {bubbles:true}));
    });
    host.querySelector("[data-keep-dialect]").addEventListener("change", event => { instances.get(host).keep_dialect = event.target.checked; });
    host.querySelector("[data-auto-detect]").addEventListener("change", event => {
      const state = instances.get(host); state.auto_detect = event.target.checked;
      if (!state.source_languages.length) state.source_languages = ["en"];
      render(host);
    });
    host.querySelector("[data-single-target]").addEventListener("change", event => {
      const state = instances.get(host);
      state.lower_target = event.target.value;

      render(host);
    });
    host.querySelector("[data-bidirectional]").addEventListener("change", event => {
      instances.get(host).bidirectional = event.target.checked; render(host);
    });
    host.querySelector("[data-reverse-target]").addEventListener("change", event => {
      instances.get(host).upper_target = event.target.value; render(host);
    });
    write(id, {});
  }
  function write(id, value = {}) {
    const host = document.getElementById(id);
    if (!host) return;
    const [sourceControl,targetControl] = controls(host);
    const target = value.lower_target || targetControl?.value || "zh-CN";
    const original = sourceControl?.value === "zh" ? "zh-CN" : sourceControl?.value;
    const sources = value.source_languages || (value.enabled ? [...(value.upper_languages || []), ...(value.lower_languages || [])] : languages.some(([code]) => code === original) ? [original] : []);
    const state = {source_languages:[...new Set(sources)].filter(valid).slice(0,4), keep_dialect:Boolean(value.keep_dialect), auto_detect:value.auto_detect ?? !sources.length, lower_target:target,
      upper_target:value.upper_target || sources.find(code => code !== target) || (target === "en" ? "zh-CN" : "en"),
      bidirectional:value.bidirectional ?? Boolean(value.enabled)};
    instances.set(host,state);
    host.querySelector("[data-single-target]").value = target;
    host.querySelector("[data-bidirectional]").checked = state.bidirectional;
    render(host);
  }
  function read(id) {
    const host = document.getElementById(id);
    if (!host) return {};
    const state = instances.get(host);
    return {enabled:true, source_languages:state.auto_detect ? [] : [...state.source_languages], auto_detect:state.auto_detect,
      upper_languages:languages.map(([code]) => code).filter(code => code !== state.lower_target), lower_languages:[state.lower_target],
      upper_target:state.upper_target, lower_target:state.lower_target, bidirectional:state.bidirectional, keep_dialect:!host.querySelector("[data-keep-dialect]").disabled && state.keep_dialect};
  }
  function configureModel(model) {
    document.querySelectorAll(".translation-language-layout").forEach(host => { models.set(host,model); if(instances.has(host)) render(host); });
  }
  root.SubstarTranslationLayout = {mount, read, write, configureModel, languages};
  document.querySelectorAll(".translation-language-layout").forEach(node => mount(node.id));
})(globalThis);
