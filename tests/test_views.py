"""Rendering smoke tests and per-mode navigation rules.

Everything here runs under headless SDL with no window manager, no joystick,
and no writes outside a temporary directory (acceptance criterion I3).
"""

from __future__ import annotations

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import unittest

from launcher.input_state import Direction
from launcher.gallery import GallerySession
from launcher.manifest import CardArt, GameEntry, Manifest, Runtime, load_manifest
from launcher.status import GameState, GameStatus, Notice
from launcher.ui import SCREEN_HEIGHT, SCREEN_SIZE, SCREEN_WIDTH
from launcher.ui.components import HEADER_HEIGHT, HEADER_RECT
from launcher.ui.effects import edge_alpha, edge_window
from launcher.ui.pygame_runtime import pygame
from launcher.ui.scene import Renderer
from launcher.ui.surfaces import SurfaceCache
from launcher.ui.theme import PALETTE, FontBook, PixelFont, mix, shade
from launcher.ui.viewmodel import GalleryFrame, Toast
from launcher.ui.views import VIEWS, view_for
from launcher.ui.views import carousel, coverflow, grid
from launcher.ui.views.carousel import NEIGHBOUR_CEILING, CarouselView
from launcher.ui.views.coverflow import MAX_DEPTH, CoverFlowView
from launcher.ui.views.grid import GridView
from launcher.viewmodes import ViewMode

MANIFEST = load_manifest()


def synthetic_manifest(count: int) -> Manifest:
    """A manifest of *count* coming-soon games, for arbitrary-count tests.

    Bypasses JSON validation (as :func:`tests.support.entry` does) so a test
    can ask for catalogue sizes -- 1, 2, 12, 20 -- the shipped manifest does
    not have, without inventing a fake ``data/games.json``.
    """
    games = tuple(
        GameEntry(
            id=f"game-{index}",
            title=f"Game {index}",
            description="Synthetic entry for a navigation/layout test.",
            runtime=Runtime.PYTHON,
            launchable=False,
            art=CardArt(motif="duel", palette=("cmu_red", "warm_amber", "ink"), seed=index),
        )
        for index in range(count)
    )
    return Manifest(version=1, games=games)


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
    """The grid is the only mode with genuine two-dimensional movement.

    Navigation flows in reading order across the *whole* catalogue -- item 4
    -- so it scales to any manifest size instead of a hard-coded board.
    Crossing a row edge continues onto the next row rather than wrapping
    back to the start of the same row, which is what makes a single rule
    reach every card whether the catalogue holds 1 game or 20. Scrolling
    (see :class:`GridScrollTests`) is purely a rendering concern layered on
    top of this -- navigation itself never paginates or clips.
    """

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

    def test_right_past_the_last_column_continues_into_the_next_row(self) -> None:
        last_in_row = GridView.columns - 1
        self.assertEqual(
            self.view.navigate(last_in_row, self.count, Direction.RIGHT), GridView.columns
        )

    def test_empty_gallery_is_handled(self) -> None:
        self.assertEqual(self.view.navigate(0, 0, Direction.RIGHT), 0)

    def test_navigation_reaches_every_index_for_several_catalogue_sizes(self) -> None:
        """Criterion (item 4): the board must genuinely work for any count."""
        for count in (1, 2, 7, 12, 20):
            with self.subTest(count=count):
                index = 0
                seen = {index}
                for _ in range(count * 4):
                    index = self.view.navigate(index, count, Direction.RIGHT)
                    self.assertTrue(0 <= index < count)
                    seen.add(index)
                self.assertEqual(seen, set(range(count)))


