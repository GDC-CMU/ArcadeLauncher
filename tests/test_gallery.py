"""The gallery session: control mapping, SDL lifecycle, and refusals.

Runs the real event loop under headless SDL, feeding it synthetic events. No
joystick hardware is required and no repository is touched.
"""

from __future__ import annotations

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import unittest

from launcher.controls import BUTTON_CYCLE_VIEW, BUTTON_EXIT, BUTTON_LAUNCH, Command
from launcher.gallery import KEY_COMMANDS, KEY_DIRECTIONS, GallerySession
from launcher.input_state import Direction
from launcher.manifest import load_manifest
from launcher.settings import Settings
from launcher.status import GameState, GameStatus, Notice
from launcher.supervisor import SessionState, UiAction
from launcher.ui.pygame_runtime import pygame
from launcher.viewmodes import ViewMode

MANIFEST = load_manifest()


def key_event(key: int, down: bool = True) -> pygame.event.Event:
    return pygame.event.Event(
        pygame.KEYDOWN if down else pygame.KEYUP, key=key, mod=0, unicode="", scancode=0
    )


def button_event(button: int) -> pygame.event.Event:
    return pygame.event.Event(pygame.JOYBUTTONDOWN, button=button, instance_id=0, joy=0)


def axis_event(axis: int, value: float) -> pygame.event.Event:
    return pygame.event.Event(
        pygame.JOYAXISMOTION, axis=axis, value=value, instance_id=0, joy=0
    )


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

    def __init__(self, *args, script, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._script = list(script)
        self.frames = 0

    def _tick(self, clock):  # type: ignore[override]
        return self.FRAME_MS

    def _pump(self):  # type: ignore[override]
        self.frames += 1
        if not self._script:
            # Safety net: never hang a test if the script forgot to exit.
            return [key_event(pygame.K_ESCAPE)]
        return self._script.pop(0)


def session(script, *, states=None, default_view=ViewMode.GRID) -> ScriptedSession:
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
    return ScriptedSession(MANIFEST, settings, live, None, script=script)


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
        script = [[axis_event(0, 1.0)]] + [[] for _ in range(12)]
        script.append([key_event(pygame.K_ESCAPE)])
        _, outcome = self.run_session(script)
        self.assertEqual(outcome.selected_index, 1)

    def test_a_long_hold_repeats_but_stays_paced(self) -> None:
        """The other half of E4: holding still scrolls, just not per-frame.

        At the scripted 16ms/frame with a 380ms initial delay and a 140ms
        repeat, a 30-frame (480ms) hold is one initial step plus exactly one
        repeat -- never the 30 steps a naive loop would take.
        """
        script = [[axis_event(0, 1.0)]] + [[] for _ in range(30)]
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


class ExitSettleTests(unittest.TestCase):
    """Regression (item 1): a stale Esc/P1 spanning the session transition
    must not bounce the visitor straight past the gallery.

    A game quits itself on Esc/P1; the instant it exits, the gallery reopens
    and starts pumping events again. If that key or button is still down --
    it is, after all, the exact input that just closed the game -- the fresh
    session must settle rather than read it as "leave the gallery too".
    These scripts hold the exit input down for far longer than a human
    reaction time to a fresh cabinet screen, standing in for a queued or
    still-held press across the transition.
    """

    def run_session(self, script, **kwargs):
        game = session(script, **kwargs)
        outcome = game(SessionState(view_mode=kwargs.get("default_view", ViewMode.GRID)))
        self.addCleanup(pygame.quit)
        return game, outcome

    def test_a_stale_escape_does_not_instantly_quit(self) -> None:
        script = [[key_event(pygame.K_ESCAPE)] for _ in range(40)]
        game, outcome = self.run_session(script)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertGreater(
            game.frames,
            10,
            "a stale Escape must settle before it is honoured, not fire on "
            "the very first frames back in the gallery",
        )

    def test_a_stale_exit_button_does_not_instantly_quit(self) -> None:
        script = [[button_event(BUTTON_EXIT)] for _ in range(40)]
        game, outcome = self.run_session(script)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertGreater(
            game.frames,
            10,
            "a stale P1 must settle before it is honoured, same as Escape",
        )

    def test_a_genuine_escape_still_quits_within_the_settle_window(self) -> None:
        """The settle window is short: an ordinary press still works quickly."""
        script = [[key_event(pygame.K_ESCAPE)] for _ in range(40)]
        game, outcome = self.run_session(script)
        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertLess(game.frames, 40, "the settle window must not hang")


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
