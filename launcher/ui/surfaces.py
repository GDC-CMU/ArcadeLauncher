"""The surface cache -- the reason the gallery holds 60 FPS on weak hardware.

Card art, backgrounds, panels, scanline overlays and perspective variants are
expensive to build and never change between frames, so each one is built once,
keyed, and reused.  The cache records hits and misses so a test can *prove*
that a second frame does no rebuilding (acceptance criterion D9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Hashable

from .pygame_runtime import pygame

__all__ = ["CacheStats", "SurfaceCache"]


@dataclass(slots=True)
class CacheStats:
    """Counters for cache effectiveness."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    def reset(self) -> None:
        self.hits = 0
        self.misses = 0
        self.evictions = 0


@dataclass(slots=True)
class SurfaceCache:
    """A keyed store of pre-rendered :class:`pygame.Surface` objects.

    Args:
        capacity: Soft upper bound. When exceeded, the least recently used
            entries are dropped so a long club-fair session cannot grow without
            limit.
    """

    capacity: int = 512
    stats: CacheStats = field(default_factory=CacheStats)
    _entries: dict[Hashable, pygame.Surface] = field(default_factory=dict)

    def get(
        self, key: Hashable, build: Callable[[], pygame.Surface]
    ) -> pygame.Surface:
        """Return the surface for *key*, building it once on first request."""
        surface = self._entries.get(key)
        if surface is not None:
            self.stats.hits += 1
            # Refresh recency: dicts preserve insertion order.
            self._entries.pop(key)
            self._entries[key] = surface
            return surface
        self.stats.misses += 1
        surface = build()
        self._entries[key] = surface
        self._evict_if_needed()
        return surface

    def peek(self, key: Hashable) -> pygame.Surface | None:
        """Return a cached surface without building or counting a lookup."""
        return self._entries.get(key)

    def clear(self) -> None:
        """Drop everything (called when SDL is released before a launch)."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: Hashable) -> bool:
        return key in self._entries

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self.capacity:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
            self.stats.evictions += 1
