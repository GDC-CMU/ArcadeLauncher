"""Probe: resolve every launcher path from a foreign working directory.

Run by :mod:`tests.test_paths` in a subprocess whose working directory is an
unrelated temporary one -- the situation on the arcade cabinet, where the outer
menu runs ``main.py`` from a directory nobody has documented.  It prints a
single JSON object describing what the launcher resolved, and the test asserts
on it.

Only the repository root goes on ``sys.path``, which is exactly what ``python
main.py`` does implicitly: ``sys.path[0]`` is the script's directory, never the
working directory.  So this probe reaches the package the same way the cabinet
does, and any path that secretly depended on the cwd shows up here as a wrong
answer or an outright failure.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from launcher.manifest import load_manifest  # noqa: E402 - needs the path above
from launcher.paths import (  # noqa: E402
    BRANDING_LOGO,
    MANIFEST_FILE,
    SETTINGS_FILE,
    default_cache_root,
    games_root,
    run_root,
)
from launcher.settings import load_settings  # noqa: E402


def main() -> int:
    """Load everything the launcher loads at start-up and report the result."""
    manifest = load_manifest()
    settings = load_settings()
    report = {
        "cwd": os.getcwd(),
        "repo_root": str(REPO_ROOT),
        "manifest_file": str(MANIFEST_FILE),
        "settings_file": str(SETTINGS_FILE),
        "branding_logo": str(BRANDING_LOGO),
        "branding_logo_exists": BRANDING_LOGO.is_file(),
        "cache_root": str(default_cache_root()),
        "games_root": str(games_root()),
        "run_root": str(run_root()),
        "game_ids": [entry.id for entry in manifest],
        "default_view": settings.default_view.value,
    }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
