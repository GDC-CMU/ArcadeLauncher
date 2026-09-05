"""The renderer: shared background, view dispatch, vignette finish.

Views draw the *content*; the scene owns everything that is identical in all
three modes -- the deep gradient field and the vignette that goes on last.
Both are built once and cached, so a frame costs two blits regardless of how
long the gallery has been running.
"""

from __future__ import annotations

from ..viewmodes import ViewMode
from . import SCREEN_SIZE
from .components import RenderContext
from .effects import vertical_gradient, vignette
from .pygame_runtime import pygame
from .theme import PALETTE, mix, shade
from .viewmodel import GalleryFrame
from .views import GalleryView, view_for

__all__ = ["Renderer"]


class Renderer:
    """Draws a :class:`~launcher.ui.viewmodel.GalleryFrame` onto a surface."""

    def __init__(self, ctx: RenderContext | None = None) -> None:
        self.ctx = ctx if ctx is not None else RenderContext()

    # -- background -----------------------------------------------------
    def background(self, size: tuple[int, int]) -> pygame.Surface:
        """Return the cached arcade backdrop for *size*.

        Public so documentation tooling can render auxiliary sheets on the
        same backdrop the gallery uses, instead of copying the recipe and
        letting the two drift apart.
        """

        def build() -> pygame.Surface:
            field = vertical_gradient(
                size,
                shade(PALETTE["deep_violet"], 0.9),
                PALETTE["void"],
                bands=40,
            )
            # A soft glow behind the middle of the screen keeps the eye where
            # the content is.  It is built as two mirrored gradients so there
            # is no hard seam where the band starts.
            band_height = size[1] // 2
            top_half = vertical_gradient(
                (size[0], band_height // 2),
                PALETTE["void"],
                mix(PALETTE["deep_cyan"], PALETTE["void"], 0.75),
                bands=20,
            )
            bottom_half = pygame.transform.flip(top_half, False, True)
            band = pygame.Surface((size[0], band_height))
            band.blit(top_half, (0, 0))
            band.blit(bottom_half, (0, band_height // 2))
            band.set_alpha(58)
            field.blit(band, (0, size[1] // 2 - band_height // 2))
            return field

        return self.ctx.cache.get(("bg", size), build)

    def overlays(self, size: tuple[int, int]) -> pygame.Surface:
        """Return the cached vignette for *size*.

        The CRT scanline grid this used to layer on top read as visual noise
        rather than atmosphere -- gone in favour of a plain, deliberate dark
        field that lets card art and titles do the work.
        """
        return self.ctx.cache.get(("vignette", size), lambda: vignette(size))

    # -- frame ----------------------------------------------------------
    def draw(self, surface: pygame.Surface, frame: GalleryFrame) -> None:
        """Render one complete frame of the gallery onto *surface*."""
        size = surface.get_size()
        surface.blit(self.background(size), (0, 0))
        view_for(frame.view_mode).draw(surface, self.ctx, frame)
        surface.blit(self.overlays(size), (0, 0))

    def render(self, frame: GalleryFrame, size: tuple[int, int] = SCREEN_SIZE) -> pygame.Surface:
        """Return a freshly drawn surface -- used by the preview tool."""
        surface = pygame.Surface(size)
        self.draw(surface, frame)
        return surface

    @staticmethod
    def view(mode: ViewMode) -> GalleryView:
        """Expose the view object for a mode (navigation lives there too)."""
        return view_for(mode)
