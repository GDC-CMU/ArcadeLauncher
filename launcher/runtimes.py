"""Pinned, checksummed standard Godot engines in a user-owned native cache.

Only the current platform's standard archive is downloaded. The console
wrapper in the Windows ZIP is deliberately not extracted or launched.
``ensure`` is preparation-only; ``verify`` is disk-only and never runs Godot.
Retaining the verified ZIP permits offline repair during preparation and lets a
new process independently verify the executable against the official archive,
without trusting a mutable, self-declared executable hash.
"""

from __future__ import annotations

import hashlib
from http.client import HTTPException
import logging
import os
import platform
import shutil
import stat
import struct
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .errors import ManifestError, PreparationError, RuntimeProvisionError
from .integrity import FileVerifier, file_digest
from .manifest import GodotVersion, safe_relative_path
from .paths import default_runtime_root
from .processes import PreparationRunner

_log = logging.getLogger(__name__)
_MAX_ARCHIVE = 256 * 1024 * 1024
_MAX_EXTRACTED = 1024 * 1024 * 1024


class RuntimePlatform(Enum):
    LINUX_X86_64 = "linux-x86_64"
    WINDOWS_X86_64 = "windows-x86_64"

    @classmethod
    def current(cls) -> "RuntimePlatform":
        machine = platform.machine().lower()
        if machine not in {"amd64", "x86_64"} or struct.calcsize("P") != 8:
            raise RuntimeProvisionError(
                f"Godot requires Linux/Windows x86_64; this process is {machine}, "
                f"{struct.calcsize('P') * 8}-bit"
            )
        if sys.platform == "win32":
            return cls.WINDOWS_X86_64
        if sys.platform.startswith("linux"):
            return cls.LINUX_X86_64
        raise RuntimeProvisionError(f"no pinned Godot runtime for {sys.platform}")


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    version: GodotVersion
    platform: RuntimePlatform
    sha512: str

    @property
    def archive_name(self) -> str:
        suffix = "linux.x86_64.zip" if self.platform is RuntimePlatform.LINUX_X86_64 else "win64.exe.zip"
        return f"Godot_v{self.version.value}_{suffix}"

    @property
    def executable_name(self) -> str:
        return self.archive_name.removesuffix(".zip")

    @property
    def url(self) -> str:
        return (
            "https://github.com/godotengine/godot/releases/download/"
            f"{self.version.value}/{self.archive_name}"
        )


PINNED_RUNTIMES = {
    (version, target): RuntimeSpec(version, target, checksum)
    for version, target, checksum in (
        (GodotVersion.V4_4_1, RuntimePlatform.LINUX_X86_64,
         "ef4e76880a514257175544952c61191106fdef3095b909bafed9fcbeb230c3e5533920a0f3012882dd4bbde83028a67549825794e2d2c3cf76eba7918b71370e"),
        (GodotVersion.V4_4_1, RuntimePlatform.WINDOWS_X86_64,
         "266978b803f7532edc69bdd5d8c4fdced0ea97aef1224a8879616398a2559e135520e186add06b532aa92bdfeecf6ac024634024de4b3abd69f6c34e6d6d0563"),
        (GodotVersion.V4_5_2, RuntimePlatform.LINUX_X86_64,
         "e3ce6194b6d4d2dcef5e5b5136752c084d9d8b9071c10bc2d2011b947ec7439a257c8d23c541cb30699f16d9edea6dff36e6e84d2bec14a8fe9b4e9aacbe5a8d"),
        (GodotVersion.V4_5_2, RuntimePlatform.WINDOWS_X86_64,
         "b21115cf3620438e17959d13946bf9c7dceabb6ff71ed32a68996a21e08615edd2d1a3d25641bb3e260d084b31f500231aa59fcf026a5228bcc20ac4e8bade60"),
    )
}


