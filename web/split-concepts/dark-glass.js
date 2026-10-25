(() => {
  'use strict';
  const page = document.querySelector('[data-finish="dark-glass"]');
  if (!page) return;
  document.title = 'B · 深色液态玻璃 · Substar';
  history.replaceState(null, '', '/assets/split-concepts/dark-glass.html');
  page.querySelector('.concept-eyebrow').textContent = 'SUBSTAR / LIQUID WORKSPACE';
  page.querySelector('h1').textContent = '让好内容，清晰表达。';
  page.querySelector('.concept-intro p').textContent = '从一段音视频开始，完成听写、切分与翻译。';
  page.querySelector('.concept-local').textContent = '✦ 你的字幕创作空间';
  const atmosphere = document.createElement('div');
  atmosphere.className = 'glass-atmosphere';
  atmosphere.setAttribute('aria-hidden', 'true');
  atmosphere.innerHTML = '<i></i><i></i><i></i>';
  page.prepend(atmosphere);
  page.dataset.glassMaterial = 'soft';
  page.querySelectorAll('[data-glass-material]').forEach(button => {
    button.addEventListener('click', () => {
      page.dataset.glassMaterial = button.dataset.glassMaterial;
      page.querySelectorAll('button[data-glass-material]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    });
  });
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let pending = 0;
  let latest;
  page.addEventListener('pointermove', event => {
    if (reduced.matches || event.pointerType === 'touch') return;
    const surface = event.target.closest('.creation-card, .pipeline-card, .recent-item, .drop-zone');
    if (!surface) return;
    latest = {surface,x:event.clientX,y:event.clientY};
    if (pending) return;
    pending = requestAnimationFrame(() => {
      pending = 0;
      const {surface,x,y} = latest;
      const rect = surface.getBoundingClientRect();
      surface.style.setProperty('--glass-x', `${((x-rect.left)/rect.width)*100}%`);
      surface.style.setProperty('--glass-y', `${((y-rect.top)/rect.height)*100}%`);
    });
  }, {passive:true});
  window.addEventListener('pagehide', () => cancelAnimationFrame(pending));
})();
