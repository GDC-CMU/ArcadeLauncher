"""Grid -- the 3x2 cabinet board.

Composition: a full-width marquee band across the top, six equal cards laid out
as a board, and a two-row control legend along the bottom.  Everything is
visible at once, and the stick moves in two real dimensions.
"""

from __future__ import annotations

import math
from typing import ClassVar

from ...input_state import Direction
from ...viewmodes import ViewMode
from .. import SCREEN_HEIGHT, SCREEN_WIDTH
from ..components import (
    RenderContext,
    card_cover,
    draw_control_legend,
    draw_marquee,
    draw_mode_chip,
    draw_toast,
)
from ..effects import ease_out_cubic
from ..pygame_runtime import pygame
from ..theme import PALETTE, shade
from ..viewmodel import GalleryFrame
from .base import GalleryView, register

__all__ = ["GridView"]

COLUMNS = 3
ROWS = 2

HEADER = pygame.Rect(0, 0, SCREEN_WIDTH, 92)
STRIP = pygame.Rect(24, 98, SCREEN_WIDTH - 48, 44)
LEGEND = pygame.Rect(16, 506, SCREEN_WIDTH - 32, 82)

CARD_MARGIN_X = 26
CARD_GAP_X = 17
CARD_GAP_Y = 12
CARD_TOP = 156
CARD_WIDTH = (SCREEN_WIDTH - CARD_MARGIN_X * 2 - CARD_GAP_X * (COLUMNS - 1)) // COLUMNS
CARD_HEIGHT = 163


class GridView(GalleryView):
    """A polished 3x2 board with natural two-dimensional navigation."""

    mode: ClassVar[ViewMode] = ViewMode.GRID
    columns: ClassVar[int] = COLUMNS

    def navigate(self, index: int, count: int, direction: Direction) -> int:
        """Move by one column or one row, wrapping on both axes."""
        if count <= 0:
            return 0
        rows = max(1, math.ceil(count / COLUMNS))
        row, column = divmod(index, COLUMNS)

        if direction is Direction.LEFT:
            column -= 1
        elif direction is Direction.RIGHT:
            column += 1
        elif direction is Direction.UP:
            row -= 1
        else:
            row += 1

        if column < 0:
            column = COLUMNS - 1
        elif column >= COLUMNS:
            column = 0
        row %= rows

        target = row * COLUMNS + column
        if target >= count:
            # Short final row: fall back to the last real card in that column.
            target = min(count - 1, target)
        return target

    @staticmethod
    def card_rect(index: int) -> pygame.Rect:
        """Return the board slot for *index* (0-5, reading order)."""
        row, column = divmod(index, COLUMNS)
        return pygame.Rect(
            CARD_MARGIN_X + column * (CARD_WIDTH + CARD_GAP_X),
            CARD_TOP + row * (CARD_HEIGHT + CARD_GAP_Y),
            CARD_WIDTH,
            CARD_HEIGHT,
        )

    def draw(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        self._draw_header(surface, ctx, frame)
        self.draw_status_strip(surface, ctx, STRIP, frame)

        grow = int(6 * ease_out_cubic(min(1.0, frame.focus_ms / 220.0)))
        for index, card in enumerate(frame.cards):
            rect = self.card_rect(index)
            selected = index == frame.selected_index
            if selected:
                rect = rect.inflate(grow * 2, grow * 2)
            card_cover(
                surface,
                ctx,
                rect,
                card,
                selected=selected,
                time_ms=frame.time_ms,
                title_scale=2,
                badge_scale=1,
                pixel=3,
            )

        draw_control_legend(surface, ctx, LEGEND, layout="bar")

        if frame.toast is not None:
            draw_toast(
                surface, ctx, (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2), frame.toast, frame.time_ms
            )

    def _draw_header(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        pygame.draw.rect(surface, shade(PALETTE["night"], 1.12), HEADER)
        pygame.draw.rect(
            surface,
            PALETTE["cmu_red"],
            pygame.Rect(0, HEADER.bottom - 4, SCREEN_WIDTH, 4),
        )
        pygame.draw.rect(
            surface,
            PALETTE["warm_amber"],
            pygame.Rect(0, HEADER.bottom - 4, 240, 4),
        )
        draw_marquee(
            surface,
            ctx,
            pygame.Rect(HEADER.left + 26, HEADER.top, 520, HEADER.height - 4),
            logo_height=56,
            title_scale=3,
        )
        draw_mode_chip(
            surface, ctx, (SCREEN_WIDTH - 26, HEADER.top + 26), frame.view_mode
        )


register(GridView())
