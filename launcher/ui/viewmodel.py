"""The immutable frame handed to a view.

A view never reads the manifest, the cache, or the clock -- it is given a
:class:`GalleryFrame` and draws it.  That is what lets the preview tool render
the real UI code deterministically, and what lets the tests assert on layout
without running an event loop.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..manifest import GameEntry, Manifest
from ..status import GameState, GameStatus, Notice
from ..viewmodes import ViewMode

__all__ = ["Card", "Toast", "GalleryFrame"]


@dataclass(frozen=True, slots=True)
class Card:
    """One game plus its current availability."""

    entry: GameEntry
    state: GameState

    @property
    def status(self) -> GameStatus:
        return self.state.status

    @property
    def title(self) -> str:
        return self.entry.title

    @property
    def detail(self) -> str:
        return self.state.detail


@dataclass(frozen=True, slots=True)
class Toast:
    """Brief centred feedback -- e.g. bumping into a Coming Soon card."""

    text: str
    detail: str = ""
    started_ms: int = 0
    duration_ms: int = 1500

    def progress(self, now_ms: int) -> float:
        """0.0 at the moment it appeared, 1.0 when it should be gone."""
        if self.duration_ms <= 0:
            return 1.0
        return max(0.0, min(1.0, (now_ms - self.started_ms) / self.duration_ms))

    def is_expired(self, now_ms: int) -> bool:
        return self.progress(now_ms) >= 1.0

    def alpha(self, now_ms: int) -> int:
        """Fade in quickly, hold, then fade out."""
        t = self.progress(now_ms)
        if t >= 1.0:
            return 0
        if t < 0.12:
            return int(255 * (t / 0.12))
        if t > 0.72:
            return int(255 * (1.0 - (t - 0.72) / 0.28))
        return 255


@dataclass(frozen=True, slots=True)
class GalleryFrame:
    """Everything a view needs for exactly one frame.

    Attributes:
        cards: All six games, in manifest order.
        selected_index: Index into :attr:`cards`.
        view_mode: Which composition is active.
        time_ms: Animation clock. The preview tool pins this so screenshots
            are byte-stable.
        scroll: Smoothed floating-point selection used by the horizontal
            modes, so movement glides rather than snapping.
        focus_ms: Milliseconds since the selection last changed; drives the
            focus-in animation.
        notice: Optional banner from the supervisor.
        toast: Optional transient feedback.
    """

    cards: tuple[Card, ...]
    selected_index: int = 0
    view_mode: ViewMode = ViewMode.CAROUSEL
    time_ms: int = 0
    scroll: float = 0.0
    focus_ms: int = 0
    notice: Notice | None = None
    toast: Toast | None = None

    def __post_init__(self) -> None:
        if not self.cards:
            raise ValueError("a gallery frame needs at least one card")
        if not 0 <= self.selected_index < len(self.cards):
            raise IndexError(
                f"selected_index {self.selected_index} outside 0..{len(self.cards) - 1}"
            )

    @property
    def selected(self) -> Card:
        return self.cards[self.selected_index]

    @property
    def count(self) -> int:
        return len(self.cards)

    def with_mode(self, mode: ViewMode) -> "GalleryFrame":
        """Switch composition, keeping the selection -- criterion D7."""
        return replace(self, view_mode=mode)

    def with_selection(self, index: int) -> "GalleryFrame":
        return replace(self, selected_index=index % len(self.cards))

    @classmethod
    def build(
        cls,
        manifest: Manifest,
        states: dict[str, GameState],
        *,
        selected_index: int = 0,
        view_mode: ViewMode = ViewMode.CAROUSEL,
        **extra: object,
    ) -> "GalleryFrame":
        """Assemble a frame from the manifest plus the current state map."""
        cards = tuple(
            Card(
                entry=entry,
                state=states.get(entry.id)
                or GameState(entry.id, GameStatus.PENDING, ""),
            )
            for entry in manifest
        )
        return cls(
            cards=cards,
            selected_index=selected_index,
            view_mode=view_mode,
            **extra,  # type: ignore[arg-type]
        )
