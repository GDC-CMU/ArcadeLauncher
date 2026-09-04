"""Path resolution must not depend on the process working directory.

The cabinet's outer menu pulls this repository, installs ``requirements.txt``
and runs ``main.py`` -- and it is **not documented** what working directory it
uses when it does.  It may well not be the repository root.

Every path the launcher needs at start-up (``data/games.json``,
``config/launcher.json``, ``assets/branding/gdc-cmu-logo.png``, and the
``.arcade-cache/`` it clones games into) therefore has to be resolved from
``__file__`` -- the installed location -- and never from :func:`os.getcwd`.
A single cwd-relative path would produce "manifest not found" on the cabinet
and nowhere else: it works on every developer's machine, because developers
run the launcher from the repository root.

Three layers of guard, deliberately overlapping:

1. :class:`CwdIndependenceTests` loads the real files after ``chdir`` into an
   unrelated temporary directory.
2. :class:`ForeignWorkingDirectoryTests` does the same in a *fresh interpreter*
   started in that directory, which is the only way to catch a bad path that
   was resolved at import time -- before this test could have chdir'd.
3. :class:`NoCwdRelativePathsTests` reads the source and refuses to let
   ``Path.cwd()``, ``os.getcwd()`` or a relative path literal back into the
   package at all.

The child-process contract is checked here too: the launcher being
cwd-independent must not make the *game* cwd-independent. A game is started
with its own checkout as ``cwd``, which is what puts its directory on the
child's ``sys.path[0]`` so its sibling imports resolve. That has to stay true.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

from support import REPO_ROOT, TempDirCase, child_fixture, entry

from launcher.errors import UnsafeEntrypointError
from launcher.manifest import load_manifest, parse_manifest
from launcher.paths import (
    ASSETS_DIR,
    BRANDING_LOGO,
    CACHE_DIR_NAME,
    CACHE_ROOT_ENV,
    CONFIG_DIR,
    DATA_DIR,
    DOCS_DIR,
    MANIFEST_FILE,
    SCREENSHOTS_DIR,
    SETTINGS_FILE,
    checkout_dir,
    default_cache_root,
    games_root,
    run_root,
)
from launcher.paths import REPO_ROOT as PACKAGE_REPO_ROOT
from launcher.settings import load_settings
from launcher.supervisor import build_child_command

#: Source trees that ship to the cabinet. ``tests/`` is excluded on purpose:
#: test code is allowed to ask where it is standing.
SHIPPED_DIRECTORIES = ("launcher", "tools")

#: Calls that read the process working directory. None of them belong in code
#: that resolves a repository resource.
FORBIDDEN_CALLS = ("Path.cwd", "pathlib.Path.cwd", "os.getcwd", "os.chdir")

#: Callables whose first string argument names a file. A *relative* literal
#: there is the exact regression this module exists to prevent.
RESOURCE_CALLS = ("Path", "open")


def shipped_python_files() -> list[Path]:
    """Every source file that runs on the cabinet."""
    files = [REPO_ROOT / "main.py"]
    for directory in SHIPPED_DIRECTORIES:
        files.extend(sorted((REPO_ROOT / directory).rglob("*.py")))
    return [path for path in files if "__pycache__" not in path.parts]


def _dotted_name(node: ast.AST) -> str | None:
    """Render ``os.path.join`` / ``Path.cwd`` back into a dotted string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _is_absolute(text: str) -> bool:
    """Whether *text* is absolute under either platform's rules."""
    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()


