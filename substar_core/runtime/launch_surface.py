from __future__ import annotations

import os
from collections.abc import Callable


VISIBLE_CONSOLE = "visible_console"
NON_WINDOWS_TERMINAL = "terminal"
HEADLESS = "headless"


def backend_launch_surface(
    *,
    platform_name: str | None = None,
    console_window_provider: Callable[[], int] | None = None,
) -> str:
    """Return the user-visible surface that owns the backend process."""

    platform = platform_name or os.name
    if platform != "nt":
        return NON_WINDOWS_TERMINAL
    if console_window_provider is None:
        import ctypes

        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        user32 = ctypes.windll.user32
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)

        def console_window_provider():
            window = kernel32.GetConsoleWindow()
            if not window or user32.IsWindowVisible(window):
                return window
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
            user32.GetClassNameW(window, name, len(name))
            return window if name.value == "PseudoConsoleWindow" else 0
    try:
        return VISIBLE_CONSOLE if int(console_window_provider() or 0) else HEADLESS
    except (AttributeError, OSError, TypeError, ValueError):
        return HEADLESS


def require_visible_backend(
    *,
    platform_name: str | None = None,
    console_window_provider: Callable[[], int] | None = None,
) -> str:
    surface = backend_launch_surface(
        platform_name=platform_name,
        console_window_provider=console_window_provider,
    )
    if surface == HEADLESS:
        raise RuntimeError(
            "Substar 后端拒绝隐藏启动。请双击 Substar.exe；"
            "网络与端到端测试必须复用这个正式后端。"
        )
    return surface


def visible_backend_creation_flags(*, platform_name: str | None = None) -> int:
    """The backend inherits the launcher's visible console on Windows."""

    del platform_name
    return 0
