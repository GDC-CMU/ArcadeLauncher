"""The three gallery view modes.

Kept in a dependency-free module so that :mod:`launcher.settings` (which must
not import Pygame) and the renderer can both name the same modes.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["ViewMode", "CYCLE_ORDER"]


class ViewMode(Enum):
    """A gallery composition.

    All three modes are real views over the same manifest and the same
    selection index -- switching is a pure presentation change.
    """

    GRID = "grid"
    CAROUSEL = "carousel"
    COVER_FLOW = "cover-flow"

    @property
    def label(self) -> str:
        """Human-readable name shown in the header."""
        return {
            ViewMode.GRID: "GRID",
            ViewMode.CAROUSEL: "CAROUSEL",
            ViewMode.COVER_FLOW: "COVER FLOW",
        }[self]

    @property
    def slot(self) -> int:
        """1-based position, matching the keyboard shortcuts 1/2/3."""
        return CYCLE_ORDER.index(self) + 1

    def next(self) -> "ViewMode":
        """Return the next mode in the Grid -> Carousel -> Cover Flow cycle."""
        index = CYCLE_ORDER.index(self)
        return CYCLE_ORDER[(index + 1) % len(CYCLE_ORDER)]

    @classmethod
    def parse(cls, raw: str) -> "ViewMode":
        """Parse a mode name, tolerating case and ``_``/``-`` differences.

        Raises:
            ValueError: If *raw* names no known mode.
        """
        key = str(raw).strip().lower().replace("_", "-").replace(" ", "-")
        for mode in cls:
            if mode.value == key:
                return mode
        options = ", ".join(mode.value for mode in CYCLE_ORDER)
        raise ValueError(f"unknown gallery mode '{raw}' (expected one of: {options})")

    @classmethod
    def from_slot(cls, slot: int) -> "ViewMode":
        """Return the mode for a 1-based slot number.

        Raises:
            ValueError: If *slot* is out of range.
        """
        if not 1 <= slot <= len(CYCLE_ORDER):
            raise ValueError(f"gallery mode slot {slot} out of range 1..{len(CYCLE_ORDER)}")
        return CYCLE_ORDER[slot - 1]


#: Order used by the Select button / Tab key: Grid -> Carousel -> Cover Flow.
CYCLE_ORDER: tuple[ViewMode, ...] = (
    ViewMode.GRID,
    ViewMode.CAROUSEL,
    ViewMode.COVER_FLOW,
)
