"""Shared helpers: manifest builders, a fake git runner, and fixture repos.

Every test in this suite must satisfy three rules:

1. **No network.** Cache and sync tests use local fixture git repositories
   created in a temporary directory, or an injected fake git runner.
2. **No hardware.** Joystick behaviour is tested through the pure-logic input
   modules and synthetic SDL events, never a real controller.
3. **No real repository is modified.** Nothing here writes inside the working
   tree; caches go to ``tempfile.mkdtemp``.

Importing this module also pins SDL to its dummy drivers.  Test modules that
touch pygame import ``support`` *first* so that ``python -m unittest discover
-s tests -v`` works on a headless machine with nothing exported by the caller
(acceptance criterion I3).
"""

from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import logging  # noqa: E402 - must follow the SDL environment pinning above

# The launcher deliberately logs every refusal, crash and fallback. That is the
# right behaviour on a cabinet and the wrong behaviour in a test report, where
# it buries the actual results, so quiet it down here.
logging.getLogger("launcher").setLevel(logging.CRITICAL)

import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Sequence  # noqa: E402

from launcher.cache import GitResult  # noqa: E402
from launcher.manifest import (  # noqa: E402
    CardArt,
    GameEntry,
    Manifest,
    Runtime,
    parse_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: A fully populated, launchable manifest entry as raw JSON-ish data.
LAUNCHABLE_RAW: dict[str, Any] = {
    "id": "streetfighter",
    "title": "Street Fighter",
    "description": "Two-player duel built by the club.",
    "runtime": "python",
    "launchable": True,
    "repository": "https://github.com/GDC-CMU/StreetFighter.git",
    "ref": "main",
    "entrypoint": "main.py",
    "art": {"motif": "duel", "palette": ["cmu_red", "warm_amber", "ink"], "seed": 11},
}

#: A curated but deliberately unplayable entry.
COMING_SOON_RAW: dict[str, Any] = {
    "id": "flappy-scotty",
    "title": "Flappy Scotty",
    "description": "In development by the club.",
    "runtime": "python",
    "launchable": False,
    "note": "Coming soon",
    "art": {
        "motif": "flight",
        "palette": ["electric_cyan", "warm_amber", "ink"],
        "seed": 33,
    },
}


def manifest_document(*games: dict[str, Any]) -> dict[str, Any]:
    """Wrap raw *games* in a version-1 manifest document."""
    entries = list(games) or [dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW)]
    return {"version": 1, "games": entries}


def build_manifest(*games: dict[str, Any]) -> Manifest:
    """Parse a manifest document built from *games*."""
    return parse_manifest(manifest_document(*games))


def entry(**overrides: Any) -> GameEntry:
    """Construct a :class:`GameEntry` directly, bypassing JSON validation.

    Used where a test needs something the public manifest schema forbids on
    purpose -- for example a local filesystem clone URL, which real manifests
    may not contain but fixture git repos must use to stay offline.
    """
    fields: dict[str, Any] = {
        "id": "fixture-game",
        "title": "Fixture Game",
        "description": "A local fixture.",
        "runtime": Runtime.PYTHON,
        "launchable": True,
        "art": CardArt(motif="duel", palette=("cmu_red", "warm_amber", "ink"), seed=7),
        "repository": "https://example.invalid/fixture.git",
        "ref": "main",
        "entrypoint": "main.py",
        "note": "",
    }
    fields.update(overrides)
    return GameEntry(**fields)


class FakeGitRunner:
    """A scripted stand-in for git.

    Args:
        results: Mapping from the first git argument (``clone``, ``fetch`` ...)
            to the result it should return. Anything unscripted succeeds.
        available: Whether git should appear installed at all.
    """

    def __init__(
        self,
        results: dict[str, GitResult] | None = None,
        *,
        available: bool = True,
        on_clone: Any = None,
    ) -> None:
        self.results = results or {}
        self._available = available
        self.calls: list[tuple[str, ...]] = []
        self.on_clone = on_clone

    def available(self) -> bool:
        return self._available

    def run(self, arguments: Sequence[str], cwd: Path | None = None) -> GitResult:
        args = tuple(arguments)
        self.calls.append(args)
        verb = args[0] if args else ""
        if verb == "clone" and self.on_clone is not None:
            self.on_clone(Path(args[-1]))
        if verb == "rev-parse":
            return GitResult(0, stdout="abc1234\n")
        return self.results.get(verb, GitResult(0))

    def verbs(self) -> list[str]:
        """Just the git subcommands seen so far, for readable assertions."""
        return [call[0] for call in self.calls if call]


def git_available() -> bool:
    """Whether a usable git binary exists on this machine."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "--version"], capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def make_fixture_repo(directory: Path, entrypoint: str = "main.py") -> Path:
    """Create a real, tiny, **local** git repository at *directory*.

    This is what keeps the cache tests honest without touching the network:
    ``git clone <directory>`` exercises the same code path as a GitHub clone.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / entrypoint
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=directory,
            check=True,
            capture_output=True,
            timeout=60,
        )

    git("init", "--initial-branch=main")
    git("config", "user.email", "arcade@example.invalid")
    git("config", "user.name", "Arcade Fixture")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-m", "fixture")
    return directory


def advance_fixture_repo(directory: Path, entrypoint: str = "main.py") -> str:
    """Commit a real change onto a repo made by :func:`make_fixture_repo`.

    Used to simulate "a fix landed upstream while the cabinet was running":
    the returned short commit id is what a fresh ``sync()`` of a checkout
    cloned *before* this call must end up at.
    """
    target = directory / entrypoint
    target.write_text(
        target.read_text(encoding="utf-8") + "# advanced\n", encoding="utf-8"
    )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    git("add", "-A")
    git("commit", "-m", "advance fixture")
    return git("rev-parse", "--short", "HEAD").stdout.strip()


class TempDirCase(unittest.TestCase):
    """Base class providing a scratch directory that is always cleaned up."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="arcade-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)


def child_fixture(name: str) -> Path:
    """Absolute path to a fixture child program."""
    return FIXTURES / name


def python_command(script: Path) -> list[str]:
    """A no-shell argv for running *script* with the current interpreter."""
    return [sys.executable, script.name]
