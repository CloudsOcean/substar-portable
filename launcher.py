from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from substar_core.config import INSTALL_ROOT, PROJECT_ROOT, DATA_ROOT
from substar_core.process_command import backend_command
from substar_core.runtime.launch_surface import visible_backend_creation_flags
from substar_core.runtime_instance import (
    INSTANCE_SHUTDOWN_TIMEOUT,
    INSTANCE_STARTUP_TIMEOUT,
    build_id,
    clear_runtime_record,
    load_runtime_record,
    probe_identity,
    startup_port,
    write_runtime_record,
)


MUTEX_NAME = "Local\\Substar.Workbench.Singleton"
ERROR_ALREADY_EXISTS = 183
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
JOB_OBJECT_TERMINATE = 0x0008
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
ERROR_INVALID_PARAMETER = 87
STILL_ACTIVE = 259
GRACEFUL_PROCESS_EXIT_TIMEOUT = INSTANCE_SHUTDOWN_TIMEOUT

ProcessRecordState = Literal["matching", "absent", "different", "unknown"]


def _configure_install_path() -> None:
    """Expose portable command-line tools to every frozen child process."""
    candidates = (
        Path(INSTALL_ROOT) / "ffmpeg" / "bin",
        Path(INSTALL_ROOT) / "runtime" / "ffmpeg" / "bin",
    )
    ffmpeg_bin = next((path for path in candidates if path.is_dir()), None)
    if ffmpeg_bin is None:
        return
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    if str(ffmpeg_bin).casefold() not in {entry.casefold() for entry in entries}:
        os.environ["PATH"] = str(ffmpeg_bin) + os.pathsep + current


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _set_console_title(port: int) -> None:
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(
            f"Substar 后端 · http://127.0.0.1:{port} · 关闭窗口即停止"
        )


def _status(message: str, *, final: bool = False) -> None:
    suffix = "\n" if final else ""
    print(f"\r{message:<100}", end=suffix, flush=True)


def _acquire_mutex() -> tuple[int | None, bool]:
    if os.name != "nt":
        return None, True
    kernel32 = ctypes.windll.kernel32
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    handle = create_mutex(None, True, MUTEX_NAME)
    if not handle:
        return None, False
    return handle, kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _open_existing(identity: dict, expected_build: str) -> int:
    try:
        port = int(identity.get("port", 0))
        same_install = (
            Path(str(identity.get("install_root", ""))).resolve()
            == Path(INSTALL_ROOT).resolve()
        )
    except (OSError, TypeError, ValueError):
        port = 0
        same_install = False
    existing_build = str(identity.get("build_id", ""))
    if (
        identity.get("app") != "substar-workbench"
        or not str(identity.get("instance_id", "")).strip()
        or not 1 <= port <= 65535
        or not same_install
        or existing_build != expected_build
    ):
        print("[冲突] 已有另一套 Substar 正在运行，未自动关闭。")
        print(f"  运行目录：{identity.get('install_root', '未知')}")
        print(f"  运行版本：{existing_build or '未知'}")
        print(f"  当前版本：{expected_build}")
        print("请先关闭原 Substar 的 CMD 窗口，再启动当前版本。")
        return 3
    url = f"http://127.0.0.1:{port}"
    print(f"Substar 已在运行：{url}")
    webbrowser.open(url)
    return 0


