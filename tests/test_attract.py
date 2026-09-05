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

import atexit
import random
import shutil
import tempfile
from pathlib import Path

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


def _tick(controller, delta_ms, count, current_index=0, eligible=None):
    """Call ``controller.tick()`` with every game eligible by default.

    Most of the pure state-machine tests are about idle timing, scrolling,
    settling and cancellation -- not about *which* games attract may settle
    on (see :class:`EligibilityTests` for that) -- so they default to "every
    game qualifies" rather than repeating ``range(count)`` everywhere.
    """
    return controller.tick(delta_ms, count, current_index, range(count) if eligible is None else eligible)


class TriggeringTests(unittest.TestCase):
    def test_does_not_trigger_before_the_idle_delay(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=1000), rng=random.Random(1), navigate=_forward_only
        )
        for _ in range(9):
            self.assertIsNone(_tick(controller, 100, 6, 0))
        self.assertFalse(controller.active)

    def test_triggers_the_instant_the_idle_delay_is_reached(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=1000), rng=random.Random(1), navigate=_forward_only
        )
        for _ in range(9):
            _tick(controller, 100, 6, 0)
        snapshot = _tick(controller, 100, 6, 0)
        self.assertIsNotNone(snapshot)
        self.assertTrue(controller.active)

    def test_zero_games_never_triggers(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=10), rng=random.Random(1), navigate=_forward_only
        )
        for _ in range(50):
            self.assertIsNone(_tick(controller, 100, 0, 0))
        self.assertFalse(controller.active)

    def test_no_eligible_games_never_triggers(self) -> None:
        """Criterion B: attract must not engage with nothing worth showing,
        even when the catalogue itself is non-empty."""
        controller = AttractController(
            AttractConfig(idle_delay_ms=10), rng=random.Random(1), navigate=_forward_only
        )
        for _ in range(50):
            self.assertIsNone(controller.tick(100, 6, 0, ()))
        self.assertFalse(controller.active)

    def test_starts_from_the_callers_current_selection(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=10, step_interval_ms=1000, settle_ms=1000),
            rng=random.Random(1),
            navigate=_forward_only,
        )
        snapshot = _tick(controller, 10, 6, current_index=4)
        self.assertEqual(snapshot.index, 4)

    def test_reset_forgets_progress_towards_the_idle_delay(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=100), rng=random.Random(1), navigate=_forward_only
        )
        _tick(controller, 90, 6, 0)
        controller.reset()
        self.assertIsNone(_tick(controller, 90, 6, 0))


class EligibilityTests(unittest.TestCase):
    """Criterion B: attract only ever *settles* on an eligible game, even
    though scrolling still glides past every card, eligible or not."""

    def test_the_target_is_always_one_of_the_eligible_indices(self) -> None:
        eligible = (2, 4)
        for seed in range(30):
            controller = AttractController(
                AttractConfig(idle_delay_ms=0, step_interval_ms=5, settle_ms=20),
                rng=random.Random(seed),
                navigate=_forward_only,
            )
            snapshot = controller.tick(0, 6, 0, eligible)
            for _ in range(200):
                snapshot = controller.tick(5, 6, 0, eligible)
                if snapshot.phase is AttractPhase.SETTLED:
                    break
            with self.subTest(seed=seed):
                self.assertEqual(snapshot.phase, AttractPhase.SETTLED)
                self.assertIn(snapshot.index, eligible)

    def test_a_single_eligible_game_settles_and_stays_there(self) -> None:
        """No jarring, scroll-less view-mode cycling with nothing else to
        show -- see the module docstring's note on this in ``_advance``."""
        controller = AttractController(
            AttractConfig(idle_delay_ms=0, step_interval_ms=5, settle_ms=20),
            rng=random.Random(3),
            navigate=_forward_only,
        )
        eligible = (3,)
        snapshot = controller.tick(0, 6, 0, eligible)
        for _ in range(50):
            snapshot = controller.tick(5, 6, 0, eligible)
            if snapshot.phase is AttractPhase.SETTLED:
                break
        self.assertEqual(snapshot.index, 3)
        settled_mode = snapshot.view_mode
        # Drive it hard past several settle windows -- it must never move on.
        for _ in range(400):
            snapshot = controller.tick(50, 6, 0, eligible)
            self.assertEqual(snapshot.index, 3)
            self.assertIs(snapshot.view_mode, settled_mode)

    def test_becoming_ineligible_mid_demo_stops_attract(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=0, step_interval_ms=5, settle_ms=20),
            rng=random.Random(1),
            navigate=_forward_only,
        )
        controller.tick(0, 6, 0, (0, 1, 2))
        self.assertTrue(controller.active)
        snapshot = controller.tick(10, 6, 0, ())
        self.assertIsNone(snapshot)
        self.assertFalse(controller.active)


class ScrollAndSettleTests(unittest.TestCase):
    def _triggered(self, **config_kwargs) -> AttractController:
        controller = AttractController(
            AttractConfig(idle_delay_ms=0, **config_kwargs),
            rng=random.Random(7),
            navigate=_forward_only,
        )
        _tick(controller, 0, 6, 0)
        return controller

    def test_scrolling_advances_one_step_per_interval(self) -> None:
        controller = self._triggered(step_interval_ms=100, settle_ms=1000)
        indices = [_tick(controller, 0, 6, 0).index]
        for _ in range(20):
            snapshot = _tick(controller, 100, 6, 0)
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
            snapshot = _tick(controller, 10, 6, 0)
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
            snapshot = _tick(controller, 10, 6, 0)
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
            snapshot = _tick(controller, 50, 6, 0)
            seen_modes.add(snapshot.view_mode)
        # Over enough legs the view mode must actually have changed at least
        # once -- attract never lingers on one composition forever (with
        # more than one eligible game -- see EligibilityTests for the
        # single-game case, which deliberately never changes mode).
        self.assertGreater(len(seen_modes), 1)

    def test_a_single_game_catalogue_never_errors_and_settles_on_it(self) -> None:
        controller = self._triggered(step_interval_ms=10, settle_ms=50)
        snapshot = None
        for _ in range(50):
            snapshot = _tick(controller, 10, 1, 0)
        self.assertEqual(snapshot.index, 0)

    def test_navigate_is_never_called_with_any_direction_but_right(self) -> None:
        """Reuse, not reinvention: the injected navigate stub asserts this
        itself (see :func:`_forward_only`) -- this test just exercises enough
        ticks that a wrong direction would have already tripped it."""
        controller = self._triggered(step_interval_ms=5, settle_ms=30)
        for _ in range(500):
            _tick(controller, 5, 6, 0)


class CancellationTests(unittest.TestCase):
    def test_notice_input_reports_whether_attract_was_active(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=10), rng=random.Random(1), navigate=_forward_only
        )
        self.assertFalse(controller.notice_input(), "was never active")
        _tick(controller, 20, 6, 0)
        self.assertTrue(controller.active)
        self.assertTrue(controller.notice_input(), "was active until this call")
        self.assertFalse(controller.active)

    def test_a_cancelled_controller_needs_the_full_idle_delay_again(self) -> None:
        controller = AttractController(
            AttractConfig(idle_delay_ms=100), rng=random.Random(1), navigate=_forward_only
        )
        _tick(controller, 100, 6, 0)
        self.assertTrue(controller.active)
        controller.notice_input()
        self.assertIsNone(_tick(controller, 90, 6, 0))
        self.assertIsNotNone(_tick(controller, 10, 6, 0))

    def test_a_cancelled_controller_triggers_again_and_can_repeat_this_twice(
        self,
    ) -> None:
        """Criterion C, at the pure-logic level: cancelling attract is never
        a one-shot -- the same controller must re-trigger every time it goes
        idle again, repeatably."""
        controller = AttractController(
            AttractConfig(idle_delay_ms=50), rng=random.Random(9), navigate=_forward_only
        )
        for cycle in range(3):
            with self.subTest(cycle=cycle):
                self.assertIsNone(_tick(controller, 40, 6, 0))
                self.assertIsNotNone(_tick(controller, 20, 6, 0))
                self.assertTrue(controller.active)
                self.assertTrue(controller.notice_input())
                self.assertFalse(controller.active)


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
                snapshot = _tick(controller, 10, 6, 0)
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
                snapshot = _tick(controller, 10, 6, 0)
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


