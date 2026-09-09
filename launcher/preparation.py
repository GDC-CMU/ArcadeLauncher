"""Preparation receipts, immutable native artifacts, and isolated Python deps.

``prepare`` is a startup/operator operation. ``resolve`` is the disk-only Play
boundary. A managed checkout's receipt lives inside its independent ``.git``
directory, so candidate promotion carries source and launch metadata together.
It stores the *last successfully prepared* launch contract, not a promise to
prepare the newest manifest when a visitor presses Play.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import struct
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .commands import build_child_command
from .errors import LaunchError, ManifestError, NotLaunchableError, PreparationError
from .integrity import FileVerifier, atomic_json, filesystem_path, is_link, read_object, remove_owned_tree
from .manifest import GameEntry, GodotVersion, Runtime, safe_relative_path
from .paths import default_game_data_root
from .processes import PreparationRunner
from .runtimes import RuntimeStore

_log = logging.getLogger(__name__)
_FORMAT = 1
_SOURCE_EXCLUDES = frozenset({".git", ".godot", "__pycache__", ".venv", "venv", "retropie-venv"})
_PREPARED_EXCLUDES = frozenset({"__pycache__", "shader_cache", "editor"})
_KEY = re.compile(r"^[a-f0-9]{64}$")


def launch_metadata(entry: GameEntry) -> dict[str, str]:
    """Portable launch contract saved alongside the prepared release."""
    if not isinstance(entry.runtime, Runtime):
        raise PreparationError(f"game '{entry.id}': runtime must be a Runtime enum")
    if entry.godot_version is not None and not isinstance(entry.godot_version, GodotVersion):
        raise PreparationError(f"game '{entry.id}': godot_version must be a GodotVersion enum")
    fields = {
        "runtime": entry.runtime.value, "entrypoint": entry.entrypoint,
        "godot_version": entry.godot_version.value if entry.godot_version else None,
        "startup_script": entry.startup_script,
        "python_requirements": entry.python_requirements,
    }
    return {key: value for key, value in fields.items() if value is not None}


def _entry_with_metadata(entry: GameEntry, metadata: object) -> GameEntry:
    if not isinstance(metadata, dict) or set(metadata) - {
        "runtime", "entrypoint", "godot_version", "startup_script", "python_requirements",
    }:
        raise PreparationError(f"game '{entry.id}': invalid saved launch metadata")
    # Reuse the public strict parser, but do not reinterpret a fixture/local
    # clone URL or the current card's display fields as saved runtime metadata.
    parsed = GameEntry.parse({
        "id": entry.id, "title": entry.title, "description": entry.description,
        "launchable": True, "repository": "https://prepared.invalid/game.git", "ref": "prepared",
        "art": {"motif": entry.art.motif, "palette": entry.art.palette, "seed": entry.art.seed},
        **metadata,
    }, index=0)
    return replace(
        entry, runtime=parsed.runtime, entrypoint=parsed.entrypoint,
        godot_version=parsed.godot_version, startup_script=parsed.startup_script,
        python_requirements=parsed.python_requirements,
    )


def _fingerprint(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedGame:
    """The exact ready release to launch, which may predate the current manifest."""

    entry: GameEntry
    checkout: Path
    executable: Path
    data_dir: Path
    environment: Path | None = None

    def child_environment(self) -> dict[str, str]:
        """Build an environment without mutating the launcher's interpreter."""
        env = dict(os.environ)
        env["ARCADE_MODE"] = "1"
        env["ARCADE_GAME_DATA_DIR"] = str(self.data_dir)
        if self.entry.runtime is Runtime.GODOT:
            # Compiled packs cannot rewrite FileAccess paths such as
            # user://high_score.save. Godot derives user:// from this OS data
            # root; redirect only its child, never the launcher or Python games.
            env["APPDATA" if os.name == "nt" else "XDG_DATA_HOME"] = str(self.data_dir)
        if self.environment is not None:
            for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSAFEPATH"):
                env.pop(key, None)
            env["PYTHONNOUSERSITE"] = "1"
            env["VIRTUAL_ENV"] = str(self.environment)
            env["PATH"] = str(self.executable.parent) + os.pathsep + env.get("PATH", "")
        return env


