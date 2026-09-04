"""Rendering smoke tests and per-mode navigation rules.

Everything here runs under headless SDL with no window manager, no joystick,
and no writes outside a temporary directory (acceptance criterion I3).
"""

from __future__ import annotations

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import unittest

from launcher.input_state import Direction
from launcher.manifest import load_manifest
from launcher.status import GameState, GameStatus, Notice
from launcher.ui import SCREEN_HEIGHT, SCREEN_SIZE, SCREEN_WIDTH
from launcher.ui.pygame_runtime import pygame
from launcher.ui.scene import Renderer
from launcher.ui.surfaces import SurfaceCache
from launcher.ui.theme import PALETTE, FontBook, PixelFont, mix, shade
from launcher.ui.viewmodel import GalleryFrame, Toast
from launcher.ui.views import SUMMARY_BUCKETS, VIEWS, view_for
from launcher.ui.views.grid import GridView
from launcher.viewmodes import ViewMode

MANIFEST = load_manifest()


def all_states(status: GameStatus = GameStatus.READY) -> dict[str, GameState]:
    return {game.id: GameState(game.id, status, "ready") for game in MANIFEST}


def frame(mode: ViewMode, index: int = 0, **extra) -> GalleryFrame:
    extra.setdefault("time_ms", 1400)
    return GalleryFrame.build(
        MANIFEST,
        all_states(),
        selected_index=index,
        view_mode=mode,
        **extra,
    )


class HeadlessCase(unittest.TestCase):
    """One SDL display for the whole class; torn down completely afterwards."""

    @classmethod
    def setUpClass(cls) -> None:
        pygame.display.init()
        pygame.font.init()
        cls.screen = pygame.display.set_mode(SCREEN_SIZE)
        cls.renderer = Renderer()

    @classmethod
    def tearDownClass(cls) -> None:
        pygame.display.quit()
        pygame.font.quit()
        pygame.quit()


class RegistryTests(unittest.TestCase):
    def test_every_mode_has_a_view(self) -> None:
        self.assertEqual(set(VIEWS), set(ViewMode))

    def test_views_are_distinct_classes(self) -> None:
        classes = {type(view) for view in VIEWS.values()}
        self.assertEqual(len(classes), 3, "each mode needs its own composition")

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(KeyError):
            view_for("not-a-mode")  # type: ignore[arg-type]


class RenderTests(HeadlessCase):
    def test_every_mode_renders_at_the_cabinet_resolution(self) -> None:
        for mode in ViewMode:
            with self.subTest(mode=mode.value):
                surface = self.renderer.render(frame(mode, index=2))
                self.assertEqual(surface.get_size(), (SCREEN_WIDTH, SCREEN_HEIGHT))

    def test_rendering_is_deterministic(self) -> None:
        """Same frame in, same pixels out -- what makes the screenshots stable."""
        for mode in ViewMode:
            with self.subTest(mode=mode.value):
                first = self.renderer.render(frame(mode, index=1))
                second = self.renderer.render(frame(mode, index=1))
                self.assertEqual(
                    pygame.image.tostring(first, "RGB"),
                    pygame.image.tostring(second, "RGB"),
                )

    def test_modes_produce_visibly_different_pixels(self) -> None:
        shots = {
            mode: pygame.image.tostring(self.renderer.render(frame(mode, 2)), "RGB")
            for mode in ViewMode
        }
        self.assertEqual(len(set(shots.values())), 3)

    def test_selecting_a_different_card_changes_the_image(self) -> None:
        for mode in ViewMode:
            with self.subTest(mode=mode.value):
                first = pygame.image.tostring(self.renderer.render(frame(mode, 0)), "RGB")
                second = pygame.image.tostring(self.renderer.render(frame(mode, 4)), "RGB")
                self.assertNotEqual(first, second)

    def test_every_status_renders(self) -> None:
        for status in GameStatus:
            with self.subTest(status=status.value):
                states = all_states(status)
                built = GalleryFrame.build(
                    MANIFEST, states, selected_index=0, view_mode=ViewMode.GRID, time_ms=900
                )
                self.assertEqual(self.renderer.render(built).get_size(), SCREEN_SIZE)

    def test_notices_and_toasts_render_in_every_mode(self) -> None:
        notice = Notice("error", "Street Fighter exited with code 1", "Traceback ...")
        toast = Toast("COMING SOON", "Not on the cabinet yet", started_ms=0, duration_ms=5000)
        for mode in ViewMode:
            with self.subTest(mode=mode.value):
                built = frame(mode, 1, notice=notice, toast=toast, time_ms=200)
                self.assertEqual(self.renderer.render(built).get_size(), SCREEN_SIZE)

    def test_syncing_banner_renders(self) -> None:
        built = frame(ViewMode.CAROUSEL, 0, syncing=True)
        self.assertEqual(self.renderer.render(built).get_size(), SCREEN_SIZE)

    def test_frames_are_not_blank(self) -> None:
        """A black rectangle would technically 'render'; make sure it doesn't."""
        for mode in ViewMode:
            with self.subTest(mode=mode.value):
                surface = self.renderer.render(frame(mode, 3))
                colours = {
                    surface.get_at((x, y))[:3]
                    for x in range(0, SCREEN_WIDTH, 23)
                    for y in range(0, SCREEN_HEIGHT, 17)
                }
                self.assertGreater(len(colours), 60, "the screen looks empty")

    def test_the_logo_keeps_its_aspect_ratio(self) -> None:
        """Criterion G1: the branding must never be squashed."""
        source = pygame.image.load(str(_logo_path()))
        original = source.get_width() / source.get_height()
        logo = self.renderer.ctx.logo(120)
        self.assertAlmostEqual(logo.get_width() / logo.get_height(), original, places=2)


