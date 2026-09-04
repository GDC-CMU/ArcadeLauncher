"""Exception hierarchy for ArcadeLauncher.

Every failure mode that the launcher can recover from -- or must report to a
visitor -- has a specific class here.  Nothing on the critical path catches a
bare :class:`Exception`; handlers catch these types (or a narrow stdlib type)
so that genuine programming errors still surface as tracebacks.
"""

from __future__ import annotations

__all__ = [
    "LauncherError",
    "ManifestError",
    "ManifestFileError",
    "ManifestSchemaError",
    "DuplicateGameIdError",
    "UnsupportedRuntimeError",
    "InvalidRepositoryUrlError",
    "UnsafeEntrypointError",
    "SettingsError",
    "CacheError",
    "GitUnavailableError",
    "SyncFailedError",
    "MissingEntrypointError",
    "NotLaunchableError",
    "LaunchError",
    "PygameUnavailableError",
    "UiFatalError",
]


class LauncherError(Exception):
    """Base class for every error raised by ArcadeLauncher."""

    #: Short, stable, human-facing summary used for on-screen banners.
    headline = "Launcher error"


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------
class ManifestError(LauncherError):
    """Base class for manifest problems."""

    headline = "Manifest error"


class ManifestFileError(ManifestError):
    """The manifest file is missing or is not valid JSON."""

    headline = "Manifest unreadable"


class ManifestSchemaError(ManifestError):
    """The manifest is valid JSON but the wrong shape or version."""

    headline = "Manifest schema invalid"


class DuplicateGameIdError(ManifestError):
    """Two or more entries share the same stable id."""

    headline = "Duplicate game id"


class UnsupportedRuntimeError(ManifestError):
    """An entry declares a runtime the launcher cannot start."""

    headline = "Unsupported runtime"


class InvalidRepositoryUrlError(ManifestError):
    """A repository URL is malformed or is not an https:// git URL."""

    headline = "Invalid repository URL"


class UnsafeEntrypointError(ManifestError):
    """An entrypoint is absolute, or escapes the game's checkout directory."""

    headline = "Unsafe entrypoint"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
class SettingsError(LauncherError):
    """The launcher settings file exists but cannot be used."""

    headline = "Settings invalid"


# --------------------------------------------------------------------------
# Cache / sync
# --------------------------------------------------------------------------
class CacheError(LauncherError):
    """Base class for repository cache problems."""

    headline = "Cache error"


class GitUnavailableError(CacheError):
    """``git`` is not installed or not on PATH."""

    headline = "Git not available"


class SyncFailedError(CacheError):
    """A clone or fetch failed (offline, auth, bad ref, ...)."""

    headline = "Update failed"


class MissingEntrypointError(CacheError):
    """The checkout exists but the configured entrypoint file does not."""

    headline = "Entrypoint missing"


# --------------------------------------------------------------------------
# Rendering runtime
# --------------------------------------------------------------------------
class PygameUnavailableError(LauncherError):
    """No usable Pygame could be loaded, even after clearing a shim.

    Raised by :mod:`launcher.ui.pygame_runtime` instead of exiting the process:
    ``main.py`` owns the exit code and paints the branded startup-failure
    screen, so this has to arrive as a catchable exception rather than a
    ``sys.exit()`` fired from the middle of an import.
    """

    headline = "Pygame unavailable"


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------
class NotLaunchableError(LauncherError):
    """A launch was requested for a coming-soon or unavailable game."""

    headline = "Not playable yet"


class LaunchError(LauncherError):
    """The child process could not be started at all."""

    headline = "Could not start game"


class UiFatalError(LauncherError):
    """The gallery UI failed repeatedly, or returned an invalid action.

    Raised instead of silently restarting so the supervisor can never spin in
    an infinite restart loop.
    """

    headline = "Gallery failed"