class GridLayoutTests(unittest.TestCase):
    """The permanent board shape: always 3 columns, never 4; up to 3 rows,
    sized to how many are actually needed rather than always reserving the
    same fixed height. The client's explicit ceiling: 3x3 is readable, 4x4
    is not, so the board never grows past it -- it scrolls instead (see
    :class:`GridScrollTests`).
    """

    COUNTS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 20)

    def test_columns_are_always_exactly_three_at_any_count(self) -> None:
        self.assertEqual(GridView.columns, 3)
        self.assertEqual(grid.COLUMNS, 3)
        for count in self.COUNTS:
            for row in range(grid.rows_needed(count)):
                with self.subTest(count=count, row=row):
                    self.assertLessEqual(grid.row_columns(count, row), 3)

    def test_rows_scale_to_the_catalogue_and_never_exceed_three(self) -> None:
        expected = {
            1: 1, 2: 1, 3: 1,
            4: 2, 5: 2, 6: 2,
            7: 3, 8: 3, 9: 3, 10: 3, 12: 3, 20: 3,
        }
        for count, rows in expected.items():
            with self.subTest(count=count):
                self.assertEqual(grid.visible_rows(count), rows)
                self.assertLessEqual(grid.visible_rows(count), grid.MAX_VISIBLE_ROWS)

    def test_cards_grow_as_fewer_rows_are_needed(self) -> None:
        """1-3 games get large cards; the client accepted smaller ones at
        3 rows, but only because there was no alternative -- a catalogue
        that doesn't need 3 rows should not be shrunk to fit them anyway."""
        self.assertGreater(grid.card_height(1), grid.card_height(2))
        self.assertGreater(grid.card_height(2), grid.card_height(3))

    def test_scrolling_only_kicks_in_past_nine_games(self) -> None:
        for count in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            with self.subTest(count=count):
                self.assertEqual(grid.max_scroll_rows(count), 0.0)
        for count in (10, 12, 20):
            with self.subTest(count=count):
                self.assertGreater(grid.max_scroll_rows(count), 0.0)


class GridRowCenteringTests(unittest.TestCase):
    """A short final row -- now the whole catalogue's actual last row,
    rather than a page's -- must stay centred under the full rows above it,
    not hug the left margin. The same rule that used to apply per-page
    still applies, just to one continuous board instead of six-card pages.
    """

    TOLERANCE = 2

    @staticmethod
    def _bbox(rects: list[pygame.Rect]) -> pygame.Rect:
        return rects[0].unionall(rects[1:]) if len(rects) > 1 else rects[0].copy()

    def test_a_full_row_gets_no_centring_offset(self) -> None:
        self.assertEqual(grid.row_dx(grid.COLUMNS), 0)

    def test_a_short_final_row_is_centred_under_a_full_row(self) -> None:
        for count in (4, 5, 7, 8, 10, 11, 20):
            total_rows = grid.rows_needed(count)
            last_row = total_rows - 1
            cols_in_last = grid.row_columns(count, last_row)
            if cols_in_last == grid.COLUMNS:
                continue  # nothing short to centre for this count
            with self.subTest(count=count):
                row0 = [
                    GridView.slot_rect(0, column, count, 0.0)
                    for column in range(grid.row_columns(count, 0))
                ]
                last = [
                    GridView.slot_rect(last_row, column, count, 0.0)
                    for column in range(cols_in_last)
                ]
                self.assertAlmostEqual(
                    self._bbox(row0).centerx, self._bbox(last).centerx, delta=self.TOLERANCE
                )
                # It must actually be nudged inward, not just coincidentally
                # aligned with the full row's own left edge.
                self.assertGreater(last[0].left, row0[0].left)


