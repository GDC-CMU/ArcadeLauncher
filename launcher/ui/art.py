"""Procedural pixel card art -- one distinct composition per game.

Nothing is loaded from disk and nothing is fetched from the network: every
cover is drawn in code onto a small surface (roughly a third of the final
size) and then scaled up with nearest-neighbour, which is what gives the
chunky, deliberate pixel look at 800x600.

Motifs are addressed by name from the manifest (``art.motif``), so adding a new
game means picking an existing motif or adding one function here -- never
shipping a binary asset.
"""

from __future__ import annotations

import math
import random

from ..manifest import CardArt
from .pygame_runtime import pygame
from .theme import Color, PALETTE, color as palette_color, mix, shade

__all__ = ["render_card_art", "MOTIF_RENDERERS"]


# ---------------------------------------------------------------------------
# Fractional drawing helpers (all coordinates are 0..1 of the base surface)
# ---------------------------------------------------------------------------
def _rect(
    surface: pygame.Surface, fx: float, fy: float, fw: float, fh: float, fill: Color
) -> pygame.Rect:
    width, height = surface.get_size()
    rect = pygame.Rect(
        int(fx * width),
        int(fy * height),
        max(1, int(fw * width)),
        max(1, int(fh * height)),
    )
    surface.fill(fill, rect)
    return rect


def _ellipse(
    surface: pygame.Surface, fx: float, fy: float, fw: float, fh: float, fill: Color
) -> None:
    width, height = surface.get_size()
    rect = pygame.Rect(
        int(fx * width),
        int(fy * height),
        max(2, int(fw * width)),
        max(2, int(fh * height)),
    )
    pygame.draw.ellipse(surface, fill, rect)


def _polygon(
    surface: pygame.Surface, points: list[tuple[float, float]], fill: Color
) -> None:
    width, height = surface.get_size()
    pygame.draw.polygon(
        surface, fill, [(int(x * width), int(y * height)) for x, y in points]
    )


def _line(
    surface: pygame.Surface,
    start: tuple[float, float],
    end: tuple[float, float],
    fill: Color,
    thickness: int = 1,
) -> None:
    width, height = surface.get_size()
    pygame.draw.line(
        surface,
        fill,
        (int(start[0] * width), int(start[1] * height)),
        (int(end[0] * width), int(end[1] * height)),
        thickness,
    )


