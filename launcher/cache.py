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
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence

from .errors import CacheError, GitUnavailableError, ManifestError, NotLaunchableError, PreparationError
from .integrity import atomic_json, file_digest, filesystem_path, is_link, read_object, remove_owned_tree
from .manifest import GameEntry, safe_relative_path
from .paths import REPO_ROOT, default_cache_root
from .preparation import PreparationService, PreparedGame
from .processes import OwnedProcess
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
#: Bounds the startup check so a disconnected cabinet reaches its cached
#: games without waiting for the operating system's full connection timeout.
_DEFAULT_GIT_TIMEOUT_S = 8

#: How long a runner remembers "the network looked unreachable" after a
#: fetch or clone actually times out, before it is willing to pay the full
#: timeout again. Without this, a disconnected cabinet re-discovers the same
#: dead network once per launchable game at start-up, each paying the full
#: timeout in serial on the sync worker. After the first timeout, further
#: network-touching calls fail instantly for the rest of the cooldown.
#: This does not schedule retries: restart the launcher to check again.
_NETWORK_RETRY_COOLDOWN_S = 60.0

#: Git subcommands that actually touch the network -- only these are subject
#: to the cooldown above. ``rev-parse``/``checkout`` are purely local and must
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


def _kill_process_tree(owner: OwnedProcess) -> None:
    """Terminate git and its remote/credential helpers, never just the parent."""
    owner.terminate(grace_s=0)


