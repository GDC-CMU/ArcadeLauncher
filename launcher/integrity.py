"""Small disk-only integrity primitives shared by preparation and readiness."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

from .errors import PreparationError
from .manifest import safe_relative_path


def filesystem_path(path: Path) -> Path:
    """Use Win32 extended paths for IO, without changing public/argv paths.

    Imported Godot filenames and pip's vendored packages easily exceed MAX_PATH
    below a space-containing Windows checkout. This needs no registry change.
    """
    if os.name != "nt":
        return path
    text = str(path)
    if text.startswith("\\\\?\\"):
        return path
    if not path.is_absolute():
        raise PreparationError(f"filesystem IO requires an absolute path: {path}")
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def is_link(path: Path) -> bool:
    """Include Windows directory junctions, but not ordinary cloud placeholders."""
    try:
        info = filesystem_path(path).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == 0xA0000003


def _windows_change_reader():
    """Read NTFS ChangeTime (Windows st_ctime still means *creation* time).

    This lets memoized hashes detect in-place corruption even if a copy tool
    restores file length/mtime. No permissions, registry, or system settings
    are changed; the handle requests file-attribute read access only.
    """
    import ctypes
    from ctypes import wintypes as w

    class BasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation", ctypes.c_int64), ("access", ctypes.c_int64),
            ("write", ctypes.c_int64), ("change", ctypes.c_int64),
            ("attributes", w.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.GetFileInformationByHandleEx.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL

    def read(path: Path) -> int:
        handle = kernel.CreateFileW(str(path), 0x0080, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = BasicInfo()
            if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            return info.change
        finally:
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
    return read


def remove_owned_tree(path: Path) -> None:
    """Remove only a caller-owned staging tree, including Windows read-only git objects."""
    def readonly_retry(function: Any, filename: str, error: tuple) -> None:
        exception = error[1]
        candidate = Path(filename)
        if os.name != "nt" or not isinstance(exception, PermissionError) or is_link(candidate):
            raise exception
        candidate.chmod(stat.S_IREAD | stat.S_IWRITE)
        function(filename)

    path = filesystem_path(path)
    if is_link(path):
        raise PreparationError(f"refusing to recursively remove a linked staging directory: {path}")
    shutil.rmtree(path, onerror=readonly_retry)


def file_digest(path: Path, algorithm: str = "sha256") -> str:
    """Stream a file rather than holding a pack/engine in memory."""
    digest = hashlib.new(algorithm)
    with filesystem_path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    """Replace a receipt only after its complete contents have reached disk."""
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_object(path: Path) -> dict[str, Any]:
    """Read a required receipt; damaged JSON is a failure, not an empty success."""
    try:
        data = json.loads(filesystem_path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreparationError(f"cannot read preparation receipt {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PreparationError(f"preparation receipt must be an object: {path}")
    return data


class FileVerifier:
    """Content verification memoized only while file identity/stats stay equal.

    Each new launcher process hashes files again. Readiness never executes a
    binary, imports a game, creates files, or talks to the network.
    """

    def __init__(self) -> None:
        self._digests: dict[tuple[Path, str], tuple[tuple[int, ...], str]] = {}
        self._change_time = _windows_change_reader() if os.name == "nt" else None

    def digest(self, path: Path, algorithm: str = "sha256") -> str:
        path = filesystem_path(path)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise PreparationError(f"expected a regular file, not a link/directory: {path}")
        identity = (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode,
            self._change_time(path) if self._change_time else info.st_ctime_ns,
        )
        key = (path, algorithm)
        cached = self._digests.get(key)
        if cached is not None and cached[0] == identity:
            return cached[1]
        result = file_digest(path, algorithm)
        self._digests[key] = (identity, result)
        return result

    def inventory(self, root: Path, *, exclude_dirs: frozenset[str] = frozenset()) -> dict[str, str]:
        """Hash a tree without following directory links or including bytecode."""
        files: dict[str, str] = {}
        walk_root = filesystem_path(root)

        def report_error(error: OSError) -> None:
            raise error

        for directory, dirs, names in os.walk(walk_root, followlinks=False, onerror=report_error):
            parent = Path(directory)
            dirs[:] = sorted(name for name in dirs if name not in exclude_dirs)
            for name in dirs:
                if is_link(parent / name):
                    raise PreparationError(f"linked directory in prepared artifact: {parent / name}")
            for name in sorted(names):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = parent / name
                files[path.relative_to(walk_root).as_posix()] = self.digest(path)
        return files

    def verify_inventory(self, root: Path, inventory: object) -> None:
        if not isinstance(inventory, dict) or not inventory:
            raise PreparationError(f"missing file inventory for {root}")
        for raw, expected in inventory.items():
            if not isinstance(raw, str) or not isinstance(expected, str) or len(expected) != 64:
                raise PreparationError(f"invalid file inventory for {root}")
            path = safe_relative_path(raw, root, game_id="prepared", field_name="artifact")
            if self.digest(path) != expected:
                raise PreparationError(f"prepared file is corrupt or modified: {path}")
