"""Attract mode: the idle-triggered demo, pure state machine and the gallery
loop wired around it.

Pure-logic behaviour (triggering, scrolling, settling, cancelling) is tested
directly against :class:`~launcher.attract.AttractController` with a stub
navigate function -- no Pygame anywhere. The gallery-integration tests reuse
``ScriptedSession`` and the event helpers from ``tests.test_gallery`` to drive
the real event loop and observe the actual :class:`~launcher.ui.viewmodel.
GalleryFrame` objects it renders, which is the same public contract the
views themselves draw from.
"""

from __future__ import annotations

import random

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import unittest

from launcher.attract import AttractConfig, AttractController, AttractPhase
from launcher.input_state import Direction
from launcher.settings import Settings
from launcher.status import GameState, GameStatus
from launcher.supervisor import SessionState, UiAction
from launcher.ui.pygame_runtime import pygame
from launcher.ui.preview import PreviewAnimation
from launcher.viewmodes import ViewMode

import test_gallery as gt


# ---------------------------------------------------------------------------
# Pure state-machine tests -- no Pygame.
# ---------------------------------------------------------------------------
def _forward_only(mode: ViewMode, index: int, count: int, direction: Direction) -> int:
    """A stub standing in for a real ``GalleryView.navigate``: attract only
    ever steps :data:`~launcher.input_state.Direction.RIGHT`, so this is all
    the pure state-machine tests need."""
    assert direction is Direction.RIGHT
    return (index + 1) % count


class TriggeringTests(unittest.TestCase):
    def test_does_not_trigger_before_the_idle_delay(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=1000), rng=random.Random(1), navigate=_forward_only
        )
        for _ in range(9):
            self.assertIsNone(controller.tick(100, 6, 0))
        self.assertFalse(controller.active)

    def test_triggers_the_instant_the_idle_delay_is_reached(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=1000), rng=random.Random(1), navigate=_forward_only
        )
        for _ in range(9):
            controller.tick(100, 6, 0)
        snapshot = controller.tick(100, 6, 0)
        self.assertIsNotNone(snapshot)
        self.assertTrue(controller.active)

    def test_zero_games_never_triggers(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=10), rng=random.Random(1), navigate=_forward_only
        )
        for _ in range(50):
            self.assertIsNone(controller.tick(100, 0, 0))
        self.assertFalse(controller.active)

    def test_starts_from_the_callers_current_selection(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=10, step_interval_ms=1000, settle_ms=1000),
            rng=random.Random(1),
            navigate=_forward_only,
        )
        snapshot = controller.tick(10, 6, current_index=4)
        self.assertEqual(snapshot.index, 4)

    def test_reset_forgets_progress_towards_the_idle_delay(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=100), rng=random.Random(1), navigate=_forward_only
        )
        controller.tick(90, 6, 0)
        controller.reset()
        self.assertIsNone(controller.tick(90, 6, 0))


