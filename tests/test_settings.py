"""Settings loading, defaults, and environment overrides."""

from __future__ import annotations

import json
import unittest

from launcher.errors import SettingsError
from launcher.paths import SETTINGS_FILE
from launcher.settings import Settings, load_settings
from launcher.viewmodes import CYCLE_ORDER, ViewMode

from support import TempDirCase


class DefaultsTests(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self) -> None:
        settings = load_settings(SETTINGS_FILE.parent / "does-not-exist.json", env={})
        self.assertEqual(settings, Settings())

    def test_shipped_config_is_valid(self) -> None:
        settings = load_settings(SETTINGS_FILE, env={})
        self.assertIn(settings.default_view, set(ViewMode))
        self.assertGreaterEqual(settings.frame_rate, 30)

    def test_shipped_config_selects_carousel(self) -> None:
        """Documented default: the cabinet boots into the carousel."""
        self.assertIs(load_settings(SETTINGS_FILE, env={}).default_view, ViewMode.CAROUSEL)


class FileTests(TempDirCase):
    def write(self, payload: object) -> object:
        path = self.tmp_path / "launcher.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_values_are_read_from_disk(self) -> None:
        path = self.write(
            {
                "default_view": "cover_flow",
                "fullscreen": False,
                "frame_rate": 30,
                "sync_on_start": False,
            }
        )
        settings = load_settings(path, env={})
        self.assertIs(settings.default_view, ViewMode.COVER_FLOW)
        self.assertFalse(settings.fullscreen)
        self.assertEqual(settings.frame_rate, 30)
        self.assertFalse(settings.sync_on_start)

    def test_malformed_json_is_an_error(self) -> None:
        path = self.tmp_path / "launcher.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(path, env={})

    def test_unknown_view_is_an_error(self) -> None:
        path = self.write({"default_view": "hologram"})
        with self.assertRaises(SettingsError):
            load_settings(path, env={})

    def test_out_of_range_frame_rate_is_an_error(self) -> None:
        path = self.write({"frame_rate": 5})
        with self.assertRaises(SettingsError):
            load_settings(path, env={})

    def test_non_object_root_is_an_error(self) -> None:
        path = self.write([1, 2, 3])
        with self.assertRaises(SettingsError):
            load_settings(path, env={})


class EnvironmentOverrideTests(TempDirCase):
    def test_view_override(self) -> None:
        settings = load_settings(
            self.tmp_path / "none.json", env={"ARCADE_LAUNCHER_VIEW": "grid"}
        )
        self.assertIs(settings.default_view, ViewMode.GRID)

    def test_sync_override_disables_the_network(self) -> None:
        settings = load_settings(
            self.tmp_path / "none.json", env={"ARCADE_LAUNCHER_SYNC": "0"}
        )
        self.assertFalse(settings.sync_on_start)

    def test_fullscreen_override(self) -> None:
        settings = load_settings(
            self.tmp_path / "none.json", env={"ARCADE_LAUNCHER_FULLSCREEN": "0"}
        )
        self.assertFalse(settings.fullscreen)

    def test_bad_override_is_an_error(self) -> None:
        with self.assertRaises(SettingsError):
            load_settings(self.tmp_path / "none.json", env={"ARCADE_LAUNCHER_FPS": "way too fast"})


class ViewModeTests(unittest.TestCase):
    def test_cycle_visits_every_mode_and_returns(self) -> None:
        mode = ViewMode.GRID
        seen = [mode]
        for _ in range(len(CYCLE_ORDER) - 1):
            mode = mode.next()
            seen.append(mode)
        self.assertEqual(set(seen), set(ViewMode))
        self.assertIs(mode.next(), ViewMode.GRID)

    def test_parse_accepts_hyphens_and_underscores(self) -> None:
        self.assertIs(ViewMode.parse("cover-flow"), ViewMode.COVER_FLOW)
        self.assertIs(ViewMode.parse("cover_flow"), ViewMode.COVER_FLOW)
        self.assertIs(ViewMode.parse("COVER FLOW"), ViewMode.COVER_FLOW)

    def test_parse_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            ViewMode.parse("isometric")

    def test_every_mode_has_a_distinct_direct_select_slot(self) -> None:
        slots = [mode.slot for mode in ViewMode]
        self.assertEqual(sorted(slots), [1, 2, 3])
        self.assertIs(ViewMode.from_slot(3), ViewMode.COVER_FLOW)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
