from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .artifacts import atomic_write_json
from .config import APP_DATA_DIR, INSTALL_ROOT, PROJECT_ROOT, load_settings


_INSTALL_ID = hashlib.sha256(
    str(INSTALL_ROOT).casefold().encode("utf-8")
).hexdigest()[:16]
RUNTIME_FILE = APP_DATA_DIR / "runtime" / f"{_INSTALL_ID}.json"
APP_MARKER = "substar-workbench"
BACKEND_MUTEX_NAME = "Local\\Substar.Workbench.Backend"
ERROR_ALREADY_EXISTS = 183
TASK_SCHEDULER_SHUTDOWN_TIMEOUT = 40.0
INSTANCE_SHUTDOWN_TIMEOUT = 55.0
# A newly copied transparent runtime can spend tens of seconds in antivirus
# scanning before Python reaches the ASGI lifespan. The launcher retains
# ownership throughout this bounded wait, so a longer deadline is safe.
INSTANCE_STARTUP_TIMEOUT = 90.0


def acquire_backend_mutex() -> int | None:
    """Claim the backend singleton independently of the launcher process."""

    if os.name != "nt":
        # Windows is the supported desktop target. Keep source-mode tests and
        # development on other platforms usable without a platform lock.
        return 0
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    handle = create_mutex(None, True, BACKEND_MUTEX_NAME)
    if not handle:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def release_backend_mutex(handle: int | None) -> None:
    if not handle or os.name != "nt":
        return
    import ctypes

    ctypes.windll.kernel32.CloseHandle(handle)


def startup_port() -> int:
    raw = os.environ.get("SUBSTAR_PORT", "").strip()
    if raw:
        try:
            return max(1024, min(65535, int(raw)))
        except ValueError:
            pass
    return int(load_settings().get("startup_port", 8769))


def build_id(root: Path = PROJECT_ROOT) -> str:
    explicit = os.environ.get("SUBSTAR_BUILD_ID", "").strip()
    if explicit:
        return explicit
    manifest = root / "portable_manifest.json"
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            commit = str(value.get("source_commit") or "").strip()
            version = str(value.get("version") or "").strip()
            if commit and commit != "build-time":
                return f"{version}+{commit[:12]}" if version else commit
        except (OSError, json.JSONDecodeError):
            pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        if result.stdout.strip():
            commit = result.stdout.strip()
            return f"{version}-dev+{commit[:12]}" if version else commit
    except (OSError, subprocess.SubprocessError):
        pass
    return version or "development"


def load_runtime_record() -> dict[str, Any] | None:
    try:
        value = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_runtime_record(value: dict[str, Any]) -> None:
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(RUNTIME_FILE, value)


def process_start_time_ns(pid: int) -> int:
    """Return a Windows process creation FILETIME, or zero when unavailable."""

    if os.name != "nt" or pid <= 0:
        return 0
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        get_times = kernel32.GetProcessTimes
        get_times.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        get_times.restype = wintypes.BOOL
        if not get_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return 0
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def clear_runtime_record(instance_id: str | None = None) -> None:
    if instance_id:
        current = load_runtime_record()
        if current and current.get("instance_id") != instance_id:
            return
    try:
        RUNTIME_FILE.unlink()
    except (FileNotFoundError, PermissionError):
        # The launcher and an explicit stop command can observe shutdown at
        # the same time. Either side may already be replacing/removing the
        # record; the live identity probe remains the source of truth.
        pass


def probe_identity(port: int, timeout: float = 0.7) -> dict[str, Any] | None:
    try:
        with urlopen(
            f"http://127.0.0.1:{int(port)}/api/runtime/identity", timeout=timeout
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPError, URLError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("app") != APP_MARKER:
        return None
    return value
