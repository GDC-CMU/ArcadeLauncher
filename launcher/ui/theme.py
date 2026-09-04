"""Palette, pixel font and font cache -- the shared visual vocabulary.

The identity is a pixel-arcade one, so headings, titles, badges and control
keys are drawn with a hand-built 5x7 bitmap font scaled by whole numbers. That
keeps every edge crisp at 800x600, renders identically on the Windows dev
machine and the Linux cabinet, and needs no font file in the repository.

Prose (game descriptions, status details) uses Pygame's bundled font, which
handles mixed case and wrapping better than a 5x7 grid.
"""

from __future__ import annotations

from typing import Final

from ..status import GameStatus
from .pygame_runtime import pygame

__all__ = [
    "Color",
    "PALETTE",
    "STATUS_COLORS",
    "STATUS_ACCENTS",
    "color",
    "shade",
    "mix",
    "with_alpha",
    "PixelFont",
    "FontBook",
]

Color = tuple[int, int, int]

#: The deliberate house palette: CMU red, electric cyan, warm amber, violet and
#: a deep dark field, plus the neutrals needed to build panels from them.
PALETTE: Final[dict[str, Color]] = {
    # Deep dark field
    "void": (7, 7, 16),
    "night": (14, 13, 30),
    "panel": (23, 21, 44),
    "panel_light": (37, 33, 66),
    "panel_edge": (61, 54, 104),
    # CMU red
    "cmu_red": (196, 18, 48),
    "ember_red": (241, 74, 58),
    # Warm amber
    "warm_amber": (255, 178, 44),
    "amber_deep": (176, 106, 12),
    # Electric cyan
    "electric_cyan": (46, 227, 255),
    "deep_cyan": (16, 116, 152),
    # Violet
    "violet": (154, 96, 246),
    "deep_violet": (76, 44, 132),
    # Support
    "mint": (86, 245, 176),
    "bone": (238, 240, 250),
    "steel": (132, 136, 166),
    "slate": (86, 90, 120),
    "ink": (9, 8, 20),
}

#: Badge colour per lifecycle state. Every state is a different hue *and* a
#: different label, so the distinction survives a photo, a projector or a
#: colour-blind visitor.
STATUS_COLORS: Final[dict[GameStatus, Color]] = {
    GameStatus.READY: PALETTE["mint"],
    GameStatus.CACHED_OFFLINE: PALETTE["warm_amber"],
    GameStatus.UPDATING: PALETTE["electric_cyan"],
    GameStatus.PENDING: PALETTE["steel"],
    GameStatus.COMING_SOON: PALETTE["violet"],
    GameStatus.UNAVAILABLE: PALETTE["ember_red"],
}

#: Darker companion used for badge fills and card edge glows.
STATUS_ACCENTS: Final[dict[GameStatus, Color]] = {
    GameStatus.READY: (16, 74, 54),
    GameStatus.CACHED_OFFLINE: (74, 48, 8),
    GameStatus.UPDATING: (10, 58, 76),
    GameStatus.PENDING: (36, 38, 54),
    GameStatus.COMING_SOON: (44, 24, 78),
    GameStatus.UNAVAILABLE: (78, 20, 18),
}


def color(name: str) -> Color:
    """Look up a palette colour by name.

    Raises:
        KeyError: If *name* is not in the palette. Failing loudly keeps the
            manifest's ``art.palette`` honest.
    """
    try:
        return PALETTE[name]
    except KeyError:
        raise KeyError(
            f"unknown palette colour '{name}'; known colours: "
            + ", ".join(sorted(PALETTE))
        ) from None


def shade(base: Color, factor: float) -> Color:
    """Scale *base* towards black (<1) or white (>1)."""
    if factor <= 1.0:
        return tuple(max(0, min(255, int(channel * factor))) for channel in base)  # type: ignore[return-value]
    reach = factor - 1.0
    return tuple(  # type: ignore[return-value]
        max(0, min(255, int(channel + (255 - channel) * reach))) for channel in base
    )


def mix(first: Color, second: Color, amount: float) -> Color:
    """Blend *first* towards *second* by *amount* in ``0..1``."""
    ratio = max(0.0, min(1.0, amount))
    return tuple(  # type: ignore[return-value]
        int(round(a + (b - a) * ratio)) for a, b in zip(first, second)
    )


def with_alpha(base: Color, alpha: int) -> tuple[int, int, int, int]:
    """Return *base* as an RGBA tuple."""
    return (base[0], base[1], base[2], max(0, min(255, alpha)))


