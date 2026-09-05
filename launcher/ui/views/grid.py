"""Grid -- the cabinet board.

Composition: the shared marquee header (identical across all three views) and
a board of cards beneath it, sized to fill the freed space now that the
permanent status strip and control legend are gone. Everything currently
visible is on screen at once, and the stick moves in two real dimensions.

**Always three columns, never four.** Four columns of cards would be
unreadable at 800x600, so three is the permanent maximum width regardless of
catalogue size.

**Up to three rows visible, and the board is sized to the rows actually
needed** rather than always reserving the same fixed height: 1-3 games get one
big row, 4-6 games get two, and 7 or more get the maximum three (small but
still legible) rows. This is the client's explicit call: shrink the card art
before the text, and never grow past 3x3.

**Beyond nine games the board scrolls vertically** instead of paginating.
Pressing DOWN past the last visible row -- or UP past the first -- eases the
view by exactly enough to keep the selection inside the visible band, the
same frame-delta-driven glide the horizontal modes use for their selection
scroll (see ``GallerySession._glide_linear``). This replaced an earlier
paginated design (fixed pages, page dots); the pages are gone, but the same
per-row centring that kept a page's short final row from hugging the left
margin is still here, now applied to the whole catalogue's actual final row
instead of a page's.
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
    draw_toast,
)
from ..effects import ease_out_cubic
from ..pygame_runtime import pygame
from ..theme import PALETTE
from ..viewmodel import GalleryFrame
from .base import GalleryView, register

__all__ = ["GridView"]

#: Permanent board width. The client has explicitly ruled out four columns as
#: unreadable at 800x600, so this never changes with catalogue size.
COLUMNS = 3

#: Permanent board height ceiling. 3x3 is judged readable; 4x4 is not.
MAX_VISIBLE_ROWS = 3

BANNER = pygame.Rect(24, HEADER_RECT.bottom + 8, SCREEN_WIDTH - 48, 40)

CARD_MARGIN_X = 30
CARD_GAP_X = 20
CARD_GAP_Y = 18
CARD_TOP = BANNER.bottom + 14
CARD_BOTTOM_MARGIN = 30
CARD_WIDTH = (SCREEN_WIDTH - CARD_MARGIN_X * 2 - CARD_GAP_X * (COLUMNS - 1)) // COLUMNS

#: The area cards are laid out within -- everything below the banner, above
#: the bottom margin, and inset from both sides.
CONTENT_WIDTH = SCREEN_WIDTH - CARD_MARGIN_X * 2
CONTENT_HEIGHT = SCREEN_HEIGHT - CARD_TOP - CARD_BOTTOM_MARGIN

#: Scrollbar geometry -- a restrained sliver, not a new busy element. Only
#: drawn once a catalogue actually needs to scroll.
SCROLLBAR_WIDTH = 4
SCROLLBAR_X = SCREEN_WIDTH - CARD_MARGIN_X + 10


def rows_needed(count: int) -> int:
    """Total rows the whole catalogue occupies at a fixed 3 columns."""
    return max(1, math.ceil(max(0, count) / COLUMNS))


def visible_rows(count: int) -> int:
    """How many rows are shown at once: 1, 2, or the permanent max of 3.

    Sized to the rows actually needed so a small catalogue gets large cards
    instead of being shrunk to fill an unused third (or two-thirds) of the
    board it doesn't need yet.
    """
    return min(MAX_VISIBLE_ROWS, rows_needed(count))


def max_scroll_rows(count: int) -> float:
    """How far the view can scroll, in whole rows (0 if everything fits)."""
    return float(max(0, rows_needed(count) - visible_rows(count)))


def card_height(rows: int) -> int:
    """Card height so *rows* rows exactly fill the vertical content area."""
    rows = max(1, rows)
    return (CONTENT_HEIGHT - CARD_GAP_Y * (rows - 1)) // rows


def row_columns(count: int, row: int) -> int:
    """How many cards actually occupy *row* (0-based, absolute across the
    whole catalogue, not relative to any visible window) of *count* games."""
    return max(0, min(COLUMNS, count - row * COLUMNS))


def row_dx(cols_in_row: int) -> int:
    """Horizontal centring offset for a row holding *cols_in_row* cards.

    A full row of :data:`COLUMNS` already spans the content area up to a
    rounding remainder of a pixel or two (:data:`CARD_WIDTH` is itself
    floor-divided), so this floors to zero for a full row -- the common case
    is a genuine no-op, not a special case. A short final row (fewer than
    :data:`COLUMNS` cards) is nudged to sit centred under the full rows above
    it instead of hugging the left margin.
    """
    block_width = cols_in_row * CARD_WIDTH + max(0, cols_in_row - 1) * CARD_GAP_X
    return (CONTENT_WIDTH - block_width) // 2


def target_scroll(index: int, count: int) -> float:
    """The row-scroll position that keeps *index* comfortably visible.

    A pure function of the selection alone -- not persisted state -- so it is
    exact regardless of how the selection got there (one arrow press, several
    holds, or a wrap straight from the last game to the first). It keeps the
    selected row inside the visible band, scrolling by exactly enough the
    moment the row would otherwise leave the band rather than waiting for it
    to already be off-screen or overshooting into a recentre.
    """
    if count <= 0:
        return 0.0
    row = index // COLUMNS
    rows = visible_rows(count)
    maximum = max_scroll_rows(count)
    return min(maximum, max(0.0, float(row - (rows - 1))))


def _down_index(index: int, count: int) -> int:
    """DOWN's target index -- the client's own words: 'if I just keep
    clicking down down, it will scroll down and find games there'. A plain
    ``+COLUMNS`` wrap keeps a card orbiting its own column forever whenever
    the catalogue size shares a factor with :data:`COLUMNS` (any multiple of
    3 -- 6, 9, 12 games all failed this before this fix), since the same
    residue class is revisited every wrap and the other two columns are
    never reached without a LEFT/RIGHT press. Wrapping into the *next*
    column instead of the same one turns every wrap into progress rather
    than a repeat, which is what makes repeated DOWN presses alone -- no
    LEFT or RIGHT ever -- eventually surface every game, for any count.
    """
    if count <= 0:
        return 0
    if rows_needed(count) <= 1:
        # Only one row exists; there is nothing below to move to, so DOWN
        # behaves like RIGHT rather than being a dead control.
        return (index + 1) % count
    column = index % COLUMNS
    row = index // COLUMNS
    next_row = row + 1
    if next_row < rows_needed(count) and column < row_columns(count, next_row):
        return next_row * COLUMNS + column
    # Hit the bottom of this column (either the last row, or a short final
    # row that doesn't reach this column) -- carry into the next column's
    # top row instead of restarting this one.
    new_column = (column + 1) % COLUMNS
    if new_column < row_columns(count, 0):
        return new_column
    return 0


def _up_index(index: int, count: int) -> int:
    """UP's target index -- the mirror image of :func:`_down_index`, for the
    same reason: it must make progress on every wrap, not repeat.
    """
    if count <= 0:
        return 0
    if rows_needed(count) <= 1:
        return (index - 1) % count
    column = index % COLUMNS
    row = index // COLUMNS
    prev_row = row - 1
    if prev_row >= 0 and column < row_columns(count, prev_row):
        return prev_row * COLUMNS + column
    # Hit the top of this column -- carry into the previous column's bottom
    # row. Only the final row can be short, so the row above it is always a
    # full row; fall back one further row if the short final row doesn't
    # reach the wrapped-to column.
    new_column = (column - 1) % COLUMNS
    last_row = rows_needed(count) - 1
    if new_column >= row_columns(count, last_row):
        last_row -= 1
    return last_row * COLUMNS + new_column


class GridView(GalleryView):
    """A board of cards, always 3 columns, up to 3 rows, scrolling beyond
    that rather than paginating."""

    mode: ClassVar[ViewMode] = ViewMode.GRID
    columns: ClassVar[int] = COLUMNS

    def navigate(self, index: int, count: int, direction: Direction) -> int:
        """Move one slot, wrapping at every edge so the same rule reaches
        every card in a catalogue of any size.

        Left/right step by one card. Up/down step by a whole row when the
        row directly above/below has a card in the same column; otherwise
        (the top or bottom of a column) they carry into the neighbouring
        column rather than restarting the same one -- see
        :func:`_down_index` for why that carry is not optional. Scrolling
        (see :func:`target_scroll`) is purely a rendering concern layered on
        top of this, never a navigation one.
        """
        if count <= 0:
            return 0
        if direction is Direction.LEFT:
            return (index - 1) % count
        if direction is Direction.RIGHT:
            return (index + 1) % count
        if direction is Direction.UP:
            return _up_index(index, count)
        return _down_index(index, count)

    @staticmethod
    def target_scroll(index: int, count: int) -> float:
        return target_scroll(index, count)

    @staticmethod
    def slot_rect(row: int, column: int, count: int, scroll: float) -> pygame.Rect:
        """The on-screen rect for absolute (*row*, *column*) at *scroll*.

        May land partially or fully outside the visible content band --
        callers are responsible for clipping while a scroll is in progress.
        """
        rows = visible_rows(count)
        height = card_height(rows)
        cols_in_row = row_columns(count, row)
        dx = row_dx(cols_in_row)
        x = CARD_MARGIN_X + dx + column * (CARD_WIDTH + CARD_GAP_X)
        y = CARD_TOP + (row - scroll) * (height + CARD_GAP_Y)
        return pygame.Rect(int(round(x)), int(round(y)), CARD_WIDTH, height)

    def draw(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        draw_gallery_header(surface, ctx, frame)
        self.draw_banner(surface, ctx, BANNER, frame)

        count = frame.count
        total_rows = rows_needed(count)
        rows_shown = visible_rows(count)
        maximum = max_scroll_rows(count)
        scrolling = maximum > 0
        scroll = min(maximum, max(0.0, frame.grid_scroll)) if scrolling else 0.0

        content_rect = pygame.Rect(CARD_MARGIN_X, CARD_TOP, CONTENT_WIDTH, CONTENT_HEIGHT)
        if scrolling:
            surface.set_clip(content_rect)

        # One row of slack on each side of the visible band so a row that is
        # only partially scrolled into view still renders (and gets clipped
        # to a sliver) instead of popping in only once fully aligned.
        first_row = max(0, int(math.floor(scroll)) - 1)
        last_row = min(total_rows - 1, int(math.ceil(scroll + rows_shown)))

        grow = int(6 * ease_out_cubic(min(1.0, frame.focus_ms / 220.0)))
        for row in range(first_row, last_row + 1):
            for column in range(row_columns(count, row)):
                index = row * COLUMNS + column
                if index >= count:
                    continue
                rect = self.slot_rect(row, column, count, scroll)
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

        if scrolling:
            surface.set_clip(None)
            self._draw_scrollbar(surface, total_rows, rows_shown, scroll)

        if frame.toast is not None:
            draw_toast(
                surface, ctx, (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2), frame.toast, frame.time_ms
            )

    @staticmethod
    def _draw_scrollbar(
        surface: pygame.Surface, total_rows: int, rows_shown: int, scroll: float
    ) -> None:
        """A restrained sliver marking how far into the catalogue the visible
        band sits -- the only scroll affordance, deliberately unobtrusive."""
        track = pygame.Rect(SCROLLBAR_X, CARD_TOP, SCROLLBAR_WIDTH, CONTENT_HEIGHT)
        pygame.draw.rect(surface, PALETTE["panel_edge"], track, border_radius=2)
        thumb_height = max(18, int(CONTENT_HEIGHT * rows_shown / total_rows))
        travel = CONTENT_HEIGHT - thumb_height
        thumb_top = CARD_TOP + int(travel * (scroll / max(1e-9, total_rows - rows_shown)))
        thumb = pygame.Rect(SCROLLBAR_X, thumb_top, SCROLLBAR_WIDTH, thumb_height)
        pygame.draw.rect(surface, PALETTE["slate"], thumb, border_radius=2)


register(GridView())
