"""Runtime settings: a committed JSON file plus environment overrides.

Changing the default gallery mode -- or any timing constant -- must never
require a code edit.  Precedence is:

1. Environment variable (highest; useful for a one-off demo or a test).
2. ``config/launcher.json`` (what a club member edits and commits).
3. The built-in defaults below (lowest).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import SettingsError
from .paths import SETTINGS_FILE
from .viewmodes import ViewMode

__all__ = [
    "Settings",
    "DEFAULTS",
    "ENV_DEFAULT_VIEW",
    "load_settings",
]

#: Environment variable that overrides the default gallery mode.
ENV_DEFAULT_VIEW = "ARCADE_LAUNCHER_VIEW"
ENV_FULLSCREEN = "ARCADE_LAUNCHER_FULLSCREEN"
ENV_FRAME_RATE = "ARCADE_LAUNCHER_FPS"
ENV_SYNC_ON_START = "ARCADE_LAUNCHER_SYNC"
ENV_ATTRACT_IDLE_MS = "ARCADE_LAUNCHER_ATTRACT_IDLE_MS"


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated launcher configuration.

    Attributes:
        default_view: Gallery mode shown when the launcher starts.
        fullscreen: Whether to open the 800x600 display fullscreen. The arcade
            box wants ``true``; a development machine usually wants ``false``.
        frame_rate: Target frames per second.
        sync_on_start: Whether to kick off the background repository sync as
            soon as the gallery appears.
        nav_initial_delay_ms: Hold time before a held direction starts to
            auto-repeat.
        nav_repeat_ms: Interval between auto-repeats once repeating.
        axis_deadzone: Magnitude above which an arcade axis counts as pushed.
            The arcade stick is digital, so 0.5 matches the reference
            ``joystick.py`` from the arcade startercode.
        network_timeout_s: Per-git-command timeout for background syncing, in
            seconds. Kept low deliberately: a disconnected cabinet pays this
            once per launchable game at start-up and once more before every
            launch, so a high value turns "no Wi-Fi" into a long visible
            stall rather than an instant, honest ``CACHED_OFFLINE``. See
            ``launcher.cache._DEFAULT_GIT_TIMEOUT_S`` for the fair-day
            reasoning behind the default.
        attract_idle_ms: How long the gallery must see *zero* genuine input
            (buttons, keys, or stick movement past ``axis_deadzone``) before
            it drops into attract mode -- see :mod:`launcher.attract`. This
            governs the gallery screen, not a game's own attract mode: the
            club's games use a fixed 15 seconds for theirs, but the gallery
            is a lower-stakes screen a visitor is more likely to be reading
            (a description, a status badge) without touching the stick, so
            a minute gives them room to do that before the demo takes over.
    """

    default_view: ViewMode = ViewMode.CAROUSEL
    fullscreen: bool = True
    frame_rate: int = 60
    sync_on_start: bool = True
    nav_initial_delay_ms: int = 380
    nav_repeat_ms: int = 140
    axis_deadzone: float = 0.5
    network_timeout_s: int = 8
    attract_idle_ms: int = 60_000


