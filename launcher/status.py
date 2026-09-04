"""Availability states shared between the cache layer and the renderer.

Keeping this in its own module means the UI can be tested without importing
git/subprocess code, and the cache can be tested without importing Pygame.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["GameStatus", "GameState", "Notice"]


class GameStatus(Enum):
    """Lifecycle state of a single manifest entry.

    ``value`` is the short badge label rendered on a card; it is deliberately
    upper-case and terse so it stays readable from several feet away.
    """

    #: Curated but intentionally not playable. Never cloned, never launched.
    COMING_SOON = "COMING SOON"
    #: Queued for a background sync but not started yet.
    PENDING = "QUEUED"
    #: A background clone/fetch is currently running.
    UPDATING = "UPDATING"
    #: Checkout is present and up to date; the game can be launched.
    READY = "READY"
    #: Refresh failed but a previously cached checkout is still usable.
    CACHED_OFFLINE = "CACHED OFFLINE"
    #: No usable checkout. Cannot be launched; an error message is shown.
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def is_playable(self) -> bool:
        """Whether a launch request for this state may start a child process."""
        return self in (GameStatus.READY, GameStatus.CACHED_OFFLINE)

    @property
    def is_busy(self) -> bool:
        """Whether the launcher is still working on this entry."""
        return self in (GameStatus.PENDING, GameStatus.UPDATING)

    @property
    def requires_sync(self) -> bool:
        """Whether reaching this state means the game was queued for git.

        Coming-soon entries are pinned to :attr:`COMING_SOON` by
        :func:`launcher.sync.initial_states` and never leave it, because the
        cache refuses to touch them at all.  Every *other* status is therefore
        only reachable for a launchable entry -- a fact the preview tool
        asserts, so documentation screenshots cannot depict a state the
        launcher structurally forbids.
        """
        return self is not GameStatus.COMING_SOON


@dataclass(frozen=True, slots=True)
class GameState:
    """Immutable snapshot of one game's availability.

    Attributes:
        game_id: Stable manifest id.
        status: Current :class:`GameStatus`.
        detail: Short human-readable explanation shown under the badge.
            Empty string when there is nothing to add.
    """

    game_id: str
    status: GameStatus
    detail: str = ""

    def with_status(self, status: GameStatus, detail: str = "") -> "GameState":
        """Return a copy carrying a new status/detail pair."""
        return GameState(game_id=self.game_id, status=status, detail=detail)


@dataclass(frozen=True, slots=True)
class Notice:
    """A short banner shown at the top of the gallery.

    Produced by the supervisor (a game exited badly, the gallery restarted) and
    rendered by the UI, so it lives here rather than in either of them.
    """

    level: str  # "error" | "info"
    title: str
    detail: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == "error"
