"""Attract-mode preview manifests: schema and containment validation.

A game may optionally ship a short, pre-rendered looping animation at
``assets/preview/`` inside its own checkout -- see the cross-repo preview
contract this module implements the launcher's half of. It parses and
validates ``assets/preview/manifest.json`` and proves every frame path named
in it stays inside that directory, using the exact same containment rule
already applied to entrypoints (:func:`launcher.manifest.safe_relative_path`).

Deliberately free of Pygame: decoding image bytes, checking pixel dimensions
and enforcing the decoded-size cap is :mod:`launcher.ui.preview`'s job, since
that needs an image decoder. This module only ever reads and validates the
manifest JSON and resolves/validates frame paths -- it never opens an image
file.

Nothing here is fatal. Every rejection past "no preview shipped at all" is a
single logged warning and a ``None`` return; the caller's job is to fall back
to procedural card art, never to crash or blank the gallery over a game's
broken preview.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .errors import UnsafeEntrypointError
from .manifest import safe_relative_path

__all__ = [
    "SUPPORTED_PREVIEW_VERSION",
    "MIN_PREVIEW_FPS",
    "MAX_PREVIEW_FPS",
    "MAX_PREVIEW_FRAMES",
    "PREVIEW_DIRNAME",
    "MANIFEST_FILENAME",
    "PreviewManifest",
    "load_preview_manifest",
]

_log = logging.getLogger(__name__)

#: The only preview manifest schema version this launcher understands.
SUPPORTED_PREVIEW_VERSION = 1

MIN_PREVIEW_FPS = 1
MAX_PREVIEW_FPS = 30

#: Hard structural cap on frame count -- checked before anything is decoded.
#: A 1-3 second loop (the contract's own target) at up to 30fps is at most
#: ~90 frames; this leaves comfortable headroom for a slower-fps, slightly
#: longer loop while still refusing an unbounded list a broken or hostile
#: manifest might carry.
MAX_PREVIEW_FRAMES = 64

#: Directory a preview lives in, relative to a game's checkout root.
PREVIEW_DIRNAME = "assets/preview"
MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class PreviewManifest:
    """A validated preview manifest: playback rate and safe, absolute frame paths.

    ``frames`` are already resolved and proven to stay inside the game's own
    ``assets/preview`` directory -- nothing further needs re-checking before
    they are opened.
    """

    fps: int
    frames: tuple[Path, ...]


def _reject(game_id: str, reason: str) -> None:
    _log.warning("rejecting preview for game '%s': %s", game_id, reason)


def load_preview_manifest(checkout: Path, *, game_id: str) -> PreviewManifest | None:
    """Load and validate ``assets/preview/manifest.json`` inside *checkout*.

    Returns ``None`` -- never raises -- for anything short of a fully valid,
    in-bounds manifest: no preview directory at all (the common, unremarkable
    case for a game that has not added one yet), a missing or unreadable
    manifest file, invalid JSON, a schema violation, a frame count over
    :data:`MAX_PREVIEW_FRAMES`, or any frame path that escapes the preview
    directory (absolute, ``..``, or a symlink that resolves outside it).
    Every rejection past "no preview shipped at all" logs exactly one warning
    naming *game_id* and the reason.
    """
    preview_dir = checkout / PREVIEW_DIRNAME
    manifest_path = preview_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        _reject(game_id, f"could not read {manifest_path}: {exc}")
        return None

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        _reject(game_id, f"{manifest_path} is not valid JSON: {exc}")
        return None

    if not isinstance(document, dict):
        _reject(game_id, "manifest root must be a JSON object")
        return None

    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        _reject(game_id, "'version' must be an integer")
        return None
    if version != SUPPORTED_PREVIEW_VERSION:
        _reject(
            game_id,
            f"preview manifest version {version} is not supported (this "
            f"launcher understands version {SUPPORTED_PREVIEW_VERSION})",
        )
        return None

    fps = document.get("fps")
    if not isinstance(fps, int) or isinstance(fps, bool):
        _reject(game_id, "'fps' must be an integer")
        return None
    if not MIN_PREVIEW_FPS <= fps <= MAX_PREVIEW_FPS:
        _reject(game_id, f"'fps' {fps} is outside {MIN_PREVIEW_FPS}..{MAX_PREVIEW_FPS}")
        return None

    raw_frames = document.get("frames")
    if (
        not isinstance(raw_frames, list)
        or not raw_frames
        or not all(isinstance(item, str) for item in raw_frames)
    ):
        _reject(game_id, "'frames' must be a non-empty list of strings")
        return None
    if len(raw_frames) > MAX_PREVIEW_FRAMES:
        _reject(
            game_id,
            f"{len(raw_frames)} frames exceeds the cap of {MAX_PREVIEW_FRAMES}",
        )
        return None

    resolved: list[Path] = []
    for index, raw in enumerate(raw_frames):
        try:
            resolved.append(
                safe_relative_path(
                    raw,
                    preview_dir,
                    game_id=game_id,
                    field_name=f"preview frame [{index}]",
                )
            )
        except UnsafeEntrypointError as exc:
            _reject(game_id, str(exc))
            return None

    return PreviewManifest(fps=fps, frames=tuple(resolved))