class CwdIndependenceTests(TempDirCase):
    """Load the real files with the working directory somewhere else entirely."""

    def setUp(self) -> None:
        super().setUp()
        origin = Path.cwd()
        self.addCleanup(os.chdir, origin)
        # A cache override left behind by another test would mask the very
        # thing this class checks, so the environment is pinned to the default.
        previous = os.environ.pop(CACHE_ROOT_ENV, None)
        if previous is not None:
            self.addCleanup(os.environ.__setitem__, CACHE_ROOT_ENV, previous)
        os.chdir(self.tmp_path)

    def test_the_working_directory_really_did_move(self) -> None:
        """Guard the guard: a no-op chdir would make everything below vacuous."""
        self.assertNotEqual(Path.cwd().resolve(), REPO_ROOT)

    def test_the_manifest_loads_from_an_unrelated_working_directory(self) -> None:
        manifest = load_manifest()
        self.assertGreater(len(manifest), 0)
        self.assertEqual(
            [game.id for game in manifest],
            [game.id for game in load_manifest(MANIFEST_FILE)],
        )

    def test_the_settings_load_from_an_unrelated_working_directory(self) -> None:
        self.assertEqual(load_settings(), load_settings(SETTINGS_FILE))

    def test_the_cache_root_lands_inside_the_checkout(self) -> None:
        root = default_cache_root()
        self.assertEqual(root, REPO_ROOT / CACHE_DIR_NAME)
        self.assertIn(REPO_ROOT, root.parents)
        self.assertNotIn(self.tmp_path, root.parents)

    def test_the_derived_cache_directories_follow_the_root(self) -> None:
        for path in (games_root(), run_root(), checkout_dir("streetfighter")):
            with self.subTest(path=path):
                self.assertIn(REPO_ROOT / CACHE_DIR_NAME, [path, *path.parents])

    def test_the_branding_logo_resolves(self) -> None:
        self.assertTrue(BRANDING_LOGO.is_absolute())
        self.assertTrue(BRANDING_LOGO.is_file())

    def test_every_declared_path_is_absolute_and_inside_the_checkout(self) -> None:
        declared = {
            "ASSETS_DIR": ASSETS_DIR,
            "BRANDING_LOGO": BRANDING_LOGO,
            "CONFIG_DIR": CONFIG_DIR,
            "SETTINGS_FILE": SETTINGS_FILE,
            "DATA_DIR": DATA_DIR,
            "MANIFEST_FILE": MANIFEST_FILE,
            "DOCS_DIR": DOCS_DIR,
            "SCREENSHOTS_DIR": SCREENSHOTS_DIR,
        }
        for name, path in declared.items():
            with self.subTest(path=name):
                self.assertTrue(path.is_absolute(), f"{name} is relative")
                self.assertIn(REPO_ROOT, path.parents)

    def test_the_package_agrees_with_the_test_suite_about_the_root(self) -> None:
        self.assertEqual(PACKAGE_REPO_ROOT, REPO_ROOT)

    def test_entrypoint_validation_does_not_move_with_the_working_directory(
        self,
    ) -> None:
        """The synthetic validation root must not be ``Path.cwd()``-derived.

        Entrypoints are checked for escapes before anything has been cloned, so
        the check needs a stand-in checkout directory. Anchoring that to the
        working directory would make a manifest's validity depend on where the
        cabinet happened to be standing.
        """
        document = {
            "version": 1,
            "games": [
                {
                    "id": "probe",
                    "title": "Probe",
                    "description": "Fixture.",
                    "runtime": "python",
                    "launchable": True,
                    "repository": "https://example.com/probe.git",
                    "ref": "main",
                    "entrypoint": "../escape.py",
                    "art": {
                        "motif": "duel",
                        "palette": ["cmu_red", "warm_amber", "ink"],
                        "seed": 1,
                    },
                }
            ],
        }
        with self.assertRaises(UnsafeEntrypointError):
            parse_manifest(document)

        document["games"][0]["entrypoint"] = "main.py"
        self.assertEqual(parse_manifest(document)[0].entrypoint, "main.py")


class ChildProcessCwdTests(TempDirCase):
    """The launcher's cwd-independence must not change the *child's* contract."""

    def test_a_game_is_still_started_in_its_own_checkout(self) -> None:
        checkout = self.tmp_path / "games" / "fixture-game"
        checkout.mkdir(parents=True)
        (checkout / "main.py").write_text("print('hi')\n", encoding="utf-8")

        command = build_child_command(entry(), checkout)
        self.assertEqual(command, [sys.executable, "main.py"])
        # Relative on purpose: the checkout is passed as the child's cwd, which
        # is what puts the game's own directory on its sys.path[0].
        self.assertFalse(Path(command[1]).is_absolute())

    def test_the_child_reports_the_checkout_as_its_working_directory(self) -> None:
        """Not a re-test of the supervisor: a guard on *this* change.

        Making the launcher read its own files from ``__file__`` must not tempt
        anyone into launching games from ``__file__`` too. A game is handed its
        checkout as ``cwd`` and a *relative* entrypoint, and that is what puts
        its own directory on the child's ``sys.path[0]``.
        """
        origin = Path.cwd()
        self.addCleanup(os.chdir, origin)

        checkout = self.tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "main.py").write_text(
            "import os, sys\nprint(os.getcwd())\nprint(sys.path[0])\n",
            encoding="utf-8",
        )
        # Start from somewhere else entirely, so a leaked cwd would show up.
        os.chdir(self.tmp_path)

        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            build_child_command(entry(), checkout),
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reported_cwd, reported_path0 = completed.stdout.strip().splitlines()[:2]
        self.assertEqual(Path(reported_cwd).resolve(), checkout.resolve())
        self.assertEqual(Path(reported_path0).resolve(), checkout.resolve())


