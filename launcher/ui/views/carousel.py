"""Carousel -- one spotlit cover with a large information panel.

Composition: the shared marquee header (identical across all three views), a
stage where the selected cover is large and its neighbours bleed in from both
edges, a run of position dots, and a full-width information panel carrying
the title, badge and description at a size that reads across a room.
"""

from __future__ import annotations

from typing import ClassVar

from ...status import GameStatus
from ...viewmodes import ViewMode
from .. import SCREEN_WIDTH
from ..components import (
    HEADER_RECT,
    RenderContext,
    card_cover,
    draw_gallery_header,
    draw_position_dots,
    draw_status_badge,
    draw_toast,
)
from ..effects import ease_out_cubic, lerp_stops, panel, pulse, wrapped_distance
from ..pygame_runtime import pygame
from ..theme import PALETTE, STATUS_COLORS, mix, shade
from ..viewmodel import GalleryFrame
from .base import GalleryView, register

__all__ = ["CarouselView"]

BANNER = pygame.Rect(24, HEADER_RECT.bottom + 6, SCREEN_WIDTH - 48, 40)
STAGE = pygame.Rect(0, BANNER.bottom + 8, SCREEN_WIDTH, 300)
DOTS_Y = STAGE.bottom + 14
INFO = pygame.Rect(36, DOTS_Y + 18, SCREEN_WIDTH - 72, 122)

HERO_SIZE = (224, 248)
#: Continuous (distance, value) stops the *position* lerps between -- see
#: :func:`~launcher.ui.effects.lerp_stops`. Distance 0 is the selected card;
#: beyond the last stop nothing is drawn. Only position is continuous: the
#: rendered *size* stays one of a handful of discrete steps (below) so the
#: cached card art is never rebuilt at a new size every frame -- a card slides
#: smoothly between slots and steps its scale once it crosses the midpoint to
#: the next one, rather than the surface cache thrashing on a float that is
#: different every frame.
OFFSET_STOPS: tuple[tuple[float, float], ...] = ((0.0, 0.0), (1.0, 206.0), (2.0, 352.0))
VERTICAL_STOPS: tuple[tuple[float, float], ...] = ((0.0, 0.0), (1.0, 6.0), (2.0, 6.0))
MAX_DRAW_DISTANCE = 2.0
#: Discrete size steps, indexed by ``round(distance)`` clamped to the last one.
SCALE_BY_BUCKET: tuple[float, ...] = (1.0, 0.74, 0.50)