def _logo_path():
    from launcher.paths import BRANDING_LOGO

    return BRANDING_LOGO


class GridNavigationTests(unittest.TestCase):
    """The grid is the only mode with genuine two-dimensional movement."""

    def setUp(self) -> None:
        self.view = view_for(ViewMode.GRID)
        self.count = len(MANIFEST)

    def test_right_moves_along_the_row(self) -> None:
        self.assertEqual(self.view.navigate(0, self.count, Direction.RIGHT), 1)

    def test_down_moves_a_whole_row(self) -> None:
        self.assertEqual(
            self.view.navigate(0, self.count, Direction.DOWN), GridView.columns
        )

    def test_up_from_the_top_row_wraps_to_the_bottom(self) -> None:
        moved = self.view.navigate(0, self.count, Direction.UP)
        self.assertGreaterEqual(moved, GridView.columns)

    def test_navigation_always_stays_in_range(self) -> None:
        index = 0
        for direction in Direction:
            for _ in range(9):
                index = self.view.navigate(index, self.count, direction)
                self.assertTrue(0 <= index < self.count)

    def test_row_ends_wrap_within_the_row(self) -> None:
        last_in_row = GridView.columns - 1
        self.assertEqual(self.view.navigate(last_in_row, self.count, Direction.RIGHT), 0)

    def test_empty_gallery_is_handled(self) -> None:
        self.assertEqual(self.view.navigate(0, 0, Direction.RIGHT), 0)


class LinearNavigationTests(unittest.TestCase):
    """Carousel and cover flow are one long wrapping row."""

    MODES = (ViewMode.CAROUSEL, ViewMode.COVER_FLOW)

    def test_right_advances_and_wraps(self) -> None:
        count = len(MANIFEST)
        for mode in self.MODES:
            with self.subTest(mode=mode.value):
                view = view_for(mode)
                self.assertEqual(view.navigate(0, count, Direction.RIGHT), 1)
                self.assertEqual(view.navigate(count - 1, count, Direction.RIGHT), 0)

    def test_left_wraps_backwards(self) -> None:
        count = len(MANIFEST)
        for mode in self.MODES:
            with self.subTest(mode=mode.value):
                self.assertEqual(view_for(mode).navigate(0, count, Direction.LEFT), count - 1)

    def test_vertical_still_moves_so_the_stick_is_never_dead(self) -> None:
        count = len(MANIFEST)
        for mode in self.MODES:
            with self.subTest(mode=mode.value):
                view = view_for(mode)
                self.assertNotEqual(view.navigate(2, count, Direction.UP), 2)
                self.assertNotEqual(view.navigate(2, count, Direction.DOWN), 2)


