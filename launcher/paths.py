"""Filesystem locations used by ArcadeLauncher.

Everything is derived from ``__file__`` -- the *installed* location -- with
:mod:`pathlib`, so the same code works on the Windows development machine and
on the Linux arcade box.  Importing this module has no side effects: nothing is
created here.

**Nothing here may ever be resolved against the process working directory.**
The arcade menu runs ``main.py`` from a directory nobody has documented, and it
is quite possibly not the repository root.  A single ``Path("data/games.json")``
would turn that into "manifest not found" on the cabinet and nowhere else --
the launcher would come up black in front of a queue of visitors while working
perfectly on every machine anyone could test it on.  :mod:`tests.test_paths`
holds that line, both by running the real loaders from an unrelated working
directory and by refusing to let ``Path.cwd()`` back into the package.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import SettingsError

__all__ = [
    "REPO_ROOT",
    "ASSETS_DIR",
    "BRANDING_LOGO",
    "CONFIG_DIR",
    "SETTINGS_FILE",
    "DATA_DIR",
    "MANIFEST_FILE",
    "DOCS_DIR",
    "SCREENSHOTS_DIR",
    "CACHE_DIR_NAME",
    "CACHE_ROOT_ENV",
    "default_cache_root",
    "games_root",
    "run_root",
    "checkout_dir",
    "default_runtime_root",
    "default_game_data_root",
]

#: Repository root -- ``launcher/paths.py`` lives one directory below it.
#: ``resolve()`` also collapses the symlink the cabinet's ROM folder may be,
#: so every path below is absolute and stable for the life of the process.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

ASSETS_DIR: Path = REPO_ROOT / "assets"
BRANDING_LOGO: Path = ASSETS_DIR / "branding" / "gdc-cmu-logo.png"

CONFIG_DIR: Path = REPO_ROOT / "config"
SETTINGS_FILE: Path = CONFIG_DIR / "launcher.json"

DATA_DIR: Path = REPO_ROOT / "data"
MANIFEST_FILE: Path = DATA_DIR / "games.json"

DOCS_DIR: Path = REPO_ROOT / "docs"
SCREENSHOTS_DIR: Path = DOCS_DIR / "screenshots"

#: Name of the git-ignored directory that holds managed game checkouts.
CACHE_DIR_NAME = ".arcade-cache"

#: Environment variable that relocates the managed cache (used by tests).
CACHE_ROOT_ENV = "ARCADE_LAUNCHER_CACHE"


def _absolute_override(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value).expanduser() if value else fallback
    if not path.is_absolute():
        raise SettingsError(f"{name} must be an absolute path, not {path!s}")
    return path


def default_runtime_root() -> Path:
    """User-owned engine cache, independent of disposable game checkouts."""
    if os.name == "nt":
        local = _absolute_override("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        fallback = local / "GDC-CMU" / "ArcadeLauncher" / "runtimes"
    else:
        cache = _absolute_override("XDG_CACHE_HOME", Path.home() / ".cache")
        fallback = cache / "arcade-launcher" / "runtimes"
    return _absolute_override("ARCADE_LAUNCHER_RUNTIME_CACHE", fallback)


def default_game_data_root() -> Path:
    """Persistent saves: never under a build, venv, rollback, or runtime cache."""
    if os.name == "nt":
        local = _absolute_override("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        fallback = local / "GDC-CMU" / "ArcadeLauncher" / "userdata"
    else:
        data = _absolute_override("XDG_DATA_HOME", Path.home() / ".local" / "share")
        fallback = data / "arcade-launcher" / "games"
    return _absolute_override("ARCADE_LAUNCHER_DATA_ROOT", fallback)


def default_cache_root() -> Path:
    """Return the managed cache root.

    Defaults to ``<repo>/.arcade-cache`` -- *inside the checkout*, not beside
    whatever directory the launcher was started from -- but may be redirected
    with the ``ARCADE_LAUNCHER_CACHE`` environment variable so that the test
    suite can work inside a temporary directory and never touch the real
    checkout.
    """
    override = os.environ.get(CACHE_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return REPO_ROOT / CACHE_DIR_NAME


def games_root(cache_root: Path | None = None) -> Path:
    """Directory holding one checkout per launchable game."""
    return (cache_root or default_cache_root()) / "games"


def run_root(cache_root: Path | None = None) -> Path:
    """Directory for transient per-run files (child logs, action records)."""
    return (cache_root or default_cache_root()) / "run"


def checkout_dir(game_id: str, cache_root: Path | None = None) -> Path:
    """Return ``.arcade-cache/games/<game_id>`` for *game_id*."""
    return games_root(cache_root) / game_id