class RuntimeStore:
    """Native engine provisioning with injectable offline fixtures.

    ``ensure(version, *, allow_download=True) -> Path`` prepares/probes an
    engine. ``verify(version) -> Path`` only verifies an already installed one.
    A string version must be an exact GodotVersion value, never a floating tag.
    """

    def __init__(
        self, root: Path | None = None, *, target: RuntimePlatform | None = None,
        downloader: Callable[[str, Path], None] | None = None,
        runner: PreparationRunner | None = None,
    ) -> None:
        self.root = (root if root is not None else default_runtime_root()).resolve()
        self._target = target
        self._downloader = downloader or self._download
        self.runner = runner or PreparationRunner()
        self._files = FileVerifier()
        self._archive_binary_hashes: dict[str, str] = {}
        self._lock = threading.RLock()

    def spec(self, version: GodotVersion | str) -> RuntimeSpec:
        if not isinstance(version, GodotVersion):
            try:
                version = GodotVersion.parse(version, game_id="runtime")
            except ManifestError as exc:
                raise RuntimeProvisionError(str(exc)) from exc
        return PINNED_RUNTIMES[(version, self._target or RuntimePlatform.current())]

    def directory(self, version: GodotVersion | str) -> Path:
        spec = self.spec(version)
        return safe_relative_path(
            f"godot/{spec.version.value}/{spec.platform.value}", self.root,
            game_id="runtime", field_name="runtime directory",
        )

    def verify(self, version: GodotVersion | str) -> Path:
        """Fail closed for missing, changed, linked, or corrupt native runtimes."""
        with self._lock:
            spec = self.spec(version)
            return self._verify_directory(spec, self.directory(version))

    def _verify_directory(self, spec: RuntimeSpec, directory: Path) -> Path:
        try:
            archive = directory / spec.archive_name
            binary = directory / spec.executable_name
            if self._files.digest(archive, "sha512") != spec.sha512:
                raise RuntimeProvisionError(f"Godot {spec.version.value}: archive SHA512 mismatch")
            expected = self._archive_binary_hashes.get(spec.sha512)
            if expected is None:
                with zipfile.ZipFile(archive) as package:
                    self._validate_zip(package, directory, spec)
                    with package.open(spec.executable_name) as stream:
                        digest = hashlib.sha512()
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    expected = digest.hexdigest()
                self._archive_binary_hashes[spec.sha512] = expected
            if self._files.digest(binary, "sha512") != expected:
                raise RuntimeProvisionError(f"Godot {spec.version.value}: executable checksum mismatch")
            if spec.platform is RuntimePlatform.LINUX_X86_64 and not os.access(binary, os.X_OK):
                raise RuntimeProvisionError(f"Godot binary is not executable: {binary}")
            return binary
        except (OSError, zipfile.BadZipFile, zlib.error, PreparationError, ManifestError) as exc:
            raise RuntimeProvisionError(
                f"Godot {spec.version.value} is missing or corrupt; prepare it before play: {exc}"
            ) from exc

    def ensure(self, version: GodotVersion | str, *, allow_download: bool = True) -> Path:
        with self._lock:
            spec = self.spec(version)
            destination = self.directory(version)
            try:
                return self._verify_directory(spec, destination)
            except RuntimeProvisionError as exc:
                _log.info("runtime preparation needed: %s", exc)

            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=destination.parent))
            try:
                archive = temporary / spec.archive_name
                cached_archive = destination / spec.archive_name
                if cached_archive.is_file() and self._files.digest(cached_archive, "sha512") == spec.sha512:
                    shutil.copyfile(cached_archive, archive)
                elif allow_download:
                    _log.info("downloading official Godot %s (%s)", spec.version.value, spec.platform.value)
                    self._downloader(spec.url, archive)
                else:
                    raise RuntimeProvisionError(
                        f"Godot {spec.version.value} is not prepared and no valid offline archive exists"
                    )
                if file_digest(archive, "sha512") != spec.sha512:
                    raise RuntimeProvisionError(
                        f"Godot {spec.version.value}: downloaded archive failed SHA512 verification"
                    )
                with zipfile.ZipFile(archive) as package:
                    self._validate_zip(package, temporary, spec)
                    # Do not extract arbitrary archive members or the console wrapper.
                    binary = temporary / spec.executable_name
                    with package.open(spec.executable_name) as source, binary.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                if spec.platform is RuntimePlatform.LINUX_X86_64:
                    binary.chmod(0o755)
                self._verify_directory(spec, temporary)
                self._probe(spec, binary)

                backup: Path | None = None
                if destination.exists():
                    backup = destination.with_name(f".previous-{spec.platform.value}-{uuid.uuid4().hex}")
                    destination.replace(backup)
                try:
                    temporary.replace(destination)
                except OSError:
                    if backup is not None:
                        backup.replace(destination)
                    raise
                return self._verify_directory(spec, destination)
            except (OSError, URLError, HTTPException, zipfile.BadZipFile, zlib.error, PreparationError, ManifestError) as exc:
                raise RuntimeProvisionError(f"could not prepare Godot {spec.version.value}: {exc}") from exc
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    def _probe(self, spec: RuntimeSpec, binary: Path) -> None:
        result = self.runner.run([str(binary), "--version"], cwd=binary.parent, timeout_s=20)
        expected = spec.version.value.replace("-stable", ".stable.")
        if result.returncode != 0 or result.diagnostic_error or not result.output.strip().startswith(expected):
            raise RuntimeProvisionError(
                f"Godot {spec.version.value} version probe failed: "
                f"exit {result.returncode}: {result.output.strip()[:240]}"
            )
        _log.info("Godot verified: %s (%s)", binary, result.output.strip())

    @staticmethod
    def _validate_zip(package: zipfile.ZipFile, root: Path, spec: RuntimeSpec) -> None:
        total = 0
        seen: set[str] = set()
        for item in package.infolist():
            if item.orig_filename != item.filename:
                raise RuntimeProvisionError(f"ambiguous/normalized ZIP member: {item.orig_filename!r}")
            name = item.filename.rstrip("/") if item.is_dir() else item.filename
            # Validate even members we don't extract (defence against unsafe fixtures,
            # future archive layout changes, symlinks, duplicate/case-alias names).
            safe_relative_path(name, root, game_id="runtime", field_name="ZIP member")
            if any(part in {".", ".."} for part in name.split("/")) or "//" in name:
                raise RuntimeProvisionError(f"unsafe ZIP member: {item.filename}")
            key = name.casefold()
            if key in seen:
                raise RuntimeProvisionError(f"duplicate ZIP member: {name}")
            seen.add(key)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or item.flag_bits & 1:
                raise RuntimeProvisionError(f"linked, special, or encrypted ZIP member: {name}")
            total += item.file_size
            if total > _MAX_EXTRACTED or len(seen) > 64:
                raise RuntimeProvisionError("Godot archive exceeds extraction limits")
        if spec.executable_name not in package.namelist():
            raise RuntimeProvisionError(f"Godot archive has no {spec.executable_name}")
        item = package.getinfo(spec.executable_name)
        if item.is_dir() or item.file_size == 0 or PurePosixPath(item.filename).name != item.filename:
            raise RuntimeProvisionError("Godot executable is not a nonempty top-level file")

    def _download(self, url: str, destination: Path) -> None:
        deadline = time.monotonic() + 300
        request = Request(url, headers={"User-Agent": "ArcadeLauncher-runtime/1"})
        with urlopen(request, timeout=20) as response, destination.open("xb") as output:
            if response.status != 200 or not response.url.startswith("https://"):
                raise RuntimeProvisionError("runtime download did not return an HTTPS success response")
            total = 0
            while True:
                if self.runner.cancelled.is_set():
                    raise RuntimeProvisionError("runtime download cancelled")
                if time.monotonic() > deadline:
                    raise RuntimeProvisionError("runtime download exceeded its five-minute deadline")
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > _MAX_ARCHIVE:
                    raise RuntimeProvisionError("runtime download exceeds size limit")
                output.write(block)

    def cancel(self) -> None:
        self.runner.cancel()

    def reset_cancel(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise RuntimeProvisionError("previous runtime installation is still stopping")
        try:
            self.runner.reset_cancel()
        finally:
            self._lock.release()