class GridScrollTests(unittest.TestCase):
    """The vertical scroll: keeps the selection visible without ever letting
    it leave the band, glides rather than snaps, and (the client's own
    words) walking DOWN repeatedly must eventually surface every game."""

    COUNTS = (1, 2, 6, 7, 9, 10, 11, 12, 20)

    def test_no_scroll_needed_when_everything_fits(self) -> None:
        for count in (1, 2, 3, 6, 7, 9):
            for index in range(count):
                with self.subTest(count=count, index=index):
                    self.assertEqual(grid.target_scroll(index, count), 0.0)

    def test_selection_always_stays_within_the_visible_band(self) -> None:
        for count in self.COUNTS:
            rows = grid.visible_rows(count)
            for index in range(count):
                with self.subTest(count=count, index=index):
                    target = grid.target_scroll(index, count)
                    row = index // grid.COLUMNS
                    self.assertGreaterEqual(row + 1e-9, target)
                    self.assertLessEqual(row, target + rows - 1 + 1e-9)

    def test_scroll_never_exceeds_its_bounds(self) -> None:
        for count in self.COUNTS:
            maximum = grid.max_scroll_rows(count)
            for index in range(count):
                with self.subTest(count=count, index=index):
                    target = grid.target_scroll(index, count)
                    self.assertGreaterEqual(target, 0.0)
                    self.assertLessEqual(target, maximum)

    def test_every_index_reachable_by_pressing_down_repeatedly(self) -> None:
        """The client's request, almost verbatim: 'if I just keep clicking
        down down, it will scroll down and find games there'."""
        view = view_for(ViewMode.GRID)
        for count in (1, 2, 7, 9, 10, 12, 20):
            with self.subTest(count=count):
                index = 0
                seen = {index}
                for _ in range(count * 4):
                    index = view.navigate(index, count, Direction.DOWN)
                    seen.add(index)
                self.assertEqual(seen, set(range(count)))

    def test_the_selection_stays_visible_throughout_a_down_walk(self) -> None:
        """Not just reachable eventually -- comfortably in view at every
        single step of the walk, not only once it settles."""
        view = view_for(ViewMode.GRID)
        for count in (10, 12, 20):
            with self.subTest(count=count):
                index = 0
                rows = grid.visible_rows(count)
                for _ in range(count):
                    index = view.navigate(index, count, Direction.DOWN)
                    target = grid.target_scroll(index, count)
                    row = index // grid.COLUMNS
                    self.assertGreaterEqual(row + 1e-9, target)
                    self.assertLessEqual(row, target + rows - 1 + 1e-9)

    def test_glide_linear_eases_towards_the_target_without_overshoot(self) -> None:
        """The same frame-delta-driven approach as the horizontal modes'
        glide -- consistent motion, monotonic, never overshoots."""
        value = 0.0
        target = 3.0
        for _ in range(200):
            previous = value
            value = GallerySession._glide_linear(value, target, 16)
            self.assertGreaterEqual(value, previous - 1e-9)
            self.assertLessEqual(value, target + 1e-9)
        self.assertAlmostEqual(value, target, places=2)


