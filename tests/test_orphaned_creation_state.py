from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import _has_durable_job_identity


class OrphanedCreationStateTests(unittest.TestCase):
    def test_creation_state_alone_is_not_a_restorable_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            (job_dir / "creation_state.json").write_text("{}", encoding="utf-8")
            self.assertFalse(_has_durable_job_identity(job_dir))

    def test_real_project_and_packaged_tutorial_have_durable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            (job_dir / "project_creation.json").write_text("{}", encoding="utf-8")
            self.assertTrue(_has_durable_job_identity(job_dir))
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            (job_dir / "tutorial_project.json").write_text("{}", encoding="utf-8")
            self.assertTrue(_has_durable_job_identity(job_dir))


if __name__ == "__main__":
    unittest.main()