#: An empty, isolated cache root -- never the repository's real
#: ``.arcade-cache`` -- so a game's *actual* on-disk preview (or lack of one)
#: can never make these tests depend on what happens to be checked out on
#: the machine running them. Every test that wants a game to have a preview
#: injects one directly via :func:`_give_previews`, bypassing disk entirely.
_EMPTY_CACHE_ROOT = Path(tempfile.mkdtemp(prefix="attract-test-cache-"))
atexit.register(shutil.rmtree, _EMPTY_CACHE_ROOT, True)


def _give_previews(game, game_ids=None) -> None:
    """Register a fake, always-available preview for *game_ids*.

    Defaults to every launchable game in the manifest. Most of the gallery
    tests below are about idle timing, cancellation and re-arming -- not
    about preview *eligibility* (see ``EligibilityIntegrationTests``) -- and
    since attract can only ever settle on an eligible game, those tests need
    at least one to look "watchable" or attract could never trigger at all.

    ``game.renderer`` does not exist yet at the point every caller uses this
    (before the session has opened its display for the first time -- see the
    attribute docstring on ``GallerySession.renderer``), so the injection is
    deferred to :meth:`~launcher.gallery.GallerySession._on_renderer_ready`,
    which fires once a fresh ``RenderContext`` actually exists.
    """
    ids = game_ids if game_ids is not None else [g.id for g in gt.MANIFEST if g.launchable]

    def inject(session) -> None:
        for game_id in ids:
            session.renderer.ctx.previews._animations[game_id] = PreviewAnimation(
                fps=8, frames=tuple(pygame.Surface((4, 3)) for _ in range(8))
            )

    game.on_renderer_ready_hooks.append(inject)


def _make_session(script, *, attract_idle_ms=1000, attract_rng=None, give_previews=True, **kwargs):
    settings = Settings(
        fullscreen=False,
        frame_rate=60,
        sync_on_start=False,
        attract_idle_ms=attract_idle_ms,
    )
    kwargs.setdefault("cache_root", _EMPTY_CACHE_ROOT)
    game = gt.ScriptedSession(
        gt.MANIFEST,
        settings,
        _states(),
        None,
        script=script,
        attract_rng=attract_rng or random.Random(1234),
        **kwargs,
    )
    if give_previews:
        _give_previews(game)
    return game


def _record(game) -> list:
    """Capture every :class:`GalleryFrame` the session renders, in order.

    Wraps ``renderer.draw`` -- but the renderer is rebuilt fresh every time
    the session opens its display (see the attribute docstring on
    ``GallerySession.renderer``), so the wrap is (re)applied via
    ``_on_renderer_ready`` rather than just once, here, on whatever renderer
    happens to exist at call time (there may not be one yet at all).
    """
    recorded: list = []

    def wrap(session) -> None:
        original = session.renderer.draw

        def spy(surface, frame):
            recorded.append(frame)
            return original(surface, frame)

        session.renderer.draw = spy

    game.on_renderer_ready_hooks.append(wrap)
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
        # give_previews defaults to True: every launchable game (the only
        # possible attract targets -- see EligibilityIntegrationTests) gets
        # a fake preview, so whichever one attract lands on (random, seeded)
        # has something to animate.
        game = _make_session(script, attract_idle_ms=idle_ms)
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


