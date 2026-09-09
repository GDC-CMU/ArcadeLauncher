"""Real owned-tree cleanup after exit, crash, cancellation and preparation timeout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from support import TempDirCase, child_fixture

from launcher.errors import LaunchError, PreparationError
from launcher.cache import _run_git
from launcher.processes import PreparationRunner
from launcher.supervisor import ProcessGameRunner


def alive(pid: int) -> bool:
    """Check an exact fixture PID; a terminated POSIX zombie is not running."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.GetExitCodeProcess.restype = w.BOOL
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # process no longer exists
                return False
            raise ctypes.WinError(error)
        try:
            code = w.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_file = Path(f"/proc/{pid}/stat")
    try:
        state = stat_file.read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


class OwnedTreeTests(TempDirCase):
    def command(self, *, exit_code=None):
        args = [sys.executable, str(child_fixture("child_tree.py")), "--record", str(self.tmp_path / "tree.json")]
        if exit_code is not None:
            args.extend(["--exit-code", str(exit_code)])
        return args

    def record(self):
        path = self.tmp_path / "tree.json"
        deadline = time.monotonic() + 10
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.is_file(), "fixture tree did not finish starting")
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_tree_stopped(self, pids):
        deadline = time.monotonic() + 5
        remaining = []
        while time.monotonic() < deadline:
            remaining = [pid for pid in pids.values() if alive(pid)]
            if not remaining:
                break
            time.sleep(0.02)
        self.assertEqual(remaining, [], f"owned processes survived cleanup: {remaining}")

    def test_clean_parent_exit_cleans_helpers_and_grandchildren(self):
        runner = ProcessGameRunner(self.tmp_path / "run")
        result = runner.run(self.command(exit_code=0), self.tmp_path, game_id="clean-tree")
        self.assertTrue(result.ok, result.tail)
        self.assert_tree_stopped(self.record())

    def test_crashed_parent_also_cleans_helpers_and_grandchildren(self):
        runner = ProcessGameRunner(self.tmp_path / "run")
        result = runner.run(self.command(exit_code=7), self.tmp_path, game_id="crash-tree")
        self.assertEqual(result.returncode, 7)
        self.assert_tree_stopped(self.record())

    def test_cancellation_kills_only_the_owned_tree(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        runner = ProcessGameRunner(self.tmp_path / "run")
        try:
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(runner.run, self.command(), self.tmp_path, game_id="cancel-tree")
                try:
                    pids = self.record()
                    self.assertTrue(all(alive(pid) for pid in pids.values()))
                    runner.terminate(grace_s=0.2)
                    future.result(timeout=10)
                    self.assert_tree_stopped(pids)
                    self.assertIsNone(unrelated.poll(), "cleanup killed an unrelated process")
                finally:
                    runner.terminate(grace_s=0.2)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_preparation_timeout_cleans_the_real_tree(self):
        runner = PreparationRunner()
        with self.assertRaisesRegex(PreparationError, "timed out"):
            runner.run(self.command(), cwd=self.tmp_path, timeout_s=2)
        self.assert_tree_stopped(self.record())

    def test_git_timeout_uses_the_same_real_tree_cleanup(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_git(self.command(), self.tmp_path, timeout_s=2)
        self.assert_tree_stopped(self.record())

    def test_an_early_zero_exit_error_is_not_lost_when_the_log_tail_is_bounded(self):
        runner = PreparationRunner()
        result = runner.run(
            [sys.executable, "-c", "print('SCRIPT ERROR: early failure'); print('progress\\n' * 20000)"],
            cwd=self.tmp_path,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("early failure", result.output)
        self.assertIn("early failure", result.diagnostic_error)

    def test_shutdown_before_spawn_cannot_start_a_new_child(self):
        runner = ProcessGameRunner(self.tmp_path / "run")
        runner.terminate()
        with mock.patch("launcher.supervisor.OwnedProcess") as spawn:
            with self.assertRaisesRegex(LaunchError, "cancelled"):
                runner.run(self.command(), self.tmp_path, game_id="cancelled-before-start")
        spawn.assert_not_called()

    def test_repeated_children_do_not_leave_logs_or_process_owners(self):
        runner = ProcessGameRunner(self.tmp_path / "run")
        for _ in range(3):
            result = runner.run([sys.executable, "-c", "print('returned')"], self.tmp_path, game_id="repeated")
            self.assertTrue(result.ok)
            self.assertFalse(runner.is_running)
            self.assertIsNone(runner._owner)
        self.assertEqual(list((self.tmp_path / "run").glob("*.child.log")), [])