def _sky(surface: pygame.Surface, top: Color, bottom: Color, bands: int = 12) -> None:
    """Fill the base surface with a banded gradient."""
    width, height = surface.get_size()
    band = max(1, height // bands)
    y = 0
    while y < height:
        surface.fill(
            mix(top, bottom, y / max(1, height - 1)),
            pygame.Rect(0, y, width, min(band, height - y)),
        )
        y += band


def _speckle(
    surface: pygame.Surface, rng: random.Random, fill: Color, count: int, ceiling: float = 1.0
) -> None:
    """Scatter deterministic single-pixel highlights."""
    width, height = surface.get_size()
    for _ in range(count):
        x = rng.randrange(width)
        y = rng.randrange(max(1, int(height * ceiling)))
        surface.set_at((x, y), fill)


def _dither(surface: pygame.Surface, fill: Color, step: int = 3) -> None:
    """Lay a sparse checkerboard over the whole cover for pixel texture."""
    width, height = surface.get_size()
    for y in range(0, height, step):
        for x in range((y // step) % 2, width, step):
            surface.set_at((x, y), mix(surface.get_at((x, y))[:3], fill, 0.18))


# ---------------------------------------------------------------------------
# Motifs
# ---------------------------------------------------------------------------
def _motif_duel(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """Two fighters squaring off under a sunburst -- Street Fighter."""
    primary, secondary, dark = colors
    _sky(surface, shade(primary, 0.30), shade(dark, 1.25))

    # Sunburst behind the fighters.
    for index in range(12):
        angle = math.pi * (index / 11.0)
        _polygon(
            surface,
            [
                (0.5, 0.62),
                (0.5 + math.cos(angle) * 0.75, 0.62 - math.sin(angle) * 0.62),
                (0.5 + math.cos(angle + 0.11) * 0.75, 0.62 - math.sin(angle + 0.11) * 0.62),
            ],
            mix(shade(primary, 0.55), secondary, 0.22 if index % 2 else 0.05),
        )
    _ellipse(surface, 0.34, 0.16, 0.32, 0.34, mix(secondary, PALETTE["bone"], 0.25))

    # Arena floor.
    _rect(surface, 0.0, 0.72, 1.0, 0.28, shade(dark, 1.6))
    _rect(surface, 0.0, 0.72, 1.0, 0.03, secondary)
    for index in range(6):
        _rect(surface, index / 6.0 + 0.01, 0.80, 0.02, 0.16, shade(dark, 2.1))

    # Left fighter, mid-punch.
    body = shade(dark, 0.6)
    _rect(surface, 0.16, 0.34, 0.09, 0.09, body)          # head
    _rect(surface, 0.15, 0.43, 0.11, 0.19, body)          # torso
    _rect(surface, 0.26, 0.46, 0.14, 0.05, body)          # punching arm
    _rect(surface, 0.38, 0.44, 0.06, 0.09, primary)       # glove
    _rect(surface, 0.15, 0.62, 0.05, 0.12, body)          # legs
    _rect(surface, 0.21, 0.62, 0.05, 0.12, body)

    # Right fighter, guarding.
    _rect(surface, 0.74, 0.32, 0.09, 0.09, body)
    _rect(surface, 0.73, 0.41, 0.11, 0.21, body)
    _rect(surface, 0.63, 0.45, 0.11, 0.05, body)
    _rect(surface, 0.59, 0.43, 0.06, 0.09, secondary)
    _rect(surface, 0.73, 0.62, 0.05, 0.12, body)
    _rect(surface, 0.79, 0.62, 0.05, 0.12, body)

    # Impact spark between them.
    _polygon(
        surface,
        [(0.50, 0.40), (0.56, 0.47), (0.50, 0.54), (0.44, 0.47)],
        mix(secondary, PALETTE["bone"], 0.5),
    )

    # Health bars.
    _rect(surface, 0.05, 0.06, 0.40, 0.05, shade(dark, 1.8))
    _rect(surface, 0.055, 0.068, 0.30, 0.034, secondary)
    _rect(surface, 0.55, 0.06, 0.40, 0.05, shade(dark, 1.8))
    _rect(surface, 0.555, 0.068, 0.22, 0.034, primary)
    _speckle(surface, rng, mix(secondary, PALETTE["bone"], 0.6), 16, ceiling=0.7)


def _motif_relay(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """Cartridges handed along an arc, mutating as they go -- Pass The Game."""
    primary, secondary, dark = colors
    _sky(surface, shade(dark, 1.5), shade(primary, 0.42))

    # Faint grid floor for depth.
    for index in range(1, 7):
        _line(surface, (0.0, 0.62 + index * 0.06), (1.0, 0.62 + index * 0.06), shade(primary, 0.55))
    for index in range(9):
        _line(surface, (index / 8.0, 0.62), (index / 8.0 * 1.6 - 0.3, 1.0), shade(primary, 0.5))

    # Five cartridges along an arc, drifting from primary to secondary.
    for index in range(5):
        t = index / 4.0
        x = 0.08 + t * 0.72
        y = 0.46 - math.sin(t * math.pi) * 0.24
        tint = mix(primary, secondary, t)
        _rect(surface, x, y, 0.14, 0.17, shade(tint, 0.45))
        _rect(surface, x + 0.012, y + 0.02, 0.116, 0.09, tint)
        _rect(surface, x + 0.035, y + 0.13, 0.07, 0.03, shade(tint, 1.4))
        if index < 4:
            _line(
                surface,
                (x + 0.15, y + 0.08),
                (0.08 + (index + 1) / 4.0 * 0.72, 0.46 - math.sin((index + 1) / 4.0 * math.pi) * 0.24 + 0.08),
                mix(secondary, PALETTE["bone"], 0.35),
            )

    # Arrow tip closing the loop.
    _polygon(surface, [(0.86, 0.44), (0.96, 0.51), (0.86, 0.58)], secondary)
    _speckle(surface, rng, mix(secondary, PALETTE["bone"], 0.7), 22, ceiling=0.6)


def _motif_flight(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """Pipes, clouds and a plucky flyer -- Flappy Scotty."""
    primary, secondary, dark = colors
    _sky(surface, shade(primary, 0.55), mix(primary, PALETTE["bone"], 0.32))

    _ellipse(surface, 0.72, 0.06, 0.22, 0.22, mix(secondary, PALETTE["bone"], 0.45))

    for cloud_x, cloud_y, cloud_w in ((0.06, 0.16, 0.22), (0.40, 0.08, 0.18)):
        _rect(surface, cloud_x, cloud_y, cloud_w, 0.07, PALETTE["bone"])
        _rect(surface, cloud_x + 0.05, cloud_y - 0.045, cloud_w * 0.5, 0.06, PALETTE["bone"])

    # Pipe pairs.
    pipe_color = mix(secondary, PALETTE["mint"], 0.35)
    for pipe_index, (px, gap_y) in enumerate(((0.30, 0.44), (0.62, 0.58), (0.90, 0.36))):
        _rect(surface, px, 0.0, 0.13, gap_y - 0.13, shade(pipe_color, 0.75))
        _rect(surface, px - 0.015, gap_y - 0.17, 0.16, 0.05, pipe_color)
        _rect(surface, px, gap_y + 0.13, 0.13, 1.0, shade(pipe_color, 0.75))
        _rect(surface, px - 0.015, gap_y + 0.13, 0.16, 0.05, pipe_color)
        _rect(surface, px + 0.012, 0.0, 0.03, gap_y - 0.13, shade(pipe_color, 1.25))
        del pipe_index

    # Ground strip.
    _rect(surface, 0.0, 0.88, 1.0, 0.12, shade(dark, 1.7))
    _rect(surface, 0.0, 0.88, 1.0, 0.02, secondary)

    # The flyer.
    body = secondary
    _rect(surface, 0.10, 0.46, 0.13, 0.11, body)
    _rect(surface, 0.20, 0.44, 0.06, 0.06, shade(body, 1.25))     # head
    _rect(surface, 0.25, 0.46, 0.04, 0.02, PALETTE["warm_amber"])  # beak
    _rect(surface, 0.215, 0.452, 0.015, 0.015, PALETTE["ink"])     # eye
    _rect(surface, 0.09, 0.42, 0.09, 0.04, shade(body, 0.7))       # wing up
    _rect(surface, 0.07, 0.53, 0.06, 0.03, shade(body, 0.7))       # tail
    _speckle(surface, rng, PALETTE["bone"], 14, ceiling=0.5)


def _motif_hazard(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """Hazard stripes and a grinning skull -- Smart Ways To Die."""
    primary, secondary, dark = colors
    surface.fill(shade(dark, 1.3))

    # Diagonal caution stripes.
    width, height = surface.get_size()
    stripe = max(3, width // 12)
    for offset in range(-height, width + height, stripe * 2):
        pygame.draw.polygon(
            surface,
            shade(primary, 0.85),
            [
                (offset, 0),
                (offset + stripe, 0),
                (offset + stripe - height, height),
                (offset - height, height),
            ],
        )

    # Dark vignette panel so the skull reads clearly.
    _rect(surface, 0.14, 0.14, 0.72, 0.72, shade(dark, 1.05))
    _rect(surface, 0.14, 0.14, 0.72, 0.03, secondary)
    _rect(surface, 0.14, 0.83, 0.72, 0.03, secondary)

    # Skull.
    bone = mix(PALETTE["bone"], primary, 0.12)
    _rect(surface, 0.30, 0.26, 0.40, 0.34, bone)
    _rect(surface, 0.26, 0.32, 0.04, 0.22, bone)
    _rect(surface, 0.70, 0.32, 0.04, 0.22, bone)
    _rect(surface, 0.36, 0.60, 0.28, 0.12, bone)
    _rect(surface, 0.36, 0.34, 0.11, 0.13, shade(dark, 0.8))   # eye
    _rect(surface, 0.53, 0.34, 0.11, 0.13, shade(dark, 0.8))
    _polygon(surface, [(0.50, 0.48), (0.545, 0.56), (0.455, 0.56)], shade(dark, 0.8))
    for tooth in range(4):
        _rect(surface, 0.385 + tooth * 0.07, 0.60, 0.02, 0.12, shade(dark, 0.8))

    # Cracks.
    _line(surface, (0.30, 0.26), (0.38, 0.18), secondary)
    _line(surface, (0.66, 0.30), (0.76, 0.22), secondary)
    _speckle(surface, rng, secondary, 18)


def _motif_ember(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """A chilli under a rising flame -- Spicy Adventures."""
    primary, secondary, dark = colors
    _sky(surface, shade(dark, 1.15), shade(primary, 0.7))

    # Heat haze bands.
    for index in range(5):
        _rect(surface, 0.0, 0.70 + index * 0.06, 1.0, 0.02, shade(secondary, 0.55 + index * 0.08))

    # Flame behind the pepper.
    _polygon(
        surface,
        [(0.50, 0.04), (0.68, 0.34), (0.60, 0.32), (0.66, 0.56), (0.34, 0.56), (0.40, 0.32), (0.32, 0.34)],
        shade(secondary, 0.9),
    )
    _polygon(
        surface,
        [(0.50, 0.16), (0.61, 0.38), (0.56, 0.37), (0.59, 0.54), (0.41, 0.54), (0.44, 0.37), (0.39, 0.38)],
        mix(secondary, PALETTE["bone"], 0.45),
    )

    # Chilli body.
    _polygon(
        surface,
        [(0.42, 0.46), (0.60, 0.50), (0.66, 0.66), (0.56, 0.84), (0.42, 0.86), (0.34, 0.72), (0.36, 0.56)],
        primary,
    )
    _polygon(
        surface,
        [(0.44, 0.52), (0.55, 0.56), (0.57, 0.68), (0.50, 0.78), (0.43, 0.74)],
        shade(primary, 1.35),
    )
    # Stem.
    _rect(surface, 0.46, 0.38, 0.05, 0.11, PALETTE["mint"])
    _polygon(surface, [(0.40, 0.44), (0.54, 0.40), (0.52, 0.47)], shade(PALETTE["mint"], 0.8))

    # Rising embers.
    for _ in range(18):
        x = rng.uniform(0.08, 0.92)
        y = rng.uniform(0.05, 0.95)
        _rect(surface, x, y, 0.02, 0.025, mix(secondary, PALETTE["bone"], rng.uniform(0.0, 0.6)))


def _motif_orbit(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """A saucer racing past a ringed planet -- UFO Race."""
    primary, secondary, dark = colors
    _sky(surface, shade(dark, 1.0), shade(secondary, 0.45))

    # Star field.
    for _ in range(40):
        x = rng.randrange(surface.get_width())
        y = rng.randrange(surface.get_height())
        surface.set_at((x, y), mix(PALETTE["bone"], secondary, rng.uniform(0.0, 0.7)))

    # Ringed planet, lower right.
    _ellipse(surface, 0.60, 0.62, 0.46, 0.46, shade(secondary, 0.8))
    _ellipse(surface, 0.63, 0.65, 0.34, 0.34, shade(secondary, 1.15))
    for ring in range(3):
        pygame.draw.ellipse(
            surface,
            mix(primary, PALETTE["bone"], 0.25),
            pygame.Rect(
                int(0.48 * surface.get_width()),
                int((0.74 + ring * 0.03) * surface.get_height()),
                int(0.70 * surface.get_width()),
                max(2, int(0.10 * surface.get_height())),
            ),
            1,
        )

    # Speed streaks.
    for index in range(5):
        y = 0.16 + index * 0.09
        _rect(surface, 0.02 + index * 0.03, y, 0.20 - index * 0.02, 0.012, shade(primary, 0.9))

    # Saucer.
    _polygon(surface, [(0.20, 0.44), (0.68, 0.44), (0.56, 0.60), (0.30, 0.60)], shade(primary, 0.55))
    _ellipse(surface, 0.18, 0.36, 0.52, 0.16, primary)
    _ellipse(surface, 0.34, 0.24, 0.22, 0.18, mix(PALETTE["bone"], primary, 0.45))
    _ellipse(surface, 0.38, 0.27, 0.14, 0.11, shade(dark, 1.4))
    for light in range(3):
        _rect(surface, 0.26 + light * 0.14, 0.415, 0.05, 0.035, PALETTE["warm_amber"])

    # Tractor beam.
    _polygon(
        surface,
        [(0.30, 0.52), (0.58, 0.52), (0.70, 0.98), (0.18, 0.98)],
        mix(shade(primary, 0.6), PALETTE["bone"], 0.10),
    )


def _motif_maze(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """Maze corridors, scattered kibble and a chomping terrier -- Pac Dawg."""
    primary, secondary, dark = colors
    surface.fill(shade(dark, 1.05))

    # Maze walls: an outer loop plus a handful of L-shaped partitions, which
    # reads as "maze" without becoming a busy full grid of lines.
    wall = shade(primary, 0.85)
    _rect(surface, 0.08, 0.10, 0.84, 0.04, wall)
    _rect(surface, 0.08, 0.86, 0.84, 0.04, wall)
    _rect(surface, 0.08, 0.10, 0.04, 0.80, wall)
    _rect(surface, 0.88, 0.10, 0.04, 0.80, wall)
    _rect(surface, 0.24, 0.10, 0.04, 0.30, wall)
    _rect(surface, 0.24, 0.36, 0.20, 0.04, wall)
    _rect(surface, 0.60, 0.10, 0.04, 0.26, wall)
    _rect(surface, 0.40, 0.32, 0.24, 0.04, wall)
    _rect(surface, 0.36, 0.58, 0.04, 0.28, wall)
    _rect(surface, 0.36, 0.58, 0.30, 0.04, wall)
    _rect(surface, 0.68, 0.50, 0.04, 0.36, wall)
    _rect(surface, 0.44, 0.72, 0.28, 0.04, wall)

    # Kibble pellets scattered along the open corridors.
    pellet = mix(PALETTE["bone"], primary, 0.15)
    for fx, fy in (
        (0.14, 0.18), (0.14, 0.26), (0.14, 0.46), (0.14, 0.70), (0.14, 0.78),
        (0.30, 0.18), (0.46, 0.18), (0.52, 0.46), (0.60, 0.62), (0.76, 0.20),
        (0.80, 0.40), (0.80, 0.60), (0.80, 0.78), (0.46, 0.80), (0.60, 0.80),
    ):
        _rect(surface, fx, fy, 0.03, 0.03, pellet)

    # A bigger power-kibble, lower-left.
    _ellipse(surface, 0.145, 0.545, 0.06, 0.06, mix(PALETTE["bone"], primary, 0.35))

    # Four small rival chasers -- "four rivals hunt him down" -- evenly spread
    # so none crowds Pac-Dawg himself.
    rival_tints = (
        secondary,
        mix(secondary, PALETTE["bone"], 0.4),
        mix(secondary, primary, 0.5),
        shade(secondary, 1.3),
    )
    for tint, (fx, fy) in zip(
        rival_tints, ((0.74, 0.16), (0.50, 0.60), (0.24, 0.66), (0.70, 0.72))
    ):
        _rect(surface, fx, fy, 0.07, 0.055, tint)
        _rect(surface, fx + 0.008, fy + 0.05, 0.016, 0.016, shade(dark, 0.85))
        _rect(surface, fx + 0.045, fy + 0.05, 0.016, 0.016, shade(dark, 0.85))

    # Pac-Dawg: a round chomping head with two terrier ears, mid-corridor.
    head_x, head_y, radius = 0.30, 0.48, 0.11
    mouth = math.radians(32)
    _ellipse(
        surface,
        head_x - radius,
        head_y - radius,
        radius * 2,
        radius * 2,
        secondary,
    )
    _polygon(
        surface,
        [
            (head_x, head_y),
            (head_x + radius * 1.2, head_y - radius * math.sin(mouth) * 1.3),
            (head_x + radius * 1.2, head_y + radius * math.sin(mouth) * 1.3),
        ],
        shade(dark, 0.95),
    )
    _polygon(
        surface,
        [
            (head_x - 0.07, head_y - radius + 0.01),
            (head_x - 0.025, head_y - radius - 0.07),
            (head_x + 0.005, head_y - radius + 0.02),
        ],
        secondary,
    )
    _polygon(
        surface,
        [
            (head_x + 0.03, head_y - radius + 0.015),
            (head_x + 0.075, head_y - radius - 0.055),
            (head_x + 0.095, head_y - radius + 0.03),
        ],
        secondary,
    )
    _rect(surface, head_x - 0.015, head_y - radius * 0.45, 0.022, 0.022, shade(dark, 0.8))

    _speckle(surface, rng, mix(primary, PALETTE["bone"], 0.5), 10, ceiling=0.12)


def _motif_pitch(surface: pygame.Surface, colors: tuple[Color, Color, Color], rng: random.Random) -> None:
    """A head-soccer pitch: goals at both ends, a ball and two big-headed
    rivals squaring off -- Head Scotter.

    The characters are dark silhouettes (the same high-contrast trick
    ``_motif_duel`` uses for its fighters) rather than tints of *primary* or
    *secondary* mixed toward ``bone`` -- *primary* is already spent on the
    grass here, so a pale mix of the other two reads as a washed-out blob
    sitting on a background of a similar value. A near-black silhouette pops
    against the pitch regardless of which two colours a manifest entry picks,
    and a single accent stripe per jersey is enough to tell the two sides
    apart.
    """
    primary, secondary, dark = colors
    body = shade(dark, 0.55)

    # Night-match sky with a few floodlights.
    _sky(surface, shade(dark, 1.2), shade(dark, 0.8))
    for light_x in (0.14, 0.5, 0.86):
        _rect(surface, light_x - 0.03, 0.03, 0.06, 0.022, mix(PALETTE["bone"], secondary, 0.2))
        _rect(surface, light_x - 0.006, 0.052, 0.012, 0.05, shade(dark, 0.7))

    # Grass, mowed into a few bold bands -- fewer, thicker stripes than a
    # dense set of thin ones, which is what actually reads at card scale.
    field_top = 0.34
    band = (1.0 - field_top) / 4.0
    for index in range(4):
        tone = primary if index % 2 == 0 else shade(primary, 0.82)
        _rect(surface, 0.0, field_top + index * band, 1.0, band + 0.01, tone)

    # Halfway line and centre spot.
    line = mix(PALETTE["bone"], primary, 0.55)
    _rect(surface, 0.485, field_top, 0.03, 1.0 - field_top, line)
    _ellipse(surface, 0.465, 0.585, 0.07, 0.07, line)

    def _goal(near_x: float, direction: int) -> None:
        """Posts, crossbar and net opening onto the field from the edge."""
        post = PALETTE["bone"]
        far_x = near_x + direction * 0.12
        _rect(surface, near_x, 0.42, 0.02, 0.32, post)
        _rect(surface, far_x, 0.42, 0.02, 0.32, post)
        _rect(surface, min(near_x, far_x), 0.42, 0.14, 0.02, post)
        net = shade(post, 1.15)
        for i in range(3):
            nx = near_x + direction * (0.03 + i * 0.03)
            _line(surface, (nx, 0.44), (nx, 0.71), net)
        for j in range(4):
            ny = 0.44 + j * 0.07
            _line(surface, (near_x, ny), (far_x, ny), net)

    _goal(0.01, direction=1)
    _goal(0.99, direction=-1)

    def _player(head_x: float, accent: Color, ears: bool) -> None:
        """One big-headed rival: dark silhouette, single accent stripe."""
        head_y = 0.56
        radius = 0.115
        facing = 1 if head_x < 0.5 else -1

        if ears:
            # Two upright terrier ears, same silhouette colour as the head
            # so they read as part of one continuous shape.
            _polygon(
                surface,
                [
                    (head_x - radius * 0.75, head_y - radius * 0.65),
                    (head_x - radius * 0.30, head_y - radius * 1.55),
                    (head_x - radius * 0.05, head_y - radius * 0.75),
                ],
                body,
            )
            _polygon(
                surface,
                [
                    (head_x + radius * 0.75, head_y - radius * 0.65),
                    (head_x + radius * 0.30, head_y - radius * 1.55),
                    (head_x + radius * 0.05, head_y - radius * 0.75),
                ],
                body,
            )
        else:
            # A rounded mohawk band marks the rival's head instead of ears.
            _ellipse(
                surface,
                head_x - radius * 0.55,
                head_y - radius * 1.35,
                radius * 1.1,
                radius * 0.9,
                accent,
            )

        _ellipse(surface, head_x - radius, head_y - radius, radius * 2, radius * 2, body)
        eye_x = head_x + facing * radius * 0.35
        _rect(surface, eye_x - 0.014, head_y - 0.02, 0.028, 0.028, PALETTE["bone"])

        # Torso: attaches directly under the head (no gap), with one accent
        # stripe so the two sides are distinguishable at a glance.
        torso_top = head_y + radius - 0.01
        _rect(surface, head_x - 0.075, torso_top, 0.15, 0.16, body)
        _rect(surface, head_x - 0.075, torso_top + 0.06, 0.15, 0.035, accent)

        # An even, planted stance -- two short legs, two boot blocks, both
        # sides identical. A kicking pose was tried here first: an extended
        # leg reaching toward the ball, but at the sizes these cards actually
        # render at, its length and near-uniform thickness left it reading
        # as a detached slab rather than a limb. The ball sitting between
        # the two players already carries the action; the stance does not
        # need to.
        legs_top = torso_top + 0.16 - 0.01
        _rect(surface, head_x - 0.075, legs_top, 0.05, 0.11, body)
        _rect(surface, head_x + 0.025, legs_top, 0.05, 0.11, body)
        _rect(surface, head_x - 0.08, legs_top + 0.09, 0.06, 0.04, shade(body, 1.35))
        _rect(surface, head_x + 0.02, legs_top + 0.09, 0.06, 0.04, shade(body, 1.35))

    _player(0.28, PALETTE["warm_amber"], ears=True)
    _player(0.72, secondary, ears=False)

    # The ball, lifted just off the ground between the two heads so it reads
    # as airborne, with a dark ring so it pops against the pitch regardless
    # of what falls behind it.
    ball_x, ball_y = 0.5, 0.56
    _ellipse(surface, ball_x - 0.065, ball_y - 0.065, 0.13, 0.13, shade(dark, 0.5))
    _ellipse(surface, ball_x - 0.05, ball_y - 0.05, 0.10, 0.10, PALETTE["bone"])
    for dx, dy in ((-0.012, -0.01), (0.014, -0.006), (0.0, 0.016)):
        _rect(surface, ball_x + dx - 0.008, ball_y + dy - 0.008, 0.016, 0.016, shade(dark, 0.6))

    _speckle(surface, rng, mix(PALETTE["bone"], primary, 0.4), 14, ceiling=field_top)


MOTIF_RENDERERS = {
    "duel": _motif_duel,
    "relay": _motif_relay,
    "flight": _motif_flight,
    "hazard": _motif_hazard,
    "ember": _motif_ember,
    "orbit": _motif_orbit,
    "maze": _motif_maze,
    "pitch": _motif_pitch,
}


def render_card_art(
    art: CardArt, size: tuple[int, int], pixel: int = 3, dim: float = 1.0
) -> pygame.Surface:
    """Render *art* at *size*.

    Args:
        art: Manifest card-art configuration.
        size: Final pixel size of the cover.
        pixel: Chunk size. The motif is drawn at ``size / pixel`` and scaled up
            with nearest-neighbour, which is what makes the pixels square and
            crisp instead of smoothed.
        dim: Multiplier applied at the end; coming-soon covers are dimmed so
            playable games dominate the composition.

    Returns:
        A fresh opaque surface. Callers should cache it -- see
        :class:`launcher.ui.surfaces.SurfaceCache`.
    """
    width = max(16, size[0])
    height = max(12, size[1])
    base_size = (max(20, width // pixel), max(15, height // pixel))

    renderer = MOTIF_RENDERERS.get(art.motif)
    if renderer is None:  # pragma: no cover - manifest validation prevents this
        raise KeyError(f"unknown card-art motif '{art.motif}'")

    colors = (
        palette_color(art.palette[0]),
        palette_color(art.palette[1]),
        palette_color(art.palette[2]),
    )
    base = pygame.Surface(base_size)
    renderer(base, colors, random.Random(art.seed))
    _dither(base, PALETTE["ink"], step=3)

    scaled = pygame.transform.scale(base, (width, height))
    if dim < 1.0:
        veil = pygame.Surface((width, height), pygame.SRCALPHA)
        veil.fill((*PALETTE["ink"], int(255 * (1.0 - dim))))
        scaled.blit(veil, (0, 0))
    return scaled