class ScrollAndSettleTests(unittest.TestCase):
    def _triggered(self, **config_kwargs) -> AttractController:
        controller = AttractController(
            AttractConfig(idle_delay_ms=0, **config_kwargs),
            rng=random.Random(7),
            navigate=_forward_only,
        )
        controller.tick(0, 6, 0)
        return controller

    def test_scrolling_advances_one_step_per_interval(self) -> None:
        controller = self._triggered(step_interval_ms=100, settle_ms=1000)
        indices = [controller.tick(0, 6, 0).index]
        for _ in range(20):
            snapshot = controller.tick(100, 6, 0)
            indices.append(snapshot.index)
            if snapshot.phase is AttractPhase.SETTLED:
                break
        # Every tick moves the index forward by exactly one slot (mod 6, the
        # stub's own rule) or leaves it alone -- never further, and never any
        # other direction -- until it settles on the leg's target.
        for before, after in zip(indices, indices[1:]):
            self.assertIn(after, {before, (before + 1) % 6})

    def test_eventually_settles_on_a_target(self) -> None:
        controller = self._triggered(step_interval_ms=10, settle_ms=1000)
        snapshot = None
        for _ in range(200):
            snapshot = controller.tick(10, 6, 0)
            if snapshot.phase is AttractPhase.SETTLED:
                break
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.phase, AttractPhase.SETTLED)
        self.assertEqual(snapshot.phase_elapsed_ms, 0, "the preview clock restarts at 0")

    def test_settle_clock_advances_and_a_new_leg_eventually_starts(self) -> None:
        controller = self._triggered(step_interval_ms=10, settle_ms=200)
        # Drive it to SETTLED first.
        snapshot = None
        for _ in range(200):
            snapshot = controller.tick(10, 6, 0)
            if snapshot.phase is AttractPhase.SETTLED:
                break
        self.assertEqual(snapshot.phase, AttractPhase.SETTLED)
        settled_mode = snapshot.view_mode

        # Advance through the whole settle window; a new leg must begin --
        # phase back to SCROLLING (or straight back to SETTLED if the new
        # random target happens to already be the current index) with a
        # freshly restarted animation clock either way.
        seen_modes = {settled_mode}
        for _ in range(50):
            snapshot = controller.tick(50, 6, 0)
            seen_modes.add(snapshot.view_mode)
        # Over enough legs the view mode must actually have changed at least
        # once -- attract never lingers on one composition forever.
        self.assertGreater(len(seen_modes), 1)

    def test_a_single_game_catalogue_never_errors_and_settles_on_it(self) -> None:
        controller = self._triggered(step_interval_ms=10, settle_ms=50)
        snapshot = None
        for _ in range(50):
            snapshot = controller.tick(10, 1, 0)
        self.assertEqual(snapshot.index, 0)

    def test_navigate_is_never_called_with_any_direction_but_right(self) -> None:
        """Reuse, not reinvention: the injected navigate stub asserts this
        itself (see :func:`_forward_only`) -- this test just exercises enough
        ticks that a wrong direction would have already tripped it."""
        controller = self._triggered(step_interval_ms=5, settle_ms=30)
        for _ in range(500):
            controller.tick(5, 6, 0)


class CancellationTests(unittest.TestCase):
    def test_notice_input_reports_whether_attract_was_active(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=10), rng=random.Random(1), navigate=_forward_only
        )
        self.assertFalse(controller.notice_input(), "was never active")
        controller.tick(20, 6, 0)
        self.assertTrue(controller.active)
        self.assertTrue(controller.notice_input(), "was active until this call")
        self.assertFalse(controller.active)

    def test_a_cancelled_controller_needs_the_full_idle_delay_again(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=100), rng=random.Random(1), navigate=_forward_only
        )
        controller.tick(100, 6, 0)
        self.assertTrue(controller.active)
        controller.notice_input()
        self.assertIsNone(controller.tick(90, 6, 0))
        self.assertIsNotNone(controller.tick(10, 6, 0))


class DeterminismTests(unittest.TestCase):
    def test_same_seed_produces_the_same_sequence(self) -> None:
        def run() -> list:
            controller = AttractController(
                AttractConfig(idle_delay_ms=0, step_interval_ms=10, settle_ms=40),
                rng=random.Random(2024),
                navigate=_forward_only,
            )
            trace = []
            for _ in range(300):
                snapshot = controller.tick(10, 6, 0)
                trace.append((snapshot.view_mode, snapshot.index, snapshot.phase))
            return trace

        self.assertEqual(run(), run())

    def test_different_seeds_can_diverge(self) -> None:
        def run(seed: int) -> list:
            controller = AttractController(
                AttractConfig(idle_delay_ms=0, step_interval_ms=10, settle_ms=40),
                rng=random.Random(seed),
                navigate=_forward_only,
            )
            trace = []
            for _ in range(300):
                snapshot = controller.tick(10, 6, 0)
                trace.append((snapshot.view_mode, snapshot.index))
            return trace

        self.assertNotEqual(run(1), run(2))


# ---------------------------------------------------------------------------
# Gallery integration -- the real event loop, scripted input.
# ---------------------------------------------------------------------------
def _states() -> dict[str, GameState]:
    return {
        game.id: GameState(
            game.id, GameStatus.READY if game.launchable else GameStatus.COMING_SOON, ""
        )
        for game in gt.MANIFEST
    }


