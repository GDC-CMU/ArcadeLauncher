"""Repository-level guarantees that are easy to lose and expensive to notice.

These assert on the tree itself: no shell execution anywhere, the branding
asset untouched, the cache ignored by git, and the documentation honest.
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

from support import REPO_ROOT, git_available

SOURCE_DIRECTORIES = ("launcher", "tools", "tests")
#: Blob hash of assets/branding/gdc-cmu-logo.png at the baseline commit.
LOGO_BLOB_SHA = "b8e55567de20933931c430fa8a6b049b120f9e96"


def python_files() -> list[Path]:
    files = [REPO_ROOT / "main.py"]
    for directory in SOURCE_DIRECTORIES:
        files.extend(sorted((REPO_ROOT / directory).rglob("*.py")))
    return [path for path in files if "__pycache__" not in path.parts]


def _imported_modules(path: Path) -> set[str]:
    """Every module name *path* imports, however it spells the import."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):  # `from . import x`
            names.update(alias.name for alias in node.names)
    return names


def _imports_pygame_directly(path: Path) -> set[str]:
    """The bare ``pygame`` imports in *path*, if any.

    ``from launcher.ui.pygame_runtime import pygame`` does not count: the name
    recorded for it is ``launcher.ui.pygame_runtime[.pygame]``, which is the
    whole point -- the loader is the only thing allowed to reach the real
    module, and everything else borrows its result.
    """
    return {
        name
        for name in _imported_modules(path)
        if name == "pygame" or name.startswith("pygame.")
    }


class PygameLoaderTests(unittest.TestCase):
    """The cabinet's cmu_graphics shim answers to the name ``pygame``.

    On the arcade box a shim can shadow the real module, and a bare
    ``import pygame`` anywhere in the tree would load it and kill the launcher
    before a single frame is drawn. Every import therefore goes through
    :mod:`launcher.ui.pygame_runtime`, which verifies what it got and repairs
    ``sys.path`` if it got the wrong thing.

    There are no exemptions, including for the loader itself: it reaches the
    module through :func:`importlib.import_module`, so this rule can be
    absolute and a new file cannot quietly opt out of it.
    """

    LOADER = REPO_ROOT / "launcher" / "ui" / "pygame_runtime.py"

    def test_no_file_imports_pygame_directly(self) -> None:
        for path in python_files():
            with self.subTest(file=path.relative_to(REPO_ROOT).as_posix()):
                self.assertEqual(
                    _imports_pygame_directly(path),
                    set(),
                    "import it as `from launcher.ui.pygame_runtime import pygame` "
                    "so the cmu_graphics shim cannot shadow it on the cabinet",
                )

    def test_the_loader_exists_and_is_documented(self) -> None:
        self.assertTrue(self.LOADER.is_file())
        tree = ast.parse(self.LOADER.read_text(encoding="utf-8"), filename="loader")
        self.assertIsNotNone(ast.get_docstring(tree))

    def test_the_loader_lives_in_the_rendering_layer(self) -> None:
        """So the pure-logic modules stay Pygame-free by construction."""
        self.assertEqual(self.LOADER.parent, REPO_ROOT / "launcher" / "ui")

    def test_the_loader_does_not_exit_the_process(self) -> None:
        """``main.py`` owns the exit code and paints the branded failure screen.

        A loader that called ``sys.exit()`` on a shimmed cabinet would take the
        interpreter down inside an import statement, leaving a black screen and
        a traceback nobody at a club fair can act on.
        """
        tree = ast.parse(self.LOADER.read_text(encoding="utf-8"), filename="loader")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            self.assertNotIn(name, ("exit", "_exit"), "the loader must raise, not exit")

    def test_every_renderer_routes_through_the_loader(self) -> None:
        """A new UI module that forgot the loader would be caught here."""
        renderers = [
            path
            for path in (REPO_ROOT / "launcher" / "ui").rglob("*.py")
            if "__pycache__" not in path.parts
            and path.name not in ("__init__.py", "pygame_runtime.py")
        ]
        self.assertGreater(len(renderers), 5)
        for path in renderers:
            text = path.read_text(encoding="utf-8")
            if "pygame" not in text:
                continue
            with self.subTest(file=path.relative_to(REPO_ROOT).as_posix()):
                self.assertIn("pygame_runtime import pygame", text)


class NoShellTests(unittest.TestCase):
    """Criterion F4: subprocesses are argv lists; a shell is never involved.

    The needles are assembled from fragments on purpose. Spelling them out
    would plant the very literal this test exists to forbid, and the scanner
    would then fail on itself -- so the guarantee genuinely covers every file
    in the tree, including this one.
    """

    SHELL_KWARG = "shell" + "=True"
    SHELL_CALLS = ("os." + "system(", "os." + "popen(", "subprocess." + "getoutput(")

    def test_shell_true_appears_nowhere(self) -> None:
        for path in python_files():
            with self.subTest(file=path.relative_to(REPO_ROOT).as_posix()):
                self.assertNotIn(self.SHELL_KWARG, path.read_text(encoding="utf-8"))

    def test_no_subprocess_call_passes_shell(self) -> None:
        """Belt and braces: check the syntax tree, not just the text."""
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        self.fail(f"{path} passes shell= at line {node.lineno}")

    def test_the_shell_helpers_are_never_used(self) -> None:
        for path in python_files():
            text = path.read_text(encoding="utf-8")
            for needle in self.SHELL_CALLS:
                with self.subTest(file=path.name, call=needle):
                    self.assertNotIn(needle, text)


