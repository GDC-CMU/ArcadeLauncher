"""The Pygame loader: it must survive the cabinet's cmu_graphics shim.

The CMU-Q arcade box has ``cmu_graphics`` installed -- the ROM folder the games
live in is named after it -- and ``cmu_graphics`` ships a module that answers to
the name ``pygame`` without being it.  Nothing on a development machine
reproduces that, which is precisely why it needs a test: the failure only
happens on the one machine the launcher exists to run on, and it happens inside
an import, before anything can be drawn.

Two things are proved here:

* A shim on ``sys.path`` is detected, its path entry (and **only** its path
  entry) is removed, and the real Pygame comes back.
* A genuinely absent Pygame raises :class:`~launcher.errors.PygameUnavailableError`
  rather than calling :func:`sys.exit`. ``main.py`` owns the exit code and
  paints the branded failure screen; an import that killed the interpreter
  would leave the cabinet black.

The first of those really does re-import Pygame. ``setUp`` snapshots
``sys.path`` and every ``pygame*`` / ``cmu_graphics*`` entry in ``sys.modules``
and puts them back afterwards, so the rest of the suite is unaffected.
"""

from __future__ import annotations

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType

from support import REPO_ROOT, TempDirCase

from launcher.errors import LauncherError, PygameUnavailableError
from launcher.ui.pygame_runtime import (
    PURGED_PREFIXES,
    SHIM_MARKERS,
    load_pygame,
    looks_like_pygame,
    missing_capabilities,
    origin_directory,
    pygame,
)

#: A stand-in for what the cabinet's cmu_graphics puts in the way: it answers to
#: the name, and it has neither ``init`` nor ``display``.
SHIM_SOURCE = '''"""Stand-in for the cmu_graphics pygame shim (test fixture)."""

CMU_GRAPHICS_SHIM = True
VERSION = "not-really-pygame"


def get_sdl_version():
    return (0, 0, 0)
'''

#: An absolute directory that need not exist, for the hermetic path-surgery
#: tests. Those never touch the filesystem -- they hand the loader a synthetic
#: ``sys.path`` and check what comes back out of it.
SHIM_FIXTURE_DIR = REPO_ROOT / ".__shim_fixture__"


class _MissingSubsystem:
    """Mimics Pygame's ``MissingModule`` placeholder.

    Pygame substitutes one of these for any subsystem it could not build, and
    every attribute access raises :class:`NotImplementedError` -- which
    :func:`hasattr` does **not** swallow. A probe that used ``hasattr`` would
    blow up here instead of reporting a missing capability.
    """

    def __getattr__(self, name: str) -> object:
        raise NotImplementedError(f"{name} module not available")


def make_shim(**attributes: object) -> ModuleType:
    """Build a module object that is not Pygame."""
    module = ModuleType("pygame")
    for name, value in attributes.items():
        setattr(module, name, value)
    return module


class VerificationTests(unittest.TestCase):
    """What counts as 'the real Pygame'."""

    def test_the_loaded_pygame_passes_every_check(self) -> None:
        self.assertTrue(looks_like_pygame(pygame))
        self.assertEqual(missing_capabilities(pygame), ())

    def test_a_shim_is_recognised_as_one(self) -> None:
        shim = make_shim(CMU_GRAPHICS_SHIM=True)
        self.assertFalse(looks_like_pygame(shim))
        missing = missing_capabilities(shim)
        for marker in ("init", "display", "event", "font", "Surface"):
            with self.subTest(capability=marker):
                self.assertIn(marker, missing)

    def test_a_shim_with_an_empty_display_is_still_a_shim(self) -> None:
        """A one-level check would pass this; the loader looks deeper."""
        shim = make_shim(
            init=lambda: None,
            quit=lambda: None,
            error=RuntimeError,
            Surface=object,
            Rect=object,
            Color=object,
            display=ModuleType("display"),
            event=ModuleType("event"),
        )
        self.assertTrue(looks_like_pygame(shim))
        self.assertIn("display.set_mode", missing_capabilities(shim))

    def test_a_missing_subsystem_is_reported_not_raised(self) -> None:
        module = make_shim(font=_MissingSubsystem())
        missing = missing_capabilities(module)
        self.assertIn("font.Font", missing)

    def test_every_shim_marker_is_something_real_pygame_has(self) -> None:
        for marker in SHIM_MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue(hasattr(pygame, marker))

    def test_the_origin_of_a_package_is_the_directory_above_it(self) -> None:
        module = make_shim()
        module.__file__ = str(Path("/opt/cmu/libs/pygame/__init__.py"))
        self.assertEqual(
            Path(origin_directory(module)), Path("/opt/cmu/libs").resolve()
        )

    def test_the_origin_of_a_plain_module_is_its_own_directory(self) -> None:
        module = make_shim()
        module.__file__ = str(Path("/opt/cmu/libs/pygame.py"))
        self.assertEqual(
            Path(origin_directory(module)), Path("/opt/cmu/libs").resolve()
        )

    def test_a_synthetic_module_has_no_traceable_origin(self) -> None:
        self.assertIsNone(origin_directory(make_shim()))


