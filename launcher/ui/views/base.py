"""Base class and registry for gallery views."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ...input_state import Direction
from ...viewmodes import ViewMode
from ..components import RenderContext, draw_notice
from ..pygame_runtime import pygame
from ..viewmodel import GalleryFrame

__all__ = ["GalleryView", "VIEWS", "view_for", "register"]


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
    def draw_banner(
        self,
        surface: pygame.Surface,
        ctx: RenderContext,
        rect: pygame.Rect,
        frame: GalleryFrame,
    ) -> bool:
        """Draw the supervisor's banner if one is pending, and report whether
        anything was drawn.

        This used to always draw *something* -- a banner, or else a permanent
        summary tally and an ambient "syncing" strip.  The tally never told a
        visitor anything they could act on and read as clutter, so it is
        gone; per-card status badges are where availability belongs now.  The
        banner stays: it is event-driven feedback for a real thing that just
        happened (a game crashed, the gallery restarted), not ambient noise.
        """
        if frame.notice is None:
            return False
        draw_notice(surface, ctx, rect, frame.notice)
        return True


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