class LayeringTests(unittest.TestCase):
    """Criterion C1/I5: pure logic must stay free of Pygame."""

    PURE_MODULES = (
        "attract.py",
        "cache.py",
        "controls.py",
        "errors.py",
        "input_state.py",
        "manifest.py",
        "paths.py",
        "previews.py",
        "settings.py",
        "status.py",
        "supervisor.py",
        "sync.py",
        "viewmodes.py",
    )

    def test_logic_modules_do_not_import_pygame(self) -> None:
        for name in self.PURE_MODULES:
            path = REPO_ROOT / "launcher" / name
            with self.subTest(module=name):
                imported = _imported_modules(path)
                self.assertNotIn("pygame", imported)
                # Also refuse the loader itself: routing a pure module through
                # launcher.ui.pygame_runtime would still drag Pygame into it.
                offenders = sorted(name for name in imported if "pygame" in name)
                self.assertEqual(offenders, [], f"{path.name} reaches Pygame")

    def test_the_ui_never_imports_the_supervisor(self) -> None:
        """Rendering must not depend on process control (or the reverse)."""
        for path in (REPO_ROOT / "launcher" / "ui").rglob("*.py"):
            with self.subTest(module=path.name):
                imported = _imported_modules(path)
                self.assertNotIn("launcher.supervisor", imported)
                self.assertNotIn("supervisor", imported)

    def test_every_module_has_a_docstring(self) -> None:
        for path in python_files():
            with self.subTest(file=path.relative_to(REPO_ROOT).as_posix()):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                self.assertIsNotNone(ast.get_docstring(tree), "missing module docstring")


class BrandingTests(unittest.TestCase):
    """Criterion G1: the club's logo is used, never rewritten."""

    LOGO = REPO_ROOT / "assets" / "branding" / "gdc-cmu-logo.png"

    def test_the_logo_exists(self) -> None:
        self.assertTrue(self.LOGO.is_file())

    @unittest.skipUnless(git_available(), "git is not installed")
    def test_the_logo_is_byte_for_byte_unmodified(self) -> None:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "hash-object", str(self.LOGO)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), LOGO_BLOB_SHA)


class GitignoreTests(unittest.TestCase):
    """The managed cache must never end up in a commit."""

    def setUp(self) -> None:
        self.text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    def test_the_cache_is_ignored(self) -> None:
        self.assertIn(".arcade-cache/", self.text)

    def test_bytecode_is_ignored(self) -> None:
        self.assertIn("__pycache__/", self.text)

    def test_child_logs_are_ignored(self) -> None:
        self.assertIn("*.child.log", self.text)


class PackagingTests(unittest.TestCase):
    """What the arcade box needs in order to run this at all."""

    def test_requirements_pin_pygame(self) -> None:
        text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pygame-ce", text)

    def test_the_arcade_entrypoint_exists(self) -> None:
        self.assertTrue((REPO_ROOT / "main.py").is_file())

    def test_the_entrypoint_exits_with_a_status_code(self) -> None:
        """The arcade menu reads our exit code; it must be explicit."""
        text = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("sys.exit(main())", text)


class ReadmeTests(unittest.TestCase):
    """Criterion J1: the README must actually cover every required topic."""

    REQUIRED_SECTIONS = (
        "## What it is",
        "## Architecture",
        "## Controls",
        "## Setup",
        "## Deploying to the CMU-Q arcade",
        "## Offline behaviour",
        "## Screenshots",
        "## Changing the default gallery mode",
        "## Adding a game",
        "## What a game must provide",
        "## Troubleshooting",
        "## Club-fair preflight",
    )

    def setUp(self) -> None:
        self.text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_all_required_sections_are_present(self) -> None:
        for heading in self.REQUIRED_SECTIONS:
            with self.subTest(section=heading):
                self.assertIn(heading, self.text)

    def test_the_screenshots_are_embedded(self) -> None:
        for name in ("grid.png", "carousel.png", "cover-flow.png"):
            with self.subTest(screenshot=name):
                self.assertIn(f"docs/screenshots/{name}", self.text)

    def test_the_documented_commands_are_the_real_ones(self) -> None:
        for command in (
            "python -m unittest discover -s tests -v",
            "python -m tools.generate_previews",
            "python main.py",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.text)

    def test_the_two_level_exit_is_explained(self) -> None:
        self.assertIn("P1", self.text)

    def test_the_documented_button_ids_match_the_bindings(self) -> None:
        """Criterion J2: the controls table must not drift away from the code."""
        from launcher.controls import BUTTON_CYCLE_VIEW, BUTTON_EXIT, BUTTON_LAUNCH

        documented: dict[str, int] = {}
        for line in self.text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[1].isdigit():
                documented[cells[0].strip("`")] = int(cells[1])

        self.assertEqual(documented.get("A"), BUTTON_LAUNCH)
        self.assertEqual(documented.get("Select"), BUTTON_CYCLE_VIEW)
        self.assertEqual(documented.get("P1"), BUTTON_EXIT)

    def test_every_view_mode_is_documented_by_its_config_value(self) -> None:
        from launcher.viewmodes import ViewMode

        for mode in ViewMode:
            with self.subTest(mode=mode.value):
                self.assertIn(f"`{mode.value}`", self.text)

    def test_the_documented_default_view_is_the_shipped_one(self) -> None:
        import json

        shipped = json.loads(
            (REPO_ROOT / "config" / "launcher.json").read_text(encoding="utf-8")
        )["default_view"]
        self.assertIn(f'"default_view": "{shipped}"', self.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
