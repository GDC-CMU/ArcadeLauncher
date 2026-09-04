"""The preview tool: it must render the real UI, deterministically, at 800x600.

The screenshots are the launcher's public face, so these tests also pin their
*honesty*: a screenshot may only show a screen the launcher can really produce.
"""

from __future__ import annotations

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from launcher.manifest import load_manifest
from launcher.paths import MANIFEST_FILE, SCREENSHOTS_DIR
from launcher.status import GameState, GameStatus
from launcher.ui import SCREEN_SIZE
from launcher.ui.pygame_runtime import pygame
from launcher.ui.views import view_for
from launcher.viewmodes import ViewMode
from tools.generate_previews import (
    BADGE_MEANINGS,
    BADGE_SHEET_FILENAME,
    GALLERY_FILENAMES,
    RENDER_MANIFEST_FILENAME,
    assert_states_are_reachable,
    build_frame,
    cabinet_states,
    describe_environment,
    fingerprint_environment,
    fingerprint_sources,
    generate,
    main,
    read_render_manifest,
    render_inputs,
    write_render_manifest,
)

from support import TempDirCase

GALLERY_SHOTS = ("grid.png", "carousel.png", "cover-flow.png")
ALL_SHOTS = GALLERY_SHOTS + (BADGE_SHEET_FILENAME,)


def _manifest():
    return load_manifest(MANIFEST_FILE)


class FilenameTests(unittest.TestCase):
    def test_one_screenshot_per_mode(self) -> None:
        self.assertEqual(set(GALLERY_FILENAMES), set(ViewMode))

    def test_documented_filenames(self) -> None:
        self.assertEqual(set(GALLERY_FILENAMES.values()), set(GALLERY_SHOTS))

    def test_the_badge_sheet_is_not_mistaken_for_a_gallery_shot(self) -> None:
        self.assertNotIn(BADGE_SHEET_FILENAME, GALLERY_FILENAMES.values())


class HonestyTests(unittest.TestCase):
    """Regression guard: the previews may not invent unreachable states.

    A previous revision dressed the screenshots up by giving coming-soon games
    ``UPDATING``/``CACHED OFFLINE``/``UNAVAILABLE`` badges.  Those states only
    come from syncing, coming-soon entries are never synced, and the header
    consequently advertised a tally the product cannot produce.  These tests
    make that class of defect fail the build.
    """

    def setUp(self) -> None:
        self.manifest = _manifest()
        self.states = cabinet_states(self.manifest)

    def test_no_coming_soon_game_is_given_a_sync_only_status(self) -> None:
        for entry in self.manifest:
            if entry.launchable:
                continue
            with self.subTest(game=entry.id):
                status = self.states[entry.id].status
                self.assertIs(status, GameStatus.COMING_SOON)
                self.assertFalse(status.requires_sync)

    def test_launchable_games_are_shown_as_playable(self) -> None:
        for entry in self.manifest:
            if entry.launchable:
                with self.subTest(game=entry.id):
                    self.assertTrue(self.states[entry.id].status.is_playable)

    def test_the_guard_rejects_an_impossible_state(self) -> None:
        coming_soon = next(e for e in self.manifest if not e.launchable)
        rigged = dict(self.states)
        rigged[coming_soon.id] = GameState(
            coming_soon.id, GameStatus.UPDATING, "impossible"
        )
        with self.assertRaises(ValueError) as caught:
            assert_states_are_reachable(self.manifest, rigged)
        self.assertIn(coming_soon.id, str(caught.exception))

    def test_the_guard_rejects_every_sync_only_status(self) -> None:
        coming_soon = next(e for e in self.manifest if not e.launchable)
        for status in GameStatus:
            if not status.requires_sync:
                continue
            with self.subTest(status=status.name):
                rigged = dict(self.states)
                rigged[coming_soon.id] = GameState(coming_soon.id, status, "impossible")
                with self.assertRaises(ValueError):
                    assert_states_are_reachable(self.manifest, rigged)

    def test_the_header_tally_matches_the_manifest(self) -> None:
        """Criterion C7/H1: the stat line must be arithmetic, not decoration."""
        playable = sum(1 for entry in self.manifest if entry.launchable)
        soon = sum(1 for entry in self.manifest if not entry.launchable)
        expected = f"{len(self.manifest)} GAMES  {playable} PLAYABLE  {soon} SOON"
        for mode in ViewMode:
            with self.subTest(mode=mode.name):
                frame = build_frame(self.manifest, mode)
                self.assertEqual(view_for(mode).summary(frame), expected)

    def test_the_shipped_manifest_renders_one_playable_and_five_soon(self) -> None:
        frame = build_frame(self.manifest, ViewMode.GRID)
        self.assertEqual(
            view_for(ViewMode.GRID).summary(frame), "6 GAMES  1 PLAYABLE  5 SOON"
        )

    def test_no_shot_hides_the_tally_behind_a_banner(self) -> None:
        """The status strip shows a notice *instead of* the summary."""
        for mode in ViewMode:
            with self.subTest(mode=mode.name):
                self.assertIsNone(build_frame(self.manifest, mode).notice)

    def test_the_badge_sheet_documents_every_status(self) -> None:
        """D6 is proven by the reference sheet, not by faking game states."""
        self.assertEqual({status for status, _ in BADGE_MEANINGS}, set(GameStatus))


