"""Carousel -- one spotlit cover with a large information panel.

Composition: a slim header, a stage where the selected cover is large and its
neighbours bleed in from both edges, a run of position dots, and a full-width
information panel carrying the title, badge and description at a size that
reads across a room.
"""

from __future__ import annotations

from typing import ClassVar

from ...status import GameStatus
from ...viewmodes import ViewMode
from .. import SCREEN_WIDTH
from ..components import (
    RenderContext,
    card_cover,
    draw_control_legend,
    draw_marquee,
    draw_mode_chip,
    draw_position_dots,
    draw_status_badge,
    draw_toast,
)
from ..effects import ease_out_cubic, panel, pulse
from ..pygame_runtime import pygame
from ..theme import PALETTE, STATUS_COLORS, mix, shade
from ..viewmodel import GalleryFrame
from .base import GalleryView, register

__all__ = ["CarouselView"]

HEADER = pygame.Rect(0, 0, SCREEN_WIDTH, 66)
STRIP = pygame.Rect(24, 72, SCREEN_WIDTH - 48, 46)
STAGE = pygame.Rect(0, 122, SCREEN_WIDTH, 224)
DOTS_Y = 354
INFO = pygame.Rect(36, 364, SCREEN_WIDTH - 72, 134)
LEGEND = pygame.Rect(16, 504, SCREEN_WIDTH - 32, 84)

HERO_SIZE = (200, 214)
#: Horizontal offset and scale for each distance from the selection.
NEIGHBOUR_SLOTS: tuple[tuple[int, float], ...] = ((206, 0.74), (352, 0.50))


class CarouselView(GalleryView):
    """One large selected cover, neighbours partially visible, big info panel."""

    mode: ClassVar[ViewMode] = ViewMode.CAROUSEL

    def draw(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        self._draw_header(surface, ctx, frame)
        self.draw_status_strip(surface, ctx, STRIP, frame)
        self._draw_stage(surface, ctx, frame)
        draw_position_dots(
            surface, (SCREEN_WIDTH // 2, DOTS_Y), frame.count, frame.selected_index
        )
        self._draw_info(surface, ctx, frame)
        draw_control_legend(surface, ctx, LEGEND, layout="split")

        if frame.toast is not None:
            draw_toast(surface, ctx, (SCREEN_WIDTH // 2, STAGE.centery), frame.toast, frame.time_ms)

    # ------------------------------------------------------------------
    def _draw_header(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        pygame.draw.rect(surface, shade(PALETTE["night"], 1.08), HEADER)
        pygame.draw.line(
            surface,
            PALETTE["deep_cyan"],
            (0, HEADER.bottom - 1),
            (SCREEN_WIDTH, HEADER.bottom - 1),
            2,
        )
        draw_marquee(
            surface,
            ctx,
            pygame.Rect(24, HEADER.top, 460, HEADER.height),
            logo_height=42,
            title_scale=2,
            show_subtitle=False,
        )
        draw_mode_chip(surface, ctx, (SCREEN_WIDTH - 24, HEADER.top + 16), frame.view_mode)

    def _draw_stage(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        centre_x = SCREEN_WIDTH // 2
        # Smooth glide: the fractional scroll trails the integer selection.
        drift = int((frame.selected_index - frame.scroll) * 40)
        drift = max(-70, min(70, drift))

        # Neighbours first (back to front), then the hero on top.
        for distance in range(len(NEIGHBOUR_SLOTS), 0, -1):
            offset, scale = NEIGHBOUR_SLOTS[distance - 1]
            size = (int(HERO_SIZE[0] * scale), int(HERO_SIZE[1] * scale))
            for sign in (-1, 1):
                index = (frame.selected_index + sign * distance) % frame.count
                rect = pygame.Rect(0, 0, *size)
                rect.center = (centre_x + sign * offset - drift, STAGE.centery + 6)
                card_cover(
                    surface,
                    ctx,
                    rect,
                    frame.cards[index],
                    selected=False,
                    time_ms=frame.time_ms,
                    show_title=distance == 1,
                    title_scale=1,
                    show_badge=distance == 1,
                    badge_scale=1,
                    pixel=2,
                )

        grow = int(8 * ease_out_cubic(min(1.0, frame.focus_ms / 260.0)))
        hero = pygame.Rect(0, 0, HERO_SIZE[0] + grow, HERO_SIZE[1] + grow)
        hero.center = (centre_x - drift, STAGE.centery)
        card_cover(
            surface,
            ctx,
            hero,
            frame.selected,
            selected=True,
            time_ms=frame.time_ms,
            show_title=False,
            show_badge=False,
            pixel=3,
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
