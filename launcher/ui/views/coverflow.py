"""Cover Flow -- a pseudo-3D wall of covers receding towards a horizon.

Composition: no top band at all.  The covers own the screen, angled away from
the viewer on both sides, standing on a reflective floor with a glowing
horizon.  The selected title sits below the wall in large type.

The perspective is a genuine per-column trapezoid transform, not a plain
rescale: the edge of a card facing the viewer stays tall while the far edge is
squeezed, which is what sells the rotation.  Because that transform is
expensive, cards snap to one of a handful of discrete depth slots and every
``(card, size, slot)`` variant is cached.  Position on the wall is continuous
-- driven by each card's real distance to the smoothed scroll position rather
than the integer selection -- so the whole wall glides; only the *depth slot*
a card renders at (its size and skew) still steps between a few fixed values,
which is what keeps the perspective transform built at most once per slot
instead of every frame.
"""

from __future__ import annotations

from typing import ClassVar

from ...status import GameStatus
from ...viewmodes import ViewMode
from .. import SCREEN_WIDTH
from ..components import (
    RenderContext,
    card_cover,
    draw_position_dots,
    draw_status_badge,
    draw_toast,
)
from ..effects import ease_out_cubic, horizon_glow, lerp_stops, pulse, reflect, wrapped_distance
from ..pygame_runtime import pygame
from ..theme import PALETTE, STATUS_COLORS, mix, shade
from ..viewmodel import Card, GalleryFrame
from .base import GalleryView, register

__all__ = ["CoverFlowView"]

HERO_SIZE = (208, 244)
STAGE_CENTRE_Y = 250
HORIZON_Y = 372
REFLECTION_HEIGHT = 34

#: Deepest slot still drawn.  Anything further is off the wall entirely.
MAX_DEPTH = 3
#: Distance from screen centre for depth 1, and the step for each slot after.
DEPTH_BASE = 150
DEPTH_STEP = 96
#: Uniform shrink per depth slot, and the foreshortening of a rotated card.
DEPTH_SHRINK = 0.87
ROTATION_SQUEEZE = 0.58
#: How much the far edge of a rotated card is compressed.
FAR_EDGE_SCALE = 0.72

#: Continuous horizontal offset per distance from the selection -- see
#: :func:`~launcher.ui.effects.lerp_stops`. Only *position* is continuous;
#: which depth slot (size/skew) a card renders at still snaps to the nearest
#: whole step, so the expensive perspective transform stays cached.
OFFSET_STOPS: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (1.0, DEPTH_BASE),
    (2.0, DEPTH_BASE + DEPTH_STEP),
    (3.0, DEPTH_BASE + DEPTH_STEP * 2),
)