def _process_image_path(pid: int) -> str:
    if os.name != "nt" or pid <= 0:
        return ""
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        query = kernel32.QueryFullProcessImageNameW
        query.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        query.restype = wintypes.BOOL
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not query(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _process_start_time_ns(pid: int) -> int:
    """Return the Windows process creation FILETIME in 100-ns units."""

    if os.name != "nt" or pid <= 0:
        return 0
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


def _process_matches_record(pid: int, expected_start_time: object, root: Path) -> bool:
    if pid <= 0:
        return False
    image_path = _process_image_path(pid)
    if not image_path:
        return False
    try:
        if Path(image_path).resolve() != Path(sys.executable).resolve():
            return False
    except OSError:
        return False
    try:
        expected = int(expected_start_time)
    except (TypeError, ValueError):
        return False
    if expected <= 0 or _process_start_time_ns(pid) != expected:
        return False
    return Path(image_path).resolve().parent == Path(sys.executable).resolve().parent


def _process_exists(pid: int) -> bool | None:
    """Return True/False only when process liveness can be determined safely."""

    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return None
        return True
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False if kernel32.GetLastError() == ERROR_INVALID_PARAMETER else None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return int(exit_code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _recorded_process_state(
    pid: int,
    expected_start_time: object,
    root: Path,
) -> ProcessRecordState:
    exists = _process_exists(pid)
    if exists is False:
        return "absent"
    if exists is None:
        return "unknown"
    image_path = _process_image_path(pid)
    if not image_path:
        return "unknown"
    try:
        image = Path(image_path).resolve()
        executable = Path(sys.executable).resolve()
    except OSError:
        return "unknown"
    if image != executable or image.parent != executable.parent:
        return "different"
    try:
        expected = int(expected_start_time)
    except (TypeError, ValueError):
        return "unknown"
    if expected <= 0:
        return "unknown"
    actual_start_time = _process_start_time_ns(pid)
    if not actual_start_time:
        return "unknown"
    if actual_start_time != expected:
        return "different"
    return "matching"


def _record_is_safely_stale(
    record: dict,
    root: Path,
    process_state: Callable[[int, object, Path], ProcessRecordState] | None = None,
) -> bool:
    """Accept a stale record only when every recorded owner is definitely gone."""

    if not isinstance(record, dict) or record.get("app") != "substar-workbench":
        return False
    if not str(record.get("instance_id", "")).strip():
        return False
    try:
        if Path(str(record.get("install_root", ""))).resolve() != root:
            return False
        backend_pid = int(record.get("pid", 0))
        launcher_pid = int(record.get("launcher_pid", 0) or 0)
        port = int(record.get("port", 0))
    except (OSError, TypeError, ValueError):
        return False
    if backend_pid <= 0 or not 1 <= port <= 65535:
        return False
    checker = process_state or _recorded_process_state
    terminal_states = {"absent", "different"}
    backend_state = checker(backend_pid, record.get("backend_start_time_ns"), root)
    if backend_state not in terminal_states:
        return False
    if launcher_pid > 0:
        launcher_state = checker(
            launcher_pid, record.get("launcher_start_time_ns"), root
        )
        if launcher_state not in terminal_states:
            return False
    return True


def _terminate_process_tree(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _terminate_named_job(job_name: str) -> bool:
    if os.name != "nt" or not job_name:
        return False
    kernel32 = ctypes.windll.kernel32
    open_job = kernel32.OpenJobObjectW
    open_job.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_job.restype = wintypes.HANDLE
    handle = open_job(JOB_OBJECT_TERMINATE, False, job_name)
    if not handle:
        return False
    try:
        terminate_job = kernel32.TerminateJobObject
        terminate_job.argtypes = (wintypes.HANDLE, wintypes.UINT)
        terminate_job.restype = wintypes.BOOL
        return bool(terminate_job(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _wait_for_mutex(handle: int | None, timeout_seconds: float = 20.0) -> bool:
    if os.name != "nt":
        return True
    if not handle:
        return False
    result = ctypes.windll.kernel32.WaitForSingleObject(
        handle, max(1, int(timeout_seconds * 1000))
    )
    return result in {WAIT_OBJECT_0, WAIT_ABANDONED}


def _close_handle(handle: int | None) -> None:
    if handle and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(handle)


def _target_matches_instance(
    record: dict,
    identity: dict | None,
    root: Path,
) -> tuple[bool, int, int]:
    """Validate the old instance before any forceful termination."""

    target_root = str((identity or record).get("install_root", "")).strip()
    try:
        if not target_root or Path(target_root).resolve() != root:
            return False, 0, 0
    except OSError:
        return False, 0, 0
    record_instance = str(record.get("instance_id", "")).strip()
    if identity and record_instance and str(identity.get("instance_id", "")) != record_instance:
        return False, 0, 0
    try:
        backend_pid = int((identity or record).get("pid", 0))
        recorded_pid = int(record.get("pid", backend_pid))
    except (TypeError, ValueError):
        return False, 0, 0
    if backend_pid <= 0 or (recorded_pid and backend_pid != recorded_pid):
        return False, 0, 0
    if identity and str(identity.get("app", "")) != "substar-workbench":
        return False, 0, 0
    if not _process_matches_record(
        backend_pid, record.get("backend_start_time_ns"), root
    ):
        return False, 0, 0
    try:
        launcher_pid = int(record.get("launcher_pid", 0) or 0)
    except (TypeError, ValueError):
        launcher_pid = 0
    if launcher_pid:
        launcher_state = _recorded_process_state(
            launcher_pid, record.get("launcher_start_time_ns"), root
        )
        if launcher_state == "matching":
            pass
        elif launcher_state in {"absent", "different"}:
            launcher_pid = 0
        else:
            return False, 0, 0
    return True, backend_pid, launcher_pid


def _force_stop_instance(record: dict, identity: dict | None, root: Path) -> bool:
    valid, backend_pid, launcher_pid = _target_matches_instance(record, identity, root)
    if not valid:
        print("无法安全确认旧实例属于当前安装目录，未执行强制结束。")
        return False
    instance_id = str(record.get("instance_id", ""))
    expected_job_name = f"Local\\Substar.Workbench.Job.{instance_id}"
    recorded_job_name = str(record.get("job_name", ""))
    job_terminated = recorded_job_name == expected_job_name and _terminate_named_job(
        recorded_job_name
    )
    if not job_terminated:
        _terminate_process_tree(backend_pid)
    if launcher_pid and launcher_pid != os.getpid():
        _terminate_process_tree(launcher_pid)
    port = int((identity or record).get("port", 0) or 0)
    for _ in range(100):
        if port and probe_identity(port, timeout=0.1):
            time.sleep(0.1)
            continue
        if launcher_pid and _process_image_path(launcher_pid):
            time.sleep(0.1)
            continue
        if backend_pid and _process_image_path(backend_pid):
            time.sleep(0.1)
            continue
        clear_runtime_record(str(record.get("instance_id", "")) or None)
        return True
    print("旧实例未能在规定时间内完全退出，未启动新实例。")
    return False


def _request_graceful_shutdown(port: int, instance_id: str, timeout: float = 1.5) -> bool:
    if not port or not instance_id:
        return False
    request = Request(
        f"http://127.0.0.1:{int(port)}/api/runtime/shutdown",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Substar-Instance-ID": instance_id,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 0)) < 300
    except (OSError, HTTPError, URLError, TimeoutError):
        return False


def _wait_for_identity_exit(
    port: int,
    instance_id: str,
    timeout: float = GRACEFUL_PROCESS_EXIT_TIMEOUT,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        identity = probe_identity(port, timeout=0.25)
        if not identity:
            return True
        if str(identity.get("instance_id", "")) != instance_id:
            return False
        time.sleep(0.1)
    return False


def _recorded_owners_exited(record: dict, root: Path) -> bool:
    try:
        backend_pid = int(record.get("pid", 0) or 0)
        launcher_pid = int(record.get("launcher_pid", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    owners = (
        (backend_pid, record.get("backend_start_time_ns")),
        (launcher_pid, record.get("launcher_start_time_ns")),
    )
    for pid, started_at in owners:
        if pid <= 0:
            continue
        if _recorded_process_state(pid, started_at, root) not in {
            "absent",
            "different",
        }:
            return False
    return True


def _wait_for_recorded_owners_exit(
    record: dict, root: Path, timeout: float = GRACEFUL_PROCESS_EXIT_TIMEOUT
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _recorded_owners_exited(record, root):
            return True
        time.sleep(0.1)
    return _recorded_owners_exited(record, root)


def _stop_running_instance() -> int:
    root = Path(INSTALL_ROOT).resolve()
    record = load_runtime_record() or {}
    if not record:
        identity = probe_identity(startup_port())
        if identity:
            print("停止失败：发现健康实例但缺少可验证的运行记录，未发送关闭请求。")
            return 2
        print("Substar 当前没有正在运行的实例。")
        return 0
    try:
        port = int(record.get("port", 0))
    except (TypeError, ValueError):
        port = 0
    identity = probe_identity(port) if port else None
    if not identity:
        if _record_is_safely_stale(record, root):
            clear_runtime_record(str(record.get("instance_id", "")) or None)
            print("Substar 当前没有正在运行的实例；已清理过期运行记录。")
            return 0
        valid, _, _ = _target_matches_instance(record, None, root)
        if valid:
            if _force_stop_instance(record, None, root):
                print("Substar 已停止（身份端点不可用，已严格验证进程后终止）。")
                return 0
            return 3
        print("停止失败：后端身份不可用，且运行记录不能安全确认为 stale；未结束任何进程。")
        return 2
    valid, _, _ = _target_matches_instance(record, identity, root)
    if not valid:
        print("停止失败：运行记录与健康后端身份不一致，未结束任何进程。")
        return 2
    instance_id = str(record.get("instance_id", ""))
    graceful_requested = _request_graceful_shutdown(port, instance_id)
    if (
        graceful_requested
        and _wait_for_identity_exit(port, instance_id)
        and _wait_for_recorded_owners_exit(record, root)
    ):
        clear_runtime_record(instance_id)
        print("Substar 已正常停止。")
        return 0
    latest_identity = probe_identity(port, timeout=0.4)
    if not latest_identity and _recorded_owners_exited(record, root):
        clear_runtime_record(instance_id)
        print("Substar 已停止。")
        return 0
    if not _force_stop_instance(record, latest_identity, root):
        print("停止失败：优雅关闭未完成，且实例身份无法再次严格验证；未强制结束进程。")
        return 3
    print("Substar 已停止（优雅关闭不可用，已使用受验证的强制终止）。")
    return 0


def _create_kill_on_close_job(process: subprocess.Popen, job_name: str) -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    create_job.restype = wintypes.HANDLE
    handle = create_job(None, job_name)
    if not handle:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    set_job_info = kernel32.SetInformationJobObject
    set_job_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    set_job_info.restype = wintypes.BOOL
    ok = set_job_info(
        handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    assign_process.restype = wintypes.BOOL
    if not ok or not assign_process(handle, process._handle):
        kernel32.CloseHandle(handle)
        return None
    return handle


def _runtime_record_port(record: dict, fallback: int) -> int:
    try:
        value = int(record.get("port", fallback) or fallback)
    except (AttributeError, TypeError, ValueError):
        return fallback
    return value if 1 <= value <= 65535 else fallback


def _probe_candidate_identity(record: dict, fallback_port: int) -> dict | None:
    ports = [_runtime_record_port(record, fallback_port)]
    if fallback_port not in ports:
        ports.append(fallback_port)
    for candidate in ports:
        identity = probe_identity(candidate)
        if identity:
            return identity
    return None


def _release_launcher_mutex(handle: int | None, owns_mutex: bool) -> None:
    if handle and owns_mutex and os.name == "nt":
        ctypes.windll.kernel32.ReleaseMutex(handle)
    _close_handle(handle)


def _terminate_owned_process(
    process: subprocess.Popen | None, timeout: float = 3.0
) -> None:
    """Boundedly reap a child created by this launcher during failed startup."""

    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=max(0.1, timeout))
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=max(0.1, timeout))
    except (OSError, subprocess.TimeoutExpired):
        return


def _observe_existing_runtime(
    fallback_port: int,
    root: Path,
    timeout: float = 10.0,
) -> tuple[dict, dict | None]:
    """Wait through a normal startup, but return immediately for proven stale state."""

    deadline = time.monotonic() + max(0.0, timeout)
    record: dict = {}
    while True:
        record = load_runtime_record() or {}
        identity = _probe_candidate_identity(record, fallback_port)
        if identity or (record and _record_is_safely_stale(record, root)):
            return record, identity
        if time.monotonic() >= deadline:
            return record, None
        time.sleep(0.25)


def main() -> int:
    _configure_install_path()
    if len(sys.argv) > 1 and sys.argv[1] in {"--help", "-h"}:
        print("launcher.py [--stop | --smoke-import]")
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke-import":
        import app as packaged_app

        if not packaged_app.WEB_DIR.is_dir() or not packaged_app.PROMPTS_DIR.is_dir():
            print("Substar packaged resources are incomplete.", file=sys.stderr, flush=True)
            # Importing the complete packaged graph can initialize third-party
            # non-daemon helpers.  This command is a release probe, not a server;
            # terminate deterministically after its result is known.
            os._exit(1)
        print("Substar packaged import OK", flush=True)
        os._exit(0)
    if len(sys.argv) > 1 and sys.argv[1].lower() in {"--stop", "stop"}:
        return _stop_running_instance()
    root = Path(INSTALL_ROOT).resolve()
    port = startup_port()
    expected_build = build_id(PROJECT_ROOT)
    _set_console_title(port)

    mutex, owns_mutex = _acquire_mutex()
    if os.name == "nt" and not mutex:
        print("[启动失败] 无法创建启动器单实例锁，未启动后端。")
        return 6
    if not owns_mutex:
        existing_record, existing_identity = _observe_existing_runtime(port, root)
        if existing_identity:
            _close_handle(mutex)
            return _open_existing(existing_identity, expected_build)
        if not existing_record or not _record_is_safely_stale(existing_record, root):
            _close_handle(mutex)
            print("[冲突] 已有启动器持有单实例锁，但无法安全确认其运行记录已 stale；未接管或结束任何进程。")
            return 2
        clear_runtime_record(str(existing_record.get("instance_id", "")) or None)
        if not _wait_for_mutex(mutex, timeout_seconds=5.0):
            _close_handle(mutex)
            print("[冲突] stale 记录已清理，但原启动器未释放单实例锁；未启动新实例。")
            return 5
        owns_mutex = True
    record = load_runtime_record() or {}
    identity = _probe_candidate_identity(record, port)
    if identity:
        _release_launcher_mutex(mutex, owns_mutex)
        return _open_existing(identity, expected_build)
    if record:
        if not _record_is_safely_stale(record, root):
            _release_launcher_mutex(mutex, owns_mutex)
            print("[冲突] 运行记录没有健康身份，但其进程所有权不能安全确认为 stale；未启动新实例。")
            return 3
        clear_runtime_record(str(record.get("instance_id", "")) or None)
    if _port_is_open(port):
        _release_launcher_mutex(mutex, owns_mutex)
        print(f"[端口冲突] 127.0.0.1:{port} 已被其他程序占用。")
        print("请在设置 → Debug 中修改启动端口，或关闭占用该端口的程序。")
        return 4

    instance_id = uuid.uuid4().hex
    env = os.environ.copy()
    env["SUBSTAR_PORT"] = str(port)
    env["SUBSTAR_OPEN_BROWSER"] = "0"
    env["SUBSTAR_INSTANCE_ID"] = instance_id
    env["SUBSTAR_LAUNCH_SURFACE"] = "visible_console"
    command = backend_command()
    _status(f"Substar 正在启动 · http://127.0.0.1:{port}")
    process: subprocess.Popen | None = None
    job_handle: int | None = None
    backend_log = None
    try:
        log_directory = DATA_ROOT / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        backend_log = (log_directory / "backend.log").open(
            "a", encoding="utf-8", errors="replace"
        )
        backend_log.write(
            f"\n--- {time.strftime('%Y-%m-%dT%H:%M:%S%z')} instance={instance_id} ---\n"
        )
        backend_log.flush()
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=backend_log,
            stderr=subprocess.STDOUT,
            # The backend inherits this launcher's visible taskbar console.
            # The application lifespan independently rejects headless startup,
            # so alternate ASGI entry points cannot bypass this ownership rule.
            creationflags=visible_backend_creation_flags(),
        )
        job_name = f"Local\\Substar.Workbench.Job.{instance_id}"
        job_handle = _create_kill_on_close_job(process, job_name)
        if os.name == "nt" and not job_handle:
            raise RuntimeError("failed to create the launcher process Job Object")
        launcher_started = _process_start_time_ns(os.getpid())
        backend_started = _process_start_time_ns(process.pid)
        if os.name == "nt" and (launcher_started <= 0 or backend_started <= 0):
            raise RuntimeError("failed to capture process creation times")
        write_runtime_record({
            "app": "substar-workbench",
            "build_id": expected_build,
            "instance_id": instance_id,
            "pid": process.pid,
            "launcher_pid": os.getpid(),
            "launcher_start_time_ns": launcher_started,
            "backend_start_time_ns": backend_started,
            "job_name": job_name if job_handle else "",
            "port": port,
            "install_root": str(root),
            "data_root": str(DATA_ROOT),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "launch_surface": "visible_console" if os.name == "nt" else "terminal",
        })
        startup_deadline = time.monotonic() + INSTANCE_STARTUP_TIMEOUT
        while time.monotonic() < startup_deadline:
            if process.poll() is not None:
                print(f"后端启动失败，退出码：{process.returncode}")
                return int(process.returncode or 1)
            ready = probe_identity(port, timeout=0.3)
            if ready:
                if os.environ.get("SUBSTAR_LAUNCHER_OPEN_BROWSER", "1") != "0":
                    webbrowser.open(f"http://127.0.0.1:{port}")
                _status(
                    f"Substar 运行中 · http://127.0.0.1:{port} · 关闭本窗口或双击‘停止_Substar.cmd’即可停止"
                )
                break
            time.sleep(0.25)
        else:
            print(
                f"后端 {int(INSTANCE_STARTUP_TIMEOUT)} 秒内未就绪，请检查上方日志。"
            )
            return 5
        return process.wait()
    except KeyboardInterrupt:
        print("\n正在停止 Substar…")
        _request_graceful_shutdown(port, instance_id)
        if process is None:
            return 130
        try:
            return process.wait(timeout=GRACEFUL_PROCESS_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                return process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            return process.wait()
    except (OSError, RuntimeError) as exc:
        print(f"[启动失败] {exc}")
        return 6
    finally:
        _terminate_owned_process(process)
        clear_runtime_record(instance_id)
        if job_handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(job_handle)
        if mutex and os.name == "nt":
            ctypes.windll.kernel32.ReleaseMutex(mutex)
            ctypes.windll.kernel32.CloseHandle(mutex)
        if backend_log is not None:
            backend_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