class CarouselView(GalleryView):
    """One large selected cover, neighbours partially visible, big info panel."""

    mode: ClassVar[ViewMode] = ViewMode.CAROUSEL

    def draw(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        draw_gallery_header(surface, ctx, frame)
        self.draw_banner(surface, ctx, BANNER, frame)
        self._draw_stage(surface, ctx, frame)
        draw_position_dots(
            surface, (SCREEN_WIDTH // 2, DOTS_Y), frame.count, frame.selected_index
        )
        self._draw_info(surface, ctx, frame)

        if frame.toast is not None:
            draw_toast(surface, ctx, (SCREEN_WIDTH // 2, STAGE.centery), frame.toast, frame.time_ms)

    # ------------------------------------------------------------------
    def _draw_stage(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        centre_x = SCREEN_WIDTH // 2
        grow = int(8 * ease_out_cubic(min(1.0, frame.focus_ms / 260.0)))

        # Every card's position, scale and layering come from its continuous
        # distance to the smoothed scroll position -- not the integer index --
        # so a card slides between slots instead of teleporting the moment the
        # selection changes. Farthest first, so the near cards paint on top.
        visible = []
        for index in range(frame.count):
            distance = wrapped_distance(index, frame.scroll, frame.count)
            if abs(distance) > MAX_DRAW_DISTANCE:
                continue
            visible.append((abs(distance), distance, index))
        visible.sort(key=lambda item: -item[0])

        for magnitude, distance, index in visible:
            is_hero = index == frame.selected_index
            bucket = min(len(SCALE_BY_BUCKET) - 1, round(magnitude))
            offset = lerp_stops(magnitude, OFFSET_STOPS)
            vertical = lerp_stops(magnitude, VERTICAL_STOPS)
            scale = SCALE_BY_BUCKET[bucket]
            size = (
                int(HERO_SIZE[0] * scale) + (grow if is_hero else 0),
                int(HERO_SIZE[1] * scale) + (grow if is_hero else 0),
            )
            sign = 0.0 if distance == 0 else (1.0 if distance > 0 else -1.0)
            rect = pygame.Rect(0, 0, *size)
            rect.center = (
                int(centre_x + sign * offset),
                int(STAGE.centery + vertical),
            )
            card_cover(
                surface,
                ctx,
                rect,
                frame.cards[index],
                selected=is_hero,
                time_ms=frame.time_ms,
                show_title=not is_hero and bucket == 1,
                title_scale=1,
                show_badge=not is_hero and bucket == 1,
                badge_scale=1,
                pixel=3 if is_hero else 2,
            )

        # Edge fades so the neighbours bleed out rather than being cut off.
        for side in (0, 1):
            fade = pygame.Surface((90, STAGE.height), pygame.SRCALPHA)
            for column in range(90):
                alpha = int(232 * ((90 - column) / 90) ** 1.6)
                x = column if side == 0 else 89 - column
                pygame.draw.line(fade, (*PALETTE["void"], alpha), (x, 0), (x, STAGE.height))
            surface.blit(fade, (0 if side == 0 else SCREEN_WIDTH - 90, STAGE.top))

    def _draw_info(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        card = frame.selected
        accent = STATUS_COLORS[card.status]
        panel(surface, INFO, shade(PALETTE["panel"], 0.9), PALETTE["panel_edge"], radius=8)
        pygame.draw.rect(
            surface, accent, pygame.Rect(INFO.left, INFO.top, 6, INFO.height), border_top_left_radius=8, border_bottom_left_radius=8
        )

        badge = draw_status_badge(
            surface,
            ctx,
            (INFO.right - 18, INFO.top + 18),
            card.status,
            align="topright",
            scale=2,
            time_ms=frame.time_ms,
        )

        title = card.title.upper()
        scale = 4
        available = badge.left - INFO.left - 46
        while scale > 2 and ctx.pixel.measure(title, scale)[0] > available:
            scale -= 1
        ctx.pixel.draw(
            surface, title, (INFO.left + 26, INFO.top + 16), scale, PALETTE["bone"]
        )

        body_top = INFO.top + 52
        lines = ctx.fonts.wrap(card.entry.description, "body", INFO.width - 52, max_lines=2)
        for offset, line in enumerate(lines):
            text = ctx.fonts.render(line, "body", mix(PALETTE["bone"], PALETTE["steel"], 0.35))
            surface.blit(text, (INFO.left + 26, body_top + offset * 23))

        footer_y = INFO.bottom - 20
        detail = card.detail
        if card.status is GameStatus.COMING_SOON and not detail:
            detail = "Not available on the cabinet yet"
        if detail:
            colour = mix(accent, PALETTE["bone"], 0.25)
            if card.status.is_busy:
                colour = mix(colour, PALETTE["night"], 1.0 - pulse(frame.time_ms, 1100, 0.55, 1.0))
            text = ctx.fonts.render(detail, "caption", colour)
            surface.blit(text, text.get_rect(midleft=(INFO.left + 26, footer_y)))

        hint = "PRESS  A  TO PLAY" if card.status.is_playable else "SELECT ANOTHER GAME"
        ctx.pixel.draw(
            surface,
            hint,
            (INFO.right - 22, footer_y),
            2,
            PALETTE["warm_amber"] if card.status.is_playable else PALETTE["steel"],
            anchor="midright",
        )


register(CarouselView())