def _run_git(
    command: Sequence[str], cwd: Path | None, timeout_s: float,
    register: Callable[[OwnedProcess | None], None] | None = None,
) -> subprocess.CompletedProcess:
    """Run *command*, guaranteeing the whole process tree is gone by
    *timeout_s* -- see :func:`_kill_process_tree` for why that guarantee
    needs more than ``subprocess.run``'s own ``timeout=`` argument.
    """
    with OwnedProcess(
        list(command), cwd=cwd if cwd is not None else REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_environment(),
    ) as owner:
        process = owner.process
        assert process is not None
        if register is not None:
            register(owner)
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_tree(owner)
            stdout, stderr = process.communicate(timeout=5)
            raise subprocess.TimeoutExpired(list(command), timeout_s, output=stdout, stderr=stderr)
        finally:
            if register is not None:
                register(None)
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
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

    def cancel(self) -> None:
        """Cancel an in-flight git command and any future commands this startup."""

    def reset_cancel(self) -> None:
        """Begin a new startup after the previous worker has stopped."""


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
        # Resolve before entering a game checkout. In particular, Windows must
        # not discover a repo-supplied git.exe via its current-directory search.
        located = shutil.which(executable)
        self._executable = str(Path(located).resolve()) if located else executable
        self._timeout_s = timeout_s
        self._available: bool | None = None
        self._clock = clock
        #: Set once a fetch/clone actually times out; see item 4 and
        #: :meth:`_network_looks_unreachable`.
        self._unreachable_until: float | None = None
        self._cancelled = threading.Event()
        self._owner: OwnedProcess | None = None
        self._owner_lock = threading.RLock()

    def available(self) -> bool:
        if self._cancelled.is_set():
            return False
        if self._available is None:
            try:
                completed = _run_git([self._executable, "--version"], None, self._timeout_s, self._register)
            except (OSError, subprocess.SubprocessError):
                self._available = False
            else:
                self._available = completed.returncode == 0
        return self._available

    def run(self, args: Sequence[str], cwd: Path) -> GitResult:
        if self._cancelled.is_set():
            return GitResult(130, stderr="git preparation cancelled")
        verb = args[0] if args else ""
        if verb in _NETWORK_VERBS and self._network_looks_unreachable():
            return GitResult(
                124,
                stderr=(
                    "network looked unreachable a moment ago; skipping "
                    f"git {verb} to stay responsive (restart the launcher "
                    "to retry this game)"
                ),
            )
        # Per-command only: no system git configuration or registry changes.
        windows_options = ["-c", "core.longpaths=true"] if os.name == "nt" else []
        command = [self._executable, *windows_options, *args]
        try:
            completed = _run_git(command, cwd, self._timeout_s, self._register)
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

    def _register(self, owner: OwnedProcess | None) -> None:
        with self._owner_lock:
            self._owner = owner
        if owner is not None and self._cancelled.is_set():
            owner.terminate(grace_s=0)

    def cancel(self) -> None:
        self._cancelled.set()
        with self._owner_lock:
            owner = self._owner
        if owner is not None:
            owner.terminate(grace_s=0)

    def reset_cancel(self) -> None:
        with self._owner_lock:
            if self._owner is not None:
                raise PreparationError("previous git command is still stopping")
            self._cancelled.clear()

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
        preparation: PreparationService | None = None,
    ) -> None:
        self.root = (Path(root) if root is not None else default_cache_root()).resolve()
        self.runner: GitRunner = runner if runner is not None else SubprocessGitRunner()
        self.preparation = preparation or PreparationService(self.root / "prepared")
        if (
            self.preparation.data_root == self.root or self.root in self.preparation.data_root.parents
            or self.preparation.data_root in self.root.parents
        ):
            raise PreparationError("persistent game data must be outside the disposable repository cache")
        self._clock = clock
        self._checkout_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def checkout_guard(self, entry: GameEntry) -> Iterator[None]:
        """Keep an update and a running child from using one checkout together.

        The supervisor holds this guard for the child's entire lifetime.
        Locks are local to this cache instance, shared with the sync worker.
        """
        self._guard_launchable(entry)
        with self._locks_guard:
            lock = self._checkout_locks.get(entry.id)
            if lock is None:
                lock = threading.RLock()
                self._checkout_locks[entry.id] = lock
        with lock:
            yield

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------
    @property
    def games_dir(self) -> Path:
        return self._managed("games")

    @property
    def state_dir(self) -> Path:
        """Sync bookkeeping, kept *outside* the checkouts we manage."""
        return self._managed("state")

    def _managed(self, relative: str) -> Path:
        path = safe_relative_path(relative, self.root, game_id="cache", field_name="managed path")
        raw = self.root / relative
        while raw != self.root:
            if is_link(raw):
                raise CacheError(f"managed cache paths must not be links: {raw}")
            raw = raw.parent
        return path

    def _fixed_checkout(self, entry: GameEntry) -> Path:
        return self._managed(f"games/{entry.id}")

    def checkout_path(self, entry: GameEntry) -> Path:
        """Current checkout, or last-good after an interrupted directory swap.

        The fallback is read-only, so offline/Play resolution can still use the
        backup without performing recovery writes on the visitor's path.
        """
        destination = self._fixed_checkout(entry)
        journal = self._journal_file(entry)
        if not destination.exists() and journal.is_file():
            record = read_object(journal)
            backup = self._journal_backup(entry, record)
            if backup is not None and (backup / ".git").is_dir():
                return backup
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
        except OSError as exc:
            _log.warning("cannot inspect entrypoint for %s: %s", entry.id, exc)
            return False

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------
    def _state_file(self, entry: GameEntry) -> Path:
        return self._managed(f"state/{entry.id}.json")

    def _read_state(self, entry: GameEntry) -> dict:
        try:
            value = json.loads(self._state_file(entry).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _log.warning("cannot read optional sync bookkeeping for %s: %s", entry.id, exc)
            return {}
        if not isinstance(value, dict):
            _log.warning("optional sync bookkeeping for %s is not an object", entry.id)
            return {}
        return value

    def _write_state(self, entry: GameEntry, **values: object) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = self._read_state(entry)
        payload.update(values)
        try:
            atomic_json(self._state_file(entry), payload)
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
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", entry.id):
            raise NotLaunchableError("game id is not a safe managed directory name")
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
        state = self.verify_only(entry)
        if state.status.is_playable:
            return GameState(entry.id, GameStatus.CACHED_OFFLINE, f"{reason}; {state.detail}")
        return GameState(entry.id, GameStatus.UNAVAILABLE, f"{reason}; {state.detail}")

    def sync(self, entry: GameEntry) -> GameState:
        """Clone or refresh *entry* and report the resulting state.

        An explicit call always checks the remote; freshness is not inferred
        from a timestamp. :class:`~launcher.sync.SyncService` calls this once
        per game at launcher startup, not on gallery re-entry or game launch.
        Offline operation calls :meth:`verify_only` instead, with no git work.
        The checkout guard serializes mutation with a running child.

        Never raises for expected failures -- offline, bad ref, missing git and
        a missing entrypoint all become a :class:`~launcher.status.GameState`
        the gallery can render.

        Raises:
            NotLaunchableError: If *entry* is coming-soon. This is a programming
                error, not a runtime condition, and is deliberately loud.
        """
        with self.checkout_guard(entry):
            return self._sync(entry)

    def _sync(self, entry: GameEntry) -> GameState:
        assert entry.repository and entry.ref  # narrowed by _guard_launchable
        candidate: Path | None = None
        try:
            self._recover_promotion(entry)
            if not self.runner.available():
                return self.offline_state(entry, "git is not installed on this machine")
            checkout = self._fixed_checkout(entry)
            ignored: dict[str, str] = {}
            if self.has_checkout(entry):
                if not (checkout / ".git").is_dir():
                    raise PreparationError("refusing to update a linked worktree; use an independent managed clone")
                # Fetch only changes git objects/refs, never working files.
                fetched = self._refresh(entry, checkout)
                self._require_result(fetched)
                self._require_clean(checkout)
                head = self.runner.run(["rev-parse", "--verify", "HEAD"], cwd=checkout)
                target = self.runner.run(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=checkout)
                self._require_result(head)
                self._require_result(target)
                if not target.stdout.strip():
                    raise PreparationError("fetch did not identify a candidate commit")
                if head.stdout.strip() == target.stdout.strip():
                    # Metadata/engine/dependencies may have changed without a new
                    # source commit. Preparation writes its receipt only on success.
                    self.preparation.prepare(entry, checkout)
                    commit = self._head_commit(checkout)
                    self.mark_synced(entry, commit)
                    return self._ready_state(entry, f"verified {commit}" if commit else "verified")
                candidate = self._new_candidate(entry)
                self._require_result(self.runner.run(
                    ["init", "--initial-branch=arcade-candidate"], cwd=candidate,
                ))
                # A shallow local clone need not copy objects reachable only
                # from FETCH_HEAD. Fetch the explicit candidate ref locally
                # instead: no second GitHub request and no shared object files.
                self._require_result(self.runner.run(
                    ["fetch", "--depth", "1", "--no-tags", "--", str(checkout),
                     "refs/arcade-launcher/candidate"], cwd=candidate,
                ))
                self._require_result(self.runner.run(
                    ["checkout", "--detach", target.stdout.strip(), "--"], cwd=candidate,
                ))
                self._require_result(self.runner.run(
                    ["remote", "add", "origin", entry.repository], cwd=candidate,
                ))
                ignored = self._preserve_ignored(entry, checkout, candidate)
            else:
                if checkout.exists():
                    raise PreparationError(f"refusing to overwrite an unmanaged directory: {checkout}")
                candidate = self._new_candidate(entry)
                self._require_result(self._clone(entry, candidate))

            self.preparation.prepare(entry, candidate)
            if checkout.exists():
                # A developer or an independently running game might have changed
                # files during the long preparation step. Never discard that work.
                self._require_clean(checkout)
                if ignored != self._ignored_inventory(entry, checkout):
                    raise PreparationError("ignored user files changed during preparation; keeping last-good")
            commit = self._head_commit(candidate)
            self._promote(entry, candidate)
            self._write_state(entry, synced_at=self._clock(), commit=commit, ref=entry.ref)
            return self._ready_state(entry, f"updated {commit}" if commit else "updated")
        except (CacheError, ManifestError, OSError) as exc:
            _log.warning("candidate not promoted for %s: %s", entry.id, exc)
            return self.offline_state(entry, str(exc))
        finally:
            if candidate is not None and candidate.exists():
                # Only this invocation's private candidate can be removed.
                # Rollbacks and persistent userdata are never garbage-collected.
                remove_owned_tree(candidate)

    def _ready_state(self, entry: GameEntry, detail: str) -> GameState:
        return GameState(entry.id, GameStatus.READY, detail)

    def _clone(self, entry: GameEntry, checkout: Path) -> GitResult:
        assert entry.repository and entry.ref
        if re.fullmatch(r"[0-9a-fA-F]{40}", entry.ref):
            # --branch does not accept commit IDs. Full SHA pins use a bounded
            # fetch + detached checkout instead, still entirely in staging.
            for args in (
                ["init", "--initial-branch=arcade-candidate"],
                ["remote", "add", "origin", entry.repository],
                ["fetch", "--depth", "1", "--no-tags", "--", entry.repository, entry.ref],
                ["checkout", "--detach", "FETCH_HEAD", "--"],
            ):
                result = self.runner.run(args, cwd=checkout)
                if not result.ok:
                    return result
            return result
        return self.runner.run(
            [
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                entry.ref,
                "--",
                entry.repository,
                str(checkout),
            ],
            cwd=checkout.parent,
        )

    def _refresh(self, entry: GameEntry, checkout: Path) -> GitResult:
        """Fetch a candidate, **never** reset or clean the working checkout."""
        assert entry.ref and entry.repository
        return self.runner.run(
            ["fetch", "--depth", "1", "--no-tags", "--force", "--", entry.repository,
             f"{entry.ref}:refs/arcade-launcher/candidate"],
            cwd=checkout,
        )

    @staticmethod
    def _require_result(result: GitResult) -> None:
        if not result.ok:
            raise PreparationError(result.message())

    def _require_clean(self, checkout: Path) -> None:
        result = self.runner.run(["status", "--porcelain=v1", "--untracked-files=all"], cwd=checkout)
        self._require_result(result)
        if result.stdout.strip():
            raise PreparationError("local source edits/untracked files preserved; update not promoted")

    def _new_candidate(self, entry: GameEntry) -> Path:
        staging = self._managed("staging")
        staging.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{entry.id}-", dir=staging))

    def _ignored_inventory(self, entry: GameEntry, checkout: Path) -> dict[str, str]:
        result = self.runner.run(
            ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=checkout,
        )
        self._require_result(result)
        files: dict[str, str] = {}
        for raw in result.stdout.split("\0"):
            if not raw:
                continue
            path = safe_relative_path(raw, checkout, game_id=entry.id, field_name="ignored user file")
            unresolved = checkout / raw
            while unresolved != checkout:
                if is_link(unresolved):
                    raise PreparationError(f"refusing to copy linked ignored data: {raw}")
                unresolved = unresolved.parent
            if not filesystem_path(path).is_file():
                raise PreparationError(f"ignored user file is not a regular file: {raw}")
            files[raw] = file_digest(path)
        return files

    def _preserve_ignored(self, entry: GameEntry, source: Path, candidate: Path) -> dict[str, str]:
        files = self._ignored_inventory(entry, source)
        for relative in files:
            old = safe_relative_path(relative, source, game_id=entry.id, field_name="ignored user file")
            new = safe_relative_path(relative, candidate, game_id=entry.id, field_name="preserved user file")
            if filesystem_path(new).exists():
                raise PreparationError(f"candidate would overwrite ignored user data: {relative}")
            filesystem_path(new.parent).mkdir(parents=True, exist_ok=True)
            shutil.copy2(filesystem_path(old), filesystem_path(new))
        return files

    def _journal_file(self, entry: GameEntry) -> Path:
        return self._managed(f"state/{entry.id}.promotion.json")

    def _journal_backup(self, entry: GameEntry, record: dict) -> Path | None:
        relative = record.get("backup")
        if record.get("format") != 1 or (
            relative is not None and (
                not isinstance(relative, str) or not relative.startswith(f"rollback/{entry.id}-")
            )
        ):
            raise PreparationError(f"invalid promotion journal for {entry.id}")
        return self._managed(relative) if relative is not None else None

    def _recover_promotion(self, entry: GameEntry) -> None:
        journal = self._journal_file(entry)
        if not journal.is_file():
            return
        record = read_object(journal)
        backup = self._journal_backup(entry, record)
        checkout = self._fixed_checkout(entry)
        if not checkout.exists() and backup is not None and backup.is_dir():
            backup.replace(checkout)
            _log.warning("restored last-good checkout after interrupted promotion: %s", entry.id)
        if checkout.exists():
            # An intact receipt is required before declaring a half-finished
            # promotion complete. Leave an invalid journal for explicit recovery.
            self.preparation.resolve(entry, checkout)
            journal.unlink()

    def _promote(self, entry: GameEntry, candidate: Path) -> None:
        checkout = self._fixed_checkout(entry)
        self.games_dir.mkdir(parents=True, exist_ok=True)
        backup = None
        if checkout.exists():
            backup = self._managed(f"rollback/{entry.id}-{uuid.uuid4().hex}")
            backup.parent.mkdir(parents=True, exist_ok=True)
        journal = self._journal_file(entry)
        atomic_json(journal, {
            "format": 1, "backup": backup.relative_to(self.root).as_posix() if backup else None,
        })
        if backup is not None:
            checkout.replace(backup)
        try:
            candidate.replace(checkout)
            self.preparation.resolve(entry, checkout)
        except (OSError, CacheError, ManifestError):
            if checkout.exists():
                checkout.replace(candidate)
            if backup is not None:
                backup.replace(checkout)
            journal.unlink()
            raise
        journal.unlink()

    def prepared_game(self, entry: GameEntry) -> PreparedGame:
        """Public, disk-only launch boundary returning the actual last-good build."""
        self._guard_launchable(entry)
        if not self.has_checkout(entry):
            raise PreparationError("not downloaded yet - no cached copy")
        return self.preparation.resolve(entry, self.checkout_path(entry))

    def cancel_preparation(self) -> None:
        """Stop downloads/imports/pip work when the launcher is shutting down."""
        self.preparation.cancel()
        self.runner.cancel()

    def begin_startup(self) -> None:
        """Reset cancellation only when a new worker starts, never on Play."""
        self.preparation.reset_cancel()
        self.runner.reset_cancel()

    def prepare_cached(self, entry: GameEntry, *, allow_download: bool = False) -> GameState:
        """Operator-only local preparation; never called by Play or offline startup."""
        with self.checkout_guard(entry):
            try:
                self._recover_promotion(entry)
                if not self.has_checkout(entry):
                    raise PreparationError("no cached checkout to prepare")
                self.preparation.prepare(entry, self.checkout_path(entry), allow_download=allow_download)
                return self._ready_state(entry, "prepared cached source")
            except (CacheError, ManifestError, OSError) as exc:
                _log.warning("cached preparation failed for %s: %s", entry.id, exc)
                return self.offline_state(entry, str(exc))

    def rollback(self, entry: GameEntry, backup: Path) -> GameState:
        """Operator-only offline rollback, retaining the newest ignored saves.

        Only this cache's rollback directories for this game are accepted.
        No fetch, dependency install, import or engine download is performed.
        The backup is copied, not consumed; current source edits block rollback.
        """
        with self.checkout_guard(entry):
            candidate: Path | None = None
            try:
                self._recover_promotion(entry)
                try:
                    relative = backup.resolve().relative_to(self.root).as_posix()
                except ValueError as exc:
                    raise PreparationError("rollback must be inside this managed cache") from exc
                saved = self._managed(relative)
                if (
                    saved.parent != self._managed("rollback")
                    or not saved.name.startswith(f"{entry.id}-")
                    or not (saved / ".git").is_dir()
                ):
                    raise PreparationError(f"not a rollback checkout for '{entry.id}'")
                checkout = self._fixed_checkout(entry)
                self._require_clean(checkout)
                self.preparation.resolve(entry, saved)
                candidate = self._new_candidate(entry)
                shutil.copytree(
                    filesystem_path(saved), filesystem_path(candidate),
                    dirs_exist_ok=True, symlinks=True,
                )
                # Old ignored saves must not overwrite progress made since the
                # update. Delete only this private copy's ignored files, then
                # carry the current ignored files forward into the rollback.
                for raw in self._ignored_inventory(entry, candidate):
                    path = safe_relative_path(raw, candidate, game_id=entry.id, field_name="old ignored data")
                    filesystem_path(path).unlink()
                ignored = self._preserve_ignored(entry, checkout, candidate)
                self.preparation.resolve(entry, candidate)
                self._require_clean(checkout)
                if ignored != self._ignored_inventory(entry, checkout):
                    raise PreparationError("user data changed during rollback; keeping the current release")
                commit = self._head_commit(candidate)
                self._promote(entry, candidate)
                self.mark_synced(entry, commit)
                return self._ready_state(entry, f"rolled back to {commit}; current user data preserved")
            except (CacheError, ManifestError, OSError) as exc:
                _log.warning("rollback refused for %s: %s", entry.id, exc)
                return self.offline_state(entry, str(exc))
            finally:
                if candidate is not None and candidate.exists():
                    remove_owned_tree(candidate)

    def _head_commit(self, checkout: Path) -> str:
        result = self.runner.run(["rev-parse", "--short", "HEAD"], cwd=checkout)
        return result.stdout.strip() if result.ok else ""

    def verify_only(self, entry: GameEntry) -> GameState:
        """Classify *entry* using only what is already on disk (no network).

        Used when background syncing is switched off, and as the pre-launch
        safety check.
        """
        self._guard_launchable(entry)
        try:
            self.prepared_game(entry)
            commit = self.last_commit(entry)
            current = self.preparation.matches(entry, self.checkout_path(entry))
            detail = f"using cached copy{f' {commit}' if commit else ''}"
            if not current:
                detail += " (last-good launch metadata; requested update not prepared)"
            return GameState(entry.id, GameStatus.CACHED_OFFLINE, detail)
        except (CacheError, ManifestError, OSError) as exc:
            return GameState(entry.id, GameStatus.UNAVAILABLE, str(exc))

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
