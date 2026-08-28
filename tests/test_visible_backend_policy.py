from __future__ import annotations

import unittest
from pathlib import Path

from substar_core.runtime.launch_surface import (
    HEADLESS,
    NON_WINDOWS_TERMINAL,
    VISIBLE_CONSOLE,
    backend_launch_surface,
    require_visible_backend,
    visible_backend_creation_flags,
)


class VisibleBackendPolicyTests(unittest.TestCase):
    def test_windows_backend_requires_a_console_window(self) -> None:
        self.assertEqual(
            backend_launch_surface(
                platform_name="nt", console_window_provider=lambda: 101
            ),
            VISIBLE_CONSOLE,
        )
        self.assertEqual(
            backend_launch_surface(
                platform_name="nt", console_window_provider=lambda: 0
            ),
            HEADLESS,
        )
        with self.assertRaisesRegex(RuntimeError, "拒绝隐藏启动"):
            require_visible_backend(
                platform_name="nt", console_window_provider=lambda: 0
            )

    def test_non_windows_source_runtime_keeps_terminal_contract(self) -> None:
        self.assertEqual(
            require_visible_backend(platform_name="posix"), NON_WINDOWS_TERMINAL
        )

    def test_launcher_never_requests_a_hidden_backend_window(self) -> None:
        self.assertEqual(visible_backend_creation_flags(platform_name="nt"), 0)

    def test_start_script_closes_when_the_visible_backend_exits(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "启动_Substar.cmd").read_text(
            encoding="utf-8"
        )
        commands = {
            line.strip().casefold()
            for line in script.splitlines()
            if line.strip() and not line.lstrip().startswith("::")
        }
        self.assertNotIn("pause", commands)


if __name__ == "__main__":
    unittest.main()