class ShimRecoveryTests(TempDirCase):
    """The real thing: a shim on the real ``sys.path``, cleared for real."""

    def setUp(self) -> None:
        super().setUp()
        self.original_path = list(sys.path)
        self.original_modules = {
            name: module
            for name, module in sys.modules.items()
            if any(
                name == prefix or name.startswith(f"{prefix}.")
                for prefix in PURGED_PREFIXES
            )
        }
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sys.path[:] = self.original_path
        for name in list(sys.modules):
            if any(
                name == prefix or name.startswith(f"{prefix}.")
                for prefix in PURGED_PREFIXES
            ):
                del sys.modules[name]
        sys.modules.update(self.original_modules)

    def _install_shim(self, *, as_package: bool) -> Path:
        """Put a fake ``pygame`` at the front of ``sys.path``; return its dir."""
        directory = self.tmp_path / ("pkg" if as_package else "mod")
        if as_package:
            (directory / "pygame").mkdir(parents=True)
            (directory / "pygame" / "__init__.py").write_text(
                SHIM_SOURCE, encoding="utf-8"
            )
        else:
            directory.mkdir(parents=True)
            (directory / "pygame.py").write_text(SHIM_SOURCE, encoding="utf-8")

        for name in list(sys.modules):
            if any(
                name == prefix or name.startswith(f"{prefix}.")
                for prefix in PURGED_PREFIXES
            ):
                del sys.modules[name]
        sys.path.insert(0, str(directory))
        return directory

    def _assert_shim_is_what_loads_first(self, directory: Path) -> None:
        import importlib

        importlib.invalidate_caches()
        shim = importlib.import_module("pygame")
        self.assertTrue(getattr(shim, "CMU_GRAPHICS_SHIM", False))
        self.assertFalse(looks_like_pygame(shim))
        del sys.modules["pygame"]

    def test_a_shim_module_is_cleared_and_the_real_pygame_returns(self) -> None:
        directory = self._install_shim(as_package=False)
        self._assert_shim_is_what_loads_first(directory)

        loaded = load_pygame()

        self.assertEqual(missing_capabilities(loaded), ())
        self.assertTrue(hasattr(loaded.display, "set_mode"))
        self.assertFalse(getattr(loaded, "CMU_GRAPHICS_SHIM", False))
        self.assertNotEqual(
            Path(loaded.__file__).parent.resolve(), directory.resolve()
        )

    def test_a_shim_package_is_cleared_too(self) -> None:
        directory = self._install_shim(as_package=True)
        self._assert_shim_is_what_loads_first(directory)

        loaded = load_pygame()

        self.assertEqual(missing_capabilities(loaded), ())
        self.assertFalse(getattr(loaded, "CMU_GRAPHICS_SHIM", False))

    def test_only_the_shim_entry_leaves_sys_path(self) -> None:
        directory = self._install_shim(as_package=False)
        before = list(sys.path)

        load_pygame()

        self.assertNotIn(str(directory), sys.path)
        self.assertEqual(
            [item for item in before if item != str(directory)],
            sys.path,
            "the loader removed something other than the shim's own path entry",
        )

    def test_cmu_graphics_is_purged_from_the_module_cache(self) -> None:
        """A half-live cmu_graphics would just hand the same shim back."""
        self._install_shim(as_package=False)
        sys.modules["cmu_graphics"] = ModuleType("cmu_graphics")
        sys.modules["cmu_graphics.libs"] = ModuleType("cmu_graphics.libs")

        load_pygame()

        self.assertNotIn("cmu_graphics", sys.modules)
        self.assertNotIn("cmu_graphics.libs", sys.modules)

    def test_sdl_is_not_initialised_by_loading_pygame(self) -> None:
        self._install_shim(as_package=False)
        loaded = load_pygame()
        self.assertFalse(loaded.get_init())


