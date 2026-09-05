"""Manifest validation: the launcher's first and most important gate."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from launcher.errors import (
    DuplicateGameIdError,
    InvalidRepositoryUrlError,
    ManifestFileError,
    ManifestSchemaError,
    UnsafeEntrypointError,
    UnsupportedRuntimeError,
)
from launcher.manifest import (
    SUPPORTED_MANIFEST_VERSION,
    Runtime,
    load_manifest,
    parse_manifest,
    safe_relative_path,
)
from launcher.paths import MANIFEST_FILE

from support import (
    COMING_SOON_RAW,
    LAUNCHABLE_RAW,
    TempDirCase,
    build_manifest,
    manifest_document,
)


class ShippedManifestTests(unittest.TestCase):
    """The manifest we actually ship must be valid and curated."""

    def test_shipped_manifest_loads(self) -> None:
        manifest = load_manifest(MANIFEST_FILE)
        self.assertEqual(manifest.version, SUPPORTED_MANIFEST_VERSION)
        self.assertEqual(len(manifest), 8)

    def test_shipped_manifest_has_unique_ids(self) -> None:
        manifest = load_manifest(MANIFEST_FILE)
        ids = [game.id for game in manifest]
        self.assertEqual(len(ids), len(set(ids)))

    def test_only_streetfighter_pacdawg_and_headscotter_are_launchable(self) -> None:
        manifest = load_manifest(MANIFEST_FILE)
        self.assertEqual(
            [game.id for game in manifest.launchable],
            ["streetfighter", "pacdawg", "headscotter"],
        )

    def test_every_game_has_distinct_card_art(self) -> None:
        manifest = load_manifest(MANIFEST_FILE)
        motifs = [game.art.motif for game in manifest]
        self.assertEqual(len(motifs), len(set(motifs)), "card art motifs must differ")

    def test_coming_soon_entries_carry_no_repository(self) -> None:
        """Structural guarantee behind 'disabled games are never fetched'."""
        manifest = load_manifest(MANIFEST_FILE)
        for game in manifest.coming_soon:
            with self.subTest(game=game.id):
                self.assertIsNone(game.repository)
                self.assertIsNone(game.ref)
                self.assertIsNone(game.entrypoint)


class SchemaTests(unittest.TestCase):
    def test_root_must_be_an_object(self) -> None:
        with self.assertRaises(ManifestSchemaError):
            parse_manifest([LAUNCHABLE_RAW])

    def test_unsupported_version_is_rejected(self) -> None:
        with self.assertRaises(ManifestSchemaError):
            parse_manifest({"version": 99, "games": [dict(LAUNCHABLE_RAW)]})

    def test_empty_game_list_is_rejected(self) -> None:
        with self.assertRaises(ManifestSchemaError):
            parse_manifest({"version": 1, "games": []})

    def test_duplicate_ids_are_rejected(self) -> None:
        with self.assertRaises(DuplicateGameIdError):
            parse_manifest(manifest_document(dict(LAUNCHABLE_RAW), dict(LAUNCHABLE_RAW)))

    def test_unknown_runtime_is_rejected(self) -> None:
        raw = dict(LAUNCHABLE_RAW, runtime="brainfuck")
        with self.assertRaises(UnsupportedRuntimeError):
            parse_manifest(manifest_document(raw))

    def test_missing_title_is_rejected(self) -> None:
        raw = dict(LAUNCHABLE_RAW)
        del raw["title"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(manifest_document(raw))

    def test_launchable_entry_requires_an_entrypoint(self) -> None:
        raw = dict(LAUNCHABLE_RAW)
        del raw["entrypoint"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(manifest_document(raw))

    def test_coming_soon_entry_may_not_declare_a_repository(self) -> None:
        raw = dict(COMING_SOON_RAW, repository="https://github.com/GDC-CMU/X.git")
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(manifest_document(raw))

    def test_parsed_runtime_is_an_enum(self) -> None:
        manifest = build_manifest(dict(LAUNCHABLE_RAW))
        self.assertIs(manifest[0].runtime, Runtime.PYTHON)


class RepositoryUrlTests(unittest.TestCase):
    """Only https clone URLs are accepted (criterion B3)."""

    def test_plain_http_is_rejected(self) -> None:
        raw = dict(LAUNCHABLE_RAW, repository="http://github.com/GDC-CMU/X.git")
        with self.assertRaises(InvalidRepositoryUrlError):
            parse_manifest(manifest_document(raw))

    def test_file_url_is_rejected(self) -> None:
        raw = dict(LAUNCHABLE_RAW, repository="file:///etc/passwd")
        with self.assertRaises(InvalidRepositoryUrlError):
            parse_manifest(manifest_document(raw))

    def test_scp_style_url_is_rejected(self) -> None:
        raw = dict(LAUNCHABLE_RAW, repository="git@github.com:GDC-CMU/X.git")
        with self.assertRaises(InvalidRepositoryUrlError):
            parse_manifest(manifest_document(raw))

    def test_url_without_host_is_rejected(self) -> None:
        raw = dict(LAUNCHABLE_RAW, repository="https:///no-host.git")
        with self.assertRaises(InvalidRepositoryUrlError):
            parse_manifest(manifest_document(raw))


class UnsafeEntrypointTests(unittest.TestCase):
    """Path traversal must be impossible, on any platform (criterion B3)."""

    ATTACKS = [
        "../../etc/passwd",
        "/etc/passwd",
        "C:\\Windows\\System32\\calc.exe",
        "games\\..\\..\\main.py",
        "sub/../../escape.py",
        "",
        ".",
        "..",
    ]

    def test_unsafe_entrypoints_are_rejected(self) -> None:
        for attack in self.ATTACKS:
            with self.subTest(entrypoint=attack):
                raw = dict(LAUNCHABLE_RAW, entrypoint=attack)
                with self.assertRaises((UnsafeEntrypointError, ManifestSchemaError)):
                    parse_manifest(manifest_document(raw))

    def test_nested_relative_entrypoint_is_allowed(self) -> None:
        raw = dict(LAUNCHABLE_RAW, entrypoint="src/main.py")
        manifest = parse_manifest(manifest_document(raw))
        self.assertEqual(manifest[0].entrypoint, "src/main.py")

    def test_unsafe_game_id_is_rejected(self) -> None:
        for bad_id in ["../escape", "with space", "Upper", "sub/dir", ""]:
            with self.subTest(game_id=bad_id):
                raw = dict(LAUNCHABLE_RAW, id=bad_id)
                with self.assertRaises((UnsafeEntrypointError, ManifestSchemaError)):
                    parse_manifest(manifest_document(raw))

    def test_safe_relative_path_resolves_inside_base(self) -> None:
        base = Path("/srv/cache/game").resolve()
        resolved = safe_relative_path("src/main.py", base, game_id="x", field_name="entrypoint")
        self.assertEqual(resolved.parent.name, "src")
        self.assertTrue(str(resolved).startswith(str(base)))

    def test_safe_relative_path_rejects_escape(self) -> None:
        base = Path("/srv/cache/game").resolve()
        with self.assertRaises(UnsafeEntrypointError):
            safe_relative_path("../other/main.py", base, game_id="x", field_name="entrypoint")


class LoadManifestTests(TempDirCase):
    def test_missing_file_raises_manifest_file_error(self) -> None:
        with self.assertRaises(ManifestFileError):
            load_manifest(self.tmp_path / "nope.json")

    def test_invalid_json_raises_manifest_file_error(self) -> None:
        path = self.tmp_path / "games.json"
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ManifestFileError):
            load_manifest(path)

    def test_round_trip_from_disk(self) -> None:
        path = self.tmp_path / "games.json"
        path.write_text(json.dumps(manifest_document()), encoding="utf-8")
        manifest = load_manifest(path)
        self.assertEqual(len(manifest), 2)
        self.assertEqual(manifest.by_id("streetfighter").title, "Street Fighter")

    def test_default_path_is_the_shipped_manifest(self) -> None:
        self.assertEqual(len(load_manifest()), len(load_manifest(MANIFEST_FILE)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
