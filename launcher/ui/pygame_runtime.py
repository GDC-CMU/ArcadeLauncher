"""The one place the launcher is allowed to import Pygame.

Why this module exists
----------------------
The CMU-Q cabinet is a RetroPie box with ``cmu_graphics`` installed -- the ROM
folder the games live in is literally named after it.  ``cmu_graphics`` ships a
*shim* that answers to the name ``pygame``: the arcade startercode's own
``joystick.py`` reaches for ``cmu_graphics.libs.pygame_loader`` before it
reaches for the real thing, and StreetFighter -- which runs on this exact
cabinet -- carries a compatibility module written specifically because the shim
shadowed real Pygame in production there.

Nothing on a development machine reproduces this.  ``import pygame`` is correct
everywhere we can test and wrong on the only machine that matters, and the
failure mode is the worst one available: the launcher dies inside an import,
before a single pixel is drawn, and the cabinet stays black.

So every module in this package imports Pygame *from here* and this module
takes responsibility for handing back the real one.

What it does
------------
1. Imports ``pygame`` normally -- the virtualenv is trusted first.
2. **Verifies** the module that arrived really is Pygame, by checking the
   surface of the API the launcher actually uses (``init``, ``display``,
   ``event``, ``font``, ``Surface`` and friends).  A shim fails this.
3. If a shim answered, works out which ``sys.path`` entry it came from, drops
   **only that entry**, purges ``pygame*`` and ``cmu_graphics*`` from
   ``sys.modules``, and imports again.  Only the offending entry is removed:
   blanket-scrubbing ``sys.path`` would take the venv's own ``site-packages``
   with it.
4. Raises :class:`~launcher.errors.PygameUnavailableError` if no real Pygame
   can be produced.

Two deliberate differences from StreetFighter's ``pygame_compat``:

* **It never calls** ``sys.exit()``.  ``main.py`` owns the exit code and paints
  a branded failure screen; an import that kills the interpreter would take
  that away and leave the visitor looking at nothing.
* **It never calls** ``pygame.init()``.  Initialising SDL is a side effect the
  gallery session performs and reverses at a precise point in its lifecycle
  (so a launched game gets the display back); doing it from an import would
  claim the device the moment anything imported this package -- including the
  test suite and the screenshot tool.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from types import ModuleType
from typing import Callable, MutableMapping

from ..errors import PygameUnavailableError

__all__ = [
    "pygame",
    "load_pygame",
    "missing_capabilities",
    "looks_like_pygame",
    "origin_directory",
    "MODULE_NAME",
    "PURGED_PREFIXES",
    "SHIM_MARKERS",
    "REQUIRED_ATTRIBUTES",
    "REQUIRED_SUBMODULES",
]

_log = logging.getLogger(__name__)

#: The name the real library and the shim both answer to.
MODULE_NAME = "pygame"

#: Module prefixes dropped from ``sys.modules`` before a retry.  ``cmu_graphics``
#: is included because it is what installs the shim in the first place, and a
#: half-initialised copy of it would just hand the same object back.
PURGED_PREFIXES: tuple[str, ...] = ("pygame", "cmu_graphics")

#: Top-level names a real Pygame always defines.  A module missing any of them
#: is not Pygame at all, which is the signal to go looking for a shim -- as
#: opposed to a genuine Pygame built without some optional subsystem, where
#: removing paths would only make things worse.
SHIM_MARKERS: tuple[str, ...] = (
    "init",
    "quit",
    "error",
    "Surface",
    "Rect",
    "display",
    "event",
)

#: Top-level attributes the launcher itself calls.
REQUIRED_ATTRIBUTES: tuple[str, ...] = (
    "init",
    "quit",
    "error",
    "Surface",
    "Rect",
    "Color",
)

#: Submodule -> the attributes of it the launcher calls.  Checked one level
#: deep on purpose: a shim can define an empty ``display`` object, and the
#: launcher would then fail later, in the middle of opening the screen.
REQUIRED_SUBMODULES: dict[str, tuple[str, ...]] = {
    "display": ("init", "quit", "set_mode", "flip", "get_surface", "set_caption"),
    "event": ("get", "Event"),
    "font": ("init", "quit", "Font"),
    "image": ("load", "save"),
    "draw": ("rect", "line"),
    "transform": ("scale", "smoothscale"),
    "joystick": ("init", "quit", "get_count", "Joystick"),
    "time": ("Clock",),
    "key": ("get_pressed",),
    "mouse": ("set_visible",),
}

#: How many times the loader may drop a path and try again.  Bounded so a
#: pathological environment cannot turn start-up into an infinite loop.
MAX_ATTEMPTS = 4


def _has(obj: object, attribute: str) -> bool:
    """Whether *obj* really exposes *attribute*.

    Not :func:`hasattr`.  Pygame substitutes a ``MissingModule`` placeholder
    for any subsystem it could not build, and that placeholder raises
    :class:`NotImplementedError` -- which :func:`hasattr` does *not* swallow --
    from every attribute access.  A probe has to survive that.
    """
    try:
        getattr(obj, attribute)
    except Exception:  # noqa: BLE001 - any failure means "cannot use this"
        return False
    return True


def looks_like_pygame(module: object) -> bool:
    """Whether *module* is Pygame at all (as opposed to something wearing its name)."""
    return all(_has(module, name) for name in SHIM_MARKERS)


def missing_capabilities(module: object) -> tuple[str, ...]:
    """Every capability the launcher needs that *module* does not provide.

    Empty means the module is usable.  The names are returned in a stable
    order so the error message a club member photographs is reproducible.
    """
    missing: list[str] = []
    for attribute in REQUIRED_ATTRIBUTES:
        if not _has(module, attribute):
            missing.append(attribute)
    for name, attributes in REQUIRED_SUBMODULES.items():
        if not _has(module, name):
            missing.append(name)
            continue
        submodule = getattr(module, name)
        missing.extend(
            f"{name}.{attribute}"
            for attribute in attributes
            if not _has(submodule, attribute)
        )
    return tuple(missing)


def origin_directory(module: object) -> str | None:
    """The ``sys.path`` entry *module* was found under, if it can be traced.

    For a package (``.../shims/pygame/__init__.py``) that is the directory
    holding the package; for a plain module (``.../shims/pygame.py``) it is the
    directory holding the file.  ``None`` means the module has no filesystem
    origin -- a synthetic object injected straight into ``sys.modules``, which
    no amount of path surgery will fix.
    """
    file_name = getattr(module, "__file__", None)
    if isinstance(file_name, str) and file_name:
        directory = os.path.dirname(os.path.abspath(file_name))
        if os.path.basename(file_name).startswith("__init__."):
            return os.path.dirname(directory)
        return directory
    try:
        locations = [str(entry) for entry in getattr(module, "__path__", [])]
    except Exception:  # noqa: BLE001 - exotic __path__ objects are not worth a crash
        locations = []
    if locations:
        return os.path.dirname(os.path.abspath(locations[0]))
    return None


def _same_entry(entry: str, target: str) -> bool:
    """Whether ``sys.path`` *entry* names the directory *target*."""
    try:
        # An empty entry means "the working directory", which is exactly how a
        # shim sitting next to the program being run gets onto the path.
        return os.path.normcase(os.path.abspath(entry or os.curdir)) == target
    except (OSError, TypeError, ValueError):
        return False


def _drop_path_entry(search_path: list[str], directory: str) -> list[str]:
    """Remove every ``sys.path`` entry naming *directory*; return what went."""
    target = os.path.normcase(os.path.abspath(directory))
    removed = [entry for entry in search_path if _same_entry(entry, target)]
    if removed:
        search_path[:] = [
            entry for entry in search_path if not _same_entry(entry, target)
        ]
    return removed


def _purge(modules: MutableMapping[str, ModuleType]) -> list[str]:
    """Forget every cached ``pygame*`` / ``cmu_graphics*`` module."""
    doomed = [
        name
        for name in list(modules)
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in PURGED_PREFIXES
        )
    ]
    for name in doomed:
        modules.pop(name, None)
    return doomed


def _default_importer() -> ModuleType:
    return importlib.import_module(MODULE_NAME)


def load_pygame(
    *,
    importer: Callable[[], ModuleType] | None = None,
    modules: MutableMapping[str, ModuleType] | None = None,
    search_path: list[str] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> ModuleType:
    """Return the real Pygame module, clearing a shim out of the way if need be.

    Args:
        importer: How to import Pygame. Defaults to a plain
            ``importlib.import_module("pygame")``. A seam: the tests drive the
            recovery logic through it without disturbing the interpreter.
            A caller that overrides *modules* should override this too, since
            the real import machinery reads :data:`sys.modules` regardless.
        modules: Module registry to purge. Defaults to :data:`sys.modules`.
        search_path: Import search path to prune. Defaults to :data:`sys.path`.
            Mutated **in place** so the surgery is visible to the real import
            machinery.
        max_attempts: Upper bound on import-and-retry rounds.

    Returns:
        The imported Pygame module. It is *not* initialised: no ``init()`` is
        called here, by design.

    Raises:
        PygameUnavailableError: Pygame could not be imported at all, or every
            module answering to the name failed verification.
    """
    importer = _default_importer if importer is None else importer
    modules = sys.modules if modules is None else modules
    search_path = sys.path if search_path is None else search_path

    trail: list[str] = []
    purged_blind = False

    for attempt in range(1, max_attempts + 1):
        try:
            module = importer()
        except ImportError as exc:
            # Nothing loaded, so there is no origin to trace and no path worth
            # removing. Report the real reason rather than guessing at a shim.
            trail.append(f"attempt {attempt}: import failed ({exc})")
            raise PygameUnavailableError(
                "could not import pygame: "
                + "; ".join(trail)
                + ". Install it with 'pip install -r requirements.txt'."
            ) from exc

        origin = getattr(module, "__file__", None) or "<no file>"
        if looks_like_pygame(module):
            missing = missing_capabilities(module)
            if not missing:
                if attempt > 1:
                    _log.warning(
                        "recovered the real pygame from %s after %s attempt(s): %s",
                        origin,
                        attempt,
                        "; ".join(trail),
                    )
                else:
                    _log.debug("pygame loaded from %s", origin)
                return module
            # It *is* Pygame, just built without something we need. Dropping
            # its directory would only delete the one real copy on the box, so
            # say what is wrong and stop.
            raise PygameUnavailableError(
                f"pygame at {origin} is missing {', '.join(missing)}; "
                "the installation is incomplete. Reinstall with "
                "'pip install -r requirements.txt'."
            )

        # Not Pygame: a shim answered to the name.
        missing = missing_capabilities(module)
        directory = origin_directory(module)
        trail.append(
            f"attempt {attempt}: '{MODULE_NAME}' at {origin} is a shim "
            f"(missing {', '.join(missing) or 'nothing identifiable'})"
        )
        _log.warning("%s", trail[-1])

        removed: list[str] = []
        if directory is not None:
            removed = _drop_path_entry(search_path, directory)
            if removed:
                _log.warning("removed shim path from sys.path: %s", ", ".join(removed))
                trail.append(f"removed path entry {directory}")

        if not removed:
            if purged_blind:
                # No path came off this round and we have already tried a plain
                # cache purge, so another round would import the same object.
                break
            purged_blind = True
            trail.append("no matching sys.path entry; purged the module cache only")

        _purge(modules)
        importlib.invalidate_caches()

    raise PygameUnavailableError(
        "the module named 'pygame' on this machine is a shim, not the real "
        "library, and it could not be cleared out of the way: "
        + "; ".join(trail)
        + ". On the arcade cabinet this is cmu_graphics shadowing pygame; run "
        "the launcher from a virtualenv where 'pip install -r requirements.txt' "
        "has installed pygame-ce."
    )


#: The verified Pygame module.  ``from .pygame_runtime import pygame`` is the
#: only supported way to reach it from anywhere else in this repository.
pygame: ModuleType = load_pygame()