class GenerationTests(TempDirCase):
    def test_generate_writes_every_screenshot_at_the_cabinet_resolution(self) -> None:
        written = generate(self.tmp_path, MANIFEST_FILE)
        self.assertEqual({path.name for path in written}, set(ALL_SHOTS))
        for path in written:
            with self.subTest(screenshot=path.name):
                self.assertEqual(pygame.image.load(str(path)).get_size(), SCREEN_SIZE)

    def test_generation_is_reproducible(self) -> None:
        first = {
            p.name: p.read_bytes() for p in generate(self.tmp_path / "a", MANIFEST_FILE)
        }
        second = {
            p.name: p.read_bytes() for p in generate(self.tmp_path / "b", MANIFEST_FILE)
        }
        self.assertEqual(first, second)

    def test_every_shot_is_a_different_image(self) -> None:
        written = generate(self.tmp_path, MANIFEST_FILE)
        payloads = {path.read_bytes() for path in written}
        self.assertEqual(
            len(payloads), len(ALL_SHOTS), "each shot must be its own composition"
        )

    def test_main_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--output", str(self.tmp_path / "shots")])
        self.assertEqual(code, 0)
        for name in ALL_SHOTS:
            with self.subTest(screenshot=name):
                self.assertTrue((self.tmp_path / "shots" / name).is_file())

    def test_main_records_what_it_rendered(self) -> None:
        """The sidecar is written by the tool, not maintained by hand."""
        output = self.tmp_path / "shots"
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--output", str(output)])

        document = read_render_manifest(output)
        self.assertEqual(set(document["screenshots"]), set(ALL_SHOTS))
        self.assertEqual(document["source_fingerprint"], fingerprint_sources())
        for name, recorded in document["screenshots"].items():
            with self.subTest(screenshot=name):
                self.assertEqual(
                    hashlib.sha256((output / name).read_bytes()).hexdigest(),
                    recorded["sha256"],
                )
                self.assertEqual(recorded["size"], list(SCREEN_SIZE))

    def test_the_sidecar_describes_this_machine(self) -> None:
        written = generate(self.tmp_path, MANIFEST_FILE)
        path = write_render_manifest(self.tmp_path, written)
        document = read_render_manifest(self.tmp_path)
        self.assertEqual(path.name, RENDER_MANIFEST_FILENAME)
        self.assertEqual(
            document["environment"]["fingerprint"], fingerprint_environment()
        )
        self.assertEqual(
            document["environment"]["pygame_flavour"],
            describe_environment()["pygame_flavour"],
        )


class RenderManifestTests(unittest.TestCase):
    """The sidecar that records what produced the screenshots."""

    def setUp(self) -> None:
        self.document = read_render_manifest()

    def test_the_sidecar_is_committed(self) -> None:
        self.assertTrue((SCREENSHOTS_DIR / RENDER_MANIFEST_FILENAME).is_file())

    def test_it_covers_exactly_the_committed_shots(self) -> None:
        self.assertEqual(set(self.document["screenshots"]), set(ALL_SHOTS))

    def test_it_records_the_rasteriser_that_produced_them(self) -> None:
        environment = self.document["environment"]
        for key in ("pygame", "pygame_flavour", "sdl", "sdl_ttf", "fingerprint"):
            with self.subTest(key=key):
                self.assertIn(key, environment)
        self.assertEqual(
            environment["fingerprint"], fingerprint_environment(environment)
        )

    def test_the_source_fingerprint_reacts_to_a_change(self) -> None:
        """A fingerprint that never changes would be a check in name only."""
        inputs = render_inputs()
        self.assertGreater(len(inputs), 5)
        self.assertEqual(fingerprint_sources(inputs), fingerprint_sources(inputs))

        for index in range(len(inputs)):
            mutated = list(inputs)
            name, content = mutated[index]
            mutated[index] = (name, content + b"# ")
            with self.subTest(changed=name):
                self.assertNotEqual(
                    fingerprint_sources(inputs), fingerprint_sources(mutated)
                )

    def test_the_fingerprint_ignores_line_ending_differences(self) -> None:
        """A CRLF checkout must agree with the cabinet's LF one."""
        self.assertEqual(
            fingerprint_sources([("a.py", b"x = 1\r\ny = 2\r\n")]),
            fingerprint_sources([("a.py", b"x = 1\ny = 2\n")]),
        )

    def test_the_rendering_source_set_is_not_empty_or_accidental(self) -> None:
        names = {name for name, _ in render_inputs()}
        for expected in (
            "tools/generate_previews.py",
            "launcher/ui/theme.py",
            "launcher/ui/scene.py",
            "launcher/status.py",
            "data/games.json",
            "assets/branding/gdc-cmu-logo.png",
        ):
            with self.subTest(source=expected):
                self.assertIn(expected, names)