#: Built-in fallbacks used when ``config/launcher.json`` is absent.
DEFAULTS = Settings()

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _as_bool(raw: Any, *, source: str, key: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in _BOOL_TRUE:
        return True
    if isinstance(raw, str) and raw.strip().lower() in _BOOL_FALSE:
        return False
    raise SettingsError(f"{source}: '{key}' must be a boolean, got {raw!r}")


def _as_int(raw: Any, *, source: str, key: str, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{source}: '{key}' must be an integer, got {raw!r}") from None
    if not low <= value <= high:
        raise SettingsError(f"{source}: '{key}' must be between {low} and {high}, got {value}")
    return value


def _as_float(raw: Any, *, source: str, key: str, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{source}: '{key}' must be a number, got {raw!r}") from None
    if not low <= value <= high:
        raise SettingsError(f"{source}: '{key}' must be between {low} and {high}, got {value}")
    return value


def _as_view(raw: Any, *, source: str, key: str) -> ViewMode:
    try:
        return ViewMode.parse(str(raw))
    except ValueError as exc:
        raise SettingsError(f"{source}: '{key}' {exc}") from exc


def _apply_document(settings: Settings, document: Mapping[str, Any], source: str) -> Settings:
    unknown = set(document) - {f for f in Settings.__slots__}
    if unknown:
        raise SettingsError(
            f"{source}: unknown setting(s): {', '.join(sorted(unknown))}"
        )
    updates: dict[str, Any] = {}
    if "default_view" in document:
        updates["default_view"] = _as_view(
            document["default_view"], source=source, key="default_view"
        )
    if "fullscreen" in document:
        updates["fullscreen"] = _as_bool(
            document["fullscreen"], source=source, key="fullscreen"
        )
    if "sync_on_start" in document:
        updates["sync_on_start"] = _as_bool(
            document["sync_on_start"], source=source, key="sync_on_start"
        )
    if "frame_rate" in document:
        updates["frame_rate"] = _as_int(
            document["frame_rate"], source=source, key="frame_rate", low=15, high=240
        )
    if "nav_initial_delay_ms" in document:
        updates["nav_initial_delay_ms"] = _as_int(
            document["nav_initial_delay_ms"],
            source=source,
            key="nav_initial_delay_ms",
            low=0,
            high=2000,
        )
    if "nav_repeat_ms" in document:
        updates["nav_repeat_ms"] = _as_int(
            document["nav_repeat_ms"], source=source, key="nav_repeat_ms", low=30, high=2000
        )
    if "axis_deadzone" in document:
        updates["axis_deadzone"] = _as_float(
            document["axis_deadzone"], source=source, key="axis_deadzone", low=0.05, high=0.95
        )
    if "network_timeout_s" in document:
        updates["network_timeout_s"] = _as_int(
            document["network_timeout_s"],
            source=source,
            key="network_timeout_s",
            low=5,
            high=600,
        )
    if "attract_idle_ms" in document:
        updates["attract_idle_ms"] = _as_int(
            document["attract_idle_ms"],
            source=source,
            key="attract_idle_ms",
            low=1_000,
            high=1_800_000,
        )
    return replace(settings, **updates)


def _apply_environment(settings: Settings, env: Mapping[str, str]) -> Settings:
    document: dict[str, Any] = {}
    if env.get(ENV_DEFAULT_VIEW):
        document["default_view"] = env[ENV_DEFAULT_VIEW]
    if env.get(ENV_FULLSCREEN):
        document["fullscreen"] = env[ENV_FULLSCREEN]
    if env.get(ENV_FRAME_RATE):
        document["frame_rate"] = env[ENV_FRAME_RATE]
    if env.get(ENV_SYNC_ON_START):
        document["sync_on_start"] = env[ENV_SYNC_ON_START]
    if env.get(ENV_ATTRACT_IDLE_MS):
        document["attract_idle_ms"] = env[ENV_ATTRACT_IDLE_MS]
    if not document:
        return settings
    return _apply_document(settings, document, source="environment")


def load_settings(
    path: Path | None = None, env: Mapping[str, str] | None = None
) -> Settings:
    """Load settings from *path*, then apply environment overrides.

    A missing settings file is fine -- the built-in defaults are used. A file
    that exists but is malformed is an error, because silently ignoring it
    would hide a club member's intent.

    Args:
        path: Settings file. Defaults to ``config/launcher.json``.
        env: Environment mapping. Defaults to :data:`os.environ`.

    Returns:
        A fully validated :class:`Settings`.

    Raises:
        SettingsError: The file is unreadable, is not JSON, or holds a value
            that is out of range or names an unknown gallery mode.
    """
    settings_path = Path(path) if path is not None else SETTINGS_FILE
    environment = os.environ if env is None else env

    settings = DEFAULTS
    if settings_path.exists():
        try:
            text = settings_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SettingsError(f"could not read {settings_path}: {exc}") from exc
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SettingsError(
                f"{settings_path} is not valid JSON (line {exc.lineno}, "
                f"column {exc.colno}): {exc.msg}"
            ) from exc
        if not isinstance(document, Mapping):
            raise SettingsError(f"{settings_path}: root must be a JSON object")
        # '$schema'-style comment keys are stripped so the file can carry notes.
        document = {k: v for k, v in document.items() if not k.startswith("_")}
        settings = _apply_document(settings, document, source=str(settings_path))

    return _apply_environment(settings, environment)
