"""Attract-mode preview manifests: schema and containment validation.

Pure-logic tests -- no Pygame, no image decoding. See
``tests/test_preview_playback.py`` for the decode-time caps and the
rendering-layer integration.
"""

from __future__ import annotations

import json

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import unittest

from launcher.previews import (
    MAX_PREVIEW_FRAMES,
    MAX_PREVIEW_FPS,
    MIN_PREVIEW_FPS,
    load_preview_manifest,
)

from support import TempDirCase


def _write_manifest(checkout, document) -> None:
    preview_dir = checkout / "assets" / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "manifest.json").write_text(json.dumps(document), encoding="utf-8")


def _valid_document(frame_count: int = 3) -> dict:
    return {
        "version": 1,
        "fps": 8,
        "frames": [f"frame_{index:03d}.png" for index in range(frame_count)],
    }


class MissingOrUnreadableTests(TempDirCase):
    def test_no_preview_directory_is_not_an_error(self) -> None:
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_preview_directory_without_a_manifest_is_not_an_error(self) -> None:
        (self.tmp_path / "assets" / "preview").mkdir(parents=True)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_a_directory_named_manifest_json_is_rejected(self) -> None:
        """Not a file -- ``read_text`` would raise; this must still degrade."""
        (self.tmp_path / "assets" / "preview" / "manifest.json").mkdir(parents=True)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_malformed_json_is_rejected(self) -> None:
        preview_dir = self.tmp_path / "assets" / "preview"
        preview_dir.mkdir(parents=True)
        (preview_dir / "manifest.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_non_object_root_is_rejected(self) -> None:
        _write_manifest(self.tmp_path, [1, 2, 3])
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))


class SchemaTests(TempDirCase):
    def test_valid_manifest_is_accepted(self) -> None:
        _write_manifest(self.tmp_path, _valid_document(3))
        manifest = load_preview_manifest(self.tmp_path, game_id="g")
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.fps, 8)
        self.assertEqual(len(manifest.frames), 3)
        self.assertEqual(manifest.frames[0].name, "frame_000.png")

    def test_missing_version_is_rejected(self) -> None:
        document = _valid_document()
        del document["version"]
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_unsupported_version_is_rejected(self) -> None:
        document = _valid_document()
        document["version"] = 2
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_boolean_version_is_rejected(self) -> None:
        document = _valid_document()
        document["version"] = True
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_fps_must_be_an_integer(self) -> None:
        document = _valid_document()
        document["fps"] = 8.5
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_boolean_fps_is_rejected(self) -> None:
        document = _valid_document()
        document["fps"] = True
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_fps_bounds_are_enforced(self) -> None:
        for bad in (0, -1, MAX_PREVIEW_FPS + 1):
            with self.subTest(fps=bad):
                document = _valid_document()
                document["fps"] = bad
                _write_manifest(self.tmp_path, document)
                self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_fps_bounds_are_inclusive(self) -> None:
        for good in (MIN_PREVIEW_FPS, MAX_PREVIEW_FPS):
            with self.subTest(fps=good):
                document = _valid_document()
                document["fps"] = good
                _write_manifest(self.tmp_path, document)
                self.assertIsNotNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_frames_must_be_a_non_empty_list(self) -> None:
        for bad in ([], "frame.png", None, {"a": 1}):
            with self.subTest(frames=bad):
                document = _valid_document()
                document["frames"] = bad
                _write_manifest(self.tmp_path, document)
                self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_frames_must_all_be_strings(self) -> None:
        document = _valid_document()
        document["frames"] = ["frame_000.png", 7]
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_frame_count_over_the_cap_is_rejected(self) -> None:
        _write_manifest(self.tmp_path, _valid_document(MAX_PREVIEW_FRAMES + 1))
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_frame_count_at_the_cap_is_accepted(self) -> None:
        _write_manifest(self.tmp_path, _valid_document(MAX_PREVIEW_FRAMES))
        self.assertIsNotNone(load_preview_manifest(self.tmp_path, game_id="g"))


class ContainmentTests(TempDirCase):
    """Untrusted input: a game's own manifest must never point outside its
    ``assets/preview`` directory -- the same containment rule already applied
    to entrypoints (criterion: reuse ``safe_relative_path``)."""

    def test_absolute_path_is_rejected(self) -> None:
        document = _valid_document()
        document["frames"] = ["/etc/passwd"]
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_windows_absolute_path_is_rejected(self) -> None:
        document = _valid_document()
        document["frames"] = ["C:\\Windows\\win.ini"]
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_parent_traversal_is_rejected(self) -> None:
        document = _valid_document()
        document["frames"] = ["../../secrets.png"]
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_one_bad_frame_rejects_the_whole_preview(self) -> None:
        """Never a partial load -- a single escaping frame voids the batch."""
        document = _valid_document(3)
        document["frames"][1] = "../escape.png"
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_empty_string_frame_is_rejected(self) -> None:
        document = _valid_document()
        document["frames"] = [""]
        _write_manifest(self.tmp_path, document)
        self.assertIsNone(load_preview_manifest(self.tmp_path, game_id="g"))

    def test_resolved_paths_stay_inside_the_preview_directory(self) -> None:
        _write_manifest(self.tmp_path, _valid_document(2))
        manifest = load_preview_manifest(self.tmp_path, game_id="g")
        preview_dir = (self.tmp_path / "assets" / "preview").resolve()
        for path in manifest.frames:
            with self.subTest(frame=path.name):
                self.assertEqual(path.parent, preview_dir)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
