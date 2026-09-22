# pyright: basic

"""worker 子进程启动、Job Object/进程组与整树回收。"""

import os
import signal
import subprocess

_JOB_HANDLES = {}


# 启动 worker 子进程并纳入整树回收范围
def spawn_worker(argv, env, cwd=None):
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "cwd": cwd,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **kwargs)
    _assign_job(process)
    return process


# 把 Windows 子进程加入 KILL_ON_JOB_CLOSE 的 Job Object
def _assign_job(process):
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [("Counter", ctypes.c_uint64) for _ in range(6)]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _ExtendedLimitInformation()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info.BasicLimitInformation.LimitFlags = 0x2000
        # JobObjectExtendedLimitInformation = 9
        kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        handle = kernel32.OpenProcess(0x0100 | 0x0001, False, process.pid)
        if not handle:
            kernel32.CloseHandle(job)
            return
        kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
        _JOB_HANDLES[process.pid] = job
    except Exception:
        # Job Object 不可用时回退到 taskkill 整树回收
        return


# 结束 worker 及其后代进程，先温和后强杀
def kill_tree(process, timeout=5.0):
    if process.poll() is not None:
        _JOB_HANDLES.pop(process.pid, None)
        return
    if os.name == "nt":
        _terminate_windows(process)
    else:
        _signal_posix(process, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        try:
            process.kill()
        except OSError:
            pass
    else:
        _signal_posix(process, signal.SIGKILL)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


# Windows 优先 TerminateJobObject，缺失时 taskkill /T
def _terminate_windows(process):
    job = _JOB_HANDLES.pop(process.pid, None)
    if job is not None:
        try:
            import ctypes

            ctypes.WinDLL("kernel32").TerminateJobObject(job, 1)
            ctypes.WinDLL("kernel32").CloseHandle(job)
        except Exception:
            pass
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# POSIX 向进程组发信号，失败回退单进程
def _signal_posix(process, sig):
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass
