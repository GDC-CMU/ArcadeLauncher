"""Typed model and validation for the curated game manifest.

The manifest (``data/games.json``) is the single source of truth for what the
gallery shows.  It is parsed once at start-up into frozen dataclasses; the UI
never touches raw dictionaries.

Validation is strict and *specific*: every rejection raises a dedicated
exception from :mod:`launcher.errors` so the on-screen error tells a club
member exactly which entry and which field is wrong.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from .errors import (
    DuplicateGameIdError,
    InvalidRepositoryUrlError,
    ManifestFileError,
    ManifestSchemaError,
    UnsafeEntrypointError,
    UnsupportedRuntimeError,
)
from .paths import MANIFEST_FILE, REPO_ROOT

__all__ = [
    "SUPPORTED_MANIFEST_VERSION",
    "SUPPORTED_MOTIFS",
    "Runtime",
    "CardArt",
    "GameEntry",
    "Manifest",
    "load_manifest",
    "parse_manifest",
    "safe_relative_path",
]

#: The only manifest schema version this launcher understands.
SUPPORTED_MANIFEST_VERSION = 1

#: Procedural card-art motifs implemented in :mod:`launcher.ui.art`.
SUPPORTED_MOTIFS: tuple[str, ...] = (
    "duel",
    "relay",
    "flight",
    "hazard",
    "ember",
    "orbit",
    "maze",
    "pitch",
)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,62}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

#: Stand-in checkout root used to prove an entrypoint is safe *before* anything
#: has been cloned.  No file is ever created here -- it exists only to give
#: :func:`safe_relative_path` a base to resolve against.
#:
#: Anchored to the installed location rather than :func:`Path.cwd`, because the
#: arcade menu runs ``main.py`` from an undocumented working directory: a
#: cwd-relative base would make manifest validation depend on where the box
#: happened to be standing, and ``Path.cwd()`` raises outright if that
#: directory has since been removed.
_VALIDATION_ROOT: Path = REPO_ROOT / ".__manifest_validation__"


class Runtime(Enum):
    """Execution model for a manifest entry.

    Only ``python`` is supported today: the launcher starts the entrypoint with
    :data:`sys.executable`.  Adding a runtime means teaching
    :mod:`launcher.supervisor` how to build its argument list, so unknown values
    are rejected loudly rather than assumed.
    """

    PYTHON = "python"

    @classmethod
    def parse(cls, raw: Any, *, game_id: str) -> "Runtime":
        if not isinstance(raw, str):
            raise UnsupportedRuntimeError(
                f"game '{game_id}': runtime must be a string, got "
                f"{type(raw).__name__}"
            )
        try:
            return cls(raw)
        except ValueError:
            supported = ", ".join(sorted(member.value for member in cls))
            raise UnsupportedRuntimeError(
                f"game '{game_id}': unsupported runtime '{raw}' "
                f"(supported: {supported})"
            ) from None


@dataclass(frozen=True, slots=True)
class CardArt:
    """Card-art configuration for one game.

    Attributes:
        motif: Which procedural generator to run (see :data:`SUPPORTED_MOTIFS`).
        palette: Three palette *keys* resolved against
            :data:`launcher.ui.theme.PALETTE` at render time. Kept as names so
            the manifest carries no raw colour values and stays theme-driven.
        seed: Deterministic seed, so the same card is drawn every run and the
            screenshots are reproducible.
    """

    motif: str
    palette: tuple[str, str, str]
    seed: int

    @classmethod
    def parse(cls, raw: Any, *, game_id: str) -> "CardArt":
        if not isinstance(raw, Mapping):
            raise ManifestSchemaError(
                f"game '{game_id}': 'art' must be an object"
            )
        motif = raw.get("motif")
        if motif not in SUPPORTED_MOTIFS:
            supported = ", ".join(SUPPORTED_MOTIFS)
            raise ManifestSchemaError(
                f"game '{game_id}': art.motif '{motif}' is not one of: {supported}"
            )
        palette = raw.get("palette")
        if (
            not isinstance(palette, Sequence)
            or isinstance(palette, str)
            or len(palette) != 3
            or not all(isinstance(name, str) and name for name in palette)
        ):
            raise ManifestSchemaError(
                f"game '{game_id}': art.palette must be a list of exactly three "
                "palette names"
            )
        seed = raw.get("seed", 0)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ManifestSchemaError(
                f"game '{game_id}': art.seed must be an integer"
            )
        return cls(
            motif=motif,
            palette=(str(palette[0]), str(palette[1]), str(palette[2])),
            seed=seed,
        )


def safe_relative_path(raw: str, base: Path, *, game_id: str, field_name: str) -> Path:
    """Resolve *raw* against *base*, refusing anything that escapes *base*.

    This single helper backs both manifest validation and child-command
    construction, so an entrypoint can never be validated by one rule and then
    executed under another.

    Rejected: absolute POSIX paths (``/etc/passwd``), absolute Windows paths
    and drive-relative paths (``C:\\x.py``, ``\\\\server\\share``), parent
    traversal (``../evil.py``), empty strings, and anything that -- after
    normalisation -- lands outside *base*.

    Args:
        raw: The path as written in the manifest.
        base: The directory the path must stay inside.
        game_id: Used only for the error message.
        field_name: Used only for the error message.

    Returns:
        The absolute, normalised path inside *base*.

    Raises:
        UnsafeEntrypointError: If *raw* is unusable or escapes *base*.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeEntrypointError(
            f"game '{game_id}': {field_name} must be a non-empty relative path"
        )
    candidate = raw.strip()

    if PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute():
        raise UnsafeEntrypointError(
            f"game '{game_id}': {field_name} '{candidate}' is an absolute path; "
            "entrypoints must be relative to the game checkout"
        )
    if _WINDOWS_DRIVE.match(candidate) or candidate.startswith(("\\", "/")):
        raise UnsafeEntrypointError(
            f"game '{game_id}': {field_name} '{candidate}' is drive- or "
            "root-relative; entrypoints must be relative to the game checkout"
        )
    if "\\" in candidate:
        raise UnsafeEntrypointError(
            f"game '{game_id}': {field_name} '{candidate}' uses backslashes; "
            "use forward slashes so the manifest stays cross-platform"
        )

    parts = PurePosixPath(candidate).parts
    if any(part == ".." for part in parts):
        raise UnsafeEntrypointError(
            f"game '{game_id}': {field_name} '{candidate}' escapes the checkout "
            "directory with '..'"
        )

    base_resolved = Path(base).resolve()
    resolved = (base_resolved / PurePosixPath(candidate)).resolve()
    if base_resolved not in resolved.parents:
        # Covers both escapes ('../x') and degenerate self-references ('.'),
        # which name a directory rather than a program to run.
        raise UnsafeEntrypointError(
            f"game '{game_id}': {field_name} '{candidate}' resolves outside the "
            f"checkout directory"
        )
    return resolved


def _validate_repository(raw: Any, *, game_id: str) -> str:
    """Validate a clone URL: https only, real host, ends in a repo name."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidRepositoryUrlError(
            f"game '{game_id}': repository must be a non-empty string"
        )
    url = raw.strip()
    split = urlsplit(url)
    if split.scheme != "https":
        raise InvalidRepositoryUrlError(
            f"game '{game_id}': repository '{url}' must use https:// "
            f"(got '{split.scheme or 'no scheme'}')"
        )
    if not split.netloc:
        raise InvalidRepositoryUrlError(
            f"game '{game_id}': repository '{url}' has no host"
        )
    if not split.path.strip("/"):
        raise InvalidRepositoryUrlError(
            f"game '{game_id}': repository '{url}' has no repository path"
        )
    return url


def _validate_id(raw: Any, *, index: int) -> str:
    """Validate a stable id; it also becomes a cache directory name."""
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestSchemaError(
            f"games[{index}]: 'id' must be a non-empty string"
        )
    game_id = raw.strip()
    if game_id in {".", ".."} or "/" in game_id or "\\" in game_id:
        raise UnsafeEntrypointError(
            f"games[{index}]: id '{game_id}' is not a safe directory name; it "
            "would escape the managed cache directory"
        )
    if not _ID_PATTERN.match(game_id):
        raise ManifestSchemaError(
            f"games[{index}]: id '{game_id}' must be 3-40 lowercase characters "
            "using a-z, 0-9 and '-'"
        )
    return game_id


def _validate_text(raw: Any, *, game_id: str, field_name: str, limit: int) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestSchemaError(
            f"game '{game_id}': '{field_name}' must be a non-empty string"
        )
    text = raw.strip()
    if len(text) > limit:
        raise ManifestSchemaError(
            f"game '{game_id}': '{field_name}' is longer than {limit} characters"
        )
    return text


def _validate_ref(raw: Any, *, game_id: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestSchemaError(
            f"game '{game_id}': 'ref' must be a non-empty git ref"
        )
    ref = raw.strip()
    if not _REF_PATTERN.match(ref) or ".." in ref:
        raise ManifestSchemaError(
            f"game '{game_id}': ref '{ref}' is not a valid git ref name"
        )
    return ref


@dataclass(frozen=True, slots=True)
class GameEntry:
    """One curated game.

    A *launchable* entry carries everything needed to clone and start the game.
    A *coming soon* entry deliberately carries no repository, ref or entrypoint
    at all -- which makes it structurally impossible for the cache layer to
    fetch it or for the supervisor to spawn it.
    """

    id: str
    title: str
    description: str
    runtime: Runtime
    launchable: bool
    art: CardArt
    repository: str | None = None
    ref: str | None = None
    entrypoint: str | None = None
    note: str = ""

    @property
    def is_coming_soon(self) -> bool:
        """True when the entry is curated but intentionally not playable."""
        return not self.launchable

    def resolved_entrypoint(self, checkout: Path) -> Path:
        """Return the absolute entrypoint inside *checkout*.

        Re-runs the same containment check used at manifest-validation time, so
        a checkout containing a symlink cannot redirect execution outside the
        cache.

        Raises:
            UnsafeEntrypointError: If the entry has no entrypoint or the path
                escapes *checkout*.
        """
        if not self.entrypoint:
            raise UnsafeEntrypointError(
                f"game '{self.id}': has no entrypoint and cannot be launched"
            )
        return safe_relative_path(
            self.entrypoint, checkout, game_id=self.id, field_name="entrypoint"
        )

    @classmethod
    def parse(cls, raw: Any, *, index: int) -> "GameEntry":
        if not isinstance(raw, Mapping):
            raise ManifestSchemaError(f"games[{index}] must be an object")

        game_id = _validate_id(raw.get("id"), index=index)
        title = _validate_text(raw.get("title"), game_id=game_id, field_name="title", limit=40)
        description = _validate_text(
            raw.get("description"), game_id=game_id, field_name="description", limit=400
        )
        runtime = Runtime.parse(raw.get("runtime"), game_id=game_id)

        launchable = raw.get("launchable")
        if not isinstance(launchable, bool):
            raise ManifestSchemaError(
                f"game '{game_id}': 'launchable' must be true or false"
            )

        art = CardArt.parse(raw.get("art"), game_id=game_id)

        note = raw.get("note", "")
        if not isinstance(note, str):
            raise ManifestSchemaError(f"game '{game_id}': 'note' must be a string")

        repository: str | None = None
        ref: str | None = None
        entrypoint: str | None = None

        if launchable:
            repository = _validate_repository(raw.get("repository"), game_id=game_id)
            ref = _validate_ref(raw.get("ref"), game_id=game_id)
            raw_entrypoint = raw.get("entrypoint")
            if raw_entrypoint is None:
                raise ManifestSchemaError(
                    f"game '{game_id}': launchable entries must define "
                    "'entrypoint' (the file the launcher runs)"
                )
            # Validate against a synthetic checkout root: the rule must hold
            # before anything has been cloned, and the answer must not depend
            # on the working directory the arcade menu happened to use.
            safe_relative_path(
                raw_entrypoint if isinstance(raw_entrypoint, str) else "",
                _VALIDATION_ROOT / game_id,
                game_id=game_id,
                field_name="entrypoint",
            )
            entrypoint = str(raw_entrypoint).strip()
        else:
            for forbidden in ("repository", "ref", "entrypoint"):
                if raw.get(forbidden):
                    raise ManifestSchemaError(
                        f"game '{game_id}': coming-soon entries must not define "
                        f"'{forbidden}'; it would imply the launcher may fetch or "
                        "run them"
                    )

        return cls(
            id=game_id,
            title=title,
            description=description,
            runtime=runtime,
            launchable=launchable,
            art=art,
            repository=repository,
            ref=ref,
            entrypoint=entrypoint,
            note=note.strip(),
        )


@dataclass(frozen=True, slots=True)
class Manifest:
    """A validated, ordered collection of :class:`GameEntry` objects."""

    version: int
    games: tuple[GameEntry, ...] = field(default_factory=tuple)

    def __iter__(self) -> Iterator[GameEntry]:
        return iter(self.games)

    def __len__(self) -> int:
        return len(self.games)

    def __getitem__(self, index: int) -> GameEntry:
        return self.games[index]

    def by_id(self, game_id: str) -> GameEntry:
        """Return the entry with *game_id*.

        Raises:
            KeyError: If no such entry exists.
        """
        for entry in self.games:
            if entry.id == game_id:
                return entry
        raise KeyError(game_id)

    @property
    def launchable(self) -> tuple[GameEntry, ...]:
        """Only the entries the launcher is allowed to fetch and run."""
        return tuple(entry for entry in self.games if entry.launchable)

    @property
    def coming_soon(self) -> tuple[GameEntry, ...]:
        """Only the entries that must never be fetched or run."""
        return tuple(entry for entry in self.games if not entry.launchable)


def parse_manifest(data: Any) -> Manifest:
    """Validate an already-decoded manifest document.

    Raises:
        ManifestSchemaError: Wrong shape or unsupported version.
        DuplicateGameIdError: Two entries share an id.
        UnsupportedRuntimeError: Unknown runtime value.
        InvalidRepositoryUrlError: Malformed or non-https clone URL.
        UnsafeEntrypointError: Absolute or escaping entrypoint / unsafe id.
    """
    if not isinstance(data, Mapping):
        raise ManifestSchemaError("manifest root must be a JSON object")

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ManifestSchemaError("manifest 'version' must be an integer")
    if version != SUPPORTED_MANIFEST_VERSION:
        raise ManifestSchemaError(
            f"manifest version {version} is not supported "
            f"(this launcher understands version {SUPPORTED_MANIFEST_VERSION})"
        )

    raw_games = data.get("games")
    if not isinstance(raw_games, Sequence) or isinstance(raw_games, (str, bytes)):
        raise ManifestSchemaError("manifest 'games' must be a list")
    if not raw_games:
        raise ManifestSchemaError("manifest 'games' must contain at least one entry")

    entries: list[GameEntry] = []
    seen: dict[str, int] = {}
    for index, raw_entry in enumerate(raw_games):
        entry = GameEntry.parse(raw_entry, index=index)
        if entry.id in seen:
            raise DuplicateGameIdError(
                f"duplicate game id '{entry.id}' at games[{index}]; already used "
                f"at games[{seen[entry.id]}]"
            )
        seen[entry.id] = index
        entries.append(entry)

    return Manifest(version=version, games=tuple(entries))


def load_manifest(path: Path | None = None) -> Manifest:
    """Read and validate the manifest at *path* (defaults to ``data/games.json``).

    Raises:
        ManifestFileError: The file is missing, unreadable, or not valid JSON.
        ManifestError: Any validation failure (see :func:`parse_manifest`).
    """
    path = MANIFEST_FILE if path is None else Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestFileError(f"manifest not found: {path}") from None
    except OSError as exc:
        raise ManifestFileError(f"could not read manifest {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestFileError(
            f"manifest {path} is not valid JSON (line {exc.lineno}, "
            f"column {exc.colno}): {exc.msg}"
        ) from exc

    return parse_manifest(data)
