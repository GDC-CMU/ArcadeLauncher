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
    "DEFAULT_MAX_AGE_S",
]

_log = logging.getLogger(__name__)

#: A checkout synced more recently than this is considered fresh and is not
#: re-fetched, so restarting the launcher at a club fair is instant.
DEFAULT_MAX_AGE_S = 6 * 60 * 60


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
            background worker forever.
    """

    def __init__(self, executable: str = "git", timeout_s: int = 45) -> None:
        self._executable = executable
        self._timeout_s = timeout_s
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is None:
            try:
                completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [self._executable, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_s,
                )
            except (OSError, subprocess.SubprocessError):
                self._available = False
            else:
                self._available = completed.returncode == 0
        return self._available

    def run(self, args: Sequence[str], cwd: Path) -> GitResult:
        command = [self._executable, *args]
        try:
            completed = subprocess.run(  # noqa: S603 - argument list, no shell
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
        except FileNotFoundError:
            return GitResult(127, stderr=f"'{self._executable}' not found on PATH")
        except subprocess.TimeoutExpired:
            return GitResult(
                124, stderr=f"git timed out after {self._timeout_s}s: git {' '.join(args)}"
            )
        except OSError as exc:
            return GitResult(1, stderr=f"could not run git: {exc}")
        return GitResult(completed.returncode, completed.stdout or "", completed.stderr or "")


class RepositoryCache:
    """Clones and refreshes launchable games inside the managed cache.

    Args:
        root: Cache root. Defaults to ``.arcade-cache`` beside the launcher.
        runner: Git runner; injected in tests.
        clock: Monotonic-ish wall clock used for freshness checks.
        max_age_s: Age above which a checkout is refreshed from its ref.
    """

    def __init__(
        self,
        root: Path | None = None,
        runner: GitRunner | None = None,
        clock: Callable[[], float] = time.time,
        max_age_s: int = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.root = Path(root) if root is not None else default_cache_root()
        self.runner: GitRunner = runner if runner is not None else SubprocessGitRunner()
        self._clock = clock
        self._max_age_s = max_age_s

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
            # Bookkeeping is an optimisation: losing it only costs one extra
            # fetch, so it must never break a launch -- but it is still logged.
            _log.warning("could not record sync state for %s: %s", entry.id, exc)

    def is_fresh(self, entry: GameEntry) -> bool:
        """Whether the last successful sync is recent enough to skip the network."""
        last = self._read_state(entry).get("synced_at")
        if not isinstance(last, (int, float)):
            return False
        return (self._clock() - float(last)) < self._max_age_s

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

    def sync(self, entry: GameEntry, *, force: bool = False) -> GameState:
        """Clone or refresh *entry* and report the resulting state.

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
            if not force and self.is_fresh(entry) and self.has_entrypoint(entry):
                return self._ready_state(entry, "up to date")
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