class EligibilityIntegrationTests(unittest.TestCase):
    """Criterion B, wired through the real gallery loop: only launchable,
    playable games with a usable preview are ever attract targets."""

    def test_with_no_eligible_game_attract_never_engages(self) -> None:
        idle_ms = 300
        needed = _frames_for(idle_ms)
        script = _idle_batches(needed + 400) + [[gt.key_event(pygame.K_ESCAPE)]]
        # give_previews=False: nothing in the manifest has a preview, so
        # nothing qualifies, however long the cabinet sits idle.
        game = _make_session(script, attract_idle_ms=idle_ms, give_previews=False)
        recorded = _record(game)
        outcome = game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertTrue(all(frame.preview is None for frame in recorded))
        self.assertTrue(all(frame.selected_index == 0 for frame in recorded))
        self.assertTrue(all(frame.view_mode is ViewMode.GRID for frame in recorded))

    def test_a_coming_soon_card_is_never_the_attract_target(self) -> None:
        """Every coming-soon entry gets a preview too here -- if eligibility
        only checked "has a preview" and not "is launchable", attract could
        still settle on one. It must not."""
        idle_ms = 300
        needed = _frames_for(idle_ms)
        script = _idle_batches(needed + 500) + [[gt.key_event(pygame.K_ESCAPE)]]
        game = _make_session(
            script, attract_idle_ms=idle_ms, give_previews=False
        )
        _give_previews(game, [g.id for g in gt.MANIFEST])  # every game, including coming-soon
        recorded = _record(game)
        game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        launchable_indices = {i for i, g in enumerate(gt.MANIFEST) if g.launchable}
        settled_indices = {
            f.selected_index for f in recorded if f.preview is not None
        }
        self.assertTrue(settled_indices, "attract must have settled on something")
        self.assertTrue(settled_indices <= launchable_indices)

    def test_a_game_with_no_preview_is_never_the_attract_target(self) -> None:
        """pacdawg only -- streetfighter is launchable and playable but,
        deliberately, given no preview here."""
        idle_ms = 300
        needed = _frames_for(idle_ms)
        script = _idle_batches(needed + 500) + [[gt.key_event(pygame.K_ESCAPE)]]
        game = _make_session(
            script, attract_idle_ms=idle_ms, give_previews=False
        )
        _give_previews(game, ["pacdawg"])
        recorded = _record(game)
        game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        settled_indices = {
            f.selected_index for f in recorded if f.preview is not None
        }
        pacdawg_index = next(i for i, g in enumerate(gt.MANIFEST) if g.id == "pacdawg")
        self.assertTrue(settled_indices, "attract must have settled on something")
        self.assertEqual(settled_indices, {pacdawg_index})

    def test_a_single_eligible_game_never_gets_jarring_mode_switches(self) -> None:
        idle_ms = 300
        needed = _frames_for(idle_ms)
        script = _idle_batches(needed + 700) + [[gt.key_event(pygame.K_ESCAPE)]]
        game = _make_session(
            script, attract_idle_ms=idle_ms, give_previews=False
        )
        _give_previews(game, ["pacdawg"])
        recorded = _record(game)
        game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        settled = [f for f in recorded if f.preview is not None]
        self.assertTrue(settled, "attract must have settled on the one eligible game")
        # Once settled it must never leave that mode again -- nothing else
        # to switch to without an instant, scroll-less jump.
        modes = {f.view_mode for f in settled}
        self.assertEqual(len(modes), 1)


class ReArmingTests(unittest.TestCase):
    """Criterion C: dismissing attract is never a one-shot -- the idle clock
    re-arms, so the gallery drops back into attract after another full idle
    period, repeatably, within the same session."""

    def test_two_full_cycles_of_attract_input_idle_attract(self) -> None:
        idle_ms = 300
        needed = _frames_for(idle_ms)
        wander_frames = 400  # room for at least one scroll step
        script = (
            _idle_batches(needed + wander_frames)
            + [[gt.button_event(gt.BUTTON_LAUNCH)]]  # cycle 1: dismiss
            + _idle_batches(needed + wander_frames)
            + [[gt.button_event(gt.BUTTON_LAUNCH)]]  # cycle 2: dismiss
            + [[gt.key_event(pygame.K_ESCAPE)]]
        )
        game = _make_session(script, attract_idle_ms=idle_ms)
        recorded = _record(game)
        outcome = game(SessionState(view_mode=ViewMode.GRID, selected_index=0))
        self.addCleanup(pygame.quit)
        self.assertIs(outcome.action, UiAction.QUIT)
        # Restored exactly as left, both times -- neither dismissal press
        # was mistaken for a launch (that would have ended the session then
        # and there) or for anything but a wake-up.
        self.assertEqual(outcome.selected_index, 0)
        self.assertIs(outcome.view_mode, ViewMode.GRID)

        cycle1_end = needed + wander_frames
        cycle2_start = cycle1_end + 1
        cycle2_end = cycle2_start + needed + wander_frames

        def diverged(frames) -> bool:
            # Any of these proves attract genuinely engaged: a different
            # mode, a different selection, or -- the robust one, since a
            # settle can coincidentally land back on the same mode/index --
            # a preview actually playing.
            return any(
                f.view_mode is not ViewMode.GRID
                or f.selected_index != 0
                or f.preview is not None
                for f in frames
            )

        self.assertTrue(
            diverged(recorded[:cycle1_end]),
            "attract must engage during the first idle window",
        )
        self.assertTrue(
            diverged(recorded[cycle2_start:cycle2_end]),
            "attract must engage again during the second idle window -- "
            "dismissing it once must never be the last time it ever runs",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
