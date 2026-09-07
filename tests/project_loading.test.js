const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const editor = fs.readFileSync(path.join(__dirname, '../web/editor.js'), 'utf8');
const split = fs.readFileSync(path.join(__dirname, '../web/split.js'), 'utf8');

test('direct editor links load the document before requesting the catalog', async () => {
  const calls = [];
  const source = editor.slice(editor.indexOf('  async function loadProjects()'), editor.indexOf('  async function toggleComplete()'));
  const context = vm.createContext({
    URLSearchParams, window:{location:{search:'?project=sample'}}, state:{},
    ordinaryError: message => { if (message) throw Error(message); },
    loadProject: async id => calls.push(`document:${id}`),
    api: async url => { calls.push(url); return {projects:[]}; },
    renderProjectList: () => {},
  });
  await vm.runInContext(source + '\nloadProjects()', context);
  assert.deepEqual(calls, ['document:sample', '/api/projects']);
});

test('routine polling reuses the catalog and startup is independent of system checks', () => {
  assert.match(split, /setInterval\(\(\) => refreshJobs\(false\), 1300\)/);
  assert.match(split, /Date\.now\(\) - projectCatalogUpdatedAt > 30000/);
  assert.match(split, /async function loadInitialState\(\) \{\s*const initialJobs = refreshJobs\(\)/);
});
