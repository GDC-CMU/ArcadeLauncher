"""GDC Arcade Launcher -- the arcade cabinet entry point.

The CMU-Q arcade box clones this repository, installs ``requirements.txt`` and
runs this file.  Everything below is deliberately thin: build the pieces, hand
them to the supervisor, and exit with its return code.

``sys.exit(0)`` is the documented way back to the arcade's outer menu, which is
why a normal exit here is always ``0``.

Note the import list below: it is deliberately free of anything that touches
Pygame.  The rendering layer is imported *inside* :func:`main`, after logging
is configured, so that a cabinet where ``cmu_graphics`` has shadowed the real
Pygame reports a readable failure instead of dying in an import statement.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Callable

from launcher import __version__
from launcher.cache import RepositoryCache, SubprocessGitRunner
from launcher.errors import (
    LauncherError,
    ManifestError,
    PygameUnavailableError,
    SettingsError,
)
from launcher.manifest import load_manifest
from launcher.paths import MANIFEST_FILE, SETTINGS_FILE, default_cache_root
from launcher.settings import Settings, load_settings
from launcher.supervisor import SessionState, Supervisor
from launcher.sync import SyncService, initial_states

_log = logging.getLogger("launcher")

EXIT_OK = 0
EXIT_FAILED = 1


def configure_logging(verbose: bool = False) -> None:
    """Send launcher logs to stderr so the cabinet's console keeps a record."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ArcadeLauncher",
        description="Game Dev Club arcade gallery for the CMU-Q cabinet.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--config", type=Path, default=SETTINGS_FILE)
    parser.add_argument(
        "--cache", type=Path, default=None, help="override the repository cache root"
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="never touch the network; use whatever is already cached",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def load_gallery() -> Callable[..., object]:
    """Import the rendering layer, late and on purpose.

    ``launcher.gallery`` pulls in :mod:`launcher.ui.pygame_runtime`, which is
    where the cabinet's ``cmu_graphics`` shim gets cleared out of the way -- and
    which raises :class:`~launcher.errors.PygameUnavailableError` when no real
    Pygame can be produced.  Importing it at module scope would turn that into
    a traceback thrown before :func:`main` ever ran: no log line, no branded
    screen, and a black cabinet at the club fair.

    Raises:
        PygameUnavailableError: No usable Pygame on this machine.
    """
    from launcher.gallery import GallerySession

    return GallerySession


def report_fatal(error: Exception, settings: Settings | None) -> int:
    """Show a startup failure on screen when we can, and on the console always.

    A cabinet has no keyboard and no terminal in front of it, so a
    configuration mistake that only printed to stderr would look identical to a
    hang.  Criterion B5: the visitor sees a real message.

    The on-screen half needs Pygame, which is exactly what may have just
    failed; that is why it is guarded and why the console line below it is
    unconditional.
    """
    _log.error("%s", error, exc_info=True)
    headline = getattr(error, "headline", "Launcher failed to start")
    try:
        from launcher.ui.fatal import show_fatal_screen

        show_fatal_screen(headline, str(error), fullscreen=bool(settings and settings.fullscreen))
    except Exception:  # noqa: BLE001 - the console message below is the fallback
        _log.exception("could not display the on-screen error")
    print(f"{headline}: {error}", file=sys.stderr)
    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    _log.info("GDC Arcade Launcher %s starting", __version__)

    settings: Settings | None = None
    try:
        settings = load_settings(args.config)
        manifest = load_manifest(args.manifest)
        gallery_session = load_gallery()
    except (SettingsError, ManifestError, PygameUnavailableError) as exc:
        return report_fatal(exc, settings)

    cache_root = args.cache or default_cache_root()
    cache = RepositoryCache(
        cache_root, runner=SubprocessGitRunner(timeout_s=settings.network_timeout_s)
    )
    online = settings.sync_on_start and not args.no_sync
    if online and not cache.git_available:
        _log.warning("git is not available; running from cache only")
        online = False

    states = initial_states(manifest, sync_enabled=online)
    _log.info(
        "manifest: %s games (%s launchable), cache at %s",
        len(manifest),
        len(manifest.launchable),
        cache_root,
    )

    try:
        shutdown = threading.Event()
        with SyncService(cache, online=online) as sync:
            session = gallery_session(
                manifest, settings, states, sync, should_stop=shutdown.is_set
            )
            supervisor = Supervisor(
                manifest,
                cache,
                session,
                initial_state=SessionState(view_mode=settings.default_view),
                shutdown=shutdown,
            )
            return supervisor.run()
    except LauncherError as exc:
        return report_fatal(exc, settings)
    except KeyboardInterrupt:
        _log.info("interrupted; returning to the arcade menu")
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