# ---------------------------------------------------------------------------
# 5x7 bitmap font
# ---------------------------------------------------------------------------
_GLYPHS: Final[dict[str, tuple[str, ...]]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00110", "01100"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "'": ("00100", "00100", "00000", "00000", "00000", "00000", "00000"),
    '"': ("01010", "01010", "00000", "00000", "00000", "00000", "00000"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    "%": ("11001", "11010", "00010", "00100", "01000", "01011", "10011"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "#": ("01010", "01010", "11111", "01010", "11111", "01010", "01010"),
}

GLYPH_WIDTH: Final[int] = 5
GLYPH_HEIGHT: Final[int] = 7


class PixelFont:
    """Renders uppercase text from the built-in 5x7 grid.

    Glyphs are drawn once at 1:1 and then scaled by an integer factor, so the
    result is exact nearest-neighbour pixel art with no filtering.  Every
    rendered string is memoised on ``(text, scale, colour, tracking)``.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int, Color, int], pygame.Surface] = {}

    @staticmethod
    def measure(text: str, scale: int = 2, tracking: int = 1) -> tuple[int, int]:
        """Return the pixel size ``text`` will occupy."""
        count = len(text)
        if count == 0:
            return (0, GLYPH_HEIGHT * scale)
        width = (count * GLYPH_WIDTH + (count - 1) * tracking) * scale
        return (width, GLYPH_HEIGHT * scale)

    def render(
        self,
        text: str,
        scale: int = 2,
        fill: Color = PALETTE["bone"],
        tracking: int = 1,
    ) -> pygame.Surface:
        """Return a cached surface containing *text*."""
        normalised = text.upper()
        key = (normalised, scale, fill, tracking)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        surface = self._build(normalised, scale, fill, tracking)
        self._cache[key] = surface
        return surface

    def draw(
        self,
        target: pygame.Surface,
        text: str,
        position: tuple[int, int],
        scale: int = 2,
        fill: Color = PALETTE["bone"],
        tracking: int = 1,
        anchor: str = "topleft",
    ) -> pygame.Rect:
        """Blit *text* onto *target* and return the rect it occupied."""
        surface = self.render(text, scale, fill, tracking)
        rect = surface.get_rect(**{anchor: position})
        target.blit(surface, rect)
        return rect

    def _build(self, text: str, scale: int, fill: Color, tracking: int) -> pygame.Surface:
        count = len(text)
        if count == 0:
            return pygame.Surface((0, GLYPH_HEIGHT * scale), pygame.SRCALPHA)
        base_width = count * GLYPH_WIDTH + (count - 1) * tracking
        base = pygame.Surface((base_width, GLYPH_HEIGHT), pygame.SRCALPHA)
        base.fill((0, 0, 0, 0))
        rgba = with_alpha(fill, 255)
        for index, character in enumerate(text):
            rows = _GLYPHS.get(character) or _GLYPHS["?"]
            origin_x = index * (GLYPH_WIDTH + tracking)
            for row_index, row in enumerate(rows):
                for column, bit in enumerate(row):
                    if bit == "1":
                        base.set_at((origin_x + column, row_index), rgba)
        if scale == 1:
            return base
        return pygame.transform.scale(base, (base_width * scale, GLYPH_HEIGHT * scale))


class FontBook:
    """Lazily-created Pygame fonts for prose, keyed by point size.

    Uses the font bundled with Pygame rather than :func:`pygame.font.SysFont`,
    so nothing has to be installed on the cabinet and the layout is the same
    everywhere. Note that *layout* is not *pixels*: different SDL_ttf builds
    antialias the same glyphs slightly differently, so a screenshot taken on a
    developer machine is not bitwise reproducible on the cabinet. See
    :func:`tools.generate_previews.write_render_manifest`.
    """

    #: Named sizes used across the gallery. All are comfortably readable from
    #: several feet away on an 800x600 cabinet screen.
    SIZES: Final[dict[str, int]] = {
        "caption": 16,
        "body": 19,
        "lead": 22,
        "title": 28,
    }

    def __init__(self) -> None:
        self._fonts: dict[int, pygame.font.Font] = {}
        self._rendered: dict[tuple[int, str, Color], pygame.Surface] = {}

    def at(self, size: int) -> pygame.font.Font:
        """Return the bundled font at *size* points."""
        font = self._fonts.get(size)
        if font is None:
            font = pygame.font.Font(None, size)
            self._fonts[size] = font
        return font

    def named(self, name: str) -> pygame.font.Font:
        """Return the font for a named size (see :data:`SIZES`)."""
        return self.at(self.SIZES[name])

    def render(self, text: str, name: str, fill: Color) -> pygame.Surface:
        """Return a cached antialiased surface for *text*."""
        size = self.SIZES[name]
        key = (size, text, fill)
        cached = self._rendered.get(key)
        if cached is None:
            cached = self.at(size).render(text, True, fill)
            self._rendered[key] = cached
        return cached

    def wrap(self, text: str, name: str, width: int, max_lines: int = 3) -> list[str]:
        """Greedy word wrap of *text* to *width* pixels, ellipsised if needed."""
        font = self.named(name)
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if font.size(candidate)[0] <= width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
                if len(lines) == max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and (
            len(" ".join(lines).split()) < len(words)
        ):
            last = lines[-1]
            while last and font.size(last + "...")[0] > width:
                last = last[:-1].rstrip()
            lines[-1] = last + "..."
        return lines
