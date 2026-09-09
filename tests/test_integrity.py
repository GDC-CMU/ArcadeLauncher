"""Disk integrity/path checks, including native Windows import path lengths."""

from __future__ import annotations

import os
from pathlib import Path

from support import TempDirCase

from launcher.errors import PreparationError
from launcher.integrity import FileVerifier, filesystem_path, remove_owned_tree
from launcher.preparation import PreparationService
from launcher.runtimes import RuntimeStore


class IntegrityTests(TempDirCase):
    def test_content_change_is_detected_even_when_size_and_mtime_are_restored(self):
        path = self.tmp_path / "binary"
        path.write_bytes(b"original")
        files = FileVerifier()
        old = files.digest(path)
        before = path.stat()
        path.write_bytes(b"modified")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(files.digest(path), old)

    def test_native_import_inventory_supports_paths_longer_than_max_path(self):
        root = self.tmp_path / ("project-" + "x" * 100) / ("imports-" + "y" * 100)
        path = root / ("texture-" + "z" * 70 + ".ctex")
        filesystem_path(root).mkdir(parents=True)
        self.addCleanup(remove_owned_tree, self.tmp_path / ("project-" + "x" * 100))
        filesystem_path(path).write_bytes(b"long imported artifact")
        self.assertGreater(len(str(path)), 260)
        files = FileVerifier()
        inventory = files.inventory(root)
        self.assertEqual(set(inventory), {path.name})
        files.verify_inventory(root, inventory)

    def test_persistent_data_cannot_be_inside_a_runtime_cache(self):
        runtime_root = self.tmp_path / "runtimes"
        with self.assertRaisesRegex(PreparationError, "overlap"):
            PreparationService(
                self.tmp_path / "prepared", runtimes=RuntimeStore(runtime_root),
                data_root=runtime_root / "saves",
            )
