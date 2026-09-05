"""Background repository synchronisation.

The gallery must stay at 60 FPS while StreetFighter is being cloned over the
club's Wi-Fi, so all git work happens on a worker thread.  The UI never blocks:
it drains :meth:`SyncService.drain` once per frame and repaints.

Coming-soon games are rejected at the queueing boundary, which is what makes
"disabled games trigger zero network requests" testable: the service simply
refuses to accept them.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Iterable

from .cache import RepositoryCache
from .errors import CacheError, NotLaunchableError
from .manifest import GameEntry, Manifest
from .status import GameState, GameStatus

__all__ = ["SyncService", "initial_states"]

_log = logging.getLogger(__name__)

_STOP = object()


def initial_states(manifest: Manifest, *, sync_enabled: bool = True) -> dict[str, GameState]:
    """Build the starting state map for every entry in *manifest*.

    Coming-soon entries are pinned to :attr:`GameStatus.COMING_SOON` here and
    never change, which keeps them out of every code path that talks to git.
    """
    states: dict[str, GameState] = {}
    for entry in manifest:
        if not entry.launchable:
            states[entry.id] = GameState(
                entry.id, GameStatus.COMING_SOON, entry.note or "In development"
            )
        elif sync_enabled:
            states[entry.id] = GameState(entry.id, GameStatus.PENDING, "queued for update")
        else:
            states[entry.id] = GameState(entry.id, GameStatus.PENDING, "checking cache")
    return states


class SyncService:
    """A single-worker background synchroniser.

    Args:
        cache: The repository cache to drive.
        online: When ``False`` the worker only verifies what is already on disk
            and never touches the network.

    The service owns one daemon thread.  :meth:`stop` is idempotent and is
    safe to call from a signal handler or a ``finally`` block.
    """

    def __init__(self, cache: RepositoryCache, *, online: bool = True) -> None:
        self._cache = cache
        self._online = online
        self._requests: "queue.Queue[object]" = queue.Queue()
        self._results: "queue.Queue[GameState]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the worker thread (no-op if it is already running)."""
        if self.is_running:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._work, name="arcade-sync", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the worker to finish and join it.

        A git command already in flight is bounded by the runner's own timeout,
        so the join is bounded too.  The thread is a daemon, so even a
        pathological hang cannot keep the process alive.
        """
        self._stopping.set()
        self._requests.put(_STOP)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                _log.warning("sync worker did not stop within %.1fs", timeout)

    def __enter__(self) -> "SyncService":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------
    def request(self, entry: GameEntry) -> None:
        """Queue *entry* for synchronisation.

        Raises:
            NotLaunchableError: If *entry* is coming-soon. Disabled games must
                never reach the network, so this is refused loudly.
        """
        if not entry.launchable:
            raise NotLaunchableError(
                f"game '{entry.id}' is coming-soon and must never be synchronised"
            )
        self._results.put(GameState(entry.id, GameStatus.UPDATING, "contacting GitHub"))
        self._requests.put(entry)

    def request_all(self, entries: Iterable[GameEntry]) -> int:
        """Queue every *launchable* entry; coming-soon entries are skipped.

        Returns:
            How many entries were queued.
        """
        queued = 0
        for entry in entries:
            if entry.launchable:
                self.request(entry)
                queued += 1
        return queued

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def drain(self) -> list[GameState]:
        """Return every state update produced since the last call."""
        updates: list[GameState] = []
        while True:
            try:
                updates.append(self._results.get_nowait())
            except queue.Empty:
                return updates

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until every queued request has been processed.

        Returns:
            ``True`` if the queue drained within *timeout*, else ``False``.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._requests.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._requests.unfinished_tasks == 0

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _work(self) -> None:
        while not self._stopping.is_set():
            item = self._requests.get()
            try:
                if item is _STOP:
                    return
                entry = item  # type: ignore[assignment]
                self._results.put(self._sync_one(entry))
            finally:
                self._requests.task_done()

    def _sync_one(self, entry: GameEntry) -> GameState:
        try:
            if self._online:
                return self._cache.sync(entry)
            return self._cache.verify_only(entry)
        except NotLaunchableError:
            # A coming-soon entry reached the worker: a real bug, not a runtime
            # condition. Re-raising here would kill the worker silently, so it
            # is reported as an unavailable state *and* logged loudly.
            _log.error("refused to synchronise coming-soon game %s", entry.id)
            return GameState(entry.id, GameStatus.COMING_SOON, "not available yet")
        except CacheError as exc:
            _log.warning("sync failed for %s: %s", entry.id, exc)
            return GameState(entry.id, GameStatus.UNAVAILABLE, str(exc))
        except OSError as exc:
            _log.warning("filesystem error syncing %s: %s", entry.id, exc)
            return GameState(entry.id, GameStatus.UNAVAILABLE, f"disk error: {exc}")