class GridRenderingTests(HeadlessCase):
    """Item 4, for the scrolling board: rendering must stay correct at any
    catalogue size, not just the seven games currently shipped -- every
    card on screen, and never more than three rows of it visible at once.
    """

    COUNTS = (1, 2, 3, 6, 7, 9, 10, 12, 20)

    @staticmethod
    def _content_rect() -> pygame.Rect:
        return pygame.Rect(
            grid.CARD_MARGIN_X,
            grid.CARD_TOP,
            SCREEN_WIDTH - grid.CARD_MARGIN_X * 2,
            SCREEN_HEIGHT - grid.CARD_TOP - grid.CARD_BOTTOM_MARGIN,
        )

    def test_every_visible_card_stays_on_screen_for_several_catalogue_sizes(self) -> None:
        """Only the rows within the current visible band are meant to be
        on-screen -- a row scrolled out of view is *supposed* to land off
        the 800x600 canvas; that is what scrolling means. So this checks
        containment for the visible band only, not the whole catalogue.
        """
        surface_rect = pygame.Rect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT)
        for count in self.COUNTS:
            manifest = synthetic_manifest(count)
            states = {
                game.id: GameState(game.id, GameStatus.COMING_SOON, "")
                for game in manifest
            }
            for index in (0, count - 1):
                with self.subTest(count=count, selected=index):
                    scroll = grid.target_scroll(index, count)
                    built = GalleryFrame.build(
                        manifest,
                        states,
                        selected_index=index,
                        view_mode=ViewMode.GRID,
                        time_ms=900,
                        scroll=float(index),
                        grid_scroll=scroll,
                    )
                    self.assertEqual(self.renderer.render(built).get_size(), SCREEN_SIZE)
                    rows_shown = grid.visible_rows(count)
                    first_visible = int(scroll)
                    for row in range(first_visible, first_visible + rows_shown):
                        for column in range(grid.row_columns(count, row)):
                            rect = GridView.slot_rect(row, column, count, scroll)
                            self.assertTrue(surface_rect.contains(rect), rect)

    def test_at_most_three_rows_are_visible_at_rest(self) -> None:
        content = self._content_rect()
        for count in self.COUNTS:
            for index in (0, count // 2, count - 1):
                with self.subTest(count=count, index=index):
                    scroll = grid.target_scroll(index, count)
                    visible_rows_seen = 0
                    for row in range(grid.rows_needed(count)):
                        rect = GridView.slot_rect(row, 0, count, scroll)
                        if rect.clip(content).height > 0:
                            visible_rows_seen += 1
                    self.assertLessEqual(visible_rows_seen, grid.MAX_VISIBLE_ROWS)



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


class SharedHeaderTests(HeadlessCase):
    """Item B: the header must be a fixed anchor, identical in every view.

    A visitor cycling views used to see the brand mark change size, wording
    and position -- Grid's full band with a subtitle, Carousel's smaller band
    without one, Cover Flow's own inline "GDC ARCADE" text instead of "GAME
    DEV CLUB". These pin that all three now draw the exact same rect at the
    exact same pixels, so a future edit to one view's chrome can't quietly
    reintroduce the drift.
    """

    #: The brand portion of the header, left of where the mode chip starts.
    BRAND_AREA = pygame.Rect(0, 0, 560, HEADER_HEIGHT)

    def test_every_view_shares_the_same_header_rect_object(self) -> None:
        """All three modules must reference one constant, not their own copy."""
        self.assertIs(grid.HEADER_RECT, HEADER_RECT)
        self.assertIs(carousel.HEADER_RECT, HEADER_RECT)
        self.assertIs(coverflow.HEADER_RECT, HEADER_RECT)

    def test_the_brand_mark_is_pixel_identical_across_every_mode(self) -> None:
        """Same logo, same wording, same size, same position -- every mode.

        This is the direct regression guard for the reported bug: Grid's
        subtitle-bearing band, Carousel's smaller one and Cover Flow's own
        inline "GDC ARCADE" text used to all draw something different here.
        Cropped left of the mode chip -- the one part of the header that is
        *supposed* to differ, since it names the active view -- everything
        else must render byte-identical no matter which mode is on screen.
        """
        payloads = {}
        for mode in ViewMode:
            built = frame(mode, index=2, time_ms=1400)
            surface = self.renderer.render(built)
            crop = surface.subsurface(self.BRAND_AREA)
            payloads[mode] = pygame.image.tostring(crop, "RGB")
        distinct = set(payloads.values())
        self.assertEqual(
            len(distinct),
            1,
            "the brand mark differs between view modes -- it must not move, "
            "resize or reword when the visitor cycles views",
        )


class CoverFlowSymmetryTests(unittest.TestCase):
    """Item A: the fan must be symmetric for any game count, and item A of the
    follow-up session: entering/exiting cards must fade, not pop.

    ``wrapped_distance`` has an ambiguous case at exactly half the catalogue
    around from the selection -- the diametrically opposite card is equally
    close either way -- and resolving that tie to a fixed side is what used
    to draw three cards on the left and two on the right for six games.
    ``edge_window``/``edge_alpha`` fix both problems at once: opacity reaches
    exactly zero at that boundary (so the tie stays invisible at rest,
    keeping the fan symmetric) while ramping smoothly as a card's distance
    changes (so nothing snaps to full brightness the instant an old hard
    cutoff was crossed).
    """

    COUNTS = (1, 2, 3, 4, 5, 6, 7, 12, 20)

    def test_left_and_right_counts_are_equal_at_rest(self) -> None:
        """The steady-state fan -- what the visitor actually looks at -- must
        never be lopsided, for any catalogue size."""
        for count in self.COUNTS:
            for selected in range(count):
                with self.subTest(count=count, selected=selected):
                    slots = CoverFlowView.visible_slots(float(selected), count)
                    left = sum(1 for _, distance, _ in slots if distance < -1e-9)
                    right = sum(1 for _, distance, _ in slots if distance > 1e-9)
                    self.assertEqual(
                        left, right, f"lopsided fan at count={count} selected={selected}"
                    )

    def test_no_index_is_ever_drawn_twice(self) -> None:
        for count in self.COUNTS:
            for scroll in (0.0, 0.35, 1.0, count - 0.5):
                if scroll < 0 or scroll >= count:
                    continue
                with self.subTest(count=count, scroll=scroll):
                    slots = CoverFlowView.visible_slots(scroll, count)
                    indices = [index for index, _, _ in slots]
                    self.assertEqual(
                        len(indices), len(set(indices)), "a card rendered twice"
                    )

    def test_small_counts_have_no_gap_or_duplicate(self) -> None:
        """1 or 2 games: just the hero, cleanly -- never a lone empty side."""
        for count in (1, 2):
            with self.subTest(count=count):
                slots = CoverFlowView.visible_slots(0.0, count)
                self.assertEqual([index for index, _, _ in slots], [0])

    def test_the_window_never_reaches_the_ambiguous_exact_half(self) -> None:
        """The window must always stay strictly under count/2, at rest and
        mid-scroll, and nothing rendered may carry a non-zero opacity there."""
        for count in self.COUNTS:
            for scroll in (0.0, 0.35, 1.0, count - 0.5):
                if scroll < 0 or scroll >= count:
                    continue
                with self.subTest(count=count, scroll=scroll):
                    for _, distance, alpha in CoverFlowView.visible_slots(scroll, count):
                        self.assertLess(abs(distance), count / 2)
                        self.assertGreater(alpha, 0.0)

    def test_odd_counts_show_every_card(self) -> None:
        """Odd counts have no exact-half tie, so nothing needs hiding."""
        for count in (3, 5, 7):
            with self.subTest(count=count):
                slots = CoverFlowView.visible_slots(0.0, count)
                self.assertEqual(len(slots), count)

    def test_alpha_reaches_exactly_zero_at_the_symmetric_boundary(self) -> None:
        fade_start, window_limit = edge_window(float(MAX_DEPTH), 6, 1.0)
        self.assertEqual(edge_alpha(window_limit, fade_start, window_limit), 0.0)
        self.assertGreater(window_limit, fade_start)

    def test_alpha_is_genuinely_mid_fade_not_snapping(self) -> None:
        """Regression for the pop: an entering/exiting card's opacity must be
        strictly between 0 and full, not one or the other."""
        count = 6
        fade_start, window_limit = edge_window(float(MAX_DEPTH), count, 1.0)
        target_distance = (fade_start + window_limit) / 2.0
        scroll = 3.0 - target_distance
        slots = {index: alpha for index, _, alpha in CoverFlowView.visible_slots(scroll, count)}
        self.assertIn(3, slots)
        self.assertGreater(slots[3], 0.0)
        self.assertLess(slots[3], 1.0)


class CarouselFadeTests(unittest.TestCase):
    """The same pop/symmetry fix, applied to Carousel's neighbours."""

    COUNTS = (1, 2, 3, 4, 5, 6, 7, 12, 20)

    def test_left_and_right_counts_are_equal_at_rest(self) -> None:
        for count in self.COUNTS:
            for selected in range(count):
                with self.subTest(count=count, selected=selected):
                    slots = CarouselView.visible_slots(float(selected), count)
                    left = sum(1 for _, distance, _ in slots if distance < -1e-9)
                    right = sum(1 for _, distance, _ in slots if distance > 1e-9)
                    self.assertEqual(
                        left, right, f"lopsided stage at count={count} selected={selected}"
                    )

    def test_no_index_is_ever_drawn_twice(self) -> None:
        for count in self.COUNTS:
            for scroll in (0.0, 0.35, 1.0, count - 0.5):
                if scroll < 0 or scroll >= count:
                    continue
                with self.subTest(count=count, scroll=scroll):
                    slots = CarouselView.visible_slots(scroll, count)
                    indices = [index for index, _, _ in slots]
                    self.assertEqual(
                        len(indices), len(set(indices)), "a card rendered twice"
                    )

    def test_the_window_never_reaches_the_ambiguous_exact_half(self) -> None:
        for count in self.COUNTS:
            for scroll in (0.0, 0.35, 1.0, count - 0.5):
                if scroll < 0 or scroll >= count:
                    continue
                with self.subTest(count=count, scroll=scroll):
                    for _, distance, alpha in CarouselView.visible_slots(scroll, count):
                        self.assertLess(abs(distance), count / 2)
                        self.assertGreater(alpha, 0.0)

    def test_alpha_reaches_exactly_zero_at_the_symmetric_boundary(self) -> None:
        fade_start, window_limit = edge_window(NEIGHBOUR_CEILING, 4, 1.0)
        self.assertEqual(edge_alpha(window_limit, fade_start, window_limit), 0.0)
        self.assertGreater(window_limit, fade_start)

    def test_alpha_is_genuinely_mid_fade_not_snapping(self) -> None:
        count = 6
        fade_start, window_limit = edge_window(NEIGHBOUR_CEILING, count, 1.0)
        target_distance = (fade_start + window_limit) / 2.0
        scroll = 3.0 - target_distance
        slots = {index: alpha for index, _, alpha in CarouselView.visible_slots(scroll, count)}
        self.assertIn(3, slots)
        self.assertGreater(slots[3], 0.0)
        self.assertLess(slots[3], 1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
