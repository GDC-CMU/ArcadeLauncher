"""Grid -- the cabinet board.

Composition: the shared marquee header (identical across all three views) and
a board of equal cards beneath it, sized to fill the freed space now that the
permanent status strip and control legend are gone. Everything on the current
page is visible at once, and the stick moves in two real dimensions.

The board is a fixed 3x2 shape, so it *paginates* rather than growing without
bound: a catalogue of 20 games is four pages of up to six, not twenty cards
squeezed below the visible area. Left/right and up/down both move in reading
order across the whole catalogue -- crossing a row or page edge simply
continues onto the next slot rather than dead-ending -- so navigation always
reaches every game regardless of how many the club has published, and a
subtle dot row only appears once there is more than one page to indicate.

A page that does not fill the whole 3x2 board -- the last page of a catalogue
whose size is not a multiple of six, or a short final row on an otherwise full
page -- is centred as a block on both axes, and each row is centred
independently of the others. Left-aligning it instead would leave a lone card
(or a short row) stranded in the top-left corner, which reads as a failed
render rather than a deliberate layout. A full page needs no such nudge: its
block already spans the content area up to a sub-pixel rounding remainder, so
the centring offset for it is always zero and :func:`card_rect` is untouched.
"""

from __future__ import annotations

import math
from typing import ClassVar

from ...input_state import Direction
from ...viewmodes import ViewMode
from .. import SCREEN_HEIGHT, SCREEN_WIDTH
from ..components import (
    HEADER_RECT,
    RenderContext,
    card_cover,
    draw_gallery_header,
    draw_position_dots,
    draw_toast,
)
from ..effects import ease_out_cubic
from ..pygame_runtime import pygame
from ..viewmodel import GalleryFrame
from .base import GalleryView, register

__all__ = ["GridView"]

COLUMNS = 3
ROWS = 2
PAGE_SIZE = COLUMNS * ROWS

BANNER = pygame.Rect(24, HEADER_RECT.bottom + 8, SCREEN_WIDTH - 48, 40)
PAGE_DOTS_Y = SCREEN_HEIGHT - 20

CARD_MARGIN_X = 30
CARD_GAP_X = 20
CARD_GAP_Y = 18
CARD_TOP = BANNER.bottom + 14
CARD_BOTTOM_MARGIN = 30
CARD_WIDTH = (SCREEN_WIDTH - CARD_MARGIN_X * 2 - CARD_GAP_X * (COLUMNS - 1)) // COLUMNS
CARD_HEIGHT = (SCREEN_HEIGHT - CARD_TOP - CARD_BOTTOM_MARGIN - CARD_GAP_Y * (ROWS - 1)) // ROWS

#: The area cards are laid out within -- everything below the banner, above
#: the bottom margin, and inset from both sides.
CONTENT_WIDTH = SCREEN_WIDTH - CARD_MARGIN_X * 2
CONTENT_HEIGHT = SCREEN_HEIGHT - CARD_TOP - CARD_BOTTOM_MARGIN


def _row_columns(count_on_page: int, row: int) -> int:
    """How many cards actually occupy *row* on a page of *count_on_page*."""
    return max(0, min(COLUMNS, count_on_page - row * COLUMNS))


def _center_offset(cols_in_row: int, rows_used: int) -> tuple[int, int]:
    """``(dx, dy)`` that centres a *cols_in_row* by *rows_used* block of cards
    within the fixed content area.

    A full row/column count already spans the content area up to a rounding
    remainder of a pixel or two (:data:`CARD_WIDTH`/:data:`CARD_HEIGHT` are
    themselves floor-divided), so this floors to ``(0, 0)`` for a full page --
    the common case is a genuine no-op rather than a special case.
    """
    block_width = cols_in_row * CARD_WIDTH + max(0, cols_in_row - 1) * CARD_GAP_X
    block_height = rows_used * CARD_HEIGHT + max(0, rows_used - 1) * CARD_GAP_Y
    dx = (CONTENT_WIDTH - block_width) // 2
    dy = (CONTENT_HEIGHT - block_height) // 2
    return dx, dy


class GridView(GalleryView):
    """A board of cards, paginated so any catalogue size stays on-screen."""

    mode: ClassVar[ViewMode] = ViewMode.GRID
    columns: ClassVar[int] = COLUMNS

    def navigate(self, index: int, count: int, direction: Direction) -> int:
        """Move one slot in reading order, wrapping at either end.

        Left/right step by one card; up/down step by a whole row (one page
        width). Both flow across row and page boundaries rather than
        stopping at them, which is what lets the same rule reach every card
        in a catalogue of any size instead of only the first six.
        """
        if count <= 0:
            return 0
        if direction in (Direction.LEFT, Direction.RIGHT):
            step = -1 if direction is Direction.LEFT else 1
        else:
            step = -COLUMNS if direction is Direction.UP else COLUMNS
        return (index + step) % count

    @staticmethod
    def card_rect(slot: int) -> pygame.Rect:
        """Return the raw board position for *slot* (0-based, within one
        page), as if the page held a full :data:`PAGE_SIZE` cards.

        This is the uncentred building block: :meth:`page_card_rect` is what
        the renderer actually places on screen.
        """
        row, column = divmod(slot, COLUMNS)
        return pygame.Rect(
            CARD_MARGIN_X + column * (CARD_WIDTH + CARD_GAP_X),
            CARD_TOP + row * (CARD_HEIGHT + CARD_GAP_Y),
            CARD_WIDTH,
            CARD_HEIGHT,
        )

    @staticmethod
    def page_card_rect(slot: int, count_on_page: int) -> pygame.Rect:
        """Return *slot*'s on-screen position for a page holding
        *count_on_page* cards (``1..PAGE_SIZE``).

        Each row is centred horizontally against the cards actually present
        in *that* row, and the block of rows in use is centred vertically
        against the rows actually in use -- so a page short of a full 3x2
        board reads as a deliberate composition (a lone card in the middle,
        a short row centred under a full one) rather than a card stranded in
        the top-left corner with the rest of the screen empty.
        """
        row, _ = divmod(slot, COLUMNS)
        rows_used = max(1, math.ceil(count_on_page / COLUMNS))
        cols_in_row = _row_columns(count_on_page, row)
        dx, dy = _center_offset(cols_in_row, rows_used)
        rect = GridView.card_rect(slot)
        rect.move_ip(dx, dy)
        return rect

    def draw(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        draw_gallery_header(surface, ctx, frame)
        self.draw_banner(surface, ctx, BANNER, frame)

        pages = max(1, math.ceil(frame.count / PAGE_SIZE))
        page = frame.selected_index // PAGE_SIZE
        start = page * PAGE_SIZE
        count_on_page = min(PAGE_SIZE, frame.count - start)

        grow = int(6 * ease_out_cubic(min(1.0, frame.focus_ms / 220.0)))
        for slot, index in enumerate(range(start, min(start + PAGE_SIZE, frame.count))):
            rect = self.page_card_rect(slot, count_on_page)
            selected = index == frame.selected_index
            if selected:
                rect = rect.inflate(grow * 2, grow * 2)
            card_cover(
                surface,
                ctx,
                rect,
                frame.cards[index],
                selected=selected,
                time_ms=frame.time_ms,
                title_scale=2,
                badge_scale=1,
                pixel=3,
            )

        if pages > 1:
            draw_position_dots(surface, (SCREEN_WIDTH // 2, PAGE_DOTS_Y), pages, page)

        if frame.toast is not None:
            draw_toast(
                surface, ctx, (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2), frame.toast, frame.time_ms
            )


register(GridView())
