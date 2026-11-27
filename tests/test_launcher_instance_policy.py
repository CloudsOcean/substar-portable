from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import launcher


class LauncherInstancePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(launcher.INSTALL_ROOT).resolve()
        self.record = {
            "app": "substar-workbench",
            "build_id": "build-a",
            "instance_id": "instance-a",
            "pid": 4101,
            "launcher_pid": 4100,
            "backend_start_time_ns": 101,
            "launcher_start_time_ns": 100,
            "port": 8769,
            "install_root": str(self.root),
        }
        self.identity = {
            "app": "substar-workbench",
            "build_id": "build-a",
            "instance_id": "instance-a",
            "pid": 4101,
            "port": 8769,
            "install_root": str(self.root),
        }

    def test_stale_record_requires_every_recorded_owner_to_be_gone(self) -> None:
        def gone(pid: int, _started: object, _root: Path) -> launcher.ProcessRecordState:
            return "absent" if pid == 4101 else "different"

        self.assertTrue(
            launcher._record_is_safely_stale(self.record, self.root, gone)
        )

        def backend_alive(
            pid: int, _started: object, _root: Path
        ) -> launcher.ProcessRecordState:
            return "matching" if pid == 4101 else "absent"

        self.assertFalse(
            launcher._record_is_safely_stale(self.record, self.root, backend_alive)
        )

        def unknown(
            _pid: int, _started: object, _root: Path
        ) -> launcher.ProcessRecordState:
            return "unknown"

        self.assertFalse(
            launcher._record_is_safely_stale(self.record, self.root, unknown)
        )

    def test_stale_record_rejects_foreign_or_incomplete_metadata(self) -> None:
        gone = lambda _pid, _started, _root: "absent"
        foreign = {**self.record, "install_root": str(self.root / "other")}
        incomplete = {**self.record, "instance_id": ""}
        wrong_app = {**self.record, "app": "something-else"}
        self.assertFalse(launcher._record_is_safely_stale(foreign, self.root, gone))
        self.assertFalse(launcher._record_is_safely_stale(incomplete, self.root, gone))
        self.assertFalse(launcher._record_is_safely_stale(wrong_app, self.root, gone))

    def test_process_match_requires_a_nonzero_creation_time(self) -> None:
        with (
            patch.object(launcher, "_process_image_path", return_value=str(Path(sys.executable))),
            patch.object(launcher, "_process_start_time_ns", return_value=101),
        ):
            self.assertFalse(launcher._process_matches_record(4101, 0, self.root))
            self.assertFalse(launcher._process_matches_record(4101, None, self.root))
            self.assertTrue(launcher._process_matches_record(4101, 101, self.root))

    def test_second_launch_raises_existing_window_without_starting_another(self) -> None:
        with (
            patch.object(sys, "argv", ["Substar.exe"]),
            patch.object(launcher, "require_visible_backend"),
            patch.object(launcher, "_acquire_mutex", return_value=(99, False)),
            patch.object(launcher, "_close_handle") as close,
            patch.object(launcher, "_foreground_backend_window", return_value=True) as focus,
            patch.object(launcher, "_run_backend") as run,
        ):
            self.assertEqual(launcher.main(), 0)
        focus.assert_called_once_with()
        close.assert_called_once_with(99)
        run.assert_not_called()

    def test_headless_launcher_refuses_before_starting(self) -> None:
        with (
            patch.object(sys, "argv", ["launcher.py"]),
            patch.object(launcher, "require_visible_backend", side_effect=RuntimeError("hidden")),
            patch.object(launcher, "_acquire_mutex") as mutex,
        ):
            self.assertEqual(launcher.main(), 6)
        mutex.assert_not_called()

    def test_port_moves_forward_only_on_occupation_or_a_bind_race(self) -> None:
        with (
            patch.object(sys, "argv", ["Substar.exe"]),
            patch.object(launcher, "require_visible_backend"),
            patch.object(launcher, "remembered_startup_port", return_value=8771),
            patch.object(launcher, "_acquire_mutex", return_value=(99, True)),
            patch.object(launcher, "_release_launcher_mutex"),
            patch.object(launcher, "load_runtime_record", return_value=None),
            patch.object(launcher, "_probe_candidate_identity", return_value=None),
            patch.object(launcher, "_foreground_backend_window", return_value=False),
            patch.object(launcher, "_port_is_open", side_effect=[True, False, False]) as check,
            patch.object(launcher, "_set_console_title"),
            patch.object(launcher, "_run_backend", side_effect=[launcher.PORT_IN_USE_EXIT_CODE, 1]) as run,
        ):
            self.assertEqual(launcher.main(), 1)
        self.assertEqual([call.args[0] for call in check.call_args_list], [8771, 8772, 8773])
        self.assertEqual([call.args[1] for call in run.call_args_list], [8772, 8773])

    def test_recorded_port_is_used_before_default_and_persists(self) -> None:
        from tempfile import TemporaryDirectory
        from substar_core import runtime_instance

        with TemporaryDirectory() as directory:
            path = Path(directory) / "last-port.json"
            with patch.object(runtime_instance, "_REMEMBERED_PORT_FILE", path):
                self.assertEqual(runtime_instance.remembered_startup_port(), 8769)
                runtime_instance.remember_startup_port(8773)
                self.assertEqual(runtime_instance.remembered_startup_port(), 8773)

    def test_graceful_shutdown_sends_the_private_window_token(self) -> None:
        response = MagicMock()
        response.status = 202
        response.__enter__.return_value = response
        with patch.object(launcher, "urlopen", return_value=response) as open_url:
            self.assertTrue(launcher._request_graceful_shutdown(8769, "instance-a", token="private"))
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-substar-instance-id"), "instance-a")
        self.assertEqual(request.get_header("X-substar-shutdown-token"), "private")


if __name__ == "__main__":
    unittest.main()
