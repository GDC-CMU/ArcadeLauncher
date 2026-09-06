"""The gallery session: control mapping, SDL lifecycle, and refusals.

Runs the real event loop under headless SDL, feeding it synthetic events. No
joystick hardware is required and no repository is touched.
"""

from __future__ import annotations

import math
from typing import Callable

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import unittest

from launcher.controls import BUTTON_CYCLE_VIEW, BUTTON_EXIT, BUTTON_LAUNCH, Command
from launcher.gallery import (
    _EXIT_ARM_FALLBACK_GRACE_MS,
    KEY_COMMANDS,
    KEY_DIRECTIONS,
    GallerySession,
)
from launcher.input_state import Direction
from launcher.manifest import load_manifest
from launcher.settings import Settings
from launcher.status import GameState, GameStatus, Notice
from launcher.supervisor import SessionState, UiAction
from launcher.ui.pygame_runtime import pygame
from launcher.viewmodes import ViewMode

MANIFEST = load_manifest()

#: Scripted frames needed for GallerySession._sync_exit_arming's fallback
#: grace period to elapse, at ScriptedSession's fixed 16ms/frame clock.
#: Derived from the real constant rather than a hard-coded number so these
#: tests track it if it ever changes.
GRACE_FRAMES = math.ceil(_EXIT_ARM_FALLBACK_GRACE_MS / 16)


def key_event(key: int, down: bool = True) -> pygame.event.Event:
    return pygame.event.Event(
        pygame.KEYDOWN if down else pygame.KEYUP, key=key, mod=0, unicode="", scancode=0
    )


def button_event(button: int, instance_id: int = 0) -> pygame.event.Event:
    return pygame.event.Event(
        pygame.JOYBUTTONDOWN, button=button, instance_id=instance_id, joy=instance_id
    )


def button_up_event(button: int, instance_id: int = 0) -> pygame.event.Event:
    return pygame.event.Event(
        pygame.JOYBUTTONUP, button=button, instance_id=instance_id, joy=instance_id
    )


def axis_event(axis: int, value: float) -> pygame.event.Event:
    return pygame.event.Event(
        pygame.JOYAXISMOTION, axis=axis, value=value, instance_id=0, joy=0
    )


class FakeJoystick:
    """Minimal stand-in for ``pygame.joystick.Joystick``, for arming tests.

    Real hardware answers ``get_button`` immediately from the device itself,
    independently of window focus, unlike ``pygame.key.get_pressed()`` -- this
    fake mirrors that by returning whatever the test set, with no dependency
    on SDL or a real controller.
    """

    def __init__(self, instance_id: int, numbuttons: int = 6) -> None:
        self._instance_id = instance_id
        self._numbuttons = numbuttons

    def init(self) -> None:
        pass

    def quit(self) -> None:
        pass

    def get_instance_id(self) -> int:
        return self._instance_id

    def get_numbuttons(self) -> int:
        return self._numbuttons

    def get_button(self, button: int) -> bool:  # pragma: no cover - unused seam
        # _sync_exit_arming never reaches this in these tests: ScriptedSession
        # overrides _is_button_held instead, which is the only caller.
        return False

    def get_name(self) -> str:
        return f"fake-joystick-{self._instance_id}"


