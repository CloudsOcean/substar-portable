(() => {
  'use strict';
  const page = document.querySelector('[data-finish="paper-dark"]');
  if (!page) return;
  document.title = 'B · 深色创作室 · Substar';
  history.replaceState(null, '', '/assets/split-concepts/paper-dark.html');
  page.querySelector('.concept-eyebrow').textContent = 'SUBSTAR / B — 深色创作室';
  const comparison = page.querySelector('.concept-local');
  const link = document.createElement('a');
  link.className = 'pd-comparison'; link.href = '/assets/split-concepts/paper.html';
  link.textContent = '查看浅色 B ↗'; comparison.replaceWith(link);
  function organize() {
    const host = document.getElementById('splitLanguageLayout');
    const fields = host.querySelector('[data-layout-fields]');
    if (!fields || fields.dataset.organized) return;
    fields.dataset.organized = 'true';
    const source = fields.querySelector('.language-slot');
    const target = fields.querySelector('[data-single-target]')?.closest('label');
    const dialect = fields.querySelector('[data-keep-dialect]')?.closest('label');
    const direction = fields.querySelector('[data-bidirectional]')?.closest('label');
    const reverse = fields.querySelector('[data-reverse-field]');
    const workflow = document.getElementById('splitWorkflowInput').closest('label');
    function section(number, title) {
      const node = document.createElement('section'); node.className = 'pd-config-group';
      const heading = document.createElement('h3');
      const index = document.createElement('span'); index.textContent = number;
      heading.append(index, title); node.append(heading); return node;
    }
    const language = section('01','识别语言');
    source.querySelector('strong').textContent = '原文语言 · 可多选';
    language.append(source);
    const add = fields.querySelector('[data-add-language]');
    if (add) language.append(add);
    if (dialect) language.append(dialect);
    const translation = section('02','翻译方向');
    if (target) translation.append(target);
    if (direction) translation.append(direction);
    if (reverse) translation.append(reverse);
    const segmentation = section('03','字幕切分');
    segmentation.append(workflow);
    fields.prepend(language, translation, segmentation);
    fields.querySelector('.language-options')?.remove();
    const punctuation = document.getElementById('referenceBreakSymbolsField');
    if (punctuation) segmentation.append(punctuation);
    const dialectHelp = fields.querySelector('[data-dialect-help]');
    if (dialectHelp && !dialect?.contains(dialectHelp)) dialect?.append(dialectHelp);
    const enhanced = page.querySelector('.qwen-enhancement-settings > summary');
    if (enhanced) enhanced.firstChild.textContent = '听写增强 ';
  }
  // Defer regrouping until the existing scripts finish mounting the language controls.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', organize, {once:true});
  else organize();
})();
