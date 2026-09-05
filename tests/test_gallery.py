"""The gallery session: control mapping, SDL lifecycle, and refusals.

Runs the real event loop under headless SDL, feeding it synthetic events. No
joystick hardware is required and no repository is touched.
"""

from __future__ import annotations

import math

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
    """A test double standing in for :class:`~launcher.sync.SyncService`.

    Real :class:`SyncService` runs git on a background thread, which would
    make the pre-launch-refresh tests below racy against wall-clock time.
    This double keeps the same two-method surface the gallery actually uses
    (``request``/``drain``) but publishes a scripted result the very next
    time :meth:`drain` is called, so the test controls exactly how many
    frames the "refresh" takes without touching a thread or the filesystem.
    """

    def __init__(self, results: dict[str, GameState]) -> None:
        self._results = results
        self.requested: list[str] = []
        self._queued: list[GameState] = []

    def request_all(self, entries) -> int:  # pragma: no cover - unused here
        return len(list(entries))

    def request(self, entry) -> None:
        self.requested.append(entry.id)
        self._queued.append(self._results[entry.id])

    def drain(self) -> list[GameState]:
        published, self._queued = self._queued, []
        return published


class NeverSettlingSync:
    """A sync double that never resolves a request -- stands in for a
    background worker stuck retrying a dead or absent network. Exercises
    the bound on the pre-launch refresh (item 2 of the offline-speed fix):
    the gallery must not wait on this forever, only up to
    ``_LAUNCH_REFRESH_TIMEOUT_MS``.
    """

    def __init__(self) -> None:
        self.requested: list[str] = []

    def request_all(self, entries) -> int:  # pragma: no cover - unused here
        return len(list(entries))

    def request(self, entry) -> None:
        self.requested.append(entry.id)

    def drain(self) -> list[GameState]:
        return []


def _states_for(status_for_launchable: GameStatus) -> dict[str, GameState]:
    return {
        game.id: GameState(
            game.id,
            status_for_launchable if game.launchable else GameStatus.COMING_SOON,
            "",
        )
        for game in MANIFEST
    }


class PreLaunchRefreshTests(unittest.TestCase):
    """A launch re-syncs its game immediately before starting it (item 3):
    even a card that already reads READY may be stale in a long session, so
    pressing Play confirms the checkout is current -- via the background
    worker, never a blocking call on this thread -- before the outcome is
    handed back to the supervisor.
    """

    def run_session(self, sync, script):
        settings = Settings(fullscreen=False, frame_rate=60, sync_on_start=False)
        states = _states_for(GameStatus.READY)
        game = ScriptedSession(MANIFEST, settings, states, sync, script=script)
        outcome = game(SessionState())
        self.addCleanup(pygame.quit)
        return states, outcome

    def test_launch_waits_for_its_own_refresh_and_uses_the_new_commit(self) -> None:
        game_id = MANIFEST[0].id
        sync = ScriptedSync(
            {game_id: GameState(game_id, GameStatus.READY, "updated def5678")}
        )
        states, outcome = self.run_session(
            sync, script=[[button_event(BUTTON_LAUNCH)], []]
        )
        self.assertEqual(sync.requested, [game_id], "the selected game must be re-synced")
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(outcome.game_id, game_id)
        self.assertIn("def5678", states[game_id].detail)

    def test_launch_still_starts_the_cached_copy_if_the_refresh_fails(self) -> None:
        """A failed pre-launch fetch must not strand the visitor: a good
        checkout still launches and still reports CACHED_OFFLINE, exactly as
        it would if the failure had happened during the background sync."""
        game_id = MANIFEST[0].id
        sync = ScriptedSync(
            {
                game_id: GameState(
                    game_id, GameStatus.CACHED_OFFLINE, "network unreachable (cached abc1234)"
                )
            }
        )
        states, outcome = self.run_session(
            sync, script=[[button_event(BUTTON_LAUNCH)], []]
        )
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(outcome.game_id, game_id)
        self.assertIs(states[game_id].status, GameStatus.CACHED_OFFLINE)

    def test_launch_is_refused_if_the_refresh_leaves_nothing_playable(self) -> None:
        """If the checkout is gone by the time the refresh runs, the visitor
        stays in the gallery instead of being handed a broken launch."""
        game_id = MANIFEST[0].id
        sync = ScriptedSync(
            {game_id: GameState(game_id, GameStatus.UNAVAILABLE, "checkout corrupted")}
        )
        _, outcome = self.run_session(
            sync,
            script=[[button_event(BUTTON_LAUNCH)], [], [key_event(pygame.K_ESCAPE)]],
        )
        self.assertIs(outcome.action, UiAction.QUIT)

    def test_launch_proceeds_once_the_refresh_bound_elapses_instead_of_hanging(
        self,
    ) -> None:
        """Item 2 of the offline-speed fix: a launch must never wait the
        full network timeout for its own pre-flight refresh. The card was
        already confirmed playable the instant Play was pressed, so once the
        bound (:data:`~launcher.gallery._LAUNCH_REFRESH_TIMEOUT_MS`) elapses
        without an answer, the visitor must be handed the cached copy rather
        than stand at a cabinet that looks frozen behind an ``UPDATING``
        badge."""
        game_id = MANIFEST[0].id
        sync = NeverSettlingSync()
        # 2000ms bound / 16ms-per-frame = 125 frames; this script is much
        # longer so a regression (no bound at all) would run out the clock
        # and fall through to the safety net's Escape instead of launching.
        script = [[button_event(BUTTON_LAUNCH)]] + [[] for _ in range(400)]
        _, outcome = self.run_session(sync, script=script)
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertEqual(outcome.game_id, game_id)
        self.assertEqual(sync.requested, [game_id])

    def test_offline_mode_still_confirms_without_any_network_call(self) -> None:
        """Offline mode (``sync_on_start`` off / ``--no-sync``) re-verifies the
        disk before launch through the same path, but the double standing in
        for it here never talks to git either way -- what matters is that the
        gallery still routes the confirmation through the worker rather than
        skipping it."""
        game_id = MANIFEST[0].id
        sync = ScriptedSync(
            {game_id: GameState(game_id, GameStatus.CACHED_OFFLINE, "using cached copy abc1234")}
        )
        states, outcome = self.run_session(
            sync, script=[[button_event(BUTTON_LAUNCH)], []]
        )
        self.assertIs(outcome.action, UiAction.LAUNCH)
        self.assertIs(states[game_id].status, GameStatus.CACHED_OFFLINE)


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
