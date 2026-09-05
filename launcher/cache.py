"""Managed git checkouts for launchable games.

Everything the launcher clones lives under ``.arcade-cache/games/<id>`` which is
git-ignored.  Three rules are enforced structurally rather than by convention:

* A coming-soon entry raises :class:`~launcher.errors.NotLaunchableError`
  *before* any command is built, so it can never produce a network request.
* Every git invocation is an argument list run with an explicit ``cwd`` inside
  the cache. A shell is never involved.
* The destination is asserted to be inside the cache root, so a manifest can
  never make the launcher write into its own checkout or anywhere else.

The git runner is injected, which lets the test suite exercise clone failures,
offline fallback and a missing ``git`` binary without touching the network.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .errors import GitUnavailableError, NotLaunchableError
from .manifest import GameEntry
from .paths import default_cache_root
from .status import GameState, GameStatus

__all__ = [
    "GitResult",
    "GitRunner",
    "SubprocessGitRunner",
    "RepositoryCache",
]

_log = logging.getLogger(__name__)

#: Default per-git-command network timeout, in seconds.
#:
#: A club fair's Wi-Fi, when it exists at all, either connects in well under a
#: second or is not there -- there is no realistic "slow but working" middle
#: ground worth waiting through. This runs once per launchable game at
#: start-up (:meth:`~launcher.sync.SyncService.request_all`) *and* once more,
#: immediately, before every launch, so a high timeout does not just delay
#: one card's badge: with no network it multiplies by the whole catalogue and
#: then again by every visitor who presses Play. A previous value of 45s
#: measured at 21s to *fail* against an unreachable host on this cabinet's
#: network stack -- per game, every time. Low enough that a genuinely healthy
#: connection is never at risk of being cut off (git clone/fetch calls here
#: are shallow and small), high enough that an ordinarily slow-but-working
#: club Wi-Fi still gets to finish.
_DEFAULT_GIT_TIMEOUT_S = 8

#: How long a runner remembers "the network looked unreachable" after a
#: fetch or clone actually times out, before it is willing to pay the full
#: timeout again. Without this, a disconnected cabinet re-discovers the same
#: dead network once per launchable game at start-up and once more before
#: every launch -- each one paying the full timeout in serial on the sync
#: worker. With it, only the first attempt in a session is expensive; every
#: other network-touching call for the rest of the cooldown fails instantly.
#: Long enough to actually save something; short enough that a Wi-Fi that
#: comes back mid-fair is noticed within a minute, not ignored for the rest
#: of the day.
_NETWORK_RETRY_COOLDOWN_S = 60.0

#: Git subcommands that actually touch the network -- only these are subject
#: to the cooldown above. ``rev-parse``/``reset`` are purely local and must
#: never be skipped by it.
_NETWORK_VERBS = frozenset({"clone", "fetch"})


def _git_environment() -> dict[str, str]:
    """The environment every git subprocess call runs with.

    ``GIT_TERMINAL_PROMPT=0`` stops git from ever blocking on a username or
    password prompt it has nowhere to show -- there is no terminal at a club
    fair to answer it, and without this a private or moved repository would
    hang the sync worker indefinitely rather than failing like any other
    network error. ``GIT_ASKPASS`` is a second, belt-and-braces guard for the
    same failure mode should some local git configuration still prefer a GUI
    credential helper: pointed at ``echo``, it answers instantly with nothing
    rather than launching a helper that has no display to show either.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "echo")
    return env


