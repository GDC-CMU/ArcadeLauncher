"""Preview animation decoding, caps, and the rendering-layer integration.

Covers :mod:`launcher.ui.preview` (decode-time caps that need real image
data) plus how :class:`~launcher.ui.components.RenderContext` and
:func:`~launcher.ui.components.card_cover` play a decoded preview inside a
card's art area. Schema/containment validation lives in
``tests/test_preview_manifest.py``; this file is the Pygame half.
"""

from __future__ import annotations

import json

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import unittest
from unittest.mock import patch

from launcher.ui import SCREEN_SIZE
from launcher.ui.components import RenderContext, card_cover
from launcher.ui.preview import (
    MAX_PREVIEW_FRAME_DIMENSION,
    MAX_PREVIEW_TOTAL_DECODED_BYTES,
    PreviewAnimation,
    PreviewLibrary,
)
from launcher.ui.pygame_runtime import pygame
from launcher.ui.viewmodel import Card
from launcher.status import GameState, GameStatus

from support import TempDirCase, entry


def _write_frame(path, size, color) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    surface = pygame.Surface(size)
    surface.fill(color)
    pygame.image.save(surface, str(path))


def _write_preview(checkout, *, fps=8, frame_sizes=((32, 24), (32, 24))) -> None:
    preview_dir = checkout / "assets" / "preview"
    names = []
    for index, size in enumerate(frame_sizes):
        name = f"frame_{index:03d}.png"
        colour = (10 * index % 256, 20 * index % 256, 30 * index % 256)
        _write_frame(preview_dir / name, size, colour)
        names.append(name)
    manifest = {"version": 1, "fps": fps, "frames": names}
    (preview_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class HeadlessDisplayCase(TempDirCase):
    """A real (dummy-driver) display, so ``convert_alpha`` behaves like the
    cabinet, plus a scratch directory for building fixture checkouts."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        pygame.display.init()
        pygame.font.init()
        cls.screen = pygame.display.set_mode(SCREEN_SIZE)

    @classmethod
    def tearDownClass(cls) -> None:
        pygame.display.quit()
        pygame.font.quit()
        pygame.quit()
        super().tearDownClass()


class PreviewDecodeTests(HeadlessDisplayCase):
    def _checkout(self) -> tuple:
        game = entry(id="fixture-preview-game")
        checkout = self.tmp_path / "games" / game.id
        return game, checkout

    def test_valid_preview_decodes(self) -> None:
        game, checkout = self._checkout()
        _write_preview(checkout, fps=8, frame_sizes=((32, 24), (32, 24), (32, 24)))
        library = PreviewLibrary(self.tmp_path)
        animation = library.get(game)
        self.assertIsNotNone(animation)
        self.assertEqual(animation.fps, 8)
        self.assertEqual(len(animation.frames), 3)
        for surface in animation.frames:
            self.assertEqual(surface.get_size(), (32, 24))

    def test_missing_preview_returns_none(self) -> None:
        game, checkout = self._checkout()
        checkout.mkdir(parents=True)
        library = PreviewLibrary(self.tmp_path)
        self.assertIsNone(library.get(game))

    def test_frame_over_the_dimension_cap_is_rejected(self) -> None:
        game, checkout = self._checkout()
        oversized = MAX_PREVIEW_FRAME_DIMENSION + 1
        _write_preview(checkout, frame_sizes=((32, 24), (oversized, 24)))
        library = PreviewLibrary(self.tmp_path)
        self.assertIsNone(library.get(game))

    def test_frame_at_the_dimension_cap_is_accepted(self) -> None:
        game, checkout = self._checkout()
        _write_preview(
            checkout, frame_sizes=((MAX_PREVIEW_FRAME_DIMENSION, 4),)
        )
        library = PreviewLibrary(self.tmp_path)
        self.assertIsNotNone(library.get(game))

    def test_total_decoded_bytes_over_the_cap_is_rejected(self) -> None:
        game, checkout = self._checkout()
        # Each frame decodes to width*height*4 bytes; pick a size and frame
        # count whose product safely clears the cap without individually
        # tripping the per-frame dimension cap.
        side = 400
        per_frame = side * side * 4
        frame_count = MAX_PREVIEW_TOTAL_DECODED_BYTES // per_frame + 2
        _write_preview(checkout, frame_sizes=tuple((side, side) for _ in range(frame_count)))
        library = PreviewLibrary(self.tmp_path)
        self.assertIsNone(library.get(game))

    def test_an_unreadable_frame_is_rejected(self) -> None:
        game, checkout = self._checkout()
        _write_preview(checkout, frame_sizes=((32, 24),))
        # Corrupt the one frame the manifest names after writing it validly.
        (checkout / "assets" / "preview" / "frame_000.png").write_bytes(b"not a png")
        library = PreviewLibrary(self.tmp_path)
        self.assertIsNone(library.get(game))

    def test_frame_index_advances_at_the_manifest_fps(self) -> None:
        game, checkout = self._checkout()
        _write_preview(checkout, fps=10, frame_sizes=((32, 24),) * 4)
        library = PreviewLibrary(self.tmp_path)
        animation = library.get(game)
        # 10fps -> 100ms per frame.
        self.assertEqual(animation.frame_index(0), 0)
        self.assertEqual(animation.frame_index(99), 0)
        self.assertEqual(animation.frame_index(100), 1)
        self.assertEqual(animation.frame_index(250), 2)
        self.assertEqual(animation.frame_index(399), 3)
        self.assertEqual(animation.frame_index(400), 0)  # a full loop back to the start

    def test_the_library_never_rereads_after_the_first_load(self) -> None:
        game, checkout = self._checkout()
        _write_preview(checkout)
        library = PreviewLibrary(self.tmp_path)
        first = library.get(game)
        self.assertIsNotNone(first)
        # Remove the checkout entirely; a cached result must survive this.
        (checkout / "assets" / "preview" / "manifest.json").unlink()
        second = library.get(game)
        self.assertIs(second, first)

    def test_a_rejected_preview_is_also_remembered(self) -> None:
        game, checkout = self._checkout()
        _write_preview(checkout, frame_sizes=((MAX_PREVIEW_FRAME_DIMENSION + 1, 4),))
        library = PreviewLibrary(self.tmp_path)
        self.assertIsNone(library.get(game))
        with patch("launcher.ui.preview.load_preview_manifest") as loader:
            self.assertIsNone(library.get(game))
            loader.assert_not_called()

    def test_coming_soon_games_never_attempt_a_load(self) -> None:
        game = entry(
            id="coming-soon-fixture",
            launchable=False,
            repository=None,
            ref=None,
            entrypoint=None,
        )
        library = PreviewLibrary(self.tmp_path)
        with patch("launcher.ui.preview.load_preview_manifest") as loader:
            self.assertIsNone(library.get(game))
            loader.assert_not_called()


def _card(status: GameStatus = GameStatus.READY, **entry_kwargs) -> Card:
    game = entry(**entry_kwargs)
    return Card(entry=game, state=GameState(game.id, status, ""))


class RenderContextPreviewTests(HeadlessDisplayCase):
    def test_no_preview_returns_none(self) -> None:
        ctx = RenderContext()
        card = _card()
        self.assertIsNone(ctx.preview_frame_index(card.entry, 0))

    def test_a_registered_preview_resolves_a_frame_index(self) -> None:
        ctx = RenderContext()
        card = _card()
        frame_a = pygame.Surface((16, 12))
        frame_a.fill((200, 10, 10))
        frame_b = pygame.Surface((16, 12))
        frame_b.fill((10, 200, 10))
        ctx.previews._animations[card.entry.id] = PreviewAnimation(
            fps=1, frames=(frame_a, frame_b)
        )
        self.assertEqual(ctx.preview_frame_index(card.entry, 0), 0)
        self.assertEqual(ctx.preview_frame_index(card.entry, 1000), 1)

    def test_preview_surface_is_scaled_and_cached(self) -> None:
        ctx = RenderContext()
        card = _card()
        source = pygame.Surface((16, 12))
        source.fill((200, 10, 10))
        ctx.previews._animations[card.entry.id] = PreviewAnimation(fps=1, frames=(source,))
        surface = ctx.preview_surface(card.entry, 0, (64, 48))
        self.assertEqual(surface.get_size(), (64, 48))
        again = ctx.preview_surface(card.entry, 0, (64, 48))
        self.assertIs(again, surface, "the same (game, frame, size) must not rebuild")


class CardCoverPreviewTests(HeadlessDisplayCase):
    def _rect(self) -> pygame.Rect:
        return pygame.Rect(0, 0, 120, 100)

    def test_previewing_card_shows_the_animation_frame_in_the_art_area(self) -> None:
        ctx = RenderContext()
        card = _card()
        preview_colour = (250, 5, 5)
        source = pygame.Surface((16, 12))
        source.fill(preview_colour)
        ctx.previews._animations[card.entry.id] = PreviewAnimation(fps=1, frames=(source,))

        surface = pygame.Surface(self._rect().size)
        card_cover(
            surface,
            ctx,
            self._rect(),
            card,
            selected=True,
            time_ms=0,
            preview_time_ms=0,
            show_title=False,
            show_badge=False,
        )
        # Away from the badge/border/title bands entirely -- the art area's
        # own centre -- so this only ever samples the substituted animation
        # frame, never chrome drawn on top of or beside it.
        rect = self._rect()
        centre = surface.get_at((rect.centerx, rect.centery))
        self.assertEqual(tuple(centre)[:3], preview_colour)

    def test_a_card_with_no_preview_falls_back_to_procedural_art(self) -> None:
        ctx = RenderContext()
        card = _card()
        with_preview = pygame.Surface(self._rect().size)
        card_cover(
            with_preview,
            ctx,
            self._rect(),
            card,
            selected=True,
            time_ms=0,
            preview_time_ms=1234,
        )
        without_preview = pygame.Surface(self._rect().size)
        card_cover(
            without_preview,
            ctx,
            self._rect(),
            card,
            selected=True,
            time_ms=0,
            preview_time_ms=None,
        )
        self.assertEqual(
            pygame.image.tostring(with_preview, "RGB"),
            pygame.image.tostring(without_preview, "RGB"),
            "a game with no usable preview must render identically whether "
            "or not attract asks it to preview -- never a blank or broken card",
        )

    def test_badge_and_border_are_unaffected_by_a_playing_preview(self) -> None:
        ctx = RenderContext()
        card = _card(status=GameStatus.UPDATING)
        source = pygame.Surface((16, 12))
        source.fill((1, 2, 3))
        ctx.previews._animations[card.entry.id] = PreviewAnimation(fps=1, frames=(source,))

        rect = self._rect()
        previewing = pygame.Surface(rect.size)
        card_cover(
            previewing, ctx, rect, card, selected=True, time_ms=500, preview_time_ms=0
        )
        baseline = pygame.Surface(rect.size)
        card_cover(
            baseline, ctx, rect, card, selected=True, time_ms=500, preview_time_ms=None
        )
        # Badge lives in the top-left corner, well clear of the art area.
        self.assertEqual(previewing.get_at((10, 10)), baseline.get_at((10, 10)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
