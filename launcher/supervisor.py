"""The supervisor: owns the process, the gallery sessions, and the children.

Lifecycle, in one place:

1. Build a gallery session and run it. It owns SDL for as long as it runs.
2. The session returns an explicit :class:`UiOutcome` -- quit, or launch id X.
3. On *quit* the supervisor returns 0 and ``main.py`` calls ``sys.exit(0)``,
   which is the documented way back to the arcade's outer menu.
4. On *launch* the session has already torn SDL down; the supervisor spawns the
   game using its prepared interpreter/native engine and an argument list,
   never a shell, and waits inside an owned process-tree boundary.
5. When the child exits, the supervisor rebuilds the gallery with the same
   selected card and the same view mode, plus an error banner if the child
   failed.

That is the two-level exit: P1 inside the game ends the child and lands back in
the gallery; P1 in the gallery ends the launcher.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .cache import RepositoryCache
from .commands import build_child_command
from .errors import (
    LaunchError,
    LauncherError,
    UiFatalError,
)
from .manifest import GameEntry, Manifest, safe_relative_path
from .integrity import filesystem_path
from .paths import run_root
from .processes import OwnedProcess
from .status import Notice
from .viewmodes import ViewMode

__all__ = [
    "UiAction",
    "UiOutcome",
    "Notice",
    "SessionState",
    "ChildResult",
    "GameRunner",
    "ProcessGameRunner",
    "Supervisor",
    "build_child_command",
]

_log = logging.getLogger(__name__)


class UiAction(Enum):
    """What a finished gallery session is asking the supervisor to do."""

    QUIT = "quit"
    LAUNCH = "launch"


@dataclass(frozen=True, slots=True)
class UiOutcome:
    """The single, explicit result of one gallery session.

    Anything else -- ``None``, a wrong type, a launch without a game id -- is
    treated as a fatal programming error rather than being papered over.
    """

    action: UiAction
    view_mode: ViewMode
    selected_index: int
    game_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionState:
    """What survives across a game launch."""

    selected_index: int = 0
    view_mode: ViewMode = ViewMode.CAROUSEL
    notice: Notice | None = None

    def carry(self, outcome: UiOutcome) -> "SessionState":
        return replace(
            self,
            selected_index=outcome.selected_index,
            view_mode=outcome.view_mode,
            notice=None,
        )

    def with_notice(self, notice: Notice | None) -> "SessionState":
        return replace(self, notice=notice)


@dataclass(frozen=True, slots=True)
class ChildResult:
    """Outcome of one child game process."""

    returncode: int
    tail: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GameRunner(Protocol):
    """Starts a child game and waits for it. Injected so tests stay hermetic."""

    def run(
        self, command: Sequence[str], cwd: Path, *, game_id: str,
        env: Mapping[str, str] | None = None,
    ) -> ChildResult:
        """Run *command* in *cwd* and block until it exits."""

    def terminate(self) -> None:
        """Ask any running child to stop (used by signal handlers)."""


class ProcessGameRunner:
    """Runs games as real child processes.

    Output is captured to a transient log inside the managed cache so a failed
    game can be summarised in one banner line; the log is always deleted
    afterwards, which keeps ``git status`` clean.
    """

    def __init__(self, log_dir: Path | None = None, tail_lines: int = 4) -> None:
        self._log_dir = Path(log_dir) if log_dir is not None else run_root()
        self._tail_lines = tail_lines
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._owner: OwnedProcess | None = None
        self._cancelled = threading.Event()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def run(
        self, command: Sequence[str], cwd: Path, *, game_id: str,
        env: Mapping[str, str] | None = None,
    ) -> ChildResult:
        arguments = list(command)
        if not arguments:
            raise LaunchError("refusing to spawn an empty command")
        if self._cancelled.is_set():
            raise LaunchError("launch cancelled by a shutdown request")
        log_path = safe_relative_path(
            f"{game_id}.child.log", self._log_dir, game_id=game_id, field_name="child log",
        )
        _log.info("launching %s: %s (cwd=%s)", game_id, arguments, cwd)
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            if env is not None and "ARCADE_GAME_DATA_DIR" in env:
                data_dir = Path(env["ARCADE_GAME_DATA_DIR"])
                if not data_dir.is_absolute():
                    raise LaunchError("ARCADE_GAME_DATA_DIR must be an absolute path")
                filesystem_path(data_dir).mkdir(parents=True, exist_ok=True)
            with open(log_path, "w+b") as stream:
                owner = OwnedProcess(arguments, cwd=cwd, stdout=stream, env=env)
                process = owner.process
                assert process is not None
                with self._lock:
                    self._owner = owner
                    self._process = process
                try:
                    if self._cancelled.is_set():
                        owner.terminate()
                        raise LaunchError("launch cancelled by a shutdown request")
                    returncode = process.wait()
                except KeyboardInterrupt:
                    _log.warning(
                        "Ctrl+C while waiting for %s; terminating it", game_id
                    )
                    self.terminate()
                    raise
                finally:
                    try:
                        # Includes helpers surviving a clean parent exit.
                        owner.close()
                    finally:
                        with self._lock:
                            self._owner = None
                            self._process = None
                stream.flush()
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 65536))
                tail = self._tail(stream.read())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LaunchError(f"could not run/clean up '{game_id}': {exc}") from exc
        finally:
            self._remove(log_path)
        _log.info("%s exited with code %s", game_id, returncode)
        return ChildResult(returncode=returncode, tail=tail)

    def terminate(self, grace_s: float = 5.0) -> None:
        """Cancel a pending spawn and terminate its entire owned process tree."""
        self._cancelled.set()
        with self._lock:
            owner = self._owner
        if owner is not None:
            _log.warning("terminating the owned game process tree")
            owner.terminate(grace_s=grace_s)

    def _tail(self, blob: bytes) -> str:
        text = blob.decode("utf-8", errors="replace").strip()
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return " | ".join(lines[-self._tail_lines :])[:240]

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("could not remove transient log %s: %s", path, exc)


#: A gallery session: called with the carried state, returns an explicit outcome.
UiFactory = Callable[[SessionState], UiOutcome]


class Supervisor:
    """Runs gallery sessions and game children until the visitor exits.

    Args:
        manifest: The validated game list.
        cache: Repository cache used for the pre-launch readiness check.
        ui: Callable that runs one gallery session and returns a
            :class:`UiOutcome`.
        runner: Child process runner. Defaults to :class:`ProcessGameRunner`.
        initial_state: Starting selection and view mode.
        max_ui_restarts: How many times a *crashed* gallery may be rebuilt
            before the launcher gives up. Bounded on purpose: an unbounded
            retry would spin forever on a broken display.
        install_signal_handlers: Install SIGINT/SIGTERM handlers for the
            duration of :meth:`run`.
        shutdown: The flag a termination request sets. Pass the same event to
            the gallery (as its ``should_stop`` predicate) so a signal reaches
            the running session; the supervisor alone only checks it between
            sessions, which is not enough to interrupt one. Defaults to a
            private event, which is what the tests that never signal use.
    """

    def __init__(
        self,
        manifest: Manifest,
        cache: RepositoryCache,
        ui: UiFactory,
        runner: GameRunner | None = None,
        initial_state: SessionState | None = None,
        max_ui_restarts: int = 2,
        install_signal_handlers: bool = True,
        shutdown: threading.Event | None = None,
    ) -> None:
        self.manifest = manifest
        self.cache = cache
        self.ui = ui
        self.runner: GameRunner = (
            runner if runner is not None else ProcessGameRunner(run_root(cache.root))
        )
        self.state = initial_state or SessionState()
        self.max_ui_restarts = max_ui_restarts
        self._install_signal_handlers = install_signal_handlers
        self._shutdown = shutdown if shutdown is not None else threading.Event()
        self._previous_handlers: dict[int, object] = {}
        #: Number of completed gallery sessions -- asserted by the tests.
        self.sessions_run = 0

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------
    def _handle_signal(self, signum: int, _frame: object) -> None:
        _log.warning("received signal %s; shutting down", signum)
        self._shutdown.set()
        self.runner.terminate()

    def _install_handlers(self) -> None:
        if not self._install_signal_handlers:
            return
        if threading.current_thread() is not threading.main_thread():
            _log.debug("not on the main thread; skipping signal handlers")
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (ValueError, OSError) as exc:  # unsupported on this platform
                _log.debug("could not install handler for %s: %s", signum, exc)

    def _restore_handlers(self) -> None:
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError) as exc:
                _log.debug("could not restore handler for %s: %s", signum, exc)
        self._previous_handlers.clear()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Run gallery sessions until the visitor exits.

        Every way this can end logs why, at INFO or louder, before it
        returns or raises -- see the module docstring's incident: a launcher
        that ends silently, between one log line and the next, is
        undiagnosable. That includes the ``while`` loop's own fall-through
        (reached only once :attr:`_shutdown` is set, itself only ever set by
        a logged signal handler or a logged ``KeyboardInterrupt`` inside
        :meth:`_launch`) and a last-resort ``BaseException`` handler for
        anything -- a stray ``SystemExit``, most plausibly -- that the
        narrower handlers below it were never going to catch, since neither
        is an ``Exception`` subclass.

        Returns:
            The process exit code: 0 for a normal exit back to the arcade menu.

        Raises:
            UiFatalError: The gallery crashed more than ``max_ui_restarts``
                times, or returned an outcome the supervisor cannot act on.
            LauncherError: Any other unrecoverable launcher failure.
        """
        self._install_handlers()
        restarts = 0
        try:
            while not self._shutdown.is_set():
                try:
                    outcome = self.ui(self.state)
                except LauncherError:
                    raise
                except KeyboardInterrupt:
                    _log.info("interrupted; exiting to the arcade menu")
                    return 0
                except Exception as exc:  # noqa: BLE001 - re-raised, never swallowed
                    restarts += 1
                    _log.exception("gallery session crashed (%s/%s)", restarts, self.max_ui_restarts)
                    if restarts > self.max_ui_restarts:
                        raise UiFatalError(
                            f"gallery failed {restarts} times; refusing to restart again"
                        ) from exc
                    self.state = self.state.with_notice(
                        Notice("error", "Gallery restarted", str(exc)[:160])
                    )
                    continue
                except BaseException as exc:  # noqa: BLE001 - see docstring above
                    _log.critical(
                        "gallery session raised %s, which is not a subclass "
                        "of Exception and so is never caught by anything "
                        "above -- logging it here rather than letting it "
                        "escape silently",
                        type(exc).__name__,
                        exc_info=True,
                    )
                    raise

                restarts = 0
                self.sessions_run += 1
                self._validate(outcome)
                self.state = self.state.carry(outcome)

                if outcome.action is UiAction.QUIT:
                    _log.info("visitor exited the gallery; returning to the arcade menu")
                    return 0

                assert outcome.game_id is not None  # validated above
                self.state = self.state.with_notice(self._launch(outcome.game_id))
                _log.info(
                    "back from %s; reopening the gallery", outcome.game_id
                )
            _log.info("shutdown flag set; leaving the supervisor loop")
            return 0
        finally:
            self._restore_handlers()
            self.cleanup()

    def _validate(self, outcome: object) -> None:
        """Reject anything that is not a well-formed, actionable outcome."""
        if not isinstance(outcome, UiOutcome):
            raise UiFatalError(
                f"gallery returned {type(outcome).__name__}, expected UiOutcome"
            )
        if not isinstance(outcome.action, UiAction):
            raise UiFatalError(f"gallery returned unknown action {outcome.action!r}")
        if not 0 <= outcome.selected_index < len(self.manifest):
            raise UiFatalError(
                f"gallery returned selection {outcome.selected_index} outside "
                f"0..{len(self.manifest) - 1}"
            )
        if outcome.action is UiAction.LAUNCH:
            if not outcome.game_id:
                raise UiFatalError("gallery asked to launch but named no game")
            try:
                entry = self.manifest.by_id(outcome.game_id)
            except KeyError:
                raise UiFatalError(
                    f"gallery asked to launch unknown game '{outcome.game_id}'"
                ) from None
            if not entry.launchable:
                raise UiFatalError(
                    f"gallery asked to launch coming-soon game '{entry.id}'"
                )

    # ------------------------------------------------------------------
    # Launching
    # ------------------------------------------------------------------
    def _launch(self, game_id: str) -> Notice | None:
        entry = self.manifest.by_id(game_id)
        with self.cache.checkout_guard(entry):
            if self._shutdown.is_set():
                _log.info("shutdown requested; cancelling launch of %s", game_id)
                return None
            return self._launch_entry(entry)

    def _launch_entry(self, entry: GameEntry) -> Notice | None:
        readiness = self.cache.verify_only(entry)
        if not readiness.status.is_playable:
            _log.warning("refusing to launch %s: %s", entry.id, readiness.detail)
            return Notice("error", f"{entry.title} is not ready", readiness.detail)

        try:
            prepared = self.cache.prepared_game(entry)
            command = build_child_command(
                prepared.entry, prepared.checkout, executable=prepared.executable,
            )
        except LauncherError as exc:
            _log.error("cannot build command for %s: %s", entry.id, exc)
            return Notice("error", f"Cannot start {entry.title}", str(exc))

        try:
            result = self.runner.run(
                command, prepared.checkout, game_id=entry.id, env=prepared.child_environment(),
            )
        except LaunchError as exc:
            return Notice("error", f"Cannot start {entry.title}", str(exc))
        except KeyboardInterrupt:
            # A genuine Ctrl+C should stop the launcher entirely -- but it
            # must say so. This used to be silent, which is exactly what
            # made a real, unrelated silent-exit incident indistinguishable
            # from an operator's own Ctrl+C after the fact: neither logged
            # anything, so nothing in the log could tell them apart.
            _log.warning(
                "Ctrl+C while %s was running; shutting down the launcher "
                "instead of returning to the gallery",
                entry.id,
            )
            self._shutdown.set()
            return None

        if result.ok:
            return Notice("info", f"Thanks for playing {entry.title}!", "")
        detail = result.tail or "no output captured"
        return Notice(
            "error",
            f"{entry.title} exited with code {result.returncode}",
            detail,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def request_shutdown(self) -> None:
        """Ask the loop to stop after the current session (thread-safe)."""
        self._shutdown.set()

    def cleanup(self) -> None:
        """Terminate any child and remove the transient run directory."""
        self.runner.terminate()
        directory = run_root(self.cache.root)
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.child.log")):
            try:
                path.unlink()
            except OSError as exc:
                _log.warning("could not remove %s: %s", path, exc)
        try:
            os.rmdir(directory)
        except OSError:
            # Not empty (a concurrent run) or already gone: harmless.
            _log.debug("run directory %s not removed", directory)
