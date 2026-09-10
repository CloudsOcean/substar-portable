(function () {
  "use strict";
  window.SubstarGlossaryInjection = {init({api, getTemporary, getProject}) {
    const $ = s=>document.querySelector(s);
    let ready=false, version=0, timer, initialized=false;
    const ids=()=>[...$("#glossaryInjectionChoices").querySelectorAll("input:checked")].map(n=>n.value);
    async function refresh() {
      const epoch=++version;
      try {
        if (!ready) return;
        const result=await api("/api/glossary/injection-preview",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({collection_ids:ids(),temporary:getTemporary()})});
        if(epoch!==version)return;
        const supported=!$("#qwenHotwordsInput").disabled;
        $("#glossaryInjectionPreview").value=supported?result.hotwords.map(r=>r.text).join("、"):"当前听写模型不支持热词注入";
        $("#glossaryInjectionStatus").textContent=supported?`${result.hotwords.length} / 2000 个 · 已合并临时热词并去重`:"切换到支持热词的 Qwen 模型后生效";
      } catch(e) {if(epoch===version){$("#glossaryInjectionPreview").value="";$("#glossaryInjectionStatus").textContent=e.message;}}
    }
    async function load() {
      try {
        const chosen=new Set(ids()); const library=await api("/api/glossary");
        if (!library.collections?.every(c=>Array.isArray(c.injection_permissions))) throw new Error("请重启 Substar 以启用词库注入");
        const panel=$("#glossaryInjectionChoices");panel.replaceChildren();
        for(const collection of library.collections.filter(c=>c.kind === "project" && (c.injection_permissions||[]).includes("asr"))) {
          const label=document.createElement("label"),check=document.createElement("input");check.type="checkbox";check.value=collection.id;check.checked=!initialized||chosen.has(collection.id);
          label.append(check,document.createTextNode(`${collection.name} (${library.entries.filter(e=>e.enabled&&e.glossary_id===collection.id).length})`));panel.append(label);
        }
        if(!panel.children.length)panel.textContent="没有获准注入听写的词库，请在词库页设置权限。";
        initialized=true;ready=true;refresh();
      } catch(e){ready=false;$("#glossaryInjectionStatus").textContent=e.message;}
    }
    function schedule(){clearTimeout(timer);timer=setTimeout(refresh,250);}
    $("#glossaryInjectionChoices").addEventListener("change",schedule);
    for(const selector of ["#qwenHotwordsInput","#recognitionProfileInput"]) $(selector).addEventListener("input",schedule);
    window.addEventListener("focus",load);
    async function collect(rows,source) {
      if(!rows.length)return;
      try {await api("/api/glossary/candidates",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rows,source,project:getProject()})});}
      catch(e){$("#glossaryInjectionStatus").textContent=`候选池收集失败：${e.message}`;return;}
      schedule();
    }
    load();return {ids:()=>$("#qwenHotwordsInput").disabled?[]:ids(),refresh:schedule,collect};
  }};
})();
