"""Shared drawing helpers: panels, glows, scanlines, vignettes and easing.

Everything here is deliberately cheap or cached.  The scanline and vignette
overlays in particular are built once per screen size and then blitted, rather
than being recomputed per frame.
"""

from __future__ import annotations

import math

from .pygame_runtime import pygame
from .theme import Color, PALETTE, mix, shade, with_alpha

__all__ = [
    "ease_out_cubic",
    "ease_in_out",
    "pulse",
    "panel",
    "outline",
    "glow_frame",
    "corner_ticks",
    "vertical_gradient",
    "scanlines",
    "vignette",
    "dither_band",
    "reflect",
    "horizon_glow",
]


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------
def ease_out_cubic(t: float) -> float:
    """Standard ease-out; used for card focus growth and carousel glide."""
    clamped = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - clamped) ** 3


def ease_in_out(t: float) -> float:
    """Symmetric ease; used for slow ambient motion."""
    clamped = max(0.0, min(1.0, t))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def pulse(time_ms: int, period_ms: int = 1600, low: float = 0.35, high: float = 1.0) -> float:
    """A smooth 0..1 oscillation used for focus breathing and 'updating' dots."""
    phase = (time_ms % period_ms) / period_ms
    wave = (math.sin(phase * math.tau) + 1.0) * 0.5
    return low + (high - low) * wave


# ---------------------------------------------------------------------------
# Panels and frames
# ---------------------------------------------------------------------------
def panel(
    target: pygame.Surface,
    rect: pygame.Rect,
    fill: Color = PALETTE["panel"],
    edge: Color | None = PALETTE["panel_edge"],
    radius: int = 6,
    alpha: int = 255,
) -> None:
    """Draw a soft-cornered panel with an optional 1px edge."""
    if alpha >= 255:
        pygame.draw.rect(target, fill, rect, border_radius=radius)
    else:
        layer = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(
            layer, with_alpha(fill, alpha), layer.get_rect(), border_radius=radius
        )
        target.blit(layer, rect.topleft)
    if edge is not None:
        pygame.draw.rect(target, edge, rect, width=1, border_radius=radius)


def outline(
    target: pygame.Surface,
    rect: pygame.Rect,
    fill: Color,
    width: int = 2,
    radius: int = 6,
) -> None:
    """Draw just a rounded outline."""
    pygame.draw.rect(target, fill, rect, width=width, border_radius=radius)


def glow_frame(
    target: pygame.Surface,
    rect: pygame.Rect,
    fill: Color,
    intensity: float = 1.0,
    layers: int = 3,
    radius: int = 8,
) -> None:
    """Draw a restrained multi-ring glow around *rect*.

    Three thin rings at falling alpha read as a soft halo without the blown-out
    bloom the brief warns against.
    """
    strength = max(0.0, min(1.0, intensity))
    for index in range(layers, 0, -1):
        spread = index * 3
        alpha = int(46 * strength * (1.0 - (index - 1) / max(1, layers)))
        if alpha <= 0:
            continue
        ring = rect.inflate(spread * 2, spread * 2)
        layer = pygame.Surface(ring.size, pygame.SRCALPHA)
        pygame.draw.rect(
            layer,
            with_alpha(fill, alpha),
            layer.get_rect(),
            width=2,
            border_radius=radius + spread,
        )
        target.blit(layer, ring.topleft)


def corner_ticks(
    target: pygame.Surface, rect: pygame.Rect, fill: Color, length: int = 14, width: int = 3
) -> None:
    """Draw four L-shaped focus brackets -- the selection marker."""
    left, top, right, bottom = rect.left, rect.top, rect.right - 1, rect.bottom - 1
    for x_sign, y_sign, corner in (
        (1, 1, (left, top)),
        (-1, 1, (right, top)),
        (1, -1, (left, bottom)),
        (-1, -1, (right, bottom)),
    ):
        origin_x, origin_y = corner
        pygame.draw.line(
            target, fill, (origin_x, origin_y), (origin_x + x_sign * length, origin_y), width
        )
        pygame.draw.line(
            target, fill, (origin_x, origin_y), (origin_x, origin_y + y_sign * length), width
        )