def _kill_process_tree(process: "subprocess.Popen[str]") -> None:
    """Kill *process* and everything it spawned, not just the one pid.

    For an ``https://`` remote, git does not connect itself: it hands the URL
    to a *remote helper* it spawns as its own child (``git-remote-https``),
    which does the actual network connect and inherits git's stdout/stderr
    pipes. Killing only the top-level git process leaves that helper running
    -- still holding those pipes open -- so whatever is reading them keeps
    blocking for however long the helper's own connect attempt takes,
    regardless of any timeout configured up here. Measured directly against
    an unreachable host: a configured 8s timeout still took 21s end to end
    until the whole tree, not just git, was killed. Both branches below start
    the command in its own group/job (see the ``Popen`` call in
    :func:`_run_git`) specifically so this can target the group instead of a
    single pid.
    """
    if os.name == "nt":
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
        )
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def _run_git(
    command: Sequence[str], cwd: Path | None, timeout_s: float
) -> subprocess.CompletedProcess:
    """Run *command*, guaranteeing the whole process tree is gone by
    *timeout_s* -- see :func:`_kill_process_tree` for why that guarantee
    needs more than ``subprocess.run``'s own ``timeout=`` argument.
    """
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(  # noqa: S603 - argument list, no shell
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_git_environment(),
        **popen_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        # The tree is dead, so this drains whatever the pipes already held
        # and returns immediately -- it is not a second wait.
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(list(command), timeout_s, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


@dataclass(frozen=True, slots=True)
class GitResult:
    """Outcome of a single git invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def message(self) -> str:
        """Best single-line explanation for a UI banner."""
        for stream in (self.stderr, self.stdout):
            for line in reversed(stream.strip().splitlines()):
                if line.strip():
                    return line.strip()[:160]
        return f"git exited with code {self.returncode}"


class GitRunner(Protocol):
    """Callable that runs a git command and reports the result."""

    def available(self) -> bool:
        """Whether the git executable can be found and run."""

    def run(self, args: Sequence[str], cwd: Path) -> GitResult:
        """Run ``git *args`` with working directory *cwd*."""


class SubprocessGitRunner:
    """The real git runner, backed by :mod:`subprocess`.

    Args:
        executable: Name or path of the git binary.
        timeout_s: Per-command timeout; a hung network call must not freeze the
            background worker forever. See :data:`_DEFAULT_GIT_TIMEOUT_S` for
            why the default is as low as it is.
        clock: Wall clock backing the unreachable-network cooldown below;
            injected so tests can control it without a real sleep.
    """

    def __init__(
        self,
        executable: str = "git",
        timeout_s: int = _DEFAULT_GIT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executable = executable
        self._timeout_s = timeout_s
        self._available: bool | None = None
        self._clock = clock
        #: Set once a fetch/clone actually times out; see item 4 and
        #: :meth:`_network_looks_unreachable`.
        self._unreachable_until: float | None = None

    def available(self) -> bool:
        if self._available is None:
            try:
                completed = _run_git([self._executable, "--version"], None, self._timeout_s)
            except (OSError, subprocess.SubprocessError):
                self._available = False
            else:
                self._available = completed.returncode == 0
        return self._available

    def run(self, args: Sequence[str], cwd: Path) -> GitResult:
        verb = args[0] if args else ""
        if verb in _NETWORK_VERBS and self._network_looks_unreachable():
            return GitResult(
                124,
                stderr=(
                    "network looked unreachable a moment ago; skipping "
                    f"git {verb} to stay responsive (retries automatically "
                    f"within {_NETWORK_RETRY_COOLDOWN_S:.0f}s)"
                ),
            )
        command = [self._executable, *args]
        try:
            completed = _run_git(command, cwd, self._timeout_s)
        except FileNotFoundError:
            return GitResult(127, stderr=f"'{self._executable}' not found on PATH")
        except subprocess.TimeoutExpired:
            if verb in _NETWORK_VERBS:
                self._unreachable_until = self._clock() + _NETWORK_RETRY_COOLDOWN_S
            return GitResult(
                124, stderr=f"git timed out after {self._timeout_s}s: git {' '.join(args)}"
            )
        except OSError as exc:
            return GitResult(1, stderr=f"could not run git: {exc}")
        return GitResult(completed.returncode, completed.stdout or "", completed.stderr or "")

    def _network_looks_unreachable(self) -> bool:
        """Whether a network-touching command timed out recently enough that
        another one is not worth attempting yet -- see item 4."""
        return (
            self._unreachable_until is not None and self._clock() < self._unreachable_until
        )


class RepositoryCache:
    """Clones and refreshes launchable games inside the managed cache.

    Args:
        root: Cache root. Defaults to ``.arcade-cache`` beside the launcher.
        runner: Git runner; injected in tests.
        clock: Wall clock used to timestamp the bookkeeping written by
            :meth:`mark_synced` -- an operational record of when a checkout
            last succeeded, not a gate on whether to fetch again. See
            :meth:`sync` for why nothing here trusts a timestamp to decide
            that.
    """

    def __init__(
        self,
        root: Path | None = None,
        runner: GitRunner | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root) if root is not None else default_cache_root()
        self.runner: GitRunner = runner if runner is not None else SubprocessGitRunner()
        self._clock = clock

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------
    @property
    def games_dir(self) -> Path:
        return self.root / "games"

    @property
    def state_dir(self) -> Path:
        """Sync bookkeeping, kept *outside* the checkouts we manage."""
        return self.root / "state"

    def checkout_path(self, entry: GameEntry) -> Path:
        """Absolute checkout directory for *entry*."""
        destination = (self.games_dir / entry.id).resolve()
        games_dir = self.games_dir.resolve()
        if destination != games_dir and games_dir not in destination.parents:
            # Manifest ids are validated, but never trust a path we will rm/clone into.
            raise NotLaunchableError(
                f"game '{entry.id}': cache path escapes {games_dir}"
            )
        return destination

    def has_checkout(self, entry: GameEntry) -> bool:
        """Whether a plausible git checkout already exists for *entry*."""
        checkout = self.checkout_path(entry)
        return (checkout / ".git").exists()

    def entrypoint_path(self, entry: GameEntry) -> Path:
        """Absolute entrypoint path inside the checkout (validated)."""
        return entry.resolved_entrypoint(self.checkout_path(entry))

    def has_entrypoint(self, entry: GameEntry) -> bool:
        """Whether the configured entrypoint file exists in the checkout."""
        try:
            return self.entrypoint_path(entry).is_file()
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------
    def _state_file(self, entry: GameEntry) -> Path:
        return self.state_dir / f"{entry.id}.json"

    def _read_state(self, entry: GameEntry) -> dict:
        try:
            return json.loads(self._state_file(entry).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, entry: GameEntry, **values: object) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = self._read_state(entry)
        payload.update(values)
        try:
            self._state_file(entry).write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            # Bookkeeping is an optimisation: it only affects what the UI can
            # report (the last known commit), never whether the next sync
            # runs, so losing it must never break a launch -- but it is still
            # logged.
            _log.warning("could not record sync state for %s: %s", entry.id, exc)

    def last_commit(self, entry: GameEntry) -> str:
        """Short commit id recorded at the last successful sync ('' if unknown)."""
        value = self._read_state(entry).get("commit", "")
        return value if isinstance(value, str) else ""

    def mark_synced(self, entry: GameEntry, commit: str = "") -> None:
        """Record *entry* as synchronised right now.

        Public because both the sync path and operational tooling need to say
        "this checkout is current" without reaching into private state.
        """
        self._write_state(entry, synced_at=self._clock(), commit=commit)

    # ------------------------------------------------------------------
    # Synchronisation
    # ------------------------------------------------------------------
    def _guard_launchable(self, entry: GameEntry) -> None:
        if not entry.launchable:
            raise NotLaunchableError(
                f"game '{entry.id}' is marked coming-soon; the launcher must "
                "never clone, fetch or run it"
            )
        if not entry.repository or not entry.ref or not entry.entrypoint:
            raise NotLaunchableError(
                f"game '{entry.id}' is launchable but is missing repository, ref "
                "or entrypoint"
            )

    def offline_state(self, entry: GameEntry, reason: str) -> GameState:
        """Classify *entry* when the network is unusable.

        Returns ``CACHED_OFFLINE`` if a usable checkout exists, otherwise
        ``UNAVAILABLE`` with *reason*.
        """
        self._guard_launchable(entry)
        if self.has_checkout(entry) and self.has_entrypoint(entry):
            commit = self.last_commit(entry)
            suffix = f" (cached {commit})" if commit else ""
            return GameState(entry.id, GameStatus.CACHED_OFFLINE, f"{reason}{suffix}")
        if self.has_checkout(entry):
            return GameState(
                entry.id,
                GameStatus.UNAVAILABLE,
                f"cached copy is missing {entry.entrypoint}",
            )
        return GameState(entry.id, GameStatus.UNAVAILABLE, reason)

    def sync(self, entry: GameEntry) -> GameState:
        """Clone or refresh *entry* and report the resulting state.

        Every call that finds a checkout already on disk re-fetches it: there
        is no timestamp that decides a build is "fresh enough" to skip the
        network. *When* to call this is the caller's decision, not this
        method's -- :class:`~launcher.sync.SyncService` already makes it once
        per gallery session (via ``request_all``) and once more, immediately,
        right before a launch is allowed to proceed, both off the render
        thread. A single shallow ``fetch`` against a checkout that already
        exists is small and fast, and it runs in the background regardless, so
        there is nothing here worth trading away.

        A previous revision skipped the fetch for six hours after the last
        successful sync, on the theory that it kept a restart at a club fair
        instant. That shaved a cost no visitor could perceive (syncing was
        already backgrounded) against serving a build that was quietly hours
        stale, with the gallery reporting it as "up to date" the whole time --
        a strictly worse trade. The one place that still deliberately avoids
        the network is offline operation: ``SyncService(online=False)`` (wired
        up from ``sync_on_start`` / ``--no-sync``) calls :meth:`verify_only`
        instead of this method and never invokes git at all.

        Never raises for expected failures -- offline, bad ref, missing git and
        a missing entrypoint all become a :class:`~launcher.status.GameState`
        the gallery can render.

        Raises:
            NotLaunchableError: If *entry* is coming-soon. This is a programming
                error, not a runtime condition, and is deliberately loud.
        """
        self._guard_launchable(entry)
        assert entry.repository and entry.ref  # narrowed by _guard_launchable

        if not self.runner.available():
            return self.offline_state(entry, "git is not installed on this machine")

        checkout = self.checkout_path(entry)
        if self.has_checkout(entry):
            result = self._refresh(entry, checkout)
        else:
            result = self._clone(entry, checkout)

        if not result.ok:
            return self.offline_state(entry, result.message())

        if not self.has_entrypoint(entry):
            return GameState(
                entry.id,
                GameStatus.UNAVAILABLE,
                f"entrypoint '{entry.entrypoint}' not found in the checkout",
            )

        commit = self._head_commit(checkout)
        self._write_state(entry, synced_at=self._clock(), commit=commit, ref=entry.ref)
        return self._ready_state(entry, f"updated {commit}" if commit else "updated")

    def _ready_state(self, entry: GameEntry, detail: str) -> GameState:
        return GameState(entry.id, GameStatus.READY, detail)

    def _clone(self, entry: GameEntry, checkout: Path) -> GitResult:
        assert entry.repository and entry.ref
        self.games_dir.mkdir(parents=True, exist_ok=True)
        return self.runner.run(
            [
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                entry.ref,
                entry.repository,
                str(checkout),
            ],
            cwd=self.games_dir,
        )

    def _refresh(self, entry: GameEntry, checkout: Path) -> GitResult:
        assert entry.ref
        fetched = self.runner.run(
            ["fetch", "--depth", "1", "--force", "origin", entry.ref], cwd=checkout
        )
        if not fetched.ok:
            return fetched
        return self.runner.run(["reset", "--hard", "FETCH_HEAD"], cwd=checkout)

    def _head_commit(self, checkout: Path) -> str:
        result = self.runner.run(["rev-parse", "--short", "HEAD"], cwd=checkout)
        return result.stdout.strip() if result.ok else ""

    def verify_only(self, entry: GameEntry) -> GameState:
        """Classify *entry* using only what is already on disk (no network).

        Used when background syncing is switched off, and as the pre-launch
        safety check.
        """
        self._guard_launchable(entry)
        if not self.has_checkout(entry):
            return GameState(
                entry.id, GameStatus.UNAVAILABLE, "not downloaded yet - no cached copy"
            )
        if not self.has_entrypoint(entry):
            return GameState(
                entry.id,
                GameStatus.UNAVAILABLE,
                f"entrypoint '{entry.entrypoint}' not found in the checkout",
            )
        commit = self.last_commit(entry)
        return GameState(
            entry.id,
            GameStatus.CACHED_OFFLINE,
            f"using cached copy{f' {commit}' if commit else ''}",
        )

    def require_git(self) -> None:
        """Raise if git is unusable.

        Raises:
            GitUnavailableError: git is missing or not runnable.
        """
        if not self.runner.available():
            raise GitUnavailableError("git was not found on PATH")

    @property
    def git_available(self) -> bool:
        """Whether git can be used at all, without raising.

        The launcher must still start on a cabinet with no git and no network:
        it just runs from whatever is already cached.
        """
        return self.runner.available()