def _idle_batches(frames: int) -> list:
    return [[] for _ in range(frames)]


def _make_session(script, *, attract_idle_ms=1000, attract_rng=None, **kwargs):
    settings = Settings(
        fullscreen=False,
        frame_rate=60,
        sync_on_start=False,
        attract_idle_ms=attract_idle_ms,
    )
    game = gt.ScriptedSession(
        gt.MANIFEST,
        settings,
        _states(),
        None,
        script=script,
        attract_rng=attract_rng or random.Random(1234),
        **kwargs,
    )
    return game


def _record(game) -> list:
    """Capture every :class:`GalleryFrame` the session renders, in order."""
    recorded: list = []
    original = game.renderer.draw

    def spy(surface, frame):
        recorded.append(frame)
        return original(surface, frame)

    game.renderer.draw = spy
    return recorded


#: One frame at ScriptedSession's fixed 16ms/frame clock.
FRAME_MS = gt.ScriptedSession.FRAME_MS


def _frames_for(idle_ms: int) -> int:
    """How many empty frames it takes ``idle_ms`` to elapse at FRAME_MS."""
    import math

    return math.ceil(idle_ms / FRAME_MS)


class GalleryTriggerTests(unittest.TestCase):
    def test_attract_does_not_engage_before_the_configured_idle(self) -> None:
        idle_ms = 500
        needed = _frames_for(idle_ms)
        # One frame short of the threshold, then quit.
        script = _idle_batches(needed - 1) + [[gt.key_event(pygame.K_ESCAPE)]]
        game = _make_session(script, attract_idle_ms=idle_ms)
        recorded = _record(game)
        outcome = game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertTrue(all(frame.preview is None for frame in recorded))
        self.assertTrue(all(frame.selected_index == 0 for frame in recorded))
        self.assertTrue(all(frame.view_mode is ViewMode.GRID for frame in recorded))

    def test_attract_eventually_settles_and_plays_a_preview(self) -> None:
        idle_ms = 300
        needed = _frames_for(idle_ms)
        extra_idle = 400  # give it room to scroll and settle
        script = _idle_batches(needed + extra_idle) + [[gt.key_event(pygame.K_ESCAPE)]]
        game = _make_session(script, attract_idle_ms=idle_ms)
        # Every game gets a fake preview so whichever one attract lands on
        # (random, seeded) has something to animate.
        for entry in gt.MANIFEST:
            game.renderer.ctx.previews._animations[entry.id] = PreviewAnimation(
                fps=8, frames=tuple(pygame.Surface((4, 3)) for _ in range(8))
            )
        recorded = _record(game)
        game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertTrue(
            any(frame.preview is not None for frame in recorded),
            "attract must eventually settle on a game and play its preview",
        )


