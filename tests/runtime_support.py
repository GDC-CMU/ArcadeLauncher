"""Offline runtime/preparation fixtures: fake ZIPs, engines, pip and imports."""

from __future__ import annotations

import hashlib
import io
import os
import struct
import threading
import zipfile
from pathlib import Path
from unittest import mock

from support import entry

from launcher.manifest import GodotVersion, Runtime
from launcher.processes import CommandResult
from launcher.runtimes import PINNED_RUNTIMES, RuntimePlatform, RuntimeSpec, RuntimeStore


def zip_bytes(spec: RuntimeSpec, *, extra: list[tuple[object, bytes]] | None = None) -> bytes:
    """A small ZIP with the exact expected engine name, never a real engine."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(spec.executable_name, b"fake executable " + spec.version.value.encode())
        for name, content in extra or []:
            if isinstance(name, str):
                # Preserve malicious raw names rather than letting Windows'
                # ZipInfo constructor normalize backslashes in the fixture.
                info = zipfile.ZipInfo("fixture-entry")
                info.filename = name
                info.orig_filename = name
                name = info
            archive.writestr(name, content)
    return output.getvalue()


class FixturePreparationRunner:
    """Materializes isolated environment/import outputs without spawning them."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.cancelled = threading.Event()
        self.fail_install = False
        self.fail_import = False
        self.fail_probe = False

    def run(self, command, *, cwd, env=None, timeout_s=180):
        args = list(command)
        self.calls.append((args, Path(cwd), dict(env or {})))
        if "--version" in args:
            for version in GodotVersion:
                if version.value in args[0]:
                    return CommandResult(0, version.value.replace("-stable", ".stable.official.fixture"))
            raise AssertionError("version fixture did not recognize its engine")
        if "venv" in args:
            root = Path(args[-1])
            binary = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"isolated fixture python")
            binary.chmod(0o755)
            (root / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="utf-8")
        elif "install" in args:
            if self.fail_install:
                return CommandResult(1, "fixture dependency installation failed")
            root = Path(args[0]).parent.parent
            package = root / "fixture-site-packages" / "fixture_dep"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        elif "--import" in args:
            if self.fail_import:
                return CommandResult(0, "SCRIPT ERROR: fixture import failed despite exit zero")
            project = Path(args[args.index("--path") + 1])
            imported = project / ".godot" / "imported"
            imported.mkdir(parents=True, exist_ok=True)
            (imported / "fixture.ctex").write_bytes(b"prepared import")
        elif "--headless" in args and self.fail_probe:
            return CommandResult(0, "ERROR: fixture pack startup failed")
        return CommandResult(0, "fixture preparation ok")

    def cancel(self):
        self.cancelled.set()

    def reset_cancel(self):
        self.cancelled.clear()


def runtime_fixture(case, root: Path, *, target=None, runner=None):
    """Patch pins to known in-memory archives for this test's lifetime only."""
    target = target or RuntimePlatform.current()
    runner = runner or FixturePreparationRunner()
    specs = {}
    archives = {}
    downloads = []
    for version in GodotVersion:
        original = PINNED_RUNTIMES[(version, target)]
        archive = zip_bytes(original)
        spec = RuntimeSpec(version, target, hashlib.sha512(archive).hexdigest())
        specs[(version, target)] = spec
        archives[spec.url] = archive
    patcher = mock.patch.dict(PINNED_RUNTIMES, specs)
    patcher.start()
    case.addCleanup(patcher.stop)

    def download(url, destination):
        downloads.append(url)
        destination.write_bytes(archives[url])

    store = RuntimeStore(root, target=target, runner=runner, downloader=download)
    return store, runner, downloads


def godot_entry(**changes):
    return entry(**{
        "runtime": Runtime.GODOT, "godot_version": GodotVersion.V4_4_1,
        "entrypoint": "Flappy Scotty.pck", **changes,
    })


def write_pack(path: Path, version: GodotVersion = GodotVersion.V4_4_1) -> None:
    """Header-valid fixture; its actual start is handled by the fake runner."""
    major, minor, patch = map(int, version.value.split("-")[0].split("."))
    path.write_bytes(struct.pack("<4s4I", b"GDPC", 2, major, minor, patch) + bytes(100))


def managed_source(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    return root
