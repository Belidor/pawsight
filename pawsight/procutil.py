"""Process helpers that behave the same on macOS, Linux and Windows.

The trap this module exists for: on Windows `os.kill(pid, 0)` is NOT a
liveness probe. Any signal other than CTRL_C/CTRL_BREAK goes to
TerminateProcess, so the "is it alive?" check used to kill the process it was
asking about. Every liveness check goes through pid_alive() instead.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

WINDOWS = sys.platform == "win32"

# Windows: start children without a console window of their own. Without it
# every python.exe the app starts flashes a black window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pid_alive(pid: int) -> bool:
    if not pid or pid < 0:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True        # exists, belongs to someone else
    except OSError:
        return False
    return True


def cmdline(pid: int) -> str | None:
    """The command line of a process, or None when it cannot be read."""
    try:
        import psutil  # installed with the app (amqtt depends on it)

        return " ".join(psutil.Process(pid).cmdline())
    except ImportError:
        pass
    except Exception:
        return None
    if WINDOWS:
        return None
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    return out.strip() or None


def session_kwargs() -> dict:
    """Popen arguments for a child that must outlive its parent's restart."""
    if WINDOWS:
        return {"creationflags": NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def terminate(pid: int) -> None:
    """Ask a process (and, on POSIX, its process group) to stop. Raises OSError."""
    if WINDOWS:
        os.kill(pid, signal.SIGTERM)       # TerminateProcess
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        os.kill(pid, signal.SIGTERM)
