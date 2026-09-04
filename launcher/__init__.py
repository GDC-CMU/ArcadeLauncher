"""GDC-CMU ArcadeLauncher.

A supervisor-style Pygame launcher that presents Game Dev Club games as a
curated gallery on the CMU-Q arcade machine.

Module map (deliberately separated so each layer is testable in isolation):

* :mod:`launcher.paths`       -- filesystem locations, no side effects.
* :mod:`launcher.errors`      -- the exception hierarchy.
* :mod:`launcher.settings`    -- JSON config + environment overrides.
* :mod:`launcher.manifest`    -- typed manifest model and validation.
* :mod:`launcher.status`      -- availability states shared by cache and UI.
* :mod:`launcher.cache`       -- git checkout management (injectable runner).
* :mod:`launcher.sync`        -- background synchronisation worker.
* :mod:`launcher.input_state` -- edge detection + controlled key repeat.
* :mod:`launcher.controls`    -- arcade/keyboard binding tables.
* :mod:`launcher.supervisor`  -- child process lifecycle, two-level exit.
* :mod:`launcher.gallery`     -- the Pygame UI session.
* :mod:`launcher.ui`          -- rendering: theme, art, components, views.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
