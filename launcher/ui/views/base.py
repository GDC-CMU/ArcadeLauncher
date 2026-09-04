"""Base class and registry for gallery views."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ...input_state import Direction
from ...status import GameStatus
from ...viewmodes import ViewMode
from ..components import RenderContext, draw_notice
from ..pygame_runtime import pygame
from ..theme import PALETTE, STATUS_COLORS, mix
from ..viewmodel import GalleryFrame

__all__ = ["GalleryView", "VIEWS", "view_for", "register", "SUMMARY_BUCKETS"]

#: How the summary line groups the status vocabulary, in display order.
#:
#: The buckets *partition* :class:`~launcher.status.GameStatus`: every status
#: belongs to exactly one, so the printed counts always add up to the number of
#: games.  A status that fell through would silently make the line lie -- "6
#: GAMES 2 PLAYABLE 2 SOON" reads as if two games had vanished -- so the
#: partition is asserted by :mod:`tests.test_views`.
SUMMARY_BUCKETS: tuple[tuple[str, tuple[GameStatus, ...]], ...] = (
    ("PLAYABLE", (GameStatus.READY, GameStatus.CACHED_OFFLINE)),
    ("UPDATING", (GameStatus.PENDING, GameStatus.UPDATING)),
    ("UNAVAILABLE", (GameStatus.UNAVAILABLE,)),
    ("SOON", (GameStatus.COMING_SOON,)),
)


class GalleryView(ABC):
    """One composition of the gallery.

    Subclasses implement :meth:`draw` and may override :meth:`navigate`.  The
    default navigation is one-dimensional with wrap-around, which is what the
    two horizontal modes want; :class:`~launcher.ui.views.grid.GridView`
    overrides it for true 2-D movement.
    """

    mode: ClassVar[ViewMode]
    #: Number of columns the mode presents; used by the default navigation.
    columns: ClassVar[int] = 1

    @abstractmethod
    def draw(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        """Render *frame* onto *surface*."""

    def navigate(self, index: int, count: int, direction: Direction) -> int:
        """Return the new selection after a *direction* step.

        In the one-dimensional modes every direction moves along the row, so a
        visitor pushing the arcade stick up or down still gets a response
        instead of a dead control.
        """
        if count <= 0:
            return 0
        step = -1 if direction in (Direction.LEFT, Direction.UP) else 1
        return (index + step) % count

    # -- shared helpers -------------------------------------------------
    @staticmethod
    def summary(frame: GalleryFrame) -> str:
        """A short, honest line describing the gallery as a whole.

        Only non-empty buckets are printed, so a healthy cabinet reads
        ``6 GAMES  1 PLAYABLE  5 SOON`` rather than padding the line with
        zeroes.  Because the buckets partition the status vocabulary, whatever
        is printed always sums to the game count.
        """
        counts = {
            label: sum(1 for card in frame.cards if card.status in statuses)
            for label, statuses in SUMMARY_BUCKETS
        }
        parts = [f"{count} {label}" for label, count in counts.items() if count]
        return "  ".join([f"{frame.count} GAMES", *parts])

    def draw_status_strip(
        self,
        surface: pygame.Surface,
        ctx: RenderContext,
        rect: pygame.Rect,
        frame: GalleryFrame,
    ) -> None:
        """Draw the fixed-height strip that holds a banner or the summary.

        The strip is always present, so a banner appearing after a failed game
        can never push the rest of the composition around or clip it.
        """
        if frame.notice is not None:
            draw_notice(surface, ctx, rect, frame.notice)
            return

        accent = STATUS_COLORS[frame.selected.status]
        pygame.draw.rect(surface, mix(PALETTE["night"], accent, 0.10), rect, border_radius=4)
        pygame.draw.rect(
            surface, PALETTE["warm_amber"], pygame.Rect(rect.left, rect.top, 4, rect.height)
        )
        summary = self.summary(frame)
        ctx.pixel.draw(
            surface,
            summary,
            (rect.left + 16, rect.centery),
            2,
            PALETTE["steel"],
            anchor="midleft",
        )

        trailing = "SYNCING IN BACKGROUND" if frame.syncing else frame.selected.detail.upper()
        if not trailing:
            return
        colour = (
            PALETTE["electric_cyan"] if frame.syncing else mix(PALETTE["steel"], accent, 0.6)
        )
        room = rect.width - 32 - ctx.pixel.measure(summary, 2)[0] - 24
        while trailing and ctx.pixel.measure(trailing, 2)[0] > room:
            trailing = trailing[:-1]
        if len(trailing) >= 6:
            ctx.pixel.draw(
                surface,
                trailing.rstrip(),
                (rect.right - 16, rect.centery),
                2,
                colour,
                anchor="midright",
            )


#: Registry populated by :func:`register`, used by the scene renderer.
VIEWS: dict[ViewMode, GalleryView] = {}


def register(view: GalleryView) -> GalleryView:
    """Add *view* to the registry, keyed by its mode."""
    VIEWS[view.mode] = view
    return view


def view_for(mode: ViewMode) -> GalleryView:
    """Return the view implementing *mode*.

    Raises:
        KeyError: If no view is registered for *mode*.
    """
    try:
        return VIEWS[mode]
    except KeyError:
        raise KeyError(f"no gallery view registered for {mode}") from None
