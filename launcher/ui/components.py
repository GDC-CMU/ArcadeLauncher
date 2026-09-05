"""Shared chrome: marquee, status badges, banners and toasts.

Every view composes these differently -- that is how the three modes stay one
visual system while reading as three deliberate layouts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import BRANDING_LOGO
from ..status import GameStatus, Notice
from ..viewmodes import ViewMode
from . import SCREEN_WIDTH
from .art import render_card_art
from .effects import corner_ticks, glow_frame, outline, panel, pulse
from .pygame_runtime import pygame
from .surfaces import SurfaceCache
from .theme import (
    FontBook,
    PALETTE,
    PixelFont,
    STATUS_ACCENTS,
    STATUS_COLORS,
    mix,
    shade,
    with_alpha,
)
from .viewmodel import Card, Toast

__all__ = [
    "RenderContext",
    "draw_marquee",
    "draw_mode_chip",
    "draw_status_badge",
    "draw_notice",
    "draw_toast",
    "draw_position_dots",
    "card_cover",
]

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class RenderContext:
    """Everything a view needs to draw, built once per gallery session."""

    pixel: PixelFont = field(default_factory=PixelFont)
    fonts: FontBook = field(default_factory=FontBook)
    cache: SurfaceCache = field(default_factory=SurfaceCache)
    logo_path: Path = BRANDING_LOGO
    _logo: pygame.Surface | None = field(default=None, init=False, repr=False)
    _logo_failed: bool = field(default=False, init=False, repr=False)

    def logo(self, height: int) -> pygame.Surface | None:
        """Return the club logo scaled to *height*, preserving aspect ratio.

        The asset is never rewritten and never re-encoded: it is loaded once,
        then scaled proportionally from its real dimensions.  A missing file
        degrades to ``None`` (the caller draws a wordmark instead) rather than
        crashing the gallery.
        """
        source = self._load_logo()
        if source is None:
            return None
        source_width, source_height = source.get_size()
        if source_height <= 0:
            return None
        width = max(1, round(source_width * (height / source_height)))
        return self.cache.get(
            ("logo", width, height),
            lambda: pygame.transform.smoothscale(source, (width, height)),
        )

    def _load_logo(self) -> pygame.Surface | None:
        if self._logo is not None or self._logo_failed:
            return self._logo
        try:
            image = pygame.image.load(str(self.logo_path))
        except (pygame.error, OSError) as exc:
            _log.warning("branding logo unavailable (%s): %s", self.logo_path, exc)
            self._logo_failed = True
            return None
        self._logo = image.convert_alpha() if pygame.display.get_surface() else image
        return self._logo

    def art(self, card: Card, size: tuple[int, int], *, pixel: int = 3) -> pygame.Surface:
        """Return cached procedural cover art for *card* at *size*."""
        dim = 1.0 if card.status is not GameStatus.COMING_SOON else 0.62
        key = ("art", card.entry.id, size, pixel, round(dim, 2))
        return self.cache.get(
            key, lambda: render_card_art(card.entry.art, size, pixel=pixel, dim=dim)
        )


# ---------------------------------------------------------------------------
# Marquee
# ---------------------------------------------------------------------------
def draw_marquee(
    surface: pygame.Surface,
    ctx: RenderContext,
    rect: pygame.Rect,
    *,
    logo_height: int = 58,
    title_scale: int = 3,
    show_subtitle: bool = True,
    centered: bool = False,
) -> pygame.Rect:
    """Draw the GDC-CMU marquee inside *rect* and return the area it used."""
    logo = ctx.logo(logo_height)
    title = ctx.pixel.render("GAME DEV CLUB", title_scale, PALETTE["bone"], tracking=1)
    subtitle = (
        ctx.pixel.render(
            "CARNEGIE MELLON QATAR - ARCADE", max(1, title_scale - 1), PALETTE["steel"]
        )
        if show_subtitle
        else None
    )

    gap = 14
    text_width = max(title.get_width(), subtitle.get_width() if subtitle else 0)
    logo_width = logo.get_width() if logo is not None else 0
    total = logo_width + (gap if logo_width else 0) + text_width

    left = rect.centerx - total // 2 if centered else rect.left
    top = rect.centery

    if logo is not None:
        surface.blit(logo, logo.get_rect(midleft=(left, top)))

    text_left = left + logo_width + (gap if logo_width else 0)
    if subtitle is not None:
        block_height = title.get_height() + 6 + subtitle.get_height()
        title_top = top - block_height // 2
        surface.blit(title, (text_left, title_top))
        surface.blit(subtitle, (text_left, title_top + title.get_height() + 6))
    else:
        surface.blit(title, title.get_rect(midleft=(text_left, top)))

    return pygame.Rect(left, rect.top, total, rect.height)


def draw_mode_chip(
    surface: pygame.Surface,
    ctx: RenderContext,
    anchor: tuple[int, int],
    mode: ViewMode,
    *,
    position: int | None = None,
    count: int | None = None,
    align: str = "topright",
    scale: int = 2,
) -> pygame.Rect:
    """Draw the chip that names the active composition.

    When *position* and *count* are given the chip reads e.g. ``CAROUSEL
    4/6`` -- the selected game's place in the catalogue, which is the number a
    visitor can actually cross-check against the row of cards in front of
    them. It never reports which of the three view modes is active (that used
    to read "CAROUSEL 2/3", a developer-facing detail a visitor cannot verify
    and easily mistakes for a bug).
    """
    label = mode.label if position is None or not count else f"{mode.label} {position}/{count}"
    text = ctx.pixel.render(label, scale, PALETTE["electric_cyan"])
    box = pygame.Rect(0, 0, text.get_width() + 24, text.get_height() + 16)
    setattr(box, align, anchor)
    panel(surface, box, PALETTE["panel"], PALETTE["deep_cyan"], radius=4)
    surface.blit(text, text.get_rect(center=box.center))
    return box


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def draw_status_badge(
    surface: pygame.Surface,
    ctx: RenderContext,
    anchor: tuple[int, int],
    status: GameStatus,
    *,
    align: str = "topleft",
    scale: int = 2,
    time_ms: int = 0,
) -> pygame.Rect:
    """Draw the state pill.

    Colour, label and -- for the busy states -- a pulsing dot all encode the
    same information, so Playable / Coming Soon / Updating / Unavailable stay
    distinguishable at a glance.
    """
    fill = STATUS_COLORS[status]
    back = STATUS_ACCENTS[status]
    label = "PLAYABLE" if status is GameStatus.READY else status.value
    text = ctx.pixel.render(label, scale, fill)
    dot_width = scale * 8 if status.is_busy else 0
    box = pygame.Rect(
        0, 0, text.get_width() + 20 + dot_width, text.get_height() + 12
    )
    setattr(box, align, anchor)

    panel(surface, box, back, fill, radius=box.height // 2)
    if dot_width:
        radius = max(2, scale)
        intensity = pulse(time_ms, 900, 0.25, 1.0)
        centre = (box.left + 10 + radius, box.centery)
        pygame.draw.circle(surface, mix(back, fill, intensity), centre, radius)
        surface.blit(text, text.get_rect(midleft=(box.left + 12 + dot_width, box.centery)))
    else:
        surface.blit(text, text.get_rect(center=box.center))
    return box


def card_cover(
    surface: pygame.Surface,
    ctx: RenderContext,
    rect: pygame.Rect,
    card: Card,
    *,
    selected: bool,
    time_ms: int,
    show_title: bool = True,
    title_scale: int = 2,
    show_badge: bool = True,
    badge_scale: int = 1,
    pixel: int = 3,
) -> None:
    """Draw one game cover: art, frame, title and badge.

    Shared by all three views so a card always looks like the same object no
    matter which composition it appears in.
    """
    status_color = STATUS_COLORS[card.status]
    frame_color = (
        PALETTE["warm_amber"] if selected else mix(PALETTE["panel_edge"], status_color, 0.35)
    )

    panel(surface, rect, PALETTE["panel"], None, radius=5)

    title_band = (title_scale * 7 + 14) if show_title else 0
    art_rect = pygame.Rect(
        rect.left + 4, rect.top + 4, rect.width - 8, rect.height - 8 - title_band
    )
    if art_rect.height > 8:
        surface.blit(ctx.art(card, art_rect.size, pixel=pixel), art_rect.topleft)
        pygame.draw.rect(surface, shade(PALETTE["ink"], 1.4), art_rect, width=1)

    if show_title:
        title_top = art_rect.bottom + 6
        available = rect.width - 12
        label = card.title.upper()
        while ctx.pixel.measure(label, title_scale)[0] > available and len(label) > 4:
            label = label[:-1]
        ctx.pixel.draw(
            surface,
            label,
            (rect.centerx, title_top),
            title_scale,
            PALETTE["bone"] if selected else mix(PALETTE["bone"], PALETTE["slate"], 0.35),
            anchor="midtop",
        )

    if show_badge:
        draw_status_badge(
            surface,
            ctx,
            (rect.left + 6, rect.top + 6),
            card.status,
            scale=badge_scale,
            time_ms=time_ms,
        )

    outline(surface, rect, frame_color, width=2 if selected else 1, radius=5)
    if selected:
        glow_frame(surface, rect, PALETTE["warm_amber"], pulse(time_ms, 1800, 0.5, 1.0))
        corner_ticks(surface, rect.inflate(10, 10), PALETTE["electric_cyan"])


# ---------------------------------------------------------------------------
# Banners and toasts
# ---------------------------------------------------------------------------
def draw_notice(
    surface: pygame.Surface, ctx: RenderContext, rect: pygame.Rect, notice: Notice
) -> None:
    """Draw the supervisor's banner (a game crashed, the gallery restarted).

    The banner always fits the rect it is given: in a tall strip the detail
    goes on its own line, and in a short one it is truncated onto the title
    row.  A banner can therefore never push a layout around or spill out of it.
    """
    accent = PALETTE["ember_red"] if notice.is_error else PALETTE["mint"]
    panel(surface, rect, shade(accent, 0.22), accent, radius=5)
    pygame.draw.rect(surface, accent, pygame.Rect(rect.left, rect.top, 5, rect.height))

    title = ctx.pixel.render(notice.title, 2, PALETTE["bone"])
    detail_color = mix(PALETTE["bone"], accent, 0.4)
    two_rows = bool(notice.detail) and rect.height >= title.get_height() + 30

    if not two_rows:
        surface.blit(title, title.get_rect(midleft=(rect.left + 18, rect.centery)))
        if notice.detail:
            room = rect.right - 18 - (rect.left + 30 + title.get_width())
            if room > 60:
                detail = ctx.fonts.render(
                    _truncate(ctx, notice.detail, "caption", room), "caption", detail_color
                )
                surface.blit(
                    detail, detail.get_rect(midleft=(rect.left + 30 + title.get_width(), rect.centery + 1))
                )
        return

    top = rect.centery - (title.get_height() + 5 + ctx.fonts.named("caption").get_height()) // 2
    surface.blit(title, (rect.left + 18, top))
    detail = ctx.fonts.render(
        _truncate(ctx, notice.detail, "caption", rect.width - 40), "caption", detail_color
    )
    surface.blit(detail, (rect.left + 18, top + title.get_height() + 5))


def draw_toast(
    surface: pygame.Surface,
    ctx: RenderContext,
    centre: tuple[int, int],
    toast: Toast,
    now_ms: int,
) -> None:
    """Draw the short 'not playable yet' feedback pop."""
    alpha = toast.alpha(now_ms)
    if alpha <= 0:
        return
    title = ctx.pixel.render(toast.text, 3, PALETTE["bone"])
    detail = ctx.fonts.render(toast.detail, "body", PALETTE["violet"]) if toast.detail else None

    width = max(title.get_width(), detail.get_width() if detail else 0) + 56
    height = title.get_height() + (detail.get_height() + 8 if detail else 0) + 34
    box = pygame.Rect(0, 0, width, height)
    box.center = centre

    layer = pygame.Surface(box.size, pygame.SRCALPHA)
    pygame.draw.rect(
        layer, with_alpha(shade(PALETTE["deep_violet"], 0.75), 238), layer.get_rect(), border_radius=8
    )
    pygame.draw.rect(
        layer, with_alpha(PALETTE["violet"], 255), layer.get_rect(), width=2, border_radius=8
    )
    layer.blit(title, title.get_rect(midtop=(box.width // 2, 14)))
    if detail is not None:
        layer.blit(detail, detail.get_rect(midtop=(box.width // 2, 18 + title.get_height())))
    layer.set_alpha(alpha)
    surface.blit(layer, box.topleft)


def draw_position_dots(
    surface: pygame.Surface,
    centre: tuple[int, int],
    count: int,
    index: int,
    *,
    spacing: int = 18,
) -> None:
    """Draw the 'you are here' run of dots under a horizontal gallery."""
    total = (count - 1) * spacing
    start_x = centre[0] - total // 2
    for position in range(count):
        x = start_x + position * spacing
        if position == index:
            pygame.draw.rect(
                surface,
                PALETTE["warm_amber"],
                pygame.Rect(x - 7, centre[1] - 3, 14, 6),
                border_radius=3,
            )
        else:
            pygame.draw.circle(surface, PALETTE["slate"], (x, centre[1]), 3)


def _truncate(ctx: RenderContext, text: str, size: str, width: int) -> str:
    font = ctx.fonts.named(size)
    if font.size(text)[0] <= width:
        return text
    clipped = text
    while clipped and font.size(clipped + "...")[0] > width:
        clipped = clipped[:-1]
    return clipped.rstrip() + "..."
