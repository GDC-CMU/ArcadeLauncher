"""Owned subprocess trees, without shells, wrappers, or third-party packages.

POSIX children lead a new session/process group. Windows children are created
suspended, assigned to a kill-on-close Job Object, then resumed: even a helper
spawned immediately at startup cannot race out of ownership. Closing ownership
also kills helpers left behind after a parent's *normal* exit.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import PreparationError
from .integrity import filesystem_path


class _WindowsJob:
    """Windows kernel job, created before its first process may execute."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                ("th32ThreadID", w.DWORD), ("th32OwnerProcessID", w.DWORD),
                ("tpBasePri", w.LONG), ("tpDeltaPri", w.LONG), ("dwFlags", w.DWORD),
            ]

        self.ctypes = ctypes
        self.ThreadEntry = ThreadEntry
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes = args
            function.restype = result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, pid: int) -> None:
        c = self.ctypes
        process = self.kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise c.WinError(c.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise c.WinError(c.get_last_error())
        finally:
            self.kernel.CloseHandle(process)

        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        resumed = False
        try:
            item = self.ThreadEntry()
            item.dwSize = c.sizeof(item)
            found = self.kernel.Thread32First(snapshot, c.byref(item))
            while found:
                if item.th32OwnerProcessID == pid:
                    thread = self.kernel.OpenThread(0x0002, False, item.th32ThreadID)
                    if not thread:
                        raise c.WinError(c.get_last_error())
                    try:
                        if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise c.WinError(c.get_last_error())
                        resumed = True
                    finally:
                        self.kernel.CloseHandle(thread)
                found = self.kernel.Thread32Next(snapshot, c.byref(item))
        finally:
            self.kernel.CloseHandle(snapshot)
        if not resumed:
            raise OSError(f"could not resume the owned child {pid}")

    def terminate(self) -> None:
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise self.ctypes.WinError(self.ctypes.get_last_error())


class OwnedProcess:
    """A process and the OS ownership boundary containing all its descendants."""

    def __init__(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None,
        stdout: object = None, stderr: object = subprocess.STDOUT,
    ) -> None:
        self._job = _WindowsJob() if os.name == "nt" else None
        self._lock = threading.RLock()
        self._closed = False
        self.process: subprocess.Popen[bytes] | None = None
        started = False
        try:
            kwargs: dict[str, object] = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004}
                if self._job else {"start_new_session": True}
            )
            arguments = list(command)
            if arguments and Path(arguments[0]).is_absolute():
                arguments[0] = str(filesystem_path(Path(arguments[0])))
            self.process = subprocess.Popen(
                arguments, cwd=str(filesystem_path(cwd)), env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **kwargs,
            )
            if self._job:
                self._job.assign_and_resume(self.process.pid)
            started = True
        finally:
            if not started:
                if self.process is not None:
                    self.process.kill()
                    self.process.wait(timeout=5)
                if self._job:
                    self._job.close()

    def terminate(self, grace_s: float = 1.0) -> None:
        """Stop the whole owned tree, including helpers whose parent has exited."""
        with self._lock:
            if self._closed or self.process is None:
                return
            process = self.process
            if self._job:
                self._job.terminate()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    return
                deadline = time.monotonic() + grace_s
                while time.monotonic() < deadline:
                    process.poll()  # reap the direct child, not an unrelated pid
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self.terminate(grace_s=0.2)
            finally:
                self._closed = True
                if self._job:
                    self._job.close()

    def __enter__(self) -> "OwnedProcess":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded diagnostic output of a preparation command."""

    returncode: int
    output: str = ""
    diagnostic_error: str = ""


class PreparationRunner:
    """Bounded, cancellable preparation subprocesses; never used on Play."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._owner: OwnedProcess | None = None
        self._lock = threading.Lock()

    def run(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None,
        timeout_s: float = 180,
    ) -> CommandResult:
        if self.cancelled.is_set():
            raise PreparationError("preparation was cancelled")
        try:
            with tempfile.TemporaryFile() as output:
                with OwnedProcess(command, cwd=cwd, env=env, stdout=output) as owner:
                    with self._lock:
                        self._owner = owner
                    try:
                        if self.cancelled.is_set():
                            owner.terminate()
                            raise PreparationError("preparation was cancelled")
                        assert owner.process is not None
                        code = owner.process.wait(timeout=timeout_s)
                    finally:
                        with self._lock:
                            self._owner = None
                # Godot sometimes returns 0 after an early import/script error.
                # Inspect the whole log in bounded lines, not just its tail.
                output.seek(0)
                diagnostic_error = ""
                while True:
                    line = output.readline(8192)
                    if not line:
                        break
                    plain = re.sub(rb"\x1b\[[0-9;]*m", b"", line).lstrip()
                    if plain.startswith((b"ERROR:", b"SCRIPT ERROR:")):
                        diagnostic_error = plain.decode("utf-8", errors="replace").strip()[:1200]
                        break
                output.seek(0, os.SEEK_END)
                output.seek(max(0, output.tell() - 65536))
                text = output.read().decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PreparationError(f"preparation command failed ({command[0]}): {exc}") from exc
        if self.cancelled.is_set():
            raise PreparationError("preparation was cancelled")
        return CommandResult(code, text, diagnostic_error)

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            owner = self._owner
        if owner is not None:
            owner.terminate()

    def reset_cancel(self) -> None:
        with self._lock:
            if self._owner is not None:
                raise PreparationError("previous preparation command is still stopping")
            self.cancelled.clear()
