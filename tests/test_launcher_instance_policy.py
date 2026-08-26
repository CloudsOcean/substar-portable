from __future__ import annotations

import inspect
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

    def test_second_launch_opens_same_build_instead_of_terminating_it(self) -> None:
        with (
            patch.object(sys, "argv", ["Substar.exe"]),
            patch.object(launcher, "_configure_install_path"),
            patch.object(launcher, "startup_port", return_value=8769),
            patch.object(launcher, "build_id", return_value="build-a"),
            patch.object(launcher, "_set_console_title"),
            patch.object(launcher, "_acquire_mutex", return_value=(99, False)),
            patch.object(
                launcher,
                "_observe_existing_runtime",
                return_value=(self.record, self.identity),
            ),
            patch.object(launcher, "_close_handle") as close_handle,
            patch.object(launcher, "_open_existing", return_value=0) as open_existing,
            patch.object(launcher, "_force_stop_instance") as force_stop,
        ):
            self.assertEqual(launcher.main(), 0)

        close_handle.assert_called_once_with(99)
        open_existing.assert_called_once_with(self.identity, "build-a")
        force_stop.assert_not_called()

    def test_normal_startup_has_no_force_stop_path(self) -> None:
        self.assertNotIn("_force_stop_instance", inspect.getsource(launcher.main))

    def test_unverified_existing_launcher_returns_conflict_without_force(self) -> None:
        with (
            patch.object(sys, "argv", ["Substar.exe"]),
            patch.object(launcher, "_configure_install_path"),
            patch.object(launcher, "startup_port", return_value=8769),
            patch.object(launcher, "build_id", return_value="build-a"),
            patch.object(launcher, "_set_console_title"),
            patch.object(launcher, "_acquire_mutex", return_value=(99, False)),
            patch.object(
                launcher,
                "_observe_existing_runtime",
                return_value=(self.record, None),
            ),
            patch.object(launcher, "_record_is_safely_stale", return_value=False),
            patch.object(launcher, "_close_handle"),
            patch.object(launcher, "_force_stop_instance") as force_stop,
        ):
            self.assertEqual(launcher.main(), 2)

        force_stop.assert_not_called()

    def test_mutex_creation_failure_never_starts_a_backend(self) -> None:
        with (
            patch.object(sys, "argv", ["Substar.exe"]),
            patch.object(launcher, "_configure_install_path"),
            patch.object(launcher, "startup_port", return_value=8769),
            patch.object(launcher, "build_id", return_value="build-a"),
            patch.object(launcher, "_set_console_title"),
            patch.object(launcher, "_acquire_mutex", return_value=(None, False)),
            patch.object(launcher.subprocess, "Popen") as popen,
        ):
            self.assertEqual(launcher.main(), 6)
        popen.assert_not_called()

    def test_startup_initialization_failure_terminates_and_reaps_child(self) -> None:
        process = MagicMock()
        process.pid = 4101
        kernel32 = launcher.ctypes.windll.kernel32
        with (
            patch.object(sys, "argv", ["Substar.exe"]),
            patch.object(launcher, "_configure_install_path"),
            patch.object(launcher, "startup_port", return_value=8769),
            patch.object(launcher, "build_id", return_value="build-a"),
            patch.object(launcher, "_set_console_title"),
            patch.object(launcher, "_status"),
            patch.object(launcher, "_acquire_mutex", return_value=(99, True)),
            patch.object(launcher, "load_runtime_record", return_value=None),
            patch.object(launcher, "_probe_candidate_identity", return_value=None),
            patch.object(launcher, "_port_is_open", return_value=False),
            patch.object(launcher, "backend_command", return_value=["backend"]),
            patch.object(launcher.subprocess, "Popen", return_value=process) as popen,
            patch.object(launcher, "_create_kill_on_close_job", return_value=123),
            patch.object(launcher, "_process_start_time_ns", return_value=101),
            patch.object(
                launcher, "write_runtime_record", side_effect=OSError("disk unavailable")
            ),
            patch.object(launcher, "_terminate_owned_process") as terminate,
            patch.object(launcher, "clear_runtime_record"),
            patch.object(kernel32, "ReleaseMutex"),
            patch.object(kernel32, "CloseHandle"),
        ):
            self.assertEqual(launcher.main(), 6)

        terminate.assert_called_once_with(process)
        popen_kwargs = popen.call_args.kwargs
        self.assertEqual(popen_kwargs["stdin"], launcher.subprocess.DEVNULL)
        self.assertIs(popen_kwargs["stderr"], launcher.subprocess.STDOUT)
        self.assertGreater(int(popen_kwargs["creationflags"]), 0)

    def test_open_existing_reports_build_conflict_without_opening_browser(self) -> None:
        different = {**self.identity, "build_id": "build-b"}
        with patch.object(launcher.webbrowser, "open") as browser_open:
            self.assertEqual(launcher._open_existing(different, "build-a"), 3)
        browser_open.assert_not_called()

    def test_open_existing_rejects_same_build_from_another_installation(self) -> None:
        foreign = {
            **self.identity,
            "install_root": str(self.root / "another-install"),
        }
        with patch.object(launcher.webbrowser, "open") as browser_open:
            self.assertEqual(launcher._open_existing(foreign, "build-a"), 3)
        browser_open.assert_not_called()

    def test_graceful_shutdown_sends_instance_identity_header(self) -> None:
        response = MagicMock()
        response.status = 202
        response.__enter__.return_value = response
        with patch.object(launcher, "urlopen", return_value=response) as open_url:
            self.assertTrue(
                launcher._request_graceful_shutdown(8769, "instance-a", timeout=0.2)
            )
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            dict(request.header_items()).get("X-substar-instance-id"), "instance-a"
        )
        self.assertEqual(open_url.call_args.kwargs["timeout"], 0.2)

    def test_stop_prefers_graceful_shutdown_and_does_not_force_kill(self) -> None:
        with (
            patch.object(launcher, "load_runtime_record", return_value=self.record),
            patch.object(launcher, "probe_identity", return_value=self.identity),
            patch.object(
                launcher,
                "_target_matches_instance",
                return_value=(True, 4101, 4100),
            ),
            patch.object(
                launcher, "_request_graceful_shutdown", return_value=True
            ) as graceful,
            patch.object(launcher, "_wait_for_identity_exit", return_value=True),
            patch.object(
                launcher, "_wait_for_recorded_owners_exit", return_value=True
            ),
            patch.object(launcher, "clear_runtime_record") as clear_record,
            patch.object(launcher, "_force_stop_instance") as force_stop,
        ):
            self.assertEqual(launcher._stop_running_instance(), 0)

        graceful.assert_called_once_with(8769, "instance-a")
        clear_record.assert_called_once_with("instance-a")
        force_stop.assert_not_called()

    def test_stop_does_not_treat_a_closed_socket_as_a_stopped_process(self) -> None:
        with (
            patch.object(launcher, "load_runtime_record", return_value=self.record),
            patch.object(launcher, "probe_identity", side_effect=[self.identity, None]),
            patch.object(
                launcher,
                "_target_matches_instance",
                return_value=(True, 4101, 4100),
            ),
            patch.object(launcher, "_request_graceful_shutdown", return_value=True),
            patch.object(launcher, "_wait_for_identity_exit", return_value=True),
            patch.object(
                launcher, "_wait_for_recorded_owners_exit", return_value=False
            ),
            patch.object(launcher, "_recorded_owners_exited", return_value=False),
            patch.object(launcher, "_force_stop_instance", return_value=True) as force_stop,
        ):
            self.assertEqual(launcher._stop_running_instance(), 0)

        force_stop.assert_called_once_with(self.record, None, self.root)

    def test_stop_can_strictly_force_a_recorded_process_without_identity(self) -> None:
        with (
            patch.object(launcher, "load_runtime_record", return_value=self.record),
            patch.object(launcher, "probe_identity", return_value=None),
            patch.object(launcher, "_record_is_safely_stale", return_value=False),
            patch.object(
                launcher,
                "_target_matches_instance",
                return_value=(True, 4101, 4100),
            ),
            patch.object(launcher, "_force_stop_instance", return_value=True) as force_stop,
        ):
            self.assertEqual(launcher._stop_running_instance(), 0)
        force_stop.assert_called_once_with(self.record, None, self.root)

    def test_stop_falls_back_only_after_initial_identity_validation(self) -> None:
        with (
            patch.object(launcher, "load_runtime_record", return_value=self.record),
            patch.object(launcher, "probe_identity", return_value=self.identity),
            patch.object(
                launcher,
                "_target_matches_instance",
                return_value=(True, 4101, 4100),
            ) as validate,
            patch.object(launcher, "_request_graceful_shutdown", return_value=False),
            patch.object(launcher, "_force_stop_instance", return_value=True) as force_stop,
        ):
            self.assertEqual(launcher._stop_running_instance(), 0)

        validate.assert_called_once_with(self.record, self.identity, self.root)
        force_stop.assert_called_once_with(self.record, self.identity, self.root)

    def test_stop_never_requests_or_kills_an_identity_mismatch(self) -> None:
        with (
            patch.object(launcher, "load_runtime_record", return_value=self.record),
            patch.object(launcher, "probe_identity", return_value=self.identity),
            patch.object(
                launcher,
                "_target_matches_instance",
                return_value=(False, 0, 0),
            ),
            patch.object(launcher, "_request_graceful_shutdown") as graceful,
            patch.object(launcher, "_force_stop_instance") as force_stop,
        ):
            self.assertEqual(launcher._stop_running_instance(), 2)

        graceful.assert_not_called()
        force_stop.assert_not_called()

    def test_force_stop_itself_revalidates_before_termination(self) -> None:
        with (
            patch.object(
                launcher,
                "_target_matches_instance",
                return_value=(False, 0, 0),
            ),
            patch.object(launcher, "_terminate_named_job") as terminate_job,
            patch.object(launcher, "_terminate_process_tree") as terminate_tree,
        ):
            self.assertFalse(
                launcher._force_stop_instance(self.record, self.identity, self.root)
            )

        terminate_job.assert_not_called()
        terminate_tree.assert_not_called()


if __name__ == "__main__":
    unittest.main()