class ViewModelTests(unittest.TestCase):
    def test_switching_mode_preserves_the_selection(self) -> None:
        """Criterion D7: the visitor never loses their place."""
        built = frame(ViewMode.GRID, index=4)
        for mode in ViewMode:
            with self.subTest(mode=mode.value):
                self.assertEqual(built.with_mode(mode).selected_index, 4)

    def test_selection_wraps_rather_than_dead_ending(self) -> None:
        built = frame(ViewMode.GRID, index=0)
        self.assertEqual(built.with_selection(len(MANIFEST)).selected_index, 0)
        self.assertEqual(built.with_selection(-1).selected_index, len(MANIFEST) - 1)

    def test_out_of_range_frames_are_rejected_outright(self) -> None:
        with self.assertRaises(IndexError):
            frame(ViewMode.GRID, index=len(MANIFEST))

    def test_cards_mirror_the_manifest_order(self) -> None:
        built = frame(ViewMode.GRID)
        self.assertEqual(
            [card.entry.id for card in built.cards], [game.id for game in MANIFEST]
        )

    def test_unknown_state_falls_back_rather_than_crashing(self) -> None:
        built = GalleryFrame.build(MANIFEST, {}, selected_index=0, view_mode=ViewMode.GRID)
        self.assertEqual(len(built.cards), len(MANIFEST))

    def test_toast_expires(self) -> None:
        toast = Toast("HI", "there", started_ms=0, duration_ms=100)
        self.assertFalse(toast.is_expired(50))
        self.assertTrue(toast.is_expired(150))


class ThemeTests(HeadlessCase):
    """Criterion G2: a deliberate, named palette rather than magic numbers."""

    REQUIRED = ("cmu_red", "electric_cyan", "warm_amber", "violet", "void")

    def test_named_palette_covers_the_brand(self) -> None:
        for name in self.REQUIRED:
            with self.subTest(colour=name):
                self.assertIn(name, PALETTE)

    def test_palette_entries_are_rgb_triples(self) -> None:
        for name, value in PALETTE.items():
            with self.subTest(colour=name):
                self.assertEqual(len(value), 3)
                self.assertTrue(all(0 <= channel <= 255 for channel in value))

    def test_shade_and_mix_stay_in_range(self) -> None:
        for factor in (0.0, 0.5, 1.0, 2.5):
            self.assertTrue(all(0 <= c <= 255 for c in shade(PALETTE["cmu_red"], factor)))
        blended = mix(PALETTE["cmu_red"], PALETTE["electric_cyan"], 0.5)
        self.assertTrue(all(0 <= channel <= 255 for channel in blended))

    def test_pixel_font_measures_what_it_draws(self) -> None:
        surface = PixelFont().render("ARCADE", 2, PALETTE["bone"])
        self.assertEqual(surface.get_size(), PixelFont.measure("ARCADE", 2))

    def test_pixel_font_is_large_enough_to_read_across_a_room(self) -> None:
        """Criterion G5: no tiny text."""
        self.assertGreaterEqual(PixelFont.measure("A", 2)[1], 14)

    def test_font_book_wraps_without_losing_words(self) -> None:
        fonts = FontBook()
        lines = fonts.wrap("one two three four five six seven", "body", 120, max_lines=3)
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(lines[0])


class SurfaceCacheTests(unittest.TestCase):
    """Criterion D9: repeated frames must not rebuild the same surfaces."""

    def test_second_lookup_is_a_hit(self) -> None:
        cache = SurfaceCache(capacity=4)
        builds = 0

        def build() -> pygame.Surface:
            nonlocal builds
            builds += 1
            return pygame.Surface((4, 4))

        cache.get("k", build)
        cache.get("k", build)
        self.assertEqual(builds, 1)
        self.assertEqual(cache.stats.hits, 1)

    def test_capacity_is_enforced(self) -> None:
        cache = SurfaceCache(capacity=2)
        for key in range(5):
            cache.get(key, lambda: pygame.Surface((2, 2)))
        self.assertLessEqual(len(cache), 2)

    def test_clear_empties_the_cache(self) -> None:
        cache = SurfaceCache(capacity=4)
        cache.get("k", lambda: pygame.Surface((2, 2)))
        cache.clear()
        self.assertIsNone(cache.peek("k"))


