import test from 'node:test';
import assert from 'node:assert/strict';
import {refreshGapRows} from '../web/editor_maw_gap_render.js';
test('cross-row gap edits rebuild only affected gap overlays and retain canvases and subtitles',()=>{
  const rows=[0,2000,4000].map(start=>({dataset:{startMs:start,endMs:start+2000},
    removals:0,querySelectorAll(selector){assert.equal(selector,'.waveform-gap-block');return [{remove:()=>this.removals++}];}}));
  const touched=[];const wave={renderedRows:rows,appendGapBlocks:r=>touched.push(r),positionPlayheads(){}};
  refreshGapRows(wave,[{start:500,end:1500}],'boundary_drag');
  touched.length=0;
  refreshGapRows(wave,[{start:700,end:2500}],'boundary_drag');
  assert.deepEqual(touched,[rows[0],rows[1]]);
  assert.equal(rows[2].removals,1);
  touched.length=0;
  refreshGapRows(wave,[{start:700,end:2500}],'boundary_drag');
  assert.equal(touched.length,0);
  refreshGapRows(wave,[],'boundary_drag');
  assert.deepEqual(touched,[rows[0],rows[1]]);
});
