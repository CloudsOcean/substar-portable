import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind
from substar_core.document_operations import apply_document_operation, DocumentOperationError
from substar_core.editor.application.editing_service import EditingService, StaleOperationError


class TargetSaveSafetyTests(unittest.TestCase):
    def setUp(self):
        source = SourceToken.create(index=0, text="Hello", start=1, end=2)
        token = DisplayToken.create(position=0, text="Hello", source_token_ids=(source.token_id,), provenance=ChangeProvenance(ChangeKind.SOURCE, "test"))
        self.doc = EditorDocument("project", (source,), (token,), (DisplayCue("cue", 0, (token.token_id,), 1, 2),))
        self.doc = apply_document_operation(self.doc, self.operation("", "原文译文"))

    def operation(self, before, after):
        return {"operation_id": "edit-1", "type": "set_target", "payload": {
            "cue_id": "cue", "expected_target_text": before, "target_text": after}}

    def test_stale_text_rejected_even_on_latest_base(self):
        with self.assertRaises(DocumentOperationError):
            apply_document_operation(self.doc, self.operation("旧内容", "我的修改"))
        self.assertEqual(self.doc.cues[0].target.target_text, "原文译文")

    def test_equal_result_is_idempotent_and_clear_is_guarded(self):
        self.assertIs(apply_document_operation(self.doc, self.operation("旧内容", "原文译文")), self.doc)
        cleared = apply_document_operation(self.doc, self.operation("原文译文", ""))
        self.assertIsNone(cleared.cues[0].target)

    def test_old_revision_rebases_only_when_target_unchanged(self):
        latest = SimpleNamespace(document=self.doc, revision_id="new", document_hash="hash")
        repository = Mock()
        repository.find_operation_commit.return_value = None
        repository.load_latest.return_value = latest
        repository.save.return_value = latest
        service = EditingService(lambda _: repository)
        base = {"document_id": "project", "revision_id": "old", "document_hash": "old-hash"}
        service.commit_batch("project", base=base, operations=[self.operation("原文译文", "我的修改")], batch_id="batch")
        self.assertEqual(repository.save.call_count, 1)

        with self.assertRaises(StaleOperationError):
            service.commit_batch("project", base=base, operations=[self.operation("更旧内容", "我的修改")], batch_id="batch2")
        self.assertEqual(repository.save.call_count, 1)

    def test_lost_response_retry_does_not_write_twice(self):
        committed = SimpleNamespace(document=self.doc, revision_id="committed")
        repository = Mock()
        repository.find_operation_commit.return_value = committed
        result = EditingService(lambda _: repository).commit_batch(
            "project", base={}, operations=[self.operation("", "原文译文")], batch_id="retry")
        self.assertIs(result.after, committed)
        repository.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
