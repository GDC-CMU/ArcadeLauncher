"""Shared drawing helpers: panels, glows, vignettes and easing.

Everything here is deliberately cheap or cached.  The vignette overlay in
particular is built once per screen size and then blitted, rather than being
recomputed per frame.
"""

from __future__ import annotations

import math

from .pygame_runtime import pygame
from .theme import Color, PALETTE, mix, shade, with_alpha

__all__ = [
    "ease_out_cubic",
    "ease_in_out",
    "pulse",
    "lerp_stops",
    "wrapped_distance",
    "panel",
    "outline",
    "glow_frame",
    "corner_ticks",
    "vertical_gradient",
    "vignette",
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


def lerp_stops(value: float, stops: tuple[tuple[float, float], ...]) -> float:
    """Piecewise-linear interpolation through ascending ``(x, y)`` stops.

    Turns a continuous distance from the current selection into a position,
    scale or offset that *slides* between the values a discrete lookup would
    have snapped between -- what makes carousel and cover-flow movement read
    as motion instead of a sequence of teleports.  Below the first stop or
    above the last, the nearest edge value holds rather than extrapolating.
    """
    if value <= stops[0][0]:
        return stops[0][1]
    for (x0, y0), (x1, y1) in zip(stops, stops[1:]):
        if value <= x1:
            span = x1 - x0
            t = 0.0 if span <= 0 else (value - x0) / span
            return y0 + (y1 - y0) * t
    return stops[-1][1]


def wrapped_distance(index: int, position: float, count: int) -> float:
    """Signed distance from *position* to *index*, taking the shortest way
    around a wrapping row of *count* cards.

    Used by every mode that lays cards out along a continuous, wrapping axis:
    without this, a selection near one end of the row would compute its
    neighbours' distance the long way around and either draw them off-screen
    or -- if plugged into a glide -- sweep the whole row across the screen
    when wrapping from the last card back to the first.
    """
    if count <= 0:
        return 0.0
    raw = index - position
    return ((raw + count / 2) % count) - count / 2


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


def vignette(size: tuple[int, int], strength: int = 108, steps: int = 26) -> pygame.Surface:
    """Build a rectangular vignette that pushes focus towards the centre.

    Each ring's stroke width is sized to exactly match the gap to the next
    ring (plus a hair of overlap for rounding), so the rings tile into one
    smooth gradient. An earlier version sized the stroke independently of the
    step -- ``steps - index`` px, unrelated to the actual gap between rings --
    which left every ring's edge visible as a hard line, the exact "grid of
    lines" complaint this build was asked to fix.
    """
    width, height = size
    surface = pygame.Surface(size, pygame.SRCALPHA)
    surface.fill((0, 0, 0, 0))
    half = min(width, height) / 2
    band = half / max(1, steps - 1)
    ring_width = max(2, int(band) + 2)
    for index in range(steps):
        ratio = index / max(1, steps - 1)
        alpha = int(strength * (ratio**2.2))
        if alpha <= 0:
            continue
        inset = int((1.0 - ratio) * half)
        rect = pygame.Rect(inset, inset, width - inset * 2, height - inset * 2)
        if rect.width <= 0 or rect.height <= 0:
            continue
        pygame.draw.rect(surface, (0, 0, 0, alpha), rect, width=ring_width)
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
