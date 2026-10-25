(function () {
  "use strict";
  function refine() {
    const host = document.getElementById("splitLanguageLayout");
    if (!host) return;
    const heading = host.querySelector(".language-slot strong");
    if (heading) heading.innerHTML = '原文语言 <small>上行多选</small>';
    const target = host.querySelector(".language-target") || host.querySelector("[data-single-target]")?.closest("label");
    if (target) {
      target.classList.add("language-target");
      [...target.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).forEach(node => node.remove());
      const label = document.createElement("span");
      label.className = "split-field-title";
      label.innerHTML = '译文语言 <small>下行</small>';
      target.prepend(label);
    }
    const workflow = document.getElementById("splitWorkflowInput")?.closest("label");
    if (target && workflow) {
      workflow.classList.add("split-workflow-field");
      target.after(workflow);
    }
    const dialect = host.querySelector("[data-keep-dialect]")?.closest("label");
    const thumbnails = document.getElementById("generateThumbnailsInput")?.closest("label");
    if (dialect && thumbnails) {
      const stack = document.createElement("div");
      stack.className = "split-checkbox-stack";
      dialect.before(stack);
      stack.append(dialect, thumbnails);
    }
    const status = document.getElementById("qwenAssistStatus");
    if (status) {
      status.setAttribute("role", "status");
      status.setAttribute("aria-atomic", "true");
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", refine, {once:true});
  else refine();
})();
