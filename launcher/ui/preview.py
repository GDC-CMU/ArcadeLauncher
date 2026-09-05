"""Attract-mode preview animations: decoding, caps, and the decode-once cache.

:mod:`launcher.previews` proves a game's preview manifest is well-formed and
that every frame path stays inside that game's own ``assets/preview``
checkout directory.  This module is what actually opens the image files --
the one place a preview's pixels are decoded -- and adds the two caps that
can only be checked once an image is open: per-frame pixel dimensions and
total decoded bytes across the whole animation.  Exceeding either rejects the
preview outright, logs one warning, and the caller falls back to procedural
card art -- never a crash, never a half-loaded animation.

Decoding happens at most once per game per gallery session: :class:`PreviewLibrary`
remembers the outcome -- including a deliberate rejection -- so a broken
game's checkout is probed once, not every time attract mode considers it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from ..manifest import GameEntry
from ..paths import checkout_dir
from ..previews import PreviewManifest, load_preview_manifest
from .pygame_runtime import pygame

__all__ = [
    "MAX_PREVIEW_FRAME_DIMENSION",
    "MAX_PREVIEW_TOTAL_DECODED_BYTES",
    "PreviewAnimation",
    "PreviewLibrary",
]

_log = logging.getLogger(__name__)

#: Per-frame width/height cap, in pixels. The contract's own guidance is
#: authoring at roughly 160x120-200x150; this leaves generous headroom for a
#: larger, still-reasonable loop while refusing anything that would make a
#: single frame absurdly expensive to decode and scale every session.
MAX_PREVIEW_FRAME_DIMENSION = 512

#: Total decoded RGBA bytes across every frame of one game's animation. This
#: is the bound that actually matters, since it is what stays resident in
#: memory for the life of the session. A real 16-frame, 200x150 loop -- the
#: contract's own example -- decodes to under 2 MiB, so 8 MiB leaves a
#: healthy margin for a longer or larger, still reasonable, loop.
MAX_PREVIEW_TOTAL_DECODED_BYTES = 8 * 1024 * 1024


class PreviewAnimation(NamedTuple):
    """Decoded preview frames for one game, at their native authored size.

    Frames are never pre-scaled here: a preview plays inside cards of several
    different sizes across the three view modes, and even within one. Scaling
    to a specific size on demand -- and caching *that* -- is the caller's job
    (see :meth:`~launcher.ui.components.RenderContext.preview_surface`).
    Decoding the raw image bytes is the expensive, one-time part this class
    exists to avoid repeating.
    """

    fps: int
    frames: tuple[pygame.Surface, ...]

    def frame_index(self, time_ms: int) -> int:
        """Which frame plays at *time_ms* (looping, clamped to non-negative)."""
        if not self.frames:
            return 0
        period_ms = max(1, round(1000 / self.fps))
        return (max(0, time_ms) // period_ms) % len(self.frames)


def _decode(manifest: PreviewManifest, *, game_id: str) -> PreviewAnimation | None:
    """Open every frame, enforcing the two decode-time caps.

    Returns ``None`` -- the whole animation rejected, never a partial one --
    the moment any frame fails to decode or either cap is exceeded.
    """
    frames: list[pygame.Surface] = []
    total_bytes = 0
    for path in manifest.frames:
        try:
            image = pygame.image.load(str(path))
        except (pygame.error, OSError) as exc:
            _log.warning(
                "rejecting preview for game '%s': could not decode %s: %s",
                game_id,
                path,
                exc,
            )
            return None

        width, height = image.get_size()
        if width <= 0 or height <= 0 or max(width, height) > MAX_PREVIEW_FRAME_DIMENSION:
            _log.warning(
                "rejecting preview for game '%s': frame %s is %sx%s, over the "
                "%spx-per-side cap",
                game_id,
                path.name,
                width,
                height,
                MAX_PREVIEW_FRAME_DIMENSION,
            )
            return None

        total_bytes += width * height * 4
        if total_bytes > MAX_PREVIEW_TOTAL_DECODED_BYTES:
            _log.warning(
                "rejecting preview for game '%s': decoded frames total %s bytes, "
                "over the %s byte cap",
                game_id,
                total_bytes,
                MAX_PREVIEW_TOTAL_DECODED_BYTES,
            )
            return None

        # convert_alpha() needs a display surface; the preview tool and some
        # tests render without one, so degrade to a plain convert() there --
        # still fast to blit, just without a fast-path alpha format.
        frames.append(image.convert_alpha() if pygame.display.get_surface() else image.convert())

    if not frames:
        return None
    return PreviewAnimation(fps=manifest.fps, frames=tuple(frames))


class PreviewLibrary:
    """Loads and permanently remembers one :class:`PreviewAnimation` per game.

    A missing, malformed, unreadable, oversized or escaping preview is cached
    as ``None`` too -- a broken game's checkout is only ever probed once per
    session, never once per frame attract mode considers it.

    Args:
        cache_root: Managed cache root to resolve a game's checkout inside.
            Defaults to the launcher's own default cache root (see
            :func:`launcher.paths.default_cache_root`); a session started
            with ``--cache`` passes its override through here instead.
    """

    def __init__(self, cache_root: Path | None = None) -> None:
        self._cache_root = cache_root
        self._animations: dict[str, PreviewAnimation | None] = {}

    def get(self, entry: GameEntry) -> PreviewAnimation | None:
        """Return the decoded preview for *entry*, or ``None`` if it has none."""
        if entry.id in self._animations:
            return self._animations[entry.id]
        animation = self._load(entry)
        self._animations[entry.id] = animation
        return animation

    def _load(self, entry: GameEntry) -> PreviewAnimation | None:
        if not entry.launchable:
            # Coming-soon games have no checkout at all -- never even try.
            return None
        checkout = checkout_dir(entry.id, self._cache_root)
        manifest = load_preview_manifest(checkout, game_id=entry.id)
        if manifest is None:
            return None
        return _decode(manifest, game_id=entry.id)

    def clear(self) -> None:
        """Forget every remembered outcome (used when SDL is released)."""
        self._animations.clear()
