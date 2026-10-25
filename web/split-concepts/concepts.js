(() => {
  'use strict';
  const page = document.querySelector('.concept-page');
  if (!page) return;
  const names = {graphite:'石墨工作台', paper:'纸感创作室', studio:'媒体工作室'};
  const creation = page.querySelector('.creation-card');
  const drop = document.getElementById('videoDropZone');
  const pipeline = page.querySelector('.pipeline-card');
  const recent = page.querySelector('.recent-section');
  const workspace = page.querySelector('.workspace-grid');
  const ingest = document.createElement('section');
  ingest.className = 'concept-ingest';
  ingest.setAttribute('aria-label', '素材预览');
  ingest.innerHTML = '<div class="concept-ingest-heading"><span>素材监看</span><span>MEDIA / INPUT</span></div><div class="concept-viewer"><video controls preload="metadata" hidden aria-label="所选素材预览"></video><div class="concept-viewer-empty"><span>✦</span><strong>你的故事，等待开场</strong><small>选择素材后在这里预览画面或试听音频</small></div></div>';
  const originalDropParent = drop.parentNode;
  const originalDropNext = drop.nextSibling;
  page.append(ingest);
  let previewUrl = '';
  const video = ingest.querySelector('video');
  const empty = ingest.querySelector('.concept-viewer-empty');
  function releasePreview() {
    video.pause(); video.removeAttribute('src'); video.load();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = ''; video.hidden = true; empty.hidden = false;
  }
  function preview(file) {
    releasePreview();
    if (!file) return;
    empty.querySelector('strong').textContent = '你的故事，等待开场';
    empty.querySelector('small').textContent = '选择素材后在这里预览画面或试听音频';
    previewUrl = URL.createObjectURL(file);
    video.src = previewUrl; video.hidden = false; empty.hidden = true;
  }
  document.getElementById('videoInput').addEventListener('change', event => preview(event.target.files[0]));
  drop.addEventListener('drop', event => {
    const file = [...(event.dataTransfer?.files || [])].find(item => /^(video|audio)\//.test(item.type) || /\.(mp4|mov|mkv|webm|mp3|wav|m4a|aac|flac|ogg|avi|m4v)$/i.test(item.name));
    if (file) preview(file);
  });
  video.addEventListener('error', () => {
    video.hidden = true; empty.hidden = false;
    empty.querySelector('strong').textContent = '此格式暂不支持浏览器预览';
    empty.querySelector('small').textContent = '素材仍可正常提交字幕任务';
  });
  function apply(direction) {
    if (!names[direction]) return;
    page.dataset.concept = direction;
    if (direction === 'studio') ingest.append(drop);
    else originalDropParent.insertBefore(drop, originalDropNext);
    page.querySelectorAll('[data-concept-choice]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.conceptChoice === direction)));
    page.querySelector('.concept-eyebrow').textContent = `SUBSTAR / ${({graphite:'A',paper:'B',studio:'C'})[direction]} — ${names[direction]}`;
    const copy = {
      graphite:['专注每一次创作','导入素材，配置语言，让字幕制作有条不紊。'],
      paper:['让好内容，清晰表达。','从一段音视频开始，完成听写、切分与翻译。'],
      studio:['下一条精彩，从这里开始。','素材、参数与处理进度，在同一个工作空间就位。']
    }[direction];
    page.querySelector('h1').textContent = copy[0];
    page.querySelector('.concept-intro p').textContent = copy[1];
    document.title = `${names[direction]} · 切分 · Substar`;
    history.replaceState(null, '', `/assets/split-concepts/${direction}.html`);
  }
  page.querySelectorAll('[data-concept-choice]').forEach(button => button.addEventListener('click', () => apply(button.dataset.conceptChoice)));
  // Real elements move; their event handlers, files and form state remain intact.
  // Keep the existing workspace container for contracts and tutorial selectors.
  workspace.classList.add('concept-workspace');
  creation.querySelector('#createTitle').textContent = '新建字幕项目';
  pipeline.querySelector('#pipelineTitle').textContent = '处理队列';
  recent.querySelector('h2').textContent = '项目资料库';
  const search = document.createElement('input');
  search.className = 'concept-search'; search.type = 'search';
  search.placeholder = '搜索项目…'; search.setAttribute('aria-label', '搜索项目资料库');
  recent.querySelector('.recent-head > div:last-child').prepend(search);
  const emptySearch = document.createElement('p');
  emptySearch.className = 'concept-search-empty'; emptySearch.textContent = '没有匹配的项目'; emptySearch.hidden = true;
  recent.append(emptySearch);
  function filter() {
    const query = search.value.trim().toLowerCase();
    const items = [...document.querySelectorAll('#recentJobs .recent-item')];
    items.forEach(item => { item.hidden = !item.textContent.toLowerCase().includes(query); });
    emptySearch.hidden = !query || items.some(item => !item.hidden);
  }
  search.addEventListener('input', filter);
  new MutationObserver(filter).observe(document.getElementById('recentJobs'), {childList:true, subtree:true, characterData:true});
  window.addEventListener('pagehide', releasePreview);
  apply(page.dataset.concept);
})();
