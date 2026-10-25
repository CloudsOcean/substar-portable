const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../web/editor.js'), 'utf8');
const extract = (start,end) => source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));

test('mixed edits, checkpoints and navigation keep the same contents after every simulated refresh',()=>{
  for(const seed of [1,7,41,173]) {
    let random=seed,value='',sequence=0;
    const undo=[],redo=[];
    const initial={revision_id:'r0',revision_number:0,is_latest:true,value:''};
    const revisions=new Map([['r0',initial]]);
    const state={revision:initial,revisions:[initial],undoRevisionIds:[],redoRevisionIds:[]};
    const c=vm.createContext({state,revisionMetadata:r=>({...r,is_latest:true}),renderRevisionMenu(){},syncRevisionControls(){}});
    vm.runInContext(extract('  function logicalUndoStack(', '  function seedRevisionMetadata(')+
      extract('  function recordCommittedRevision(', '  async function loadRevisionHistory('),c);
    for(let step=0;step<300;step++) {
      random=(Math.imul(random,1664525)+1013904223)>>>0;
      const action=random%6;
      let provenance;
      if(action<3) {
        undo.push(value);redo.length=0;value=`edit-${seed}-${step}`;
        provenance={operation:'operation_batch'};
      } else if(action===3) {
        provenance={operation:'checkpoint'};
      } else if(action===4 && undo.length) {
        const target=state.undoRevisionIds.at(-1);
        redo.push(value);value=undo.pop();
        assert.equal(revisions.get(target).value,value);
        provenance={operation:'restore_revision',metadata:{
          undo_revision_ids:state.undoRevisionIds.slice(0,-1),
          redo_revision_ids:[...state.redoRevisionIds,state.revision.revision_id]}};
      } else if(action===5 && redo.length) {
        const target=state.redoRevisionIds.at(-1);
        undo.push(value);value=redo.pop();
        assert.equal(revisions.get(target).value,value);
        provenance={operation:'restore_revision',metadata:{
          undo_revision_ids:[...state.undoRevisionIds,state.revision.revision_id],
          redo_revision_ids:state.redoRevisionIds.slice(0,-1)}};
      } else continue;
      const revision={revision_id:`r${++sequence}`,revision_number:sequence,
        parent_revision_id:state.revision.revision_id,provenance,value};
      revisions.set(revision.revision_id,revision);state.revision=revision;
      c.recordCommittedRevision(revision);
      assert.equal(state.undoRevisionIds.length,undo.length);
      assert.equal(state.redoRevisionIds.length,redo.length);
      // Rebuild from persisted provenance, as a new browser session does.
      const persistedUndo=c.logicalUndoStack(revision.revision_id,revisions);
      const persistedRedo=c.logicalRedoStack(revision.revision_id,revisions);
      assert.deepEqual(Array.from(persistedUndo),Array.from(state.undoRevisionIds));
      assert.deepEqual(Array.from(persistedRedo),Array.from(state.redoRevisionIds));
      state.undoRevisionIds=persistedUndo;state.redoRevisionIds=persistedRedo;
    }
  }
});
