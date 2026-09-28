import unittest
from types import SimpleNamespace

from substar_core.domain import (ChangeKind, ChangeProvenance, DisplayCue,
    DisplayToken, EditorDocument, EntityState, SourceToken)
from substar_core.document_operations import apply_document_operation, DocumentOperationError
from substar_core.editor.calibration.exchange import inspect_calibration, SCHEMA


def fixture(deleted=True):
    provenance = ChangeProvenance(kind=ChangeKind.MANUAL, operation='fixture')
    words = ['Mid', 'Mid', 'Autumn', 'Festival']
    sources = tuple(SourceToken(f's{i}', i, text, i, i + 1) for i, text in enumerate(words))
    tokens = tuple(DisplayToken(f'd{i}', text, text, (f's{i}',), provenance,
        EntityState.DELETED if deleted and i == 1 else EntityState.ACTIVE)
        for i, text in enumerate(words))
    return EditorDocument.create(document_key='deleted-gap-test', source_tokens=sources,
        display_tokens=tokens, cues=(DisplayCue('cue', 0, tuple(t.token_id for t in tokens), 0, 4),))


def merge(document, ids, text='Mid-Autumn'):
    return apply_document_operation(document, {'operation_id':'merge-test', 'type':'merge',
        'payload':{'cue_id':'cue', 'token_ids':ids, 'text':text}})


class DeletedGapTests(unittest.TestCase):
    def test_deleted_gap_keeps_tombstone_and_unique_lineage(self):
        original = fixture()
        result = merge(original, ['d0', 'd2'])
        tombstone = next(t for t in result.display_tokens if t.token_id == 'd1')
        self.assertEqual(tombstone, original.display_tokens[1])
        self.assertIn('d1', result.cues[0].display_token_ids)
        self.assertEqual(sorted(s for t in result.display_tokens for s in t.source_token_ids),
                         ['s0', 's1', 's2', 's3'])
        self.assertEqual([t.text for t in result.display_tokens if t.state is EntityState.ACTIVE],
                         ['Mid-Autumn', 'Festival'])
        restored = apply_document_operation(result, {'operation_id':'restore-test', 'type':'restore',
            'payload':{'token_ids':['d1']}})
        self.assertEqual(next(t.state for t in restored.display_tokens if t.token_id == 'd1'), EntityState.ACTIVE)
        self.assertEqual(len(original.display_tokens), 4)

    def test_merged_document_save_reload_and_restore(self):
        import tempfile
        from pathlib import Path
        from substar_core.storage import ProjectStore
        original = fixture()
        merged = merge(original, ['d0', 'd2'])
        with tempfile.TemporaryDirectory() as root:
            store = ProjectStore.create(Path(root) / 'project', project_id='gap')
            first = store.save(original, provenance=original.display_tokens[0].provenance)
            saved = store.save(merged, provenance=merged.changes[-1], expected_revision_id=first.revision_id)
            loaded = store.load_revision(saved.revision_id).document
            self.assertEqual(loaded.content_hash(), merged.content_hash())
            restored = apply_document_operation(loaded, {'operation_id':'restore-after-save', 'type':'restore',
                'payload':{'token_ids':['d1']}})
            restored.validate()
            self.assertEqual(next(t.state for t in restored.display_tokens if t.token_id == 'd1'), EntityState.ACTIVE)
            self.assertEqual(store.load_revision(first.revision_id).document.content_hash(), original.content_hash())

    def test_active_gap_is_rejected(self):
        with self.assertRaises(DocumentOperationError):
            merge(fixture(False), ['d0', 'd2'])

    def test_invalid_selection_is_rejected(self):
        for ids in (['d2', 'd0'], ['d0', 'd0'], ['d0', 'missing'], ['d0']):
            with self.subTest(ids=ids), self.assertRaises(DocumentOperationError):
                merge(fixture(), ids)

    def test_regular_contiguous_merge(self):
        result = merge(fixture(), ['d2', 'd3'], 'Autumn Festival')
        self.assertEqual(len(result.display_tokens), 3)
        result.validate()

    def test_external_import_preview_handles_deleted_gap(self):
        revision = SimpleNamespace(document=fixture(), revision_id='revision-test')
        payload = {'schema_version':SCHEMA, 'project_id':'project-test',
                   'revision_id':revision.revision_id, 'output':'1|Mid-Autumn Festival'}
        document, _, result = inspect_calibration('project-test', revision, payload)
        self.assertEqual(result['merge_count'], 1)
        self.assertEqual(next(t.state for t in document.display_tokens if t.token_id == 'd1'), EntityState.DELETED)
        document.validate()


if __name__ == '__main__':
    unittest.main()
