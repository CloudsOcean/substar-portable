(function () {
  "use strict";
  window.SubstarAsrAssist = {init({api, getFiles, getSettings, getGlossaryIds = () => [], apply}) {
    const $ = selector => document.querySelector(selector);
    const button = $("#asrAssistButton"), menu = $("#asrAssistChoices");
    const status = $("#qwenAssistStatus"), cancel = $("#asrAssistCancel");
    const dialog = $("#asrAssistExternal");
    const cache = new WeakMap();
    let dialogFile = null, dialogConfig = null;
    let busy = false, activeTask = null, cancelled = false, selectedFile = null, selectedConfig = null;
    const config = () => JSON.stringify([$("#languageInput").value, $("#recognitionProfileInput").value, getSettings(), getGlossaryIds()]);
    const unchanged = () => getFiles().length === 1 && getFiles()[0] === selectedFile && config() === selectedConfig;
    function sync() {
      button.disabled = busy || getFiles().length !== 1 || !getSettings();
      button.title = getFiles().length !== 1 ? "请选择一个视频或音频" : "先听写一次，再生成 Prompt 和热词";
    }
    function closeMenu() { menu.hidden = true; button.setAttribute("aria-expanded", "false"); }
    button.addEventListener("click", () => { menu.hidden = !menu.hidden; button.setAttribute("aria-expanded", String(!menu.hidden)); });
    document.addEventListener("click", event => { if (!event.target.closest(".asr-assist-menu")) closeMenu(); });
    document.addEventListener("keydown", event => { if (event.key === "Escape") closeMenu(); });
    async function recognize(file, key) {
      let entries = cache.get(file);
      if (!entries) { entries = new Map(); cache.set(file, entries); }
      let row = entries.get(key);
      try {
        if (!row) {
          status.textContent = "正在上传媒体并准备初次听写…";
          const body = new FormData();
          body.append("media", file, file.name);
          body.append("asr_glossary_ids", JSON.stringify(getGlossaryIds()));
          body.append("source_language", $("#languageInput").value);
          body.append("profile_id", $("#recognitionProfileInput").value || "qwen_cloud");
          row = await api("/api/qwen-assist/asr", {method:"POST", body});
          entries.set(key, row);
        }
        activeTask = row.task_id;
        if (cancelled) await api(`/api/tasks/${encodeURIComponent(activeTask)}/cancel`, {method:"POST"});
        while (!row.transcript) {
          if (cancelled) throw new Error("已取消初次听写。");
          if (["failed", "cancelled", "interrupted"].includes(row.state)) throw new Error(row.error?.message || row.message || "初次听写未完成，请重试。");
          status.textContent = `初次听写 · ${row.message || (row.state === "queued" ? "排队中" : "处理中")}`;
          await new Promise(resolve => setTimeout(resolve, 1500));
          row = await api(`/api/qwen-assist/asr/${encodeURIComponent(activeTask)}`);
        }
        entries.set(key, row);
        return row;
      } catch (error) { entries.delete(key); throw error; }
    }
    menu.addEventListener("click", async event => {
      const mode = event.target.closest("[data-asr-assist]")?.dataset.asrAssist;
      if (!mode || busy || $("#qwenAssistButton").disabled || getFiles().length !== 1) return;
      if (mode === "open") {
        closeMenu();
        const file = getFiles()[0], key = config();
        if (dialogFile !== file || dialogConfig !== key) {
          $("#asrAssistPackage").value = "";
          $("#asrAssistResult").value = "";
          $("#asrAssistImportStatus").textContent = "";
        }
        dialogFile = file; dialogConfig = key;
        dialog.showModal();
        return;
      }
      closeMenu(); busy = true; cancelled = false; activeTask = null;
      selectedFile = getFiles()[0]; selectedConfig = config();
      const inputBefore = [$("#qwenPromptInput").value, $("#qwenHotwordsInput").value];
      const brief = $("#qwenAiBriefInput").value.trim() || "根据初次听写提取节目主题与可信专名。";
      const language = $("#languageInput").value;
      $("#qwenAssistButton").disabled = true; cancel.hidden = false; cancel.disabled = false;
      status.className = ""; sync();
      try {
        const injection = await api("/api/glossary/injection-preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({collection_ids:getGlossaryIds()})});
        const row = await recognize(selectedFile, selectedConfig + JSON.stringify(injection.hotwords));
        if (cancelled) throw new Error("已取消。");
        if (!unchanged()) throw new Error("媒体或听写设置已更改，请重新点击生成。");
        cancel.hidden = true;
        status.textContent = mode === "internal" ? "正在根据 ASR 结果生成…" : "正在准备外部模型内容包…";
        const payload = {source_language:language, user_prompt:brief, asr_task_id:row.task_id};
        const result = await api(mode === "internal" ? "/api/qwen-assist" : "/api/qwen-assist/external", {
          method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload),
        });
        if (!unchanged()) throw new Error("媒体或听写设置已更改，未填入旧结果。");
        if (mode === "external") {
          dialogFile = selectedFile; dialogConfig = selectedConfig;
          $("#asrAssistPackage").value = result.package;
          $("#asrAssistResult").value = ""; $("#asrAssistImportStatus").textContent = "";
          dialog.showModal(); status.textContent = "内容包已准备好，复制到外部模型后粘贴结果。";
        } else {
          if (inputBefore[0] !== $("#qwenPromptInput").value || inputBefore[1] !== $("#qwenHotwordsInput").value) throw new Error("输入框内容已修改，未覆盖；请重新生成。");
          apply(result); status.textContent = "已根据初次 ASR 填写 Prompt，并合并热词。";
        }
        status.className = "good";
      } catch (error) { status.textContent = error.message; status.className = "bad"; }
      finally { busy = false; cancel.hidden = true; $("#qwenAssistButton").disabled = false; sync(); }
    });
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        if (activeTask) await api(`/api/tasks/${encodeURIComponent(activeTask)}/cancel`, {method:"POST"});
        cancelled = true; status.textContent = "正在取消…";
      } catch (error) { status.textContent = error.message; cancel.disabled = false; }
    });
    $("#asrAssistClose").addEventListener("click", () => dialog.close());
    $("#asrAssistCopy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText($("#asrAssistPackage").value); $("#asrAssistImportStatus").textContent = "内容包已复制。"; }
      catch (_) { $("#asrAssistPackage").select(); $("#asrAssistImportStatus").textContent = "请按 Ctrl+C 复制选中的内容。"; }
    });
    $("#asrAssistApply").addEventListener("click", () => {
      try {
        if (getFiles().length !== 1 || getFiles()[0] !== dialogFile || config() !== dialogConfig) throw new Error("媒体或听写设置已更改，请重新打开填写界面。");
        const raw = $("#asrAssistResult").value.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
        apply(JSON.parse(raw), "external"); dialog.close(); status.textContent = "已填入外部模型的 Prompt，并合并热词。"; status.className = "good";
      } catch (error) { $("#asrAssistImportStatus").textContent = error instanceof SyntaxError ? "请粘贴包含 prompt 和 hotwords 的完整 JSON。" : error.message; }
    });
    sync();
    return {sync};
  }};
})();
