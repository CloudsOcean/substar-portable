(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SubstarLanguageLayout = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const HAN = /[\u3400-\u9fff\uf900-\ufaff]/;
  const KANA = /[\u3040-\u30ff\u31f0-\u31ff]/;
  const HANGUL = /[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]/;
  const PUNCTUATION_OR_SYMBOL = /[\p{P}\p{S}]/u;
  const CJK_BETWEEN_SPACE = /([\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\u31f0-\u31ff])\s+(?=[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\u31f0-\u31ff])/g;

  function normalizeLanguage(language) {
    const value = String(language || "").trim().toLowerCase().replaceAll("_", "-");
    return ["zh", "ja", "ko", "en", "mixed"].find(code => value.startsWith(code)) || "";
  }
  function detectLanguage(text, fallback = "en") {
    const value = String(text || "");
    if (KANA.test(value)) return "ja";
    if (HANGUL.test(value)) return "ko";
    if (HAN.test(value) && /[A-Za-z]/.test(value)) return "mixed";
    if (HAN.test(value)) return "zh";
    return normalizeLanguage(fallback) || "en";
  }
  function resolveSourceLanguage(configuredLanguage, documentText) {
    // The user-confirmed project snapshot is the authority. Content detection
    // is only the explicit Auto/empty fallback; it must never silently
    // reinterpret a frozen project contract.
    const configured = normalizeLanguage(configuredLanguage);
    return configured || detectLanguage(documentText);
  }
  function formatText(text, language = null) {
    let value = String(text || "").replace(/\s+/g, " ").trim();
    const resolved = normalizeLanguage(language) || detectLanguage(value);
    if (["zh", "ja", "mixed"].includes(resolved)) {
      value = value.replace(CJK_BETWEEN_SPACE, "$1");
    }
    return value;
  }
  function layoutTokens(tokens, language = null) {
    const values = (tokens || []).map(value => String(value || "").trim()).filter(Boolean);
    const resolved = normalizeLanguage(language) || detectLanguage(values.join(""));
    if (!["zh", "ja", "mixed"].includes(resolved)) return formatText(values.join(" "), resolved);
    const result = values.reduce((output, value) => {
      if (!output) return value;
      const previous = [...output].reverse().find(char => !PUNCTUATION_OR_SYMBOL.test(char) && !/\s/.test(char)) || "";
      const current = [...value].find(char => !PUNCTUATION_OR_SYMBOL.test(char) && !/\s/.test(char)) || "";
      const startsWithPunctuation = PUNCTUATION_OR_SYMBOL.test(value[0] || "");
      const previousIsCjk = HAN.test(previous) || KANA.test(previous);
      const currentIsCjk = HAN.test(current) || KANA.test(current);
      // Punctuation belongs to its neighbouring CJK token.  Looking only at
      // output.at(-1) made "哦，" + "对了，" become "哦， 对了，"; after a
      // comma-removal presentation rule that invisible layout space survived.
      const separator = startsWithPunctuation || (previousIsCjk && currentIsCjk) ? "" : " ";
      return output + separator + value;
    }, "");
    return formatText(result, resolved);
  }
  function characterCount(text, language = null, options = {}) {
    // One product rule: every displayed character counts.  The options
    // argument is retained only so older internal callers do not break.
    return [...formatText(text, language)].length;
  }
  function tokenUnitKind(text) {
    const lexical = [...String(text || "")].filter(char =>
      !PUNCTUATION_OR_SYMBOL.test(char) && !/\s/.test(char)
    );
    if (lexical.length && lexical.every(char => HAN.test(char) || KANA.test(char) || HANGUL.test(char))) {
      return "character";
    }
    return "word";
  }
  function virtualBoundaryKind(leftText, rightText, language = null) {
    const resolved = normalizeLanguage(language);
    const leftKind = tokenUnitKind(leftText);
    const rightKind = tokenUnitKind(rightText);
    if (["zh", "ja", "ko"].includes(resolved)) {
      const alphabeticWord = value => /[A-Za-z]/.test(String(value || ""));
      return leftKind === "word" && rightKind === "word"
        && alphabeticWord(leftText) && alphabeticWord(rightText)
        ? "word" : "character";
    }
    if (resolved === "en") return "word";
    return leftKind === "character" && rightKind === "character" ? "character" : "word";
  }
  return Object.freeze({
    normalizeLanguage,
    detectLanguage,
    resolveSourceLanguage,
    formatText,
    layoutTokens,
    characterCount,
    tokenUnitKind,
    virtualBoundaryKind
  });
});
