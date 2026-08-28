from __future__ import annotations

import unittest
from pydantic import ValidationError

from substar_core.editor.http_api import AiCalibrationRequest, _calibration_model_blocks


class AiCalibrationInstructionTests(unittest.TestCase):
    def test_instruction_is_optional_and_bounded(self) -> None:
        request = AiCalibrationRequest(expected_revision_id="revision-1")
        self.assertEqual(request.instruction, "")
        self.assertEqual(
            AiCalibrationRequest(
                expected_revision_id="revision-1", instruction="Molly Tea 是品牌名"
            ).instruction,
            "Molly Tea 是品牌名",
        )
        with self.assertRaises(ValidationError):
            AiCalibrationRequest(
                expected_revision_id="revision-1", instruction="x" * 4001
            )

    def test_calibration_request_omits_translation_context(self) -> None:
        blocks = {
            "block-1": [{
                "cue_id": "cue-1",
                "editable": True,
                "tokens": [{"token_id": "token-1", "text": "Molly Tea"}],
                "target_text": "茉莉奶白",
                "translation_mapping": {"meaning_unit_id": "meaning-1"},
            }]
        }
        request = _calibration_model_blocks(blocks)
        cue = request["block-1"][0]
        self.assertNotIn("target_text", cue)
        self.assertNotIn("translation_mapping", cue)
        self.assertEqual(cue["tokens"][0]["text"], "Molly Tea")
