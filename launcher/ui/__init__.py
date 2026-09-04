"""Rendering package for the GDC-CMU arcade gallery.

Split so the pure-logic layers above it never need Pygame:

* :mod:`launcher.ui.theme`      -- palette, pixel font, font cache.
* :mod:`launcher.ui.surfaces`   -- the surface cache (built once, reused).
* :mod:`launcher.ui.art`        -- procedural pixel card art.
* :mod:`launcher.ui.effects`    -- scanlines, vignette, easing, panels.
* :mod:`launcher.ui.components` -- marquee, badges, legend, banner, toast.
* :mod:`launcher.ui.viewmodel`  -- the immutable frame handed to a view.
* :mod:`launcher.ui.views`      -- Grid, Carousel and Cover Flow.
* :mod:`launcher.ui.scene`      -- background + dispatch to the active view.
"""

from __future__ import annotations

__all__ = ["SCREEN_SIZE", "SCREEN_WIDTH", "SCREEN_HEIGHT"]

#: The arcade cabinet runs at 800x600; every composition targets it exactly.
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
SCREEN_SIZE = (SCREEN_WIDTH, SCREEN_HEIGHT)
