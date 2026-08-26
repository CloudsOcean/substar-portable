from __future__ import annotations

import unittest

from substar_core.creation import subtitle_creation_projection


class CreationProjectionTests(unittest.TestCase):
    def test_missing_asr_credential_is_actionable(self) -> None:
        projection = subtitle_creation_projection(
            transcription={
                "state": "failed",
                "progress": 0.0,
                "error": {
                    "code": "credential_unavailable",
                    "message": "A required credential is unavailable.",
                    "details": {"credential_ref": "asr_qwen"},
                },
            },
            segmentation={"state": "queued", "progress": 0.0},
            editor_ready=False,
            cancel_requested=False,
        )
        self.assertEqual(
            projection["error"],
            "ASR_Qwen 密钥不可用，请在设置中保存密钥后重试。",
        )


if __name__ == "__main__":
    unittest.main()
