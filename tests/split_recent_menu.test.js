const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('web/split.js','utf8');
test('catalog-only subtitle projects are included and basis labels distinguish tracks', () => {
  const route=code.slice(code.indexOf('  function routeLabel('),code.indexOf('  function jobLabel('));
  const context=vm.createContext({});
  vm.runInContext(route,context);
  assert.equal(vm.runInContext('routeLabel({subtitle_basis:"sentence",workflow_mode:"subtitle_creation"})',context),'基于句级字幕');
  assert.equal(vm.runInContext('routeLabel({workflow_mode:"subtitle_creation"})',context),'基于词级字幕');
  assert.doesNotMatch(code,/filter\(project => project.tutorial_case_id && !knownProjectIds/);
  assert.match(code,/filter\(project => !knownProjectIds.has\(project.project_id\)\)/);
});
test('recent poll preserves identical cards and defers changed open menus', () => {
  const job = {id:'one',status:'done',created_at:1};
  const signature = JSON.stringify([['one','title','route','done',1,null,null,null,false,null]]);
  let open=false;
  const container = {exportPositioningReady:true,recentRenderSignature:signature,
    querySelector:()=>open?{}:null,contains:()=>false,matches:()=>false,
    replaceChildren(){throw Error('must not replace interacting or unchanged cards');}};
  const context = vm.createContext({$:()=>container,document:{activeElement:null},
    COMPLETE_STATUSES:new Set(['done']),jobLabel:()=> 'title',routeLabel:()=> 'route',
    humanStatus:j=>j.status,hasTranslation:()=>false});
  vm.runInContext(code.slice(code.indexOf('  function renderRecent('),code.indexOf('  let projectCatalog =')),context);
  context.jobs=[job];
  vm.runInContext('renderRecent(jobs)',context);
  open=true;
  context.jobs=[{...job,created_at:2}];
  vm.runInContext('renderRecent(jobs)',context);
  assert.equal(container.recentRenderSignature,signature);
});
test('export activation does not race hover and scroll repositions instead of closing', () => {
  const menu=code.slice(code.indexOf('  function createExportMenu('),code.indexOf('  function removeRecentProjectCard('));
  assert.doesNotMatch(menu,/addEventListener\("mouse(?:enter|leave)"/);
  assert.match(code,/container.addEventListener\("scroll", reposition\)/);
  assert.doesNotMatch(code,/container.onscroll\s*=/);
  assert.match(code,/event.key !== "Escape"/);
});
test('recent exports use the shared native save picker and have no arrow', () => {
  assert.match(code, /saver.saveUrl\(saver.subtitleSpec/);
  const html=fs.readFileSync('web/split.html','utf8');
  assert.ok(html.indexOf('/assets/system_save_as.js') < html.indexOf('/assets/split.js'));
  assert.match(fs.readFileSync('web/split.css','utf8'), /\.export-menu > summary::after \{ content: none; display: none; \}/);
});