class CachedRenderTests(HeadlessCase):
    def test_redrawing_reuses_cached_surfaces(self) -> None:
        renderer = Renderer()
        built = frame(ViewMode.COVER_FLOW, 2)
        renderer.render(built)
        misses = renderer.ctx.cache.stats.misses
        renderer.render(built)
        self.assertEqual(
            renderer.ctx.cache.stats.misses, misses, "an identical frame rebuilt surfaces"
        )
        self.assertGreater(renderer.ctx.cache.stats.hits, 0)


class SummaryTallyTests(unittest.TestCase):
    """The header tally must be arithmetic the reader can check.

    An earlier revision bucketed only ``is_playable`` and ``COMING_SOON``, so a
    cabinet mid-sync silently dropped games from the line and the numbers no
    longer added up.  These tests pin the partition instead of one example.
    """

    def test_the_buckets_partition_every_status(self) -> None:
        seen: list[GameStatus] = [s for _, group in SUMMARY_BUCKETS for s in group]
        self.assertEqual(len(seen), len(set(seen)), "a status is in two buckets")
        self.assertEqual(set(seen), set(GameStatus), "a status is in no bucket")

    def test_the_counts_always_sum_to_the_game_count(self) -> None:
        view = view_for(ViewMode.GRID)
        for status in GameStatus:
            with self.subTest(status=status.name):
                built = GalleryFrame.build(
                    MANIFEST,
                    all_states(status),
                    selected_index=0,
                    view_mode=ViewMode.GRID,
                    time_ms=0,
                )
                counts = [
                    int(part.split(" ", 1)[0])
                    for part in view.summary(built).split("  ")[1:]
                ]
                self.assertEqual(sum(counts), built.count)

    def test_mixed_states_still_add_up(self) -> None:
        """Every game lands in exactly one bucket, whatever the mix."""
        rotation = list(GameStatus)
        states = {
            game.id: GameState(game.id, rotation[i % len(rotation)], "x")
            for i, game in enumerate(MANIFEST)
        }
        built = GalleryFrame.build(
            MANIFEST, states, selected_index=0, view_mode=ViewMode.GRID, time_ms=0
        )
        line = view_for(ViewMode.GRID).summary(built)
        counts = [int(part.split(" ", 1)[0]) for part in line.split("  ")[1:]]
        self.assertEqual(sum(counts), built.count, line)

    def test_the_line_leads_with_the_total(self) -> None:
        built = frame(ViewMode.GRID)
        self.assertTrue(
            view_for(ViewMode.GRID).summary(built).startswith(f"{built.count} GAMES")
        )

    def test_empty_buckets_are_omitted(self) -> None:
        built = GalleryFrame.build(
            MANIFEST,
            all_states(GameStatus.READY),
            selected_index=0,
            view_mode=ViewMode.GRID,
            time_ms=0,
        )
        line = view_for(ViewMode.GRID).summary(built)
        self.assertNotIn("0 ", line)
        self.assertNotIn("SOON", line)


class StatusDistinguishabilityTests(HeadlessCase):
    """Criterion D6: the four availabilities must look different in every mode."""

    STATUSES = (
        GameStatus.READY,
        GameStatus.COMING_SOON,
        GameStatus.UPDATING,
        GameStatus.UNAVAILABLE,
    )

    def test_every_mode_renders_each_status_differently(self) -> None:
        for mode in ViewMode:
            payloads: dict[GameStatus, bytes] = {}
            for status in self.STATUSES:
                built = GalleryFrame.build(
                    MANIFEST,
                    all_states(status),
                    selected_index=0,
                    view_mode=mode,
                    time_ms=1400,
                )
                surface = self.renderer.render(built)
                payloads[status] = pygame.image.tostring(surface, "RGB")
            with self.subTest(mode=mode.name):
                self.assertEqual(
                    len(set(payloads.values())),
                    len(self.STATUSES),
                    f"{mode.name} draws two availabilities identically",
                )

    def test_each_status_has_its_own_badge_label(self) -> None:
        labels = {status: status.value for status in GameStatus}
        self.assertEqual(len(set(labels.values())), len(GameStatus))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