class ScriptedSession(GallerySession):
    """A session that plays a fixed event script, one batch per frame.

    Posting to SDL's real queue from another thread would make these tests
    timing-dependent; instead the script is injected where the loop reads.
    The frame clock is pinned for the same reason -- auto-repeat is driven by
    elapsed milliseconds, so a real clock would make the held-stick tests pass
    or fail depending on how busy the machine is.
    """

    #: One frame at 60 Hz.  Deterministic, whatever the host is doing.
    FRAME_MS = 16

    def __init__(
        self,
        *args,
        script,
        joysticks: tuple[FakeJoystick, ...] = (),
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._script = list(script)
        self.frames = 0
        self._joystick_fakes = list(joysticks)
        # Ground truth for the arming fallback seam (_is_key_held/
        # _is_button_held): what is down *as of the most recently pumped
        # frame*, derived solely from that frame's batch -- recomputed fresh
        # every _pump() call, never accumulated across frames. This is
        # deliberate and matches two things a test needs to model
        # separately: a single scripted KEYDOWN with no matching KEYUP is
        # the idiom the rest of this suite uses for "the visitor tapped and
        # let go", so it must count as held for that one frame only, not
        # forever; a *held* key/button, by contrast, is modelled by
        # repeating the KEYDOWN/JOYBUTTONDOWN in every consecutive frame's
        # batch, which keeps it in this set on every one of those frames
        # too. A batch with nothing in it (no events at all) correctly
        # reports nothing held -- exactly what a real, unfocused window's
        # input state looks like before it has started receiving input.
        self._keys_down_this_frame: set[int] = set()
        self._buttons_down_this_frame: set[tuple[int, int]] = set()
        #: How many consecutive frames the safety net below has been firing.
        #: Used only to alternate it between down and up -- see _pump.
        self._safety_net_frames = 0
        #: Called every time :meth:`_on_renderer_ready` fires -- i.e. every
        #: time ``_open_display`` (re)builds ``self.renderer`` -- so a test
        #: can register a fixture preview or wrap ``draw`` for recording at
        #: the one moment a fresh ``RenderContext`` is guaranteed to exist.
        #: A renderer built once in ``__init__`` used to make "patch it
        #: right after construction" work; now that it is rebuilt per
        #: session (see the attribute docstring on ``GallerySession.
        #: renderer``), anything a test wants to survive into the render
        #: loop has to be re-applied here instead.
        self.on_renderer_ready_hooks: list[Callable[["ScriptedSession"], None]] = []

    def _on_renderer_ready(self) -> None:  # type: ignore[override]
        for hook in self.on_renderer_ready_hooks:
            hook(self)

    def _tick(self, clock):  # type: ignore[override]
        return self.FRAME_MS

    def _pump(self):  # type: ignore[override]
        self.frames += 1
        if not self._script:
            # Safety net: never hang a test if the script forgot to exit.
            # Alternates down/up rather than repeating a bare KEYDOWN every
            # frame forever: under the per-frame tracking above, a KEYDOWN
            # repeated indefinitely *is* an indefinite hold, which the fix
            # this suite is pinned to correctly refuses to ever arm --
            # exactly the deadlock this alternation exists to avoid. The
            # up half of each cycle is also what a genuinely stuck test
            # needs to actually reach QUIT rather than hang forever.
            self._safety_net_frames += 1
            down = self._safety_net_frames % 2 == 1
            batch = [key_event(pygame.K_ESCAPE, down=down)]
        else:
            batch = self._script.pop(0)
        keys_down = set()
        buttons_down = set()
        for event in batch:
            if event.type == pygame.KEYDOWN:
                keys_down.add(event.key)
            elif event.type == pygame.KEYUP:
                keys_down.discard(event.key)
            elif event.type == pygame.JOYBUTTONDOWN:
                buttons_down.add((event.instance_id, event.button))
            elif event.type == pygame.JOYBUTTONUP:
                buttons_down.discard((event.instance_id, event.button))
        self._keys_down_this_frame = keys_down
        self._buttons_down_this_frame = buttons_down
        return batch

    def _joystick_count(self):  # type: ignore[override]
        return len(self._joystick_fakes)

    def _open_joystick(self, index: int):  # type: ignore[override]
        return self._joystick_fakes[index]

    def _is_key_held(self, key: int) -> bool:  # type: ignore[override]
        return key in self._keys_down_this_frame

    def _is_button_held(self, instance_id: int, button: int) -> bool:  # type: ignore[override]
        return (instance_id, button) in self._buttons_down_this_frame


def session(
    script,
    *,
    states=None,
    default_view=ViewMode.GRID,
    joysticks=(),
) -> ScriptedSession:
    settings = Settings(
        default_view=default_view, fullscreen=False, frame_rate=60, sync_on_start=False
    )
    live = states or {
        game.id: GameState(
            game.id,
            GameStatus.READY if game.launchable else GameStatus.COMING_SOON,
            "",
        )
        for game in MANIFEST
    }
    return ScriptedSession(
        MANIFEST,
        settings,
        live,
        None,
        script=script,
        joysticks=joysticks,
    )


class MappingTests(unittest.TestCase):
    """Criterion E1/E2: the documented arcade map and its keyboard mirror."""

    def test_escape_exits(self) -> None:
        self.assertIs(KEY_COMMANDS[pygame.K_ESCAPE], Command.EXIT)

    def test_enter_and_space_launch(self) -> None:
        self.assertIs(KEY_COMMANDS[pygame.K_RETURN], Command.LAUNCH)
        self.assertIs(KEY_COMMANDS[pygame.K_SPACE], Command.LAUNCH)

    def test_tab_cycles_views(self) -> None:
        self.assertIs(KEY_COMMANDS[pygame.K_TAB], Command.CYCLE_VIEW)

    def test_number_keys_select_modes_directly(self) -> None:
        self.assertIs(KEY_COMMANDS[pygame.K_1], Command.VIEW_GRID)
        self.assertIs(KEY_COMMANDS[pygame.K_2], Command.VIEW_CAROUSEL)
        self.assertIs(KEY_COMMANDS[pygame.K_3], Command.VIEW_COVER_FLOW)

    def test_arrows_and_wasd_agree(self) -> None:
        self.assertIs(KEY_DIRECTIONS[pygame.K_LEFT], KEY_DIRECTIONS[pygame.K_a])
        self.assertIs(KEY_DIRECTIONS[pygame.K_UP], Direction.UP)

    def test_command_lookup_matches_button_numbers(self) -> None:
        self.assertIs(GallerySession._command_for(button_event(BUTTON_EXIT)), Command.EXIT)
        self.assertIs(
            GallerySession._command_for(button_event(BUTTON_LAUNCH)), Command.LAUNCH
        )
        self.assertIs(
            GallerySession._command_for(button_event(BUTTON_CYCLE_VIEW)),
            Command.CYCLE_VIEW,
        )

    def test_unmapped_events_produce_no_command(self) -> None:
        self.assertIsNone(GallerySession._command_for(axis_event(0, 1.0)))
        self.assertIsNone(GallerySession._command_for(button_event(4)))


class LoopTests(unittest.TestCase):
    """Drive the real loop and assert on the outcome it hands the supervisor."""

    def run_session(self, script, **kwargs):
        game = session(script, **kwargs)
        outcome = game(SessionState(view_mode=kwargs.get("default_view", ViewMode.GRID)))
        self.addCleanup(pygame.quit)
        return game, outcome

    def test_exit_button_quits_the_gallery(self) -> None:
        """Criterion F6: P1 in the gallery ends the session."""
        _, outcome = self.run_session([[button_event(BUTTON_EXIT)]])
        self.assertIs(outcome.action, UiAction.QUIT)

    def test_escape_quits_the_gallery(self) -> None:
        _, outcome = self.run_session([[key_event(pygame.K_ESCAPE)]])
        self.assertIs(outcome.action, UiAction.QUIT)

    def test_window_close_quits(self) -> None:
        _, outcome = self.run_session([[pygame.event.Event(pygame.QUIT)]])
        self.assertIs(outcome.action, UiAction.QUIT)

    def test_launch_reports_the_selected_game(self) -> None:
        _, outcome = self.run_session([[button_event(BUTTON_LAUNCH)]])
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(outcome.game_id, "streetfighter")
        self.assertEqual(outcome.selected_index, 0)

    def test_navigation_then_launch_picks_the_new_card(self) -> None:
        script = [
            [key_event(pygame.K_RIGHT)],
            [key_event(pygame.K_RIGHT, down=False)],
            [key_event(pygame.K_ESCAPE)],
        ]
        _, outcome = self.run_session(script)
        self.assertEqual(outcome.selected_index, 1)

    def test_coming_soon_card_refuses_to_launch(self) -> None:
        """Criterion E6: pressing launch on a disabled game explains itself."""
        # Step to the first coming-soon entry rather than assuming its index,
        # since the second card is now the launchable PacDawg.
        target = next(index for index, game in enumerate(MANIFEST) if not game.launchable)
        script = []
        for _ in range(target):
            script.append([key_event(pygame.K_RIGHT)])
            script.append([key_event(pygame.K_RIGHT, down=False)])
        script.append([button_event(BUTTON_LAUNCH)])
        script.append([key_event(pygame.K_ESCAPE)])
        _, outcome = self.run_session(script)
        self.assertIs(outcome.action, UiAction.QUIT, "a disabled game must not launch")

    def test_unavailable_game_refuses_to_launch(self) -> None:
        states = {
            game.id: GameState(game.id, GameStatus.UNAVAILABLE, "no network")
            for game in MANIFEST
        }
        script = [[button_event(BUTTON_LAUNCH)], [key_event(pygame.K_ESCAPE)]]
        _, outcome = self.run_session(script, states=states)
        self.assertIs(outcome.action, UiAction.QUIT)

    def test_cycle_view_advances_the_mode_and_keeps_the_selection(self) -> None:
        script = [
            [key_event(pygame.K_RIGHT)],
            [key_event(pygame.K_RIGHT, down=False)],
            [button_event(BUTTON_CYCLE_VIEW)],
            [key_event(pygame.K_ESCAPE)],
        ]
        _, outcome = self.run_session(script)
        self.assertIs(outcome.view_mode, ViewMode.GRID.next())
        self.assertEqual(outcome.selected_index, 1)

    def test_direct_mode_selection(self) -> None:
        script = [[key_event(pygame.K_3)], [key_event(pygame.K_ESCAPE)]]
        _, outcome = self.run_session(script)
        self.assertIs(outcome.view_mode, ViewMode.COVER_FLOW)

    def test_joystick_axis_navigates(self) -> None:
        script = [
            [axis_event(0, 1.0)],
            [axis_event(0, 0.0)],
            [key_event(pygame.K_ESCAPE)],
        ]
        _, outcome = self.run_session(script)
        self.assertEqual(outcome.selected_index, 1)

    def test_axis_inside_the_deadzone_does_nothing(self) -> None:
        script = [
            [axis_event(0, 0.4)],
            [axis_event(0, 0.0)],
            [key_event(pygame.K_ESCAPE)],
        ]
        _, outcome = self.run_session(script)
        self.assertEqual(outcome.selected_index, 0)

    def test_a_held_stick_does_not_run_away(self) -> None:
        """Criterion E4: 12 frames of holding is one step, not twelve."""
        script = (
            [[axis_event(0, 1.0)]]
            + [[] for _ in range(12)]
            # Released before Escape: navigation must not keep stepping
            # while the exit gate's own grace period runs its course, which
            # would otherwise perturb the very count this test measures.
            + [[axis_event(0, 0.0)]]
        )
        script.append([key_event(pygame.K_ESCAPE)])
        _, outcome = self.run_session(script)
        self.assertEqual(outcome.selected_index, 1)

    def test_a_long_hold_repeats_but_stays_paced(self) -> None:
        """The other half of E4: holding still scrolls, just not per-frame.

        At the scripted 16ms/frame with a 380ms initial delay and a 140ms
        repeat, a 30-frame (480ms) hold is one initial step plus exactly one
        repeat -- never the 30 steps a naive loop would take.
        """
        script = (
            [[axis_event(0, 1.0)]]
            + [[] for _ in range(30)]
            # Released before Escape -- see test_a_held_stick_does_not_run_away.
            + [[axis_event(0, 0.0)]]
        )
        script.append([key_event(pygame.K_ESCAPE)])
        _, outcome = self.run_session(script)
        self.assertEqual(outcome.selected_index, 2)

    def test_the_incoming_notice_is_displayed(self) -> None:
        game = session([[key_event(pygame.K_ESCAPE)]])
        self.addCleanup(pygame.quit)
        notice = Notice("error", "Street Fighter exited with code 1", "boom")
        outcome = game(SessionState(view_mode=ViewMode.CAROUSEL, notice=notice))
        self.assertIs(outcome.action, UiAction.QUIT)

    def test_the_selection_carried_in_is_honoured(self) -> None:
        game = session([[key_event(pygame.K_ESCAPE)]])
        self.addCleanup(pygame.quit)
        outcome = game(SessionState(selected_index=4, view_mode=ViewMode.COVER_FLOW))
        self.assertEqual(outcome.selected_index, 4)
        self.assertIs(outcome.view_mode, ViewMode.COVER_FLOW)


class ScriptedSync:
    """Publish startup results; fail if a gallery tries to enqueue more work."""

    def __init__(self, *batches: list[GameState]) -> None:
        self._batches = list(batches)

    def request_all(self, entries) -> int:
        raise AssertionError("the entrypoint, not the gallery, owns startup sync")

    def request(self, entry) -> bool:
        raise AssertionError("launching must not request another update")

    def drain(self) -> list[GameState]:
        return self._batches.pop(0) if self._batches else []


def _states_for(status_for_launchable: GameStatus) -> dict[str, GameState]:
    return {
        game.id: GameState(
            game.id,
            status_for_launchable if game.launchable else GameStatus.COMING_SOON,
            "",
        )
        for game in MANIFEST
    }


class CachedLaunchTests(unittest.TestCase):
    """Startup results are consumed without re-fetching on launch or return."""

    def run_session(self, sync, script, status=GameStatus.READY):
        settings = Settings(fullscreen=False, frame_rate=60, sync_on_start=True)
        states = _states_for(status)
        game = ScriptedSession(MANIFEST, settings, states, sync, script=script)
        outcome = game(SessionState())
        self.addCleanup(pygame.quit)
        return game, outcome

    def test_ready_game_launches_on_the_first_frame_without_refresh(self) -> None:
        game_id = MANIFEST[0].id
        game, outcome = self.run_session(
            ScriptedSync(), script=[[button_event(BUTTON_LAUNCH)]]
        )
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(outcome.game_id, game_id)
        self.assertEqual(game.frames, 1)

    def test_gallery_can_reopen_and_launch_again_without_queueing_updates(self) -> None:
        game, outcome = self.run_session(
            ScriptedSync(), script=[[button_event(BUTTON_LAUNCH)]]
        )
        for _ in range(3):
            game._script = [[button_event(BUTTON_LAUNCH)]]
            outcome = game(
                SessionState(selected_index=outcome.selected_index, view_mode=outcome.view_mode)
            )
            self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(game.frames, 4)

    def test_failed_startup_check_still_launches_the_cached_copy(self) -> None:
        game_id = MANIFEST[0].id
        sync = ScriptedSync(
            [
                GameState(
                    game_id, GameStatus.CACHED_OFFLINE, "network unreachable (cached abc1234)"
                )
            ]
        )
        game, outcome = self.run_session(
            sync, script=[[button_event(BUTTON_LAUNCH)]], status=GameStatus.UPDATING
        )
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(outcome.game_id, game_id)
        self.assertIs(game.states[game_id].status, GameStatus.CACHED_OFFLINE)

    def test_launch_uses_the_existing_startup_jobs_result(self) -> None:
        game_id = MANIFEST[0].id
        sync = ScriptedSync(
            [GameState(game_id, GameStatus.UPDATING, "contacting GitHub")],
            [GameState(game_id, GameStatus.READY, "updated def5678")],
        )
        game, outcome = self.run_session(
            sync,
            script=[
                [button_event(BUTTON_LAUNCH)],
                [button_up_event(BUTTON_LAUNCH)],
                [button_event(BUTTON_LAUNCH)],
            ],
        )
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(game.frames, 3, "must not launch while the update is still active")
        self.assertIn("def5678", game.states[game_id].detail)

    def test_unavailable_startup_result_is_not_launched(self) -> None:
        game_id = MANIFEST[0].id
        sync = ScriptedSync(
            [GameState(game_id, GameStatus.UNAVAILABLE, "no cached copy")]
        )
        _, outcome = self.run_session(
            sync,
            script=[[button_event(BUTTON_LAUNCH)]]
            + [[] for _ in range(GRACE_FRAMES)]
            + [[key_event(pygame.K_ESCAPE)]],
        )
        self.assertIs(outcome.action, UiAction.QUIT)

    def test_cached_offline_game_needs_no_service_result_to_launch(self) -> None:
        game, outcome = self.run_session(
            ScriptedSync(),
            script=[[button_event(BUTTON_LAUNCH)]],
            status=GameStatus.CACHED_OFFLINE,
        )
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(game.frames, 1)


class ExitArmingTests(unittest.TestCase):
    """Regression (item 1): a stale Esc/P1 spanning the session transition
    must not bounce the visitor straight past the gallery.

    A game quits itself on Esc/P1; the instant it exits, the gallery reopens
    and starts pumping events again. If that key or button is still down --
    it is, after all, the exact input that just closed the game -- the fresh
    session must not read it as "leave the gallery too", no matter how long
    it stays held: arming (see ``GallerySession._sync_exit_arming`` and
    ``_exit_is_suppressed``) requires a positively observed release, not a
    fixed settle window, so these scripts hold the exit input down for far
    longer than any settle window used to be and it still must not fire.
    Once released and pressed again, a fresh press must quit promptly --
    "solved" must not mean "Escape stopped working in the gallery".

    Held state here is modelled the way :class:`ScriptedSession` reads it:
    a KEYDOWN/JOYBUTTONDOWN repeated in every consecutive frame's batch is a
    hold, not a single seeded flag -- see its docstring for why that
    distinction is what makes the focus regression below meaningful rather
    than trivially true.
    """

    def run_session(self, script, **kwargs):
        game = session(script, **kwargs)
        outcome = game(SessionState(view_mode=kwargs.get("default_view", ViewMode.GRID)))
        self.addCleanup(pygame.quit)
        return game, outcome

    def test_a_key_already_held_at_open_does_not_quit_until_released_and_pressed_again(
        self,
    ) -> None:
        """Escape is already held (physically down) the instant the session
        opens, and stays down for far longer than a human reaction time --
        standing in for the exact key that just closed the previous game,
        still under a visitor's finger. It must never quit while that hold
        continues. Only a genuine release followed by a fresh press may
        quit, and that must happen immediately, not after some further
        delay.
        """
        held_frames = 40
        script = (
            [[key_event(pygame.K_ESCAPE)] for _ in range(held_frames)]
            + [[key_event(pygame.K_ESCAPE, down=False)]]
            + [[key_event(pygame.K_ESCAPE)]]
        )
        game, outcome = self.run_session(script)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertEqual(
            game.frames,
            held_frames + 2,
            "must not quit while Escape is still held, and must quit on the "
            "very frame of the first fresh press after release",
        )

    def test_a_button_already_held_at_open_does_not_quit_until_released_and_pressed_again(
        self,
    ) -> None:
        """The joystick half of the same regression: P1 (button 5) already
        held when the session opens."""
        held_frames = 40
        script = (
            [[button_event(BUTTON_EXIT)] for _ in range(held_frames)]
            + [[button_up_event(BUTTON_EXIT)]]
            + [[button_event(BUTTON_EXIT)]]
        )
        game, outcome = self.run_session(script, joysticks=(FakeJoystick(instance_id=0),))
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertEqual(
            game.frames,
            held_frames + 2,
            "must not quit while P1 is still held, and must quit on the very "
            "frame of the first fresh press after release",
        )

    def test_a_genuine_escape_quits_once_it_has_had_a_chance_to_arm(self) -> None:
        """The overwhelmingly common case -- Escape was not held at all when
        the session opened -- must still arm and fire an ordinary press
        promptly, not be blocked forever by the fix below."""
        empty_frames = GRACE_FRAMES + 2
        script = [[] for _ in range(empty_frames)] + [[key_event(pygame.K_ESCAPE)]]
        game, outcome = self.run_session(script)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertEqual(
            game.frames, empty_frames + 1, "an armed, ordinary press must fire immediately"
        )

    def test_a_genuine_button_quits_once_it_has_had_a_chance_to_arm(self) -> None:
        empty_frames = GRACE_FRAMES + 2
        script = [[] for _ in range(empty_frames)] + [[button_event(BUTTON_EXIT)]]
        game, outcome = self.run_session(script, joysticks=(FakeJoystick(instance_id=0),))
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertEqual(
            game.frames, empty_frames + 1, "an armed, ordinary press must fire immediately"
        )

    def test_a_key_held_at_open_survives_a_long_focus_delay(self) -> None:
        """The exact failure the client reproduced, twice: the freshly
        opened window has not gained input focus yet, so it receives no
        input at all for a long stretch -- not because nothing is held, but
        because the window has not started receiving input yet. Measured
        directly on the client's cabinet, ``pygame.key.get_focused()`` did
        not turn true even four seconds after the window was created, so
        this models a delay comparable to what was actually observed: far
        longer than a naive settle window, well past where an earlier,
        shorter grace period had already been shown to fail.

        A ``pygame.key.get_pressed()`` fallback that trusts an early "not
        held" reading arms Escape before the window has ever seen the
        truth; once real focus arrives and delivers the still-held key's
        genuine KEYDOWN, that wrongly-armed source fires an immediate,
        unwanted exit straight out of the gallery. It must not: the
        fallback must not answer "not held" until it has had a real chance
        to be right, so the still-held key must stay disarmed straight
        through the no-input gap and into the frames where the real,
        still-held key starts arriving -- and only a genuine release may
        arm it.
        """
        unfocused_frames = GRACE_FRAMES - 3  # no focus yet: nothing delivered at all
        held_frames = 40  # focus has arrived: the still-held key is now visible
        script = (
            [[] for _ in range(unfocused_frames)]
            + [[key_event(pygame.K_ESCAPE)] for _ in range(held_frames)]
            + [[key_event(pygame.K_ESCAPE, down=False)]]
            + [[key_event(pygame.K_ESCAPE)]]
        )
        game, outcome = self.run_session(script)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertEqual(
            game.frames,
            unfocused_frames + held_frames + 2,
            "a still-held key must not arm just because the window had not "
            "gained focus yet when it was (wrongly) sampled as 'not held'",
        )

    def test_a_button_held_at_open_survives_a_long_focus_delay(self) -> None:
        """The joystick half of the same regression -- and the more
        important one, since P1 is the control the cabinet actually uses."""
        unfocused_frames = GRACE_FRAMES - 3
        held_frames = 40
        script = (
            [[] for _ in range(unfocused_frames)]
            + [[button_event(BUTTON_EXIT)] for _ in range(held_frames)]
            + [[button_up_event(BUTTON_EXIT)]]
            + [[button_event(BUTTON_EXIT)]]
        )
        game, outcome = self.run_session(script, joysticks=(FakeJoystick(instance_id=0),))
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertEqual(
            game.frames,
            unfocused_frames + held_frames + 2,
            "a still-held button must not arm just because the window had "
            "not gained focus yet when it was (wrongly) sampled as 'not held'",
        )


class SdlLifecycleTests(unittest.TestCase):
    """Criterion F2: SDL must be fully released before a game is spawned."""

    def test_sdl_is_released_when_the_session_ends(self) -> None:
        game = session([[key_event(pygame.K_ESCAPE)]])
        game(SessionState())
        self.assertFalse(pygame.get_init())
        self.assertFalse(pygame.display.get_init())
        self.assertFalse(pygame.joystick.get_init())

    def test_sdl_is_released_even_when_the_loop_raises(self) -> None:
        class ExplodingSession(ScriptedSession):
            def _pump(self):  # type: ignore[override]
                raise RuntimeError("boom")

        settings = Settings(fullscreen=False, frame_rate=60, sync_on_start=False)
        states = {game.id: GameState(game.id, GameStatus.READY, "") for game in MANIFEST}
        game = ExplodingSession(MANIFEST, settings, states, None, script=[])

        with self.assertRaises(RuntimeError):
            game(SessionState())
        self.assertFalse(pygame.get_init())

    def test_two_sessions_in_a_row_both_work(self) -> None:
        """What actually happens on the cabinet: gallery, game, gallery."""
        for _ in range(2):
            game = session([[key_event(pygame.K_ESCAPE)]])
            self.assertIs(game(SessionState()).action, UiAction.QUIT)
            self.assertFalse(pygame.get_init())


class RendererLifetimeTests(unittest.TestCase):
    """Regression: one ``GallerySession`` is reused across every launch by
    the real supervisor -- see ``main.py``, which builds it once and hands
    it to ``Supervisor`` as the ``ui`` callable. Nothing the renderer caches
    (fonts, surfaces, decoded preview frames, the logo) may survive the SDL
    teardown between one session and the next: ``pygame.font.quit()`` and
    ``pygame.quit()`` free that memory at the C level, so drawing with the
    same Python objects afterwards means every ``Font.size()``/``Surface``
    call touches freed memory -- readable first as pygame's own "Couldn't
    find glyph", then a native access violation, on the very next session.
    """

    def _session_with_capture(self, script):
        settings = Settings(fullscreen=False, frame_rate=60, sync_on_start=False)
        states = {game.id: GameState(game.id, GameStatus.READY, "") for game in MANIFEST}
        game = ScriptedSession(MANIFEST, settings, states, None, script=script)
        seen: list = []
        game.on_renderer_ready_hooks.append(lambda opened: seen.append(opened.renderer))
        return game, seen

    def test_no_renderer_survives_release_sdl(self) -> None:
        """The invariant that actually matters, checked directly: whatever
        the dummy driver does or does not let crash, the object must be gone."""
        game, _ = self._session_with_capture([[key_event(pygame.K_ESCAPE)]])
        game(SessionState())
        self.assertIsNone(
            game.renderer, "no SDL-backed object may survive _release_sdl()"
        )

    def test_the_renderer_is_a_new_object_every_session(self) -> None:
        """Fails against the pre-fix code: a renderer built once in
        ``__init__`` is the *same* object both times."""
        game, seen = self._session_with_capture([[key_event(pygame.K_ESCAPE)]])
        game(SessionState())
        game(SessionState())
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[0])
        self.assertIsNotNone(seen[1])
        self.assertIsNot(
            seen[0], seen[1], "the second session must not reuse the first renderer"
        )
        # And not merely the outer object -- every cache the stale-object
        # bug could hide in must also be rebuilt, not shared.
        self.assertIsNot(seen[0].ctx, seen[1].ctx)
        self.assertIsNot(seen[0].ctx.cache, seen[1].ctx.cache)
        self.assertIsNot(seen[0].ctx.fonts, seen[1].ctx.fonts)
        self.assertIsNot(seen[0].ctx.pixel, seen[1].ctx.pixel)
        self.assertIsNot(seen[0].ctx.previews, seen[1].ctx.previews)

    def test_a_notice_renders_correctly_on_a_freshly_reopened_session(self) -> None:
        """The exact path that hard-crashed for real on the cabinet: a
        ``Notice`` banner (from ``draw_notice`` -> ``_truncate`` ->
        ``font.size()``), drawn on the *second* session one reused
        ``GallerySession`` instance runs -- never the first, where a fresh
        renderer always happens to be correct even with the bug present."""
        game = session([[key_event(pygame.K_ESCAPE)]])
        first = game(SessionState())
        self.assertIs(first.action, UiAction.QUIT)

        notice = Notice(
            "error",
            "Street Fighter exited with code 1",
            "A long enough detail string that the banner has to wrap or "
            "truncate it, exactly like the real crash's own notice did.",
        )
        second = game(SessionState(notice=notice))
        self.assertIs(second.action, UiAction.QUIT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
