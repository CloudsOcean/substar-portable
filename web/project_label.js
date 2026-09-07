(function (root) {
  "use strict";
  function projectLabel(value) {
    const original = String(value || "");
    return original.replace(/\s*·\s*Qwen\s+云端\s*$/i, "").trim() || original;
  }
  if (typeof module === "object" && module.exports) module.exports = projectLabel;
  else root.SubstarProjectLabel = projectLabel;
})(typeof window === "object" ? window : globalThis);