# ---------------------------------------------------------------------------
# Full-screen treatments
# ---------------------------------------------------------------------------
def vertical_gradient(
    size: tuple[int, int], top: Color, bottom: Color, bands: int = 48
) -> pygame.Surface:
    """Build a banded vertical gradient.

    Banding is intentional -- it keeps the field readable as *pixel* art rather
    than a smooth photographic wash, and it is far cheaper than per-row fills.
    """
    width, height = size
    surface = pygame.Surface(size)
    band_height = max(1, height // bands)
    y = 0
    while y < height:
        ratio = y / max(1, height - 1)
        surface.fill(
            mix(top, bottom, ratio), pygame.Rect(0, y, width, min(band_height, height - y))
        )
        y += band_height
    return surface


def scanlines(size: tuple[int, int], alpha: int = 30, spacing: int = 3) -> pygame.Surface:
    """Build a restrained CRT scanline overlay (one dark line every *spacing*)."""
    width, height = size
    surface = pygame.Surface(size, pygame.SRCALPHA)
    surface.fill((0, 0, 0, 0))
    line = (0, 0, 0, alpha)
    for y in range(0, height, spacing):
        pygame.draw.line(surface, line, (0, y), (width, y))
    return surface


def vignette(size: tuple[int, int], strength: int = 108, steps: int = 26) -> pygame.Surface:
    """Build a rectangular vignette that pushes focus towards the centre."""
    width, height = size
    surface = pygame.Surface(size, pygame.SRCALPHA)
    surface.fill((0, 0, 0, 0))
    for index in range(steps):
        ratio = index / max(1, steps - 1)
        alpha = int(strength * (ratio**2.2))
        inset = int((1.0 - ratio) * min(width, height) * 0.5)
        rect = pygame.Rect(inset, inset, width - inset * 2, height - inset * 2)
        if rect.width <= 0 or rect.height <= 0:
            continue
        pygame.draw.rect(surface, (0, 0, 0, alpha), rect, width=max(2, steps - index))
    return surface


def dither_band(
    size: tuple[int, int], fill: Color, alpha: int = 40, step: int = 4
) -> pygame.Surface:
    """Build a checkerboard dither wash used to texture large flat areas."""
    width, height = size
    surface = pygame.Surface(size, pygame.SRCALPHA)
    surface.fill((0, 0, 0, 0))
    colour = with_alpha(fill, alpha)
    for y in range(0, height, step):
        offset = 0 if (y // step) % 2 == 0 else step // 2
        for x in range(offset, width, step):
            surface.fill(colour, pygame.Rect(x, y, step // 2, step // 2))
    return surface


def reflect(surface: pygame.Surface, height: int, fade: int = 130) -> pygame.Surface:
    """Return a vertically flipped, fading copy -- the cover-flow floor sheen."""
    flipped = pygame.transform.flip(surface, False, True)
    clipped_height = min(height, flipped.get_height())
    reflection = pygame.Surface((flipped.get_width(), clipped_height), pygame.SRCALPHA)
    reflection.blit(flipped, (0, 0))
    shadow = pygame.Surface((flipped.get_width(), clipped_height), pygame.SRCALPHA)
    bands = 16
    band_height = max(1, clipped_height // bands)
    for index in range(bands):
        alpha = int(fade + (255 - fade) * (index / max(1, bands - 1)))
        shadow.fill(
            (0, 0, 0, min(255, alpha)),
            pygame.Rect(0, index * band_height, flipped.get_width(), band_height),
        )
    reflection.blit(shadow, (0, 0), special_flags=pygame.BLEND_RGBA_SUB)
    return reflection


def horizon_glow(
    size: tuple[int, int], fill: Color, bands: int = 18
) -> pygame.Surface:
    """Build a soft light bar used as the cover-flow horizon."""
    width, height = size
    surface = pygame.Surface(size, pygame.SRCALPHA)
    surface.fill((0, 0, 0, 0))
    for index in range(bands):
        ratio = index / max(1, bands - 1)
        alpha = int(70 * (1.0 - ratio) ** 2)
        inset = int(ratio * width * 0.22)
        thickness = max(1, int((1.0 - ratio) * height))
        rect = pygame.Rect(inset, (height - thickness) // 2, width - inset * 2, thickness)
        surface.fill(with_alpha(shade(fill, 1.0), alpha), rect)
    return surface