class FailureTests(unittest.TestCase):
    """When there is no real Pygame, say so -- do not kill the interpreter."""

    def test_an_absent_pygame_raises_the_typed_error(self) -> None:
        def importer() -> ModuleType:
            raise ImportError("No module named 'pygame'")

        search_path: list[str] = ["/keep/this"]
        try:
            load_pygame(importer=importer, modules={}, search_path=search_path)
        except SystemExit:  # pragma: no cover - this is the bug being guarded
            self.fail("the loader must never exit the process at import time")
        except PygameUnavailableError as exc:
            self.assertIsInstance(exc, LauncherError)
            self.assertIn("pygame", str(exc))
            self.assertIn("requirements.txt", str(exc))
        else:  # pragma: no cover
            self.fail("expected PygameUnavailableError")
        self.assertEqual(search_path, ["/keep/this"], "nothing to remove, so remove nothing")

    def test_the_typed_error_carries_a_headline_for_the_fatal_screen(self) -> None:
        self.assertEqual(PygameUnavailableError.headline, "Pygame unavailable")
        self.assertTrue(issubclass(PygameUnavailableError, LauncherError))

    def test_an_unclearable_shim_raises_instead_of_looping_forever(self) -> None:
        shim = make_shim(CMU_GRAPHICS_SHIM=True)  # no __file__: no path to drop
        attempts = 0

        def importer() -> ModuleType:
            nonlocal attempts
            attempts += 1
            return shim

        with self.assertRaises(PygameUnavailableError) as caught:
            load_pygame(importer=importer, modules={}, search_path=[])

        self.assertLessEqual(attempts, 4)
        self.assertIn("shim", str(caught.exception))

    def test_a_real_pygame_missing_a_subsystem_keeps_its_path(self) -> None:
        """Do not amputate the only real install because SDL_ttf is absent.

        A module that *is* Pygame but was built without something we need is a
        different problem from a shim, and removing its directory would delete
        the one working copy on the box. Report it and stop.
        """
        crippled = make_shim(
            init=lambda: None,
            quit=lambda: None,
            error=RuntimeError,
            Surface=object,
            Rect=object,
            Color=object,
            display=pygame.display,
            event=pygame.event,
            image=pygame.image,
            draw=pygame.draw,
            transform=pygame.transform,
            joystick=pygame.joystick,
            time=pygame.time,
            key=pygame.key,
            mouse=pygame.mouse,
            font=_MissingSubsystem(),
        )
        crippled.__file__ = "/opt/venv/lib/pygame/__init__.py"
        search_path = ["/opt/venv/lib"]

        with self.assertRaises(PygameUnavailableError) as caught:
            load_pygame(
                importer=lambda: crippled, modules={}, search_path=search_path
            )

        self.assertIn("font.Font", str(caught.exception))
        self.assertEqual(search_path, ["/opt/venv/lib"])

    def test_a_shim_that_clears_on_the_second_attempt_is_recovered(self) -> None:
        shim = make_shim(CMU_GRAPHICS_SHIM=True)
        shim.__file__ = str(SHIM_FIXTURE_DIR / "pygame.py")
        search_path = ["/keep/before", str(SHIM_FIXTURE_DIR), "/keep/after"]
        results = [shim, pygame]

        loaded = load_pygame(
            importer=lambda: results.pop(0), modules={}, search_path=search_path
        )

        self.assertIs(loaded, pygame)
        self.assertEqual(search_path, ["/keep/before", "/keep/after"])


class StartupFailureTests(unittest.TestCase):
    """The payoff: a shimmed cabinet gets a message, not a black screen.

    This is why the loader raises instead of exiting. ``main()`` has to be able
    to catch it, log it, put it on screen and still return the exit code the
    arcade menu reads.
    """

    def setUp(self) -> None:
        super().setUp()
        import launcher.ui.fatal as fatal
        import main as entrypoint

        self.entrypoint = entrypoint
        self.shown: list[tuple[str, str]] = []

        original_loader = entrypoint.load_gallery
        original_screen = fatal.show_fatal_screen
        self.addCleanup(setattr, entrypoint, "load_gallery", original_loader)
        self.addCleanup(setattr, fatal, "show_fatal_screen", original_screen)

        def refuse() -> object:
            raise PygameUnavailableError("cmu_graphics shim could not be cleared")

        def record(headline: str, detail: str, **_: object) -> None:
            self.shown.append((headline, detail))

        entrypoint.load_gallery = refuse
        fatal.show_fatal_screen = record

    def test_the_launcher_reports_the_failure_and_returns_its_exit_code(self) -> None:
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.entrypoint.main(["--no-sync"])

        self.assertEqual(code, self.entrypoint.EXIT_FAILED)
        self.assertEqual(len(self.shown), 1, "the branded screen was not painted")
        headline, detail = self.shown[0]
        self.assertEqual(headline, PygameUnavailableError.headline)
        self.assertIn("cmu_graphics", detail)
        self.assertIn("cmu_graphics", stderr.getvalue())

    def test_the_process_is_not_killed_from_inside_the_import(self) -> None:
        import contextlib
        import io

        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.entrypoint.main(["--no-sync"])
        except SystemExit:  # pragma: no cover - this is the bug being guarded
            self.fail("startup failure must return an exit code, not raise SystemExit")


class EntryPointTests(unittest.TestCase):
    """The rest of the repository must go through the loader."""

    def test_importing_the_runtime_does_not_initialise_sdl(self) -> None:
        """Checked in a fresh interpreter: an earlier test could have inited SDL."""
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-c",
                "from launcher.ui.pygame_runtime import pygame\n"
                "print(pygame.get_init())\n",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip().splitlines()[-1], "False")

    def test_the_gallery_and_the_views_share_one_pygame(self) -> None:
        from launcher import gallery
        from launcher.ui import components, theme
        from launcher.ui.views import grid

        for module in (gallery, components, theme, grid):
            with self.subTest(module=module.__name__):
                self.assertIs(module.pygame, pygame)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
