from __future__ import annotations

import os
import subprocess
import time
from typing import Any


CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def worker_creation_flags() -> int:
    """Return safe flags for a non-interactive Windows worker process."""

    if os.name != "nt":
        return 0
    return CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


class WindowsJobObject:
    """Kill-on-close ownership for one worker process tree.

    The supervisor treats assignment failure as a worker-start failure rather
    than running a child whose descendants cannot be accounted for. The class
    is a no-op on non-Windows platforms so tests remain portable.
    """

    def __init__(self) -> None:
        self._handle: int | None = None
        self._assigned = False
        if os.name != "nt":
            return
        self._handle = self._create()

    @staticmethod
    def _create() -> int | None:
        import ctypes
        from ctypes import wintypes

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
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

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            kernel32.CloseHandle(handle)
            return None
        return int(handle)

    def assign(self, process: subprocess.Popen[Any]) -> bool:
        if os.name != "nt" or self._handle is None:
            return False
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            return False
        self._assigned = bool(
            kernel32.AssignProcessToJobObject(self._handle, int(process_handle))
        )
        return self._assigned

    def terminate(self, exit_code: int = 1) -> bool:
        if os.name != "nt" or self._handle is None or not self._assigned:
            return False
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        return bool(kernel32.TerminateJobObject(self._handle, exit_code))

    def active_process_count(self) -> int | None:
        if os.name != "nt" or self._handle is None or not self._assigned:
            return None
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        information = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        returned = wintypes.DWORD()
        if not kernel32.QueryInformationJobObject(
            self._handle,
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        ):
            return None
        return int(information.ActiveProcesses)

    def wait_empty(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            active = self.active_process_count()
            if active == 0:
                return True
            if active is None or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def close(self) -> None:
        if os.name != "nt" or self._handle is None:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self._handle)
        self._handle = None
        self._assigned = False

    def __enter__(self) -> "WindowsJobObject":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def force_kill_process_tree(
    process: subprocess.Popen[Any], job_object: WindowsJobObject | None = None
) -> bool:
    """Forcefully terminate a worker and every child process it owns."""

    if os.name == "nt":
        if job_object is not None:
            active = job_object.active_process_count()
            if active == 0:
                return True
            if job_object.terminate() and job_object.wait_empty():
                process.poll()
                return True
            return False
        if process.poll() is not None:
            # Without an assigned Job Object Windows no longer provides a
            # trustworthy tree root after the process has exited. Supervisor
            # startup therefore requires successful Job assignment.
            return True
        try:
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0 and process.poll() is None:
                process.kill()
        except (OSError, subprocess.SubprocessError):
            process.kill()
        deadline = time.monotonic() + 5.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        return process.poll() is not None

    import signal

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        if process.poll() is None:
            process.kill()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        time.sleep(0.02)
    return False
