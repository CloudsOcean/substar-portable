const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/split.js', 'utf8');
function section(name, next) { return source.slice(source.indexOf(`  function ${name}(`), source.indexOf(`  function ${next}(`)); }
test('subtitle workflow and format survive settings refresh and stored reload', () => {
  const nodes = new Map();
  const $ = key => {if (!nodes.has(key)) nodes.set(key,{value:'',textContent:''}); return nodes.get(key);};
  const state = {taskConfigLoaded:false};
  let saved = {creation_workflow:'subtitle', subtitle_format:'bilingual-inline', subtitle_first_track:'target', subtitle_separator:' / '};
  const context = vm.createContext({$, state, storedTaskConfig:()=>saved, formatTemporaryHotwords:()=>'',
    parseTemporaryHotwords:()=>[], glossaryInjection:null, MAIN_SPLIT_BRANCH:'main',
    syncLanguageRatioThreshold(){},syncQwenEnhancementModel(){},isReferenceBreakPreset:()=>false,
    referenceBreakPreset:()=>'.?!',setSettingsSaveState(){},syncWorkflowControl(){},validateForm(){},syncPrimaryPanel(){}});
  vm.runInContext(section('taskConfigFromControls','storedTaskConfig') + section('applySharedSettings','formatBytes'),context);
  for (const edition of ['full','slim']) {
    state.edition=edition;
    vm.runInContext('applySharedSettings({segmentation_enabled:false})',context);
    assert.equal($('#splitWorkflowInput').value,'subtitle');
    assert.equal($('#subtitleFormat').value,'bilingual-inline');
    assert.equal($('#subtitleSeparator').value,' / ');
  }
  saved=vm.runInContext('taskConfigFromControls()',context);
  state.taskConfigLoaded=false;
  $('#splitWorkflowInput').value='disabled';
  vm.runInContext('applySharedSettings({segmentation_enabled:false})',context);
  assert.equal($('#splitWorkflowInput').value,'subtitle');
});
test('all workflow refreshes synchronize import controls through one renderer', () => {
  assert.match(section('syncWorkflowControl','selectedRecognitionProfile'), /syncSubtitleImportControls\(\)/);
  assert.match(source, /subtitleSeparatorField.*classList.toggle\("hidden", format !== "bilingual-inline"\)/);
});
