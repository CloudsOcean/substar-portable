(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SubstarSystemSaveAs = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const exchangeNames = {
    "external-ai-prooftranslation":"外部AI校译",
    "external-ai-split":"外部AI切分",
    "external-ai-edit":"外部AI编辑",
    "external-ai-generation":"外部AI生成",
    "subtitle-project":"字幕工程"
  };

  function safeFilename(value) {
    return String(value || "export")
      .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_")
      .replace(/[. ]+$/g, "") || "export";
  }

  function subtitleSpec(projectId, mode, url) {
    return {
      url,
      suggestedName:`${safeFilename(projectId)}_${safeFilename(mode)}.srt`,
      description:"SubRip 字幕",
      mimeType:"application/x-subrip",
      extension:".srt"
    };
  }

  function exchangeSpec(projectId, kind, url) {
    const label = exchangeNames[kind];
    if (!label) throw new Error(`未知导出类型：${kind}`);
    return {
      url,
      suggestedName:`${safeFilename(projectId)}_${label}.zip`,
      description:"ZIP 压缩包",
      mimeType:"application/zip",
      extension:".zip"
    };
  }

  function glossarySpec(format, url = "") {
    const normalized = String(format).toLowerCase();
    const types = {
      xlsx:["Excel 热词表", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
      csv:["CSV 热词表", "text/csv"],
      json:["JSON 热词表", "application/json"]
    };
    if (!types[normalized]) throw new Error(`未知导出类型：${format}`);
    return {
      url,
      suggestedName:`substar_glossary.${normalized}`,
      description:types[normalized][0],
      mimeType:types[normalized][1],
      extension:`.${normalized}`
    };
  }

  function pickerFor(dependencies) {
    if (dependencies.picker) return dependencies.picker;
    if (typeof globalThis !== "undefined" && typeof globalThis.showSaveFilePicker === "function") {
      return globalThis.showSaveFilePicker.bind(globalThis);
    }
    throw new Error("当前浏览器不支持 Windows“另存为”窗口");
  }

  async function chooseHandle(spec, dependencies) {
    try {
      return await pickerFor(dependencies)({
        suggestedName:spec.suggestedName,
        types:[{
          description:spec.description,
          accept:{[spec.mimeType]:[spec.extension]}
        }]
      });
    } catch (error) {
      if (error?.name === "AbortError") return null;
      throw error;
    }
  }

  async function responseError(response) {
    const contentType = response.headers?.get?.("content-type") || "";
    if (contentType.includes("json")) {
      try {
        const body = await response.json();
        return body?.detail?.message || body?.detail || body?.message || `请求失败 (${response.status})`;
      } catch (_) {
        // Fall through to the status message.
      }
    }
    return `请求失败 (${response.status})`;
  }

  async function writeBlob(handle, blob) {
    const writable = await handle.createWritable();
    try {
      await writable.write(blob);
      await writable.close();
    } catch (error) {
      try { await writable.abort?.(); } catch (_) { /* Ignore cleanup failure. */ }
      throw error;
    }
  }

  async function saveUrl(spec, dependencies = {}) {
    // The picker must be opened before fetch so the click's user activation is
    // still live. No server export starts until the user has chosen a path.
    const handle = await chooseHandle(spec, dependencies);
    if (!handle) return {cancelled:true};
    const fetcher = dependencies.fetch || globalThis.fetch.bind(globalThis);
    const response = await fetcher(spec.url);
    if (!response.ok) throw new Error(await responseError(response));
    if (response.body && typeof response.body.pipeTo === "function") {
      const writable = await handle.createWritable();
      await response.body.pipeTo(writable);
      return {cancelled:false, filename:handle.name || spec.suggestedName};
    }
    await writeBlob(handle, await response.blob());
    return {cancelled:false, filename:handle.name || spec.suggestedName};
  }

  async function saveBlob(spec, blob, dependencies = {}) {
    const handle = await chooseHandle(spec, dependencies);
    if (!handle) return {cancelled:true};
    await writeBlob(handle, blob);
    return {cancelled:false, filename:handle.name || spec.suggestedName};
  }

  return {exchangeSpec, glossarySpec, safeFilename, saveBlob, saveUrl, subtitleSpec};
});
