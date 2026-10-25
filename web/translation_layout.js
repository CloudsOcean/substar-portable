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
      button.textContent = name;
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
    host.querySelector("[data-dialect-help]").textContent = "勾选后保留方言原文";
    const reverse = host.querySelector("[data-reverse-target]");
    reverse.replaceChildren(...languages.map(([code]) => code).filter(code => code !== state.lower_target).map(code => {
      const option = document.createElement("option"); option.value = code;
      option.textContent = nameOf(code); return option;
    }));
    if (![...reverse.options].some(option => option.value === state.upper_target)) state.upper_target = reverse.options[0]?.value || languages.find(([code]) => code !== state.lower_target)[0];
    if (!reverse.options.length) {
      const option = document.createElement("option"); option.value = state.upper_target;
      option.textContent = languages.find(([code]) => code === state.upper_target)[1]; reverse.append(option);
    }
    reverse.value = state.upper_target;
    host.querySelector("[data-reverse-field]").firstChild.textContent = `${nameOf(state.lower_target)}原文译为`;
    host.querySelector("[data-auto-detect]").title = "无需预选原文语言；根据每条字幕的实际语言确定翻译方向";
    host.querySelector("[data-auto-detect]").checked = state.auto_detect;
    sync(host);
    host.querySelector("[data-reverse-field]").hidden = !state.bidirectional;
    host.querySelector("[data-layout-status]").textContent = "";
  }
  function mount(id) {
    const host = document.getElementById(id);
    if (!host || instances.has(host)) return;
    host.innerHTML = `<div data-layout-fields>
        <section class="language-slot"><strong>原文语言（上行多选）</strong><label><input type="checkbox" data-auto-detect> 自动识别</label><div data-source-languages role="group" aria-label="原文语言上行多选"></div></section>
        <div data-add-language hidden><select data-extra-language aria-label="其他语言"></select><button type="button" data-add-confirm>添加</button></div>
        <label class="language-target">译文语言（下行）<select data-single-target aria-label="下行译文语言">${languages.map(([code,name]) => `<option value="${code}">${name}</option>`).join("")}</select></label>
        <label class="language-bidirectional language-dialect"><input type="checkbox" data-keep-dialect> 保留方言<small data-dialect-help></small></label>
        <label class="language-bidirectional language-direction"><input type="checkbox" data-bidirectional> 双向翻译<small class="language-direction-help">原译同语种时反向翻译</small></label>
        <label data-reverse-field hidden>反向译入语言<select data-reverse-target aria-label="反向译入语言"></select></label>
        <small data-layout-status role="status"></small>
      </div>`;
    if (id === "splitLanguageLayout") {
      const target = host.querySelector(".language-target");
      const workflow = document.getElementById("splitWorkflowInput")?.closest("label");
      if (workflow) {
        workflow.classList.add("split-workflow-field");
        target.after(workflow);
      }
      const options = document.createElement("div");
      options.className = "language-options";
      const direction = document.createElement("div");
      direction.className = "language-direction-option";
      direction.append(host.querySelector(".language-direction"), host.querySelector("[data-reverse-field]"));
      options.append(host.querySelector(".language-dialect"), direction);
      host.querySelector("[data-layout-status]").before(options);
    }
    // The per-run translation option lives with the language routing controls.
    if (id === "translationLanguageLayout") {
      const regroup = document.getElementById("translationRegroup")?.closest("label");
      if (regroup) {
        const help = regroup.nextElementSibling;
        regroup.append(help);
        host.querySelector("[data-layout-status]").before(regroup);
      }
    }
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