class PreparationService:
    """Prepare releases under ``root``; keep persistent saves under ``data_root``.

    Public API:
      prepare(entry, checkout, *, allow_download=True) -> PreparedGame
      resolve(entry, checkout) -> PreparedGame  # disk-only, may be last-good
      matches(entry, checkout, *, include_source=False) -> bool
      cancel()                                # bounded shutdown of preparation

    A checkout to prepare must be a managed independent clone (``.git`` directory,
    not a linked worktree). Dirty/untracked candidate files are supported:
    Godot snapshots use their content, not Git HEAD. Use a separate staged
    checkout; preparation only writes its receipt in .git, not source files.
    Resolving a legacy Python checkout needs no receipt.
    """

    def __init__(
        self, root: Path, *, runtimes: RuntimeStore | None = None,
        runner: PreparationRunner | None = None, data_root: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.runner = runner or PreparationRunner()
        self.runtimes = runtimes or RuntimeStore(runner=self.runner)
        self.data_root = (data_root if data_root is not None else default_game_data_root()).resolve()
        for disposable in (self.root, self.runtimes.root):
            if self.data_root == disposable or disposable in self.data_root.parents or self.data_root in disposable.parents:
                raise PreparationError("persistent game data must not overlap preparation/runtime caches")
        self._files = FileVerifier()

    def _data_dir(self, entry: GameEntry) -> Path:
        if is_link(self.data_root / entry.id):
            raise PreparationError(f"game '{entry.id}': persistent data directory must not be a link")
        return safe_relative_path(entry.id, self.data_root, game_id=entry.id, field_name="game data")

    def _check_checkout_roots(self, checkout: Path) -> None:
        for name, root in (
            ("preparation cache", self.root), ("runtime cache", self.runtimes.root),
            ("persistent game data", self.data_root),
        ):
            if checkout == root or checkout in root.parents or root in checkout.parents:
                raise PreparationError(f"checkout must not overlap {name}: {root}")

    @staticmethod
    def _receipt(checkout: Path) -> Path:
        return safe_relative_path(
            ".git/arcade-launcher-prepared.json", checkout,
            game_id="prepared", field_name="preparation receipt",
        )

    def matches(self, entry: GameEntry, checkout: Path, *, include_source: bool = False) -> bool:
        """Compare requested versus saved metadata; optionally audit candidate bytes.

        ``include_source`` is an operator staging check, not a Play requirement:
        last-good native snapshots remain playable even if candidate files have
        since changed or been removed. Python source runs from its checkout;
        only its dependency inputs need to match a prepared environment.
        """
        checkout = checkout.resolve()
        receipt = self._receipt(checkout)
        if not receipt.is_file():
            return entry.runtime is Runtime.PYTHON and entry.python_requirements is None
        record = read_object(receipt)
        if record.get("launch") != launch_metadata(entry):
            return False
        if include_source:
            try:
                if entry.runtime is Runtime.GODOT:
                    files = self._files.inventory(checkout, exclude_dirs=_SOURCE_EXCLUDES)
                    return record.get("godot_key") == self._godot_key(entry, files)
                if entry.python_requirements is not None:
                    return record.get("python_key") == self._dependency_key(entry, checkout)
            except (OSError, ManifestError, PreparationError):
                return False
        return True

    def resolve(self, entry: GameEntry, checkout: Path) -> PreparedGame:
        """Verify the actual installed release without subprocesses or writes."""
        if not entry.launchable:
            raise NotLaunchableError(f"game '{entry.id}' is disabled")
        checkout = checkout.resolve()
        try:
            self._check_checkout_roots(checkout)
            receipt = self._receipt(checkout)
            if not receipt.is_file():
                if entry.runtime is Runtime.PYTHON and entry.python_requirements is None:
                    build_child_command(entry, checkout)
                    return PreparedGame(entry, checkout, Path(sys.executable), self._data_dir(entry))
                raise PreparationError(f"game '{entry.id}' has not been prepared; restart online or run preparation")
            record = read_object(receipt)
            if record.get("format") != _FORMAT:
                raise PreparationError(f"game '{entry.id}': unsupported preparation receipt")
            effective = _entry_with_metadata(entry, record.get("launch"))
            if effective.runtime is Runtime.PYTHON:
                environment = None
                executable = Path(sys.executable)
                if effective.python_requirements is not None:
                    key = self._key(record.get("python_key"))
                    # Do not run old deps against edited requirements in this checkout.
                    if key != self._dependency_key(effective, checkout):
                        raise PreparationError(f"game '{entry.id}': Python requirements changed; preparation needed")
                    environment, executable = self._resolve_environment(key)
                build_child_command(effective, checkout, executable=executable)
                return PreparedGame(effective, checkout, executable, self._data_dir(entry), environment)
            assert effective.godot_version is not None
            executable = self.runtimes.verify(effective.godot_version)
            snapshot = self._resolve_godot(effective, self._key(record.get("godot_key")))
            build_child_command(effective, snapshot, executable=executable)
            return PreparedGame(effective, snapshot, executable, self._data_dir(entry))
        except (OSError, ManifestError, LaunchError) as exc:
            raise PreparationError(f"game '{entry.id}' is not ready: {exc}") from exc

    def prepare(
        self, entry: GameEntry, checkout: Path, *, allow_download: bool = True,
    ) -> PreparedGame:
        """Publish a new receipt only after all candidate preparation succeeds."""
        if not entry.launchable:
            raise NotLaunchableError(f"game '{entry.id}' is disabled")
        checkout = checkout.resolve()
        try:
            self._check_checkout_roots(checkout)
            effective = _entry_with_metadata(entry, launch_metadata(entry))
            if not (checkout / ".git").is_dir():
                raise PreparationError("preparation requires a managed independent git clone")
            if not filesystem_path(effective.resolved_entrypoint(checkout)).is_file():
                raise PreparationError(f"entrypoint '{effective.entrypoint}' not found in {checkout}")
            record: dict[str, Any] = {"format": _FORMAT, "launch": launch_metadata(effective)}
            if effective.runtime is Runtime.PYTHON:
                environment = None
                executable = Path(sys.executable)
                if effective.python_requirements is not None:
                    record["python_key"] = self._prepare_python(effective, checkout, allow_download)
                    environment, executable = self._resolve_environment(record["python_key"])
                build_child_command(effective, checkout, executable=executable)
                prepared = PreparedGame(effective, checkout, executable, self._data_dir(entry), environment)
            else:
                assert effective.godot_version is not None
                engine = self.runtimes.ensure(effective.godot_version, allow_download=allow_download)
                record["godot_key"] = self._prepare_godot(effective, checkout, engine)
                snapshot = self._resolve_godot(effective, record["godot_key"])
                build_child_command(effective, snapshot, executable=engine)
                prepared = PreparedGame(effective, snapshot, engine, self._data_dir(entry))
            # No mutation of a previous receipt if any operation above failed.
            atomic_json(self._receipt(checkout), record)
            return prepared
        except (OSError, UnicodeError, ManifestError, LaunchError) as exc:
            raise PreparationError(f"could not prepare '{entry.id}': {exc}") from exc

    @staticmethod
    def _key(value: object) -> str:
        if not isinstance(value, str) or not _KEY.fullmatch(value):
            raise PreparationError("invalid prepared artifact/dependency fingerprint")
        return value

    def _abi(self) -> dict[str, str]:
        return {
            "python": sys.version, "platform": sys.platform,
            "base_executable": str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
            "implementation": sys.implementation.name,
        }

    def _requirements(self, entry: GameEntry, checkout: Path) -> dict[str, str]:
        """Validate pip inputs, including recursive -r/-c files, before spawning it.

        Only package specifications, SHA256 hash options, and contained local
        requirement/constraint includes are accepted. Editable/path/URL/VCS
        installs, pip configuration flags and environment substitution are not
        part of the launcher's requirements contract.
        """
        assert entry.python_requirements is not None
        found: dict[str, str] = {}
        visiting: set[str] = set()

        def visit(raw: str) -> None:
            path = safe_relative_path(raw, checkout, game_id=entry.id, field_name="python_requirements")
            relative = path.relative_to(checkout).as_posix()
            if relative in visiting:
                raise PreparationError(f"requirements include cycle: {relative}")
            if relative in found:
                return
            visiting.add(relative)
            text = filesystem_path(path).read_text(encoding="utf-8").replace("\\\n", "")
            found[relative] = self._files.digest(path)
            for original in text.splitlines():
                line = original.split("#", 1)[0].strip()
                if not line:
                    continue
                if line.startswith(("-r ", "-c ", "--requirement ", "--constraint ")):
                    try:
                        parts = shlex.split(line)
                    except ValueError as exc:
                        raise PreparationError(f"invalid requirements include in {relative}: {exc}") from exc
                    if len(parts) != 2:
                        raise PreparationError(f"invalid requirements include in {relative}")
                    visit((PurePosixPath(relative).parent / parts[1]).as_posix())
                    continue
                specification = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
                if (
                    any(token in specification for token in ("@", "/", "\\", "$", "--"))
                    or re.search(r"\s-[A-Za-z]", specification)
                    or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?"
                        r"(?:\s*(?:[<>=!~].*|;.*))?", specification,
                    )
                ):
                    raise PreparationError(
                        f"unsupported requirement in {relative}; use package specs and contained -r/-c includes"
                    )
            visiting.remove(relative)

        visit(entry.python_requirements)
        return found

    def _dependency_key(self, entry: GameEntry, checkout: Path) -> str:
        return _fingerprint({"format": _FORMAT, "abi": self._abi(), "requirements": self._requirements(entry, checkout)})

    def _environment_path(self, key: str) -> Path:
        return safe_relative_path(f"python/{self._key(key)}", self.root, game_id="python", field_name="environment")

    def _resolve_environment(self, key: str) -> tuple[Path, Path]:
        directory = self._environment_path(key)
        record = read_object(directory / "ready.json")
        if record.get("format") != _FORMAT or record.get("key") != key or record.get("abi") != self._abi():
            raise PreparationError("isolated Python environment has an incompatible receipt")
        self._files.verify_inventory(directory, record.get("files"))
        python = safe_relative_path(
            "Scripts/python.exe" if os.name == "nt" else "bin/python",
            directory, game_id="python", field_name="interpreter",
        )
        if not filesystem_path(python).is_file() or (os.name != "nt" and not os.access(python, os.X_OK)):
            raise PreparationError(f"prepared Python interpreter is missing or not executable: {python}")
        return directory, python

    def _prepare_python(self, entry: GameEntry, checkout: Path, allow_download: bool) -> str:
        key = self._dependency_key(entry, checkout)
        try:
            self._resolve_environment(key)
            return key
        except (PreparationError, OSError) as exc:
            _log.info("Python dependency preparation needed for %s: %s", entry.id, exc)
        if not allow_download:
            raise PreparationError(f"game '{entry.id}': dependency environment is not prepared for offline use")
        directory = self._environment_path(key)
        self._preserve_invalid(directory)
        filesystem_path(directory).mkdir(parents=True)
        ready = False
        try:
            env = dict(os.environ)
            for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
                env.pop(name, None)
            env["PYTHONNOUSERSITE"] = "1"
            self._run_checked(
                [sys.executable, "-I", "-m", "venv", "--copies", "--without-pip",
                 str(filesystem_path(directory))], checkout, env,
            )
            python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            # Bootstrap bundled pip explicitly, in the controlled checkout cwd
            # and isolated mode. venv's hidden nested call otherwise changes cwd
            # to the long env path and hides ensurepip's actual diagnostics.
            self._run_checked(
                [str(python), "-I", "-m", "ensurepip", "--default-pip"], checkout, env,
            )
            assert entry.python_requirements is not None
            requirements = safe_relative_path(
                entry.python_requirements, checkout, game_id=entry.id, field_name="python_requirements",
            )
            self._run_checked([
                str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                "--no-input", "install", "--retries", "0", "--timeout", "15", "-r", str(filesystem_path(requirements)),
            ], checkout, env, timeout_s=300)
            self._run_checked(
                [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "check"],
                checkout, env,
            )
            if self._dependency_key(entry, checkout) != key:
                raise PreparationError("Python requirements changed during preparation; retry a stable staged checkout")
            files = self._files.inventory(directory, exclude_dirs=frozenset({"__pycache__", "lib64"}))
            atomic_json(directory / "ready.json", {
                "format": _FORMAT, "key": key, "abi": self._abi(), "files": files,
            })
            self._resolve_environment(key)
            ready = True
            return key
        finally:
            if not ready:
                remove_owned_tree(directory)

    def _godot_path(self, entry: GameEntry, key: str) -> Path:
        return safe_relative_path(
            f"godot/{entry.id}/{self._key(key)}", self.root, game_id=entry.id, field_name="native artifact",
        )

    def _godot_key(self, entry: GameEntry, source_files: dict[str, str]) -> str:
        assert entry.godot_version is not None
        return _fingerprint({
            "format": _FORMAT, "launch": launch_metadata(entry),
            "platform": self.runtimes.spec(entry.godot_version).platform.value,
            "files": source_files,
        })

    def _resolve_godot(self, entry: GameEntry, key: str) -> Path:
        directory = self._godot_path(entry, key)
        record = read_object(directory / "ready.json")
        assert entry.godot_version is not None
        if (
            record.get("format") != _FORMAT or record.get("key") != key
            or record.get("launch") != launch_metadata(entry)
            or record.get("platform") != self.runtimes.spec(entry.godot_version).platform.value
        ):
            raise PreparationError(f"game '{entry.id}': native artifact receipt mismatch")
        snapshot = directory / "project"
        self._files.verify_inventory(snapshot, record.get("files"))
        return snapshot

    @staticmethod
    def _validate_pack(entry: GameEntry, pack: Path) -> None:
        assert entry.godot_version is not None
        with filesystem_path(pack).open("rb") as stream:
            header = stream.read(20)
        if len(header) != 20 or filesystem_path(pack).stat().st_size < 100:
            raise PreparationError(f"truncated Godot pack: {pack.name}")
        magic, pack_format, major, minor, patch = struct.unpack("<4s4I", header)
        expected = tuple(int(part) for part in entry.godot_version.value.split("-")[0].split("."))
        if magic != b"GDPC" or pack_format not in (2, 3) or (major, minor, patch) != expected:
            raise PreparationError(
                f"pack {pack.name} is not a Godot {entry.godot_version.value} PCK "
                f"(header: {major}.{minor}.{patch}, format {pack_format})"
            )

    def _prepare_godot(self, entry: GameEntry, checkout: Path, engine: Path) -> str:
        assert entry.godot_version is not None
        point = entry.resolved_entrypoint(checkout)
        if point.suffix == ".pck":
            self._validate_pack(entry, point)
        source_files = self._files.inventory(checkout, exclude_dirs=_SOURCE_EXCLUDES)
        target = self.runtimes.spec(entry.godot_version).platform.value
        key = self._godot_key(entry, source_files)
        try:
            self._resolve_godot(entry, key)
            return key
        except (PreparationError, OSError) as exc:
            _log.info("native preparation needed for %s: %s", entry.id, exc)
        directory = self._godot_path(entry, key)
        self._preserve_invalid(directory)
        snapshot = directory / "project"
        filesystem_path(snapshot).mkdir(parents=True)
        ready = False
        try:
            for relative in source_files:
                source = safe_relative_path(relative, checkout, game_id=entry.id, field_name="source")
                destination = safe_relative_path(relative, snapshot, game_id=entry.id, field_name="artifact")
                filesystem_path(destination.parent).mkdir(parents=True, exist_ok=True)
                shutil.copy2(filesystem_path(source), filesystem_path(destination))
            # Do not publish different bytes under a fingerprint computed before
            # a concurrent edit/copy. Check before the engine can import them.
            self._files.verify_inventory(snapshot, source_files)
            # Headless imports/probes use disposable userdata, never real saves.
            with tempfile.TemporaryDirectory(prefix=".probe-", dir=self.root) as scratch:
                isolated = Path(scratch)
                env = dict(os.environ)
                env.update({
                    "ARCADE_MODE": "1", "ARCADE_GAME_DATA_DIR": str(isolated / "games"),
                    "XDG_DATA_HOME": str(isolated / "data"), "XDG_CONFIG_HOME": str(isolated / "config"),
                    "XDG_CACHE_HOME": str(isolated / "cache"), "APPDATA": str(isolated / "roaming"),
                    "LOCALAPPDATA": str(isolated / "local"),
                })
                for name in ("games", "data", "config", "cache", "roaming", "local"):
                    (isolated / name).mkdir()
                if point.name == "project.godot":
                    project = entry.resolved_entrypoint(snapshot).parent
                    self._run_checked([
                        str(engine), "--headless", "--editor", "--path", str(project),
                        "--rendering-method", "gl_compatibility",
                        "--log-file", str(isolated / "import.log"), "--import",
                    ], snapshot, env, godot=True)
                command = build_child_command(entry, snapshot, executable=engine)
                command.remove("--fullscreen")
                command[1:1] = ["--headless"]
                command.extend(["--log-file", str(isolated / "probe.log"), "--quit-after", "2"])
                self._run_checked(command, snapshot, env, godot=True, timeout_s=45)
            if self._files.inventory(checkout, exclude_dirs=_SOURCE_EXCLUDES) != source_files:
                raise PreparationError("source files changed during preparation; retry a stable staged checkout")
            files = self._files.inventory(snapshot, exclude_dirs=_PREPARED_EXCLUDES)
            atomic_json(directory / "ready.json", {
                "format": _FORMAT, "key": key, "launch": launch_metadata(entry),
                "platform": target, "files": files,
            })
            self._resolve_godot(entry, key)
            ready = True
            return key
        finally:
            if not ready:
                remove_owned_tree(directory)

    def _run_checked(
        self, command: list[str], cwd: Path, env: dict[str, str], *,
        godot: bool = False, timeout_s: float = 180,
    ) -> None:
        result = self.runner.run(command, cwd=cwd, env=env, timeout_s=timeout_s)
        # Godot can print script/import errors yet exit 0. Do not call that ready.
        if result.returncode or (godot and (
            result.diagnostic_error or re.search(r"(?m)^\s*(?:SCRIPT )?ERROR:", result.output)
        )):
            detail = result.diagnostic_error or result.output.strip()[-1200:] or command[0]
            raise PreparationError(
                f"preparation exited {result.returncode}: {detail}"
            )

    @staticmethod
    def _preserve_invalid(path: Path) -> None:
        path = filesystem_path(path)
        if path.exists():
            path.replace(path.with_name(f".invalid-{path.name}-{uuid.uuid4().hex}"))

    def cancel(self) -> None:
        self.runner.cancel()
        self.runtimes.cancel()

    def reset_cancel(self) -> None:
        self.runtimes.reset_cancel()
        self.runner.reset_cancel()