class CommittedScreenshotTests(unittest.TestCase):
    """Criterion H2/H3/H4: the files in the repository are the real thing.

    Split by portability on purpose. Everything here except the last test gives
    the same answer on Windows, on a teammate's laptop and on the cabinet; the
    last one is byte-exact and therefore only meaningful where the recorded
    rasteriser matches.
    """

    def setUp(self) -> None:
        self.document = read_render_manifest()

    # -- portable ---------------------------------------------------------
    def test_every_shot_is_committed(self) -> None:
        for name in ALL_SHOTS:
            with self.subTest(screenshot=name):
                self.assertTrue((SCREENSHOTS_DIR / name).is_file())

    def test_every_shot_is_exactly_800x600(self) -> None:
        for name in ALL_SHOTS:
            with self.subTest(screenshot=name):
                size = pygame.image.load(str(SCREENSHOTS_DIR / name)).get_size()
                self.assertEqual(size, (800, 600))

    def test_committed_shots_are_the_bytes_the_tool_wrote(self) -> None:
        """Catches a hand-edited or half-regenerated screenshot."""
        for name, recorded in self.document["screenshots"].items():
            with self.subTest(screenshot=name):
                actual = hashlib.sha256(
                    (SCREENSHOTS_DIR / name).read_bytes()
                ).hexdigest()
                self.assertEqual(
                    actual,
                    recorded["sha256"],
                    f"{name} is not what tools/generate_previews.py last wrote; "
                    f"run: python -m tools.generate_previews",
                )
                self.assertEqual(
                    list(pygame.image.load(str(SCREENSHOTS_DIR / name)).get_size()),
                    recorded["size"],
                )

    def test_the_screenshots_are_current_with_the_rendering_code(self) -> None:
        """The portable staleness check: did the UI change since these were made?

        This is the test that used to be a byte comparison. It asks the same
        question -- "were these regenerated after the last UI edit?" -- without
        rendering anything, so it is exact and gives the same answer on every
        machine, including the cabinet.
        """
        self.assertEqual(
            fingerprint_sources(),
            self.document["source_fingerprint"],
            "the rendering code or the manifest changed after the screenshots "
            "were generated; run: python -m tools.generate_previews",
        )

    def test_a_fresh_render_produces_the_same_files_at_the_same_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = generate(Path(tmp), MANIFEST_FILE)
            self.assertEqual({path.name for path in written}, set(ALL_SHOTS))
            for path in written:
                with self.subTest(screenshot=path.name):
                    self.assertEqual(
                        pygame.image.load(str(path)).get_size(),
                        pygame.image.load(str(SCREENSHOTS_DIR / path.name)).get_size(),
                    )

    def test_a_fresh_render_keeps_every_shot_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payloads = {path.read_bytes() for path in generate(Path(tmp), MANIFEST_FILE)}
            self.assertEqual(len(payloads), len(ALL_SHOTS))

    # -- only meaningful on the generating environment ---------------------
    def test_committed_shots_match_a_fresh_render(self) -> None:
        """Byte-for-byte, with no tolerance -- where that can mean anything.

        Skipped rather than loosened when the rasteriser differs. SDL_ttf
        antialiases the bundled font differently between builds, so a correct
        checkout on the cabinet produces visibly identical, bitwise different
        PNGs. Measured on this UI, that noise is larger than the change a
        palette tweak or a one-word caption edit produces, so there is no
        tolerance that is both portable and honest. The staleness question is
        answered portably by
        :meth:`test_the_screenshots_are_current_with_the_rendering_code`.
        """
        recorded = self.document["environment"]
        current = describe_environment()
        if fingerprint_environment(current) != recorded["fingerprint"]:
            self.skipTest(
                "byte-exact comparison only runs on the environment that "
                "generated the screenshots. Recorded: "
                f"{recorded['pygame_flavour']} {recorded['pygame']}, "
                f"SDL_ttf {tuple(recorded['sdl_ttf'] or ())}. Running: "
                f"{current['pygame_flavour']} {current['pygame']}, "
                f"SDL_ttf {tuple(current['sdl_ttf'] or ())}. These rasterise "
                "the same font differently, so the pixels differ even when the "
                "UI is identical. Staleness is still checked exactly, by "
                "test_the_screenshots_are_current_with_the_rendering_code."
            )

        with tempfile.TemporaryDirectory() as tmp:
            for path in generate(Path(tmp), MANIFEST_FILE):
                with self.subTest(screenshot=path.name):
                    self.assertEqual(
                        path.read_bytes(),
                        (SCREENSHOTS_DIR / path.name).read_bytes(),
                        f"{path.name} is stale; run: python -m tools.generate_previews",
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