def perspective(source: pygame.Surface, near_left: bool) -> pygame.Surface:
    """Return *source* skewed into a trapezoid, as if rotated about its centre.

    Args:
        source: The flat cover.
        near_left: ``True`` when the card's left edge faces the viewer, which
            is the case for cards sitting to the *right* of the selection.
    """
    width, height = source.get_size()
    out = pygame.Surface((width, height), pygame.SRCALPHA)
    if width <= 1:
        return out
    for x in range(width):
        across = x / (width - 1)
        far = across if not near_left else 1.0 - across
        scale = 1.0 + (FAR_EDGE_SCALE - 1.0) * far
        column_height = max(1, int(height * scale))
        column = pygame.transform.scale(
            source.subsurface(pygame.Rect(x, 0, 1, height)), (1, column_height)
        )
        out.blit(column, (x, (height - column_height) // 2))
    return out


class CoverFlowView(GalleryView):
    """Angled covers receding to a horizon, with reflections and big type."""

    mode: ClassVar[ViewMode] = ViewMode.COVER_FLOW

    def draw(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        self._draw_chrome(surface, ctx, frame)
        self._draw_floor(surface, ctx)
        self._draw_wall(surface, ctx, frame)
        self._draw_scrim(surface, ctx)
        draw_position_dots(
            surface, (SCREEN_WIDTH // 2, HORIZON_Y + 34), frame.count, frame.selected_index
        )
        self._draw_caption(surface, ctx, frame)

        if frame.toast is not None:
            draw_toast(
                surface, ctx, (SCREEN_WIDTH // 2, STAGE_CENTRE_Y), frame.toast, frame.time_ms
            )

    # ------------------------------------------------------------------
    def _draw_chrome(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        """No header band -- just a small wordmark and the position counter."""
        logo = ctx.logo(36)
        left = 26
        if logo is not None:
            surface.blit(logo, logo.get_rect(midleft=(left, 30)))
            left += logo.get_width() + 12
        ctx.pixel.draw(
            surface, "GDC ARCADE", (left, 30), 2, PALETTE["bone"], anchor="midleft"
        )

        # The selected game's place in the catalogue, never the view-mode
        # slot -- "COVER FLOW 4/6" is a number a visitor can check against the
        # row of cards; "2/3" (which of the three view modes) is not.
        label = f"{frame.view_mode.label}  {frame.selected_index + 1}/{frame.count}"
        ctx.pixel.draw(
            surface,
            label,
            (SCREEN_WIDTH - 26, 30),
            2,
            PALETTE["electric_cyan"],
            anchor="midright",
        )

    def _draw_floor(self, surface: pygame.Surface, ctx: RenderContext) -> None:
        glow = ctx.cache.get(
            ("cf-horizon", SCREEN_WIDTH),
            lambda: horizon_glow((SCREEN_WIDTH, 16), PALETTE["electric_cyan"]),
        )
        surface.blit(glow, (0, HORIZON_Y - 8))
        pygame.draw.line(
            surface,
            mix(PALETTE["deep_cyan"], PALETTE["night"], 0.35),
            (60, HORIZON_Y),
            (SCREEN_WIDTH - 60, HORIZON_Y),
            2,
        )

    def _draw_scrim(self, surface: pygame.Surface, ctx: RenderContext) -> None:
        """Fade the reflections out before the caption starts.

        Without this the big title sits on top of the mirrored covers and both
        become hard to read.  The band is short and starts fully transparent,
        so the bright part of the reflection directly under the wall survives.
        """
        top = HORIZON_Y + 6
        height = REFLECTION_HEIGHT + 14

        def build() -> pygame.Surface:
            layer = pygame.Surface((SCREEN_WIDTH, height), pygame.SRCALPHA)
            bands = 16
            band_height = max(1, height // bands)
            for index in range(bands + 1):
                ratio = min(1.0, index / bands)
                layer.fill(
                    (*PALETTE["void"], int(255 * ratio**1.5)),
                    pygame.Rect(0, index * band_height, SCREEN_WIDTH, band_height + 1),
                )
            return layer

        surface.blit(ctx.cache.get(("cf-scrim", SCREEN_WIDTH, height), build), (0, top))

    def _draw_wall(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        # Every card's screen position comes from its continuous distance to
        # the smoothed scroll position, farthest first so nearer cards paint
        # on top -- the same shortest-path, arbitrary-count-safe approach as
        # the carousel. Only the depth *slot* used to pick the cached, skewed
        # surface still snaps to the nearest whole step.
        visible = []
        for index in range(frame.count):
            distance = wrapped_distance(index, frame.scroll, frame.count)
            if abs(distance) > MAX_DEPTH + 0.001:
                continue
            visible.append((abs(distance), distance, index))
        visible.sort(key=lambda item: -item[0])

        for magnitude, distance, index in visible:
            slot_depth = min(MAX_DEPTH, round(magnitude))
            offset = lerp_stops(magnitude, OFFSET_STOPS)
            sign = 0.0 if distance == 0 else (1.0 if distance > 0 else -1.0)
            centre_x = int(SCREEN_WIDTH / 2 + sign * offset)
            self._blit_cover(
                surface,
                ctx,
                frame,
                index,
                depth=slot_depth,
                near_left=distance > 0,
                centre_x=centre_x,
            )

    def _blit_cover(
        self,
        surface: pygame.Surface,
        ctx: RenderContext,
        frame: GalleryFrame,
        index: int,
        *,
        depth: int,
        near_left: bool,
        centre_x: int,
    ) -> None:
        card = frame.cards[index]
        shrink = DEPTH_SHRINK ** max(0, depth - 1) * (1.0 if depth == 0 else 0.9)
        height = int(HERO_SIZE[1] * shrink)
        width = int(HERO_SIZE[0] * shrink * (1.0 if depth == 0 else ROTATION_SQUEEZE))
        if depth == 0:
            grow = int(8 * ease_out_cubic(min(1.0, frame.focus_ms / 260.0)))
            width += grow
            height += grow

        slot = 0 if depth == 0 else (depth if near_left else -depth)
        cover = ctx.cache.get(
            ("cf", card.entry.id, card.status, (width, height), slot, depth == 0),
            lambda: self._build_cover(ctx, frame, card, (width, height), depth, near_left),
        )

        rect = cover.get_rect()
        rect.midbottom = (centre_x, HORIZON_Y - 2)
        surface.blit(cover, rect.topleft)

        mirror = ctx.cache.get(
            ("cf-mirror", card.entry.id, card.status, (width, height), slot, depth == 0),
            lambda: reflect(cover, REFLECTION_HEIGHT, fade=150),
        )
        surface.blit(mirror, (rect.left, HORIZON_Y + 2))

        if depth == 0:
            pygame.draw.rect(
                surface,
                PALETTE["warm_amber"],
                pygame.Rect(rect.left - 3, rect.top - 3, rect.width + 6, rect.height + 6),
                width=2,
                border_radius=5,
            )

    def _build_cover(
        self,
        ctx: RenderContext,
        frame: GalleryFrame,
        card: Card,
        size: tuple[int, int],
        depth: int,
        near_left: bool,
    ) -> pygame.Surface:
        """Render one flat cover, then skew it into the wall's perspective."""
        flat = pygame.Surface(size, pygame.SRCALPHA)
        card_cover(
            flat,
            ctx,
            flat.get_rect(),
            card,
            selected=depth == 0,
            time_ms=0,
            show_title=False,
            show_badge=depth == 0,
            badge_scale=1,
            pixel=3 if depth == 0 else 2,
        )
        if depth == 0:
            return flat

        skewed = perspective(flat, near_left)
        # Cards further back sit deeper in the haze.
        haze = pygame.Surface(skewed.get_size(), pygame.SRCALPHA)
        haze.fill((*PALETTE["void"], min(190, 38 + depth * 44)))
        skewed.blit(haze, (0, 0))
        return skewed

    def _draw_caption(
        self, surface: pygame.Surface, ctx: RenderContext, frame: GalleryFrame
    ) -> None:
        card = frame.selected
        accent = STATUS_COLORS[card.status]

        title = card.title.upper()
        scale = 4
        while scale > 2 and ctx.pixel.measure(title, scale)[0] > SCREEN_WIDTH - 96:
            scale -= 1
        ctx.pixel.draw(
            surface, title, (SCREEN_WIDTH // 2, 422), scale, PALETTE["bone"], anchor="midtop"
        )

        underline_width = ctx.pixel.measure(title, scale)[0]
        pygame.draw.rect(
            surface,
            shade(accent, 1.1),
            pygame.Rect(
                SCREEN_WIDTH // 2 - underline_width // 2, 422 + scale * 7 + 6, underline_width, 2
            ),
        )

        draw_status_badge(
            surface,
            ctx,
            (SCREEN_WIDTH // 2, 468),
            card.status,
            align="center",
            scale=1,
            time_ms=frame.time_ms,
        )

        detail = card.detail
        if card.status is GameStatus.COMING_SOON and not detail:
            detail = "In development at Game Dev Club"
        line = detail or card.entry.description
        colour = mix(PALETTE["bone"], PALETTE["steel"], 0.4)
        if card.status.is_busy:
            colour = mix(colour, accent, pulse(frame.time_ms, 1100, 0.2, 0.9))
        lines = ctx.fonts.wrap(line, "body", SCREEN_WIDTH - 140, max_lines=2)
        for offset, wrapped in enumerate(lines):
            text = ctx.fonts.render(wrapped, "body", colour)
            surface.blit(text, text.get_rect(midtop=(SCREEN_WIDTH // 2, 494 + offset * 23)))


register(CoverFlowView())
