from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from substar_core.config import INSTALL_ROOT, PROJECT_ROOT, DATA_ROOT
from substar_core.process_command import backend_command
from substar_core.runtime.launch_surface import require_visible_backend, visible_backend_creation_flags
from substar_core.runtime_instance import (
    INSTANCE_SHUTDOWN_TIMEOUT,
    INSTANCE_STARTUP_TIMEOUT,
    PORT_IN_USE_EXIT_CODE,
    bind_backend_socket,
    build_id,
    is_port_in_use,
    remember_startup_port,
    remembered_startup_port,
    clear_runtime_record,
    load_runtime_record,
    probe_identity,
    write_runtime_record,
)


MUTEX_NAME = "Global\\Substar.Workbench.Singleton"
ERROR_ALREADY_EXISTS = 183
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
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
        with bind_backend_socket(port):
            return False
    except OSError as error:
        if is_port_in_use(error, port):
            return True
        raise


def _foreground_backend_window() -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    own_window = kernel32.GetConsoleWindow()
    found = False

    @callback_type
    def visit(hwnd, _):
        nonlocal found
        if hwnd == own_window:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        if not title.value.startswith("Substar 后端 · ") or not user32.IsWindowVisible(hwnd):
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        found = True
        return False

    user32.EnumWindows(visit, 0)
    return found


def _open_existing(identity: dict) -> int:
    print(f"Substar 已在运行：http://127.0.0.1:{identity['port']}")
    _foreground_backend_window()
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


def _close_handle(handle: int | None) -> None:
    if handle and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(handle)


def _request_graceful_shutdown(port: int, instance_id: str, timeout: float = 1.5, *, token: str = "") -> bool:
    if not port or not instance_id:
        return False
    request = Request(
        f"http://127.0.0.1:{int(port)}/api/runtime/shutdown",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Substar-Instance-ID": instance_id,
            "X-Substar-Shutdown-Token": token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 0)) < 300
    except (OSError, HTTPError, URLError, TimeoutError):
        return False


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


def main() -> int:
    _configure_install_path()
    if len(sys.argv) > 1 and sys.argv[1] in {"--help", "-h"}:
        print("launcher.py [--smoke-import]")
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
    if len(sys.argv) > 1:
        print("请通过 Substar.exe 启动，关闭后端窗口即可退出。")
        return 2
    try:
        require_visible_backend()
    except RuntimeError as error:
        print(error)
        return 6

    root = Path(INSTALL_ROOT).resolve()
    port = remembered_startup_port()
    expected_build = build_id(PROJECT_ROOT)
    mutex, owns_mutex = _acquire_mutex()
    if not owns_mutex:
        _close_handle(mutex)
        if _foreground_backend_window():
            return 0
        print("[启动失败] 已有 Substar 实例，或无法取得整机单实例锁。")
        return 6 if not mutex else 2

    try:
        record = load_runtime_record() or {}
        identity = _probe_candidate_identity(record, port)
        if identity:
            return _open_existing(identity)
        if _foreground_backend_window():
            return 0
        if record:
            if not _record_is_safely_stale(record, root):
                print("[冲突] 无法确认原 Substar 已退出，请关闭原后端窗口后再试。")
                return 3
            clear_runtime_record(str(record.get("instance_id", "")) or None)

        for candidate in range(port, 65536):
            if _port_is_open(candidate):
                continue
            _set_console_title(candidate)
            result = _run_backend(root, candidate, expected_build)
            if result != PORT_IN_USE_EXIT_CODE:
                return result
        print(f"[端口冲突] 从 {port} 到 65535 没有可用端口。")
        return 4
    except OSError as error:
        print(f"[启动失败] {error}")
        return 6
    finally:
        _release_launcher_mutex(mutex, owns_mutex)


def _register_console_close(port: int, instance_id: str, token: str, process):
    if os.name != "nt":
        return None
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleCtrlHandler.argtypes = (callback_type, wintypes.BOOL)

    @callback_type
    def close(event):
        if event != 2:  # CTRL_CLOSE_EVENT
            return False
        _request_graceful_shutdown(port, instance_id, timeout=0.5, token=token)
        try:
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return True

    return close if kernel32.SetConsoleCtrlHandler(close, True) else None


def _run_backend(root: Path, port: int, expected_build: str) -> int:
    instance_id = uuid.uuid4().hex
    env = os.environ.copy()
    env["SUBSTAR_PORT"] = str(port)
    env["SUBSTAR_INSTANCE_ID"] = instance_id
    shutdown_token = uuid.uuid4().hex
    env["SUBSTAR_SHUTDOWN_TOKEN"] = shutdown_token
    env["SUBSTAR_LAUNCH_SURFACE"] = "visible_console"
    command = backend_command()
    _status(f"Substar 正在启动 · http://127.0.0.1:{port}")
    process: subprocess.Popen | None = None
    job_handle: int | None = None
    backend_log = None
    close_handler = None
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
        close_handler = _register_console_close(port, instance_id, shutdown_token, process)
        startup_deadline = time.monotonic() + INSTANCE_STARTUP_TIMEOUT
        while time.monotonic() < startup_deadline:
            if process.poll() is not None:
                print(f"后端启动失败，退出码：{process.returncode}")
                return int(process.returncode or 1)
            ready = probe_identity(port, timeout=0.3)
            if ready and ready.get("instance_id") == instance_id and ready.get("pid") == process.pid:
                try:
                    remember_startup_port(port)
                except OSError as error:
                    print(f"\n[提示] 无法记住端口 {port}：{error}")
                _status(
                    f"Substar 运行中 · http://127.0.0.1:{port} · 关闭此窗口即退出",
                    final=True,
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
        _request_graceful_shutdown(port, instance_id, token=shutdown_token)
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
        if close_handler is not None:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(close_handler, False)
        if backend_log is not None:
            backend_log.close()


if __name__ == "__main__":
    exit_code = main()
    if "--smoke-import" not in sys.argv:
        from substar_core.updater import launch_pending_update
        launch_pending_update()
    raise SystemExit(exit_code)