class ForeignWorkingDirectoryTests(TempDirCase):
    """Start a fresh interpreter somewhere else and see what it resolves.

    The in-process tests above cannot catch a path that was computed at import
    time, because by then this module has already been imported from the
    repository root. A subprocess can.
    """

    def _run_probe(self) -> dict:
        environment = dict(os.environ)
        environment.pop(CACHE_ROOT_ENV, None)
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(child_fixture("cwd_probe.py"))],
            cwd=str(self.tmp_path),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_everything_resolves_from_an_unrelated_working_directory(self) -> None:
        report = self._run_probe()

        self.assertEqual(Path(report["cwd"]).resolve(), self.tmp_path.resolve())
        self.assertNotEqual(Path(report["cwd"]).resolve(), REPO_ROOT)
        self.assertEqual(Path(report["repo_root"]), REPO_ROOT)

        self.assertEqual(Path(report["manifest_file"]), MANIFEST_FILE)
        self.assertEqual(Path(report["settings_file"]), SETTINGS_FILE)
        self.assertEqual(Path(report["branding_logo"]), BRANDING_LOGO)
        self.assertTrue(report["branding_logo_exists"])

        self.assertEqual(Path(report["cache_root"]), REPO_ROOT / CACHE_DIR_NAME)
        self.assertEqual(Path(report["games_root"]), REPO_ROOT / CACHE_DIR_NAME / "games")
        self.assertEqual(Path(report["run_root"]), REPO_ROOT / CACHE_DIR_NAME / "run")

        self.assertEqual(report["game_ids"], [game.id for game in load_manifest()])
        self.assertEqual(report["default_view"], load_settings().default_view.value)


class NoCwdRelativePathsTests(unittest.TestCase):
    """Refuse to let a cwd-relative path back into the shipped source."""

    def test_nothing_shipped_reads_the_process_working_directory(self) -> None:
        for path in shipped_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _dotted_name(node.func)
                if name in FORBIDDEN_CALLS:
                    self.fail(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                        f"calls {name}(); resolve from __file__ instead "
                        f"(see launcher/paths.py)"
                    )

    def test_no_shipped_file_is_opened_by_a_relative_literal(self) -> None:
        for path in shipped_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if _dotted_name(node.func) not in RESOURCE_CALLS:
                    continue
                first = node.args[0]
                if not isinstance(first, ast.Constant) or not isinstance(
                    first.value, str
                ):
                    continue
                if not _is_absolute(first.value):
                    self.fail(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                        f"builds a path from the relative literal "
                        f"{first.value!r}; derive it from launcher.paths instead"
                    )

    def test_the_guard_would_actually_catch_a_regression(self) -> None:
        """A scanner that never fires is worse than no scanner."""
        offender = ast.parse("from pathlib import Path\np = Path.cwd() / 'data'\n")
        found = [
            _dotted_name(node.func)
            for node in ast.walk(offender)
            if isinstance(node, ast.Call)
        ]
        self.assertIn("Path.cwd", found)

        relative = ast.parse("open('data/games.json')\n")
        literals = [
            node.args[0].value
            for node in ast.walk(relative)
            if isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        self.assertEqual(literals, ["data/games.json"])
        self.assertFalse(_is_absolute(literals[0]))


class ManifestFromAnywhereTests(TempDirCase):
    """The shipped manifest parses identically wherever it is parsed from."""

    def test_parsing_is_stable_across_working_directories(self) -> None:
        origin = Path.cwd()
        self.addCleanup(os.chdir, origin)
        first = load_manifest()
        os.chdir(self.tmp_path)
        second = load_manifest()
        self.assertEqual(
            [(game.id, game.entrypoint) for game in first],
            [(game.id, game.entrypoint) for game in second],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
