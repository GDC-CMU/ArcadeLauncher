"""Strict native metadata and official-checksum/extraction/offline guarantees."""

from __future__ import annotations

import hashlib
from http.client import IncompleteRead
import os
import stat
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from support import LAUNCHABLE_RAW, TempDirCase, build_manifest
from runtime_support import runtime_fixture, zip_bytes

from launcher.errors import ManifestError, RuntimeProvisionError
from launcher.manifest import GodotVersion, Runtime
from launcher.runtimes import PINNED_RUNTIMES, RuntimePlatform, RuntimeSpec, RuntimeStore


class NativeSchemaTests(unittest.TestCase):
    def godot(self, **changes):
        raw = dict(
            LAUNCHABLE_RAW, runtime="godot", godot_version="4.4.1-stable",
            entrypoint="Flappy Scotty.pck",
        )
        raw.update(changes)
        return build_manifest(raw)[0]

    def test_godot_is_typed_and_space_containing_pack_is_valid(self):
        game = self.godot(startup_script="arcade/bootstrap.gd")
        self.assertIs(game.runtime, Runtime.GODOT)
        self.assertIs(game.godot_version, GodotVersion.V4_4_1)
        self.assertEqual(game.entrypoint, "Flappy Scotty.pck")

    def test_source_project_accepts_the_other_exact_pin(self):
        game = self.godot(entrypoint="source/project.godot", godot_version="4.5.2-stable")
        self.assertIs(game.godot_version, GodotVersion.V4_5_2)

    def test_floating_wrong_type_and_unapproved_versions_fail(self):
        for value in (None, True, 4.4, "", "latest", "4.4.1", "4.5-stable", "4.6-stable", "4.4.1-stable "):
            with self.subTest(value=value), self.assertRaises(ManifestError):
                self.godot(godot_version=value)

    def test_godot_rejects_shell_wrappers_and_python_requirements(self):
        for changes in (
            {"entrypoint": "run.cmd"}, {"entrypoint": "game.exe"},
            {"entrypoint": "game.py"}, {"python_requirements": "requirements.txt"},
            {"command": "godot game.pck"}, {"godot_versions": "4.4.1-stable"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ManifestError):
                self.godot(**changes)

    def test_startup_script_is_only_a_contained_pack_gd(self):
        for script in ("../bootstrap.gd", "/tmp/bootstrap.gd", "C:/bootstrap.gd",
                       "arcade\\bootstrap.gd", "bootstrap.gd:stream", "CON.gd",
                       "bootstrap.py", "", None, ["bootstrap.gd"], "bad\0.gd"):
            with self.subTest(script=script), self.assertRaises(ManifestError):
                self.godot(startup_script=script)
        with self.assertRaises(ManifestError):
            self.godot(entrypoint="project.godot", startup_script="bootstrap.gd")

    def test_python_metadata_is_mutually_exclusive(self):
        for key, value in (("godot_version", "4.4.1-stable"), ("startup_script", "bootstrap.gd")):
            with self.subTest(key=key), self.assertRaises(ManifestError):
                build_manifest(dict(LAUNCHABLE_RAW, **{key: value}))

    def test_python_requirements_is_a_contained_typed_path(self):
        game = build_manifest(dict(LAUNCHABLE_RAW, python_requirements="deps/arcade requirements.txt"))[0]
        self.assertEqual(game.python_requirements, "deps/arcade requirements.txt")
        for value in ("../requirements.txt", None, True, ["requirements.txt"]):
            with self.subTest(value=value), self.assertRaises(ManifestError):
                build_manifest(dict(LAUNCHABLE_RAW, python_requirements=value))

    def test_credentials_are_not_permitted_in_repository_urls(self):
        with self.assertRaises(ManifestError):
            build_manifest(dict(LAUNCHABLE_RAW, repository="https://secret@example.invalid/game.git"))


class RuntimeTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store, self.runner, self.downloads = runtime_fixture(self, self.tmp_path / "runtimes")
        self.version = GodotVersion.V4_4_1

    def test_only_the_requested_standard_platform_archive_is_downloaded(self):
        binary = self.store.ensure(self.version)
        spec = self.store.spec(self.version)
        self.assertEqual(self.downloads, [spec.url])
        self.assertEqual(binary.name, spec.executable_name)
        self.assertNotIn("mono", spec.url.lower())
        self.assertNotIn("console", binary.name)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.runner.calls[0][0], [str(self.runner.calls[0][1] / spec.executable_name), "--version"])

    def test_every_official_pin_is_sha512_and_uses_the_exact_release_url(self):
        self.assertEqual(len(PINNED_RUNTIMES), 4)
        for spec in PINNED_RUNTIMES.values():
            self.assertEqual(len(spec.sha512), 128)
            self.assertEqual(int(spec.sha512, 16) >= 0, True)
            self.assertEqual(
                spec.url,
                f"https://github.com/godotengine/godot/releases/download/{spec.version.value}/{spec.archive_name}",
            )

    def test_verify_and_offline_reuse_have_no_network_or_processes(self):
        binary = self.store.ensure(self.version)
        calls = len(self.runner.calls)
        with mock.patch.object(self.store, "_downloader", side_effect=AssertionError("network on reuse")):
            self.assertEqual(self.store.verify(self.version), binary)
            self.assertEqual(self.store.ensure(self.version, allow_download=False), binary)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.downloads), 1)

    def test_a_fresh_store_independently_checks_the_installed_archive_and_binary(self):
        binary = self.store.ensure(self.version)
        fresh = RuntimeStore(self.store.root, target=self.store.spec(self.version).platform)
        with mock.patch.object(fresh.runner, "run", side_effect=AssertionError("probe on Play")):
            self.assertEqual(fresh.verify(self.version), binary)
        binary.write_bytes(b"tampered binary")
        with self.assertRaisesRegex(RuntimeProvisionError, "checksum"):
            fresh.verify(self.version)

    def test_missing_engine_does_not_trigger_download_or_fallback(self):
        with self.assertRaises(RuntimeProvisionError):
            self.store.verify(self.version)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.runner.calls, [])

    def test_corrupt_or_deleted_binary_fails_disk_only_verification(self):
        binary = self.store.ensure(self.version)
        binary.write_bytes(b"wrong")
        with self.assertRaises(RuntimeProvisionError):
            self.store.verify(self.version)
        binary.unlink()
        with self.assertRaises(RuntimeProvisionError):
            self.store.verify(self.version)
        self.assertEqual(len(self.downloads), 1)

    def test_offline_preparation_repairs_from_the_verified_archive(self):
        binary = self.store.ensure(self.version)
        original = binary.read_bytes()
        binary.write_bytes(b"damaged")
        repaired = self.store.ensure(self.version, allow_download=False)
        self.assertEqual(repaired.read_bytes(), original)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.runner.calls), 2)

    def test_corrupt_archive_is_not_accepted_just_because_the_binary_exists(self):
        self.store.ensure(self.version)
        archive = self.store.directory(self.version) / self.store.spec(self.version).archive_name
        archive.write_bytes(b"not an official archive")
        with self.assertRaisesRegex(RuntimeProvisionError, "SHA512"):
            self.store.verify(self.version)
        with self.assertRaises(RuntimeProvisionError):
            self.store.ensure(self.version, allow_download=False)

    def test_bad_download_is_rejected_before_any_extraction_or_probe(self):
        self.store._downloader = lambda url, destination: destination.write_bytes(b"untrusted ZIP")
        with mock.patch("launcher.runtimes.zipfile.ZipFile") as extractor:
            with self.assertRaisesRegex(RuntimeProvisionError, "SHA512"):
                self.store.ensure(self.version)
        extractor.assert_not_called()
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.store.directory(self.version).exists())

    def test_failed_new_version_does_not_touch_the_old_one(self):
        good = self.store.ensure(self.version)
        before = good.read_bytes()
        self.store._downloader = lambda url, destination: destination.write_bytes(b"bad candidate")
        with self.assertRaises(RuntimeProvisionError):
            self.store.ensure(GodotVersion.V4_5_2)
        self.assertEqual(self.store.verify(self.version), good)
        self.assertEqual(good.read_bytes(), before)

    def test_interrupted_http_body_is_a_typed_failure_with_no_published_runtime(self):
        with mock.patch.object(self.store, "_downloader", side_effect=IncompleteRead(b"partial")):
            with self.assertRaises(RuntimeProvisionError):
                self.store.ensure(self.version)
        self.assertFalse(self.store.directory(self.version).exists())

    def test_unsupported_platform_is_an_explicit_error(self):
        with mock.patch("launcher.runtimes.platform.machine", return_value="aarch64"):
            with self.assertRaisesRegex(RuntimeProvisionError, "x86_64"):
                RuntimePlatform.current()

    def test_linux_binary_is_executable(self):
        store, _, _ = runtime_fixture(self, self.tmp_path / "linux", target=RuntimePlatform.LINUX_X86_64)
        binary = store.ensure(self.version)
        self.assertTrue(os.access(binary, os.X_OK))


class ArchiveContainmentTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store, _, _ = runtime_fixture(self, self.tmp_path / "runtimes")
        self.version = GodotVersion.V4_4_1

    def install_fixture(self, extra):
        spec = self.store.spec(self.version)
        blob = zip_bytes(spec, extra=extra)
        patched = RuntimeSpec(spec.version, spec.platform, hashlib.sha512(blob).hexdigest())
        with mock.patch.dict(PINNED_RUNTIMES, {(spec.version, spec.platform): patched}):
            self.store._downloader = lambda url, destination: destination.write_bytes(blob)
            return self.store.ensure(self.version)

    def test_all_zip_members_are_validated_even_if_not_extracted(self):
        for member in ("../escape", "/escape", "C:/escape", "a\\escape", "a/../escape",
                       "./escape", "a//escape", "CON", "file:stream"):
            with self.subTest(member=member), self.assertRaises(RuntimeProvisionError):
                self.install_fixture([(member, b"unsafe")])
        self.assertFalse((self.tmp_path / "escape").exists())

    def test_zip_symlinks_are_rejected(self):
        link = zipfile.ZipInfo("linked")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(RuntimeProvisionError):
            self.install_fixture([(link, b"../../outside")])

    def test_zip_case_aliases_are_rejected(self):
        with self.assertRaisesRegex(RuntimeProvisionError, "duplicate"):
            self.install_fixture([("extra", b"one"), ("EXTRA", b"two")])

    def test_extraction_has_a_total_size_limit(self):
        with mock.patch("launcher.runtimes._MAX_EXTRACTED", 1):
            with self.assertRaisesRegex(RuntimeProvisionError, "limits"):
                self.install_fixture([])

    def test_console_wrapper_is_not_extracted(self):
        binary = self.install_fixture([("Godot_console.exe", b"wrapper")])
        self.assertFalse((binary.parent / "Godot_console.exe").exists())