class GalleryCancellationTests(unittest.TestCase):
    def test_any_input_exits_attract_and_restores_the_prior_selection_and_view(
        self,
    ) -> None:
        idle_ms = 300
        needed = _frames_for(idle_ms)
        script = (
            # Move to a real selection and view mode before going idle.
            [[gt.key_event(pygame.K_RIGHT)]]
            + [[gt.key_event(pygame.K_RIGHT, down=False)]]
            + [[gt.key_event(pygame.K_RIGHT)]]
            + [[gt.key_event(pygame.K_RIGHT, down=False)]]
            + [[gt.button_event(gt.BUTTON_CYCLE_VIEW)]]
            + _idle_batches(needed + 40)
            # A launch press while attract is showing something else entirely:
            # must be consumed purely as a wake-up.
            + [[gt.button_event(gt.BUTTON_LAUNCH)]]
            + [[gt.key_event(pygame.K_ESCAPE)]]
        )
        game = _make_session(script, attract_idle_ms=idle_ms)
        outcome = game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertIs(
            outcome.action,
            UiAction.QUIT,
            "the launch press during attract must never have produced a launch",
        )
        self.assertEqual(outcome.selected_index, 2)
        self.assertIs(outcome.view_mode, ViewMode.GRID.next())

    def test_the_cancelling_press_is_never_also_treated_as_exit(self) -> None:
        """The interaction called out explicitly: dismissing attract with the
        P1/Escape button must not *also* count as the visitor's exit -- the
        gallery must still be running afterwards, requiring its own,
        separate exit press."""
        idle_ms = 300
        needed = _frames_for(idle_ms)
        script = (
            _idle_batches(needed + 40)
            + [[gt.key_event(pygame.K_ESCAPE)]]  # cancels attract only
            + [[gt.key_event(pygame.K_ESCAPE, down=False)]]
            + _idle_batches(10)
            + [[gt.key_event(pygame.K_ESCAPE)]]  # the real exit
        )
        game = _make_session(script, attract_idle_ms=idle_ms)
        recorded = _record(game)
        outcome = game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertIs(outcome.action, UiAction.QUIT)
        # If the first Escape had quit the gallery, the frames attract wandered
        # through (a different view mode/selection) would never have been
        # rendered at all -- but the whole point is that the session kept
        # running long enough for a *second*, later Escape to end it.
        self.assertGreater(game.frames, needed + 40 + 5)

    def test_axis_drift_below_the_deadzone_does_not_suppress_attract(self) -> None:
        idle_ms = 300
        needed = _frames_for(idle_ms)
        # Tiny, sub-deadzone jitter every frame -- never a real push.
        jitter_frames = [[gt.axis_event(0, 0.1)] for _ in range(needed + 40)]
        script = jitter_frames + [[gt.key_event(pygame.K_ESCAPE)]]
        game = _make_session(script, attract_idle_ms=idle_ms)
        recorded = _record(game)
        game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertTrue(
            any(
                frame.view_mode is not ViewMode.GRID or frame.selected_index != 0
                for frame in recorded
            ),
            "sub-deadzone stick drift must never hold off attract forever",
        )

    def test_a_real_axis_push_alone_cancels_attract_with_no_button_at_all(
        self,
    ) -> None:
        """The cabinet's stick generates no KEYDOWN/JOYBUTTONDOWN at all --
        navigation there is purely ``JOYAXISMOTION``. Attract must still be
        dismissible by that alone, with the selection and view mode restored
        exactly as if a button had done it."""
        idle_ms = 300
        needed = _frames_for(idle_ms)
        script = (
            [[gt.key_event(pygame.K_RIGHT)]]
            + [[gt.key_event(pygame.K_RIGHT, down=False)]]
            + [[gt.button_event(gt.BUTTON_CYCLE_VIEW)]]
            + _idle_batches(needed + 40)
            + [[gt.axis_event(0, 1.0)]]  # a genuine push past the deadzone
            + [[gt.axis_event(0, 0.0)]]
            + [[gt.key_event(pygame.K_ESCAPE)]]
        )
        game = _make_session(script, attract_idle_ms=idle_ms)
        outcome = game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertEqual(outcome.selected_index, 1)
        self.assertIs(outcome.view_mode, ViewMode.GRID.next())

    def test_input_during_ordinary_browsing_resets_the_idle_clock(self) -> None:
        """Not only cancellation: a steadily-active visitor who never quite
        goes idle must never see attract trigger at all -- the idle clock has
        to reset on *every* genuine press, not only once attract is already
        running."""
        idle_ms = 300
        needed = _frames_for(idle_ms)
        half = needed // 2
        # A press just under halfway through the idle window, repeated
        # forever, never lets the idle clock reach the threshold.
        script = []
        for _ in range(6):
            script += _idle_batches(half) + [[gt.key_event(pygame.K_RIGHT)]]
            script += [[gt.key_event(pygame.K_RIGHT, down=False)]]
        script += [[gt.key_event(pygame.K_ESCAPE)]]
        game = _make_session(script, attract_idle_ms=idle_ms)
        recorded = _record(game)
        game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertTrue(
            all(frame.preview is None for frame in recorded),
            "regular presses well inside the idle window must never let "
            "attract engage",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
