"""Termination and device-enumeration behaviour that only bites on hardware.

Both defects covered here were invisible to the rest of the suite and to a
development machine, and both were found by running the launcher on the real
CMU-Q cabinet:

* A SIGTERM was caught and logged, but the process kept running. The shutdown
  flag lives on the supervisor and was only read *between* gallery sessions,
  so a gallery that was mid-session -- which, on an idle cabinet, is forever --
  never observed it. The arcade menu could not reap the launcher and the
  documented recovery was the physical reset button.
* SDL announces a joystick twice at start-up: once through the initial
  ``get_count()`` enumeration and again as a ``JOYDEVICEADDED`` event. Both
  paths opened a handle, so every stick was opened twice and the first handle
  was dropped without being closed.

The tests here deliberately do not rely on a real signal being delivered or a
real joystick being present; they drive the same code paths the cabinet drives.
"""

from __future__ import annotations

import support  # noqa: F401 - pins SDL to the dummy drivers before pygame loads
import signal
import threading
import unittest
import unittest.mock

from launcher.gallery import GallerySession
from launcher.settings import Settings
from launcher.status import GameState, GameStatus
from launcher.supervisor import SessionState, Supervisor, UiAction
from launcher.ui.pygame_runtime import pygame
from launcher.viewmodes import ViewMode

from support import (
    COMING_SOON_RAW,
    LAUNCHABLE_RAW,
    FakeGitRunner,
    TempDirCase,
    build_manifest,
)
from launcher.cache import RepositoryCache


#: Enough frames that a loop which ignores the shutdown flag is unmistakable,
#: but still a fraction of a second of real cabinet time.
FRAME_BUDGET = 240


class IdleSession(GallerySession):
    """A gallery with a visitor who never touches anything.

    This is the cabinet's normal resting state, and it is what made the defect
    so severe: with no input there is no ``QUIT`` and no ``EXIT``, so the only
    way the loop can ever end is by observing the shutdown flag.

    The frame budget turns "hangs forever" into a deterministic failure instead
    of a test that never returns.
    """

    FRAME_MS = 16

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.frames = 0

    def _tick(self, clock):  # type: ignore[override]
        return self.FRAME_MS

    def _pump(self):  # type: ignore[override]
        self.frames += 1
        if self.frames > FRAME_BUDGET:
            raise AssertionError(
                f"the gallery loop ran {self.frames} frames without noticing "
                "the shutdown request; on the cabinet this is the hang that "
                "forces a physical reset"
            )
        return []


def idle_session(should_stop=None) -> IdleSession:
    manifest = build_manifest(dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW))
    settings = Settings(
        default_view=ViewMode.GRID,
        fullscreen=False,
        frame_rate=60,
        sync_on_start=False,
    )
    states = {
        game.id: GameState(
            game.id,
            GameStatus.READY if game.launchable else GameStatus.COMING_SOON,
            "",
        )
        for game in manifest
    }
    return IdleSession(manifest, settings, states, None, should_stop=should_stop)


class GalleryShutdownTests(unittest.TestCase):
    """The gallery loop must observe a shutdown request, not just the outer loop."""

    def test_an_idle_gallery_stops_when_shutdown_is_requested(self) -> None:
        stop = threading.Event()
        session = idle_session(should_stop=stop.is_set)

        # Fires part-way through the budget: the loop must already be running,
        # so this proves the flag is observed *during* a session rather than
        # only checked before one starts.
        original_pump = session._pump

        def pump_then_signal():
            events = original_pump()
            if session.frames == 5:
                stop.set()
            return events

        session._pump = pump_then_signal  # type: ignore[method-assign]

        outcome = session(SessionState(view_mode=ViewMode.GRID))

        self.assertIs(outcome.action, UiAction.QUIT)
        self.assertLess(
            session.frames,
            FRAME_BUDGET,
            "the loop should stop within a frame or two of the request",
        )

    def test_it_stops_promptly_rather_than_eventually(self) -> None:
        """The cabinet's arcade menu will not wait; neither should we."""
        stop = threading.Event()
        session = idle_session(should_stop=stop.is_set)
        original_pump = session._pump

        def pump_then_signal():
            events = original_pump()
            if session.frames == 3:
                stop.set()
            return events

        session._pump = pump_then_signal  # type: ignore[method-assign]
        session(SessionState(view_mode=ViewMode.GRID))

        # At 60 Hz a couple of frames is ~30 ms, comfortably inside the two
        # second budget the arcade menu allows.
        self.assertLessEqual(session.frames, 6)

    def test_a_session_with_no_predicate_still_runs(self) -> None:
        """The predicate is optional: nothing else in the suite passes one."""
        session = idle_session(should_stop=None)
        with self.assertRaises(AssertionError):
            session(SessionState(view_mode=ViewMode.GRID))
        self.assertGreater(session.frames, FRAME_BUDGET)

    def test_sdl_is_released_when_the_gallery_is_shut_down(self) -> None:
        stop = threading.Event()
        stop.set()
        session = idle_session(should_stop=stop.is_set)
        session(SessionState(view_mode=ViewMode.GRID))
        self.assertFalse(pygame.display.get_init())
        self.assertFalse(pygame.joystick.get_init())


class HangingUi:
    """A UI stand-in with the real gallery's fatal shape: it never returns.

    Used to prove the supervisor's shutdown reaches whatever is running, not
    merely the gap between sessions.
    """

    def __init__(self, should_stop) -> None:
        self.should_stop = should_stop
        self.frames = 0
        self.sessions = 0

    def __call__(self, state: SessionState):
        from launcher.supervisor import UiOutcome

        self.sessions += 1
        while True:
            self.frames += 1
            if self.frames > FRAME_BUDGET:
                raise AssertionError("ui never observed the shutdown request")
            if self.should_stop():
                return UiOutcome(UiAction.QUIT, state.view_mode, 0)


class TerminationRecordingRunner:
    """A child runner that records termination requests without spawning."""

    def __init__(self) -> None:
        self.terminated = 0

    def run(self, command, cwd, *, game_id):  # noqa: ANN001 - protocol shape
        raise AssertionError("no launch is expected in these tests")

    def terminate(self) -> None:
        self.terminated += 1


class SupervisorSignalTests(TempDirCase):
    """A signal must end the process, not just get logged."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest = build_manifest(dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW))
        self.cache = RepositoryCache(self.tmp_path / "cache", runner=FakeGitRunner())

    def supervise(self, runner=None):
        shutdown = threading.Event()
        ui = HangingUi(shutdown.is_set)
        supervisor = Supervisor(
            self.manifest,
            self.cache,
            ui,
            runner=runner,
            install_signal_handlers=False,
            shutdown=shutdown,
        )
        return supervisor, ui

    def test_sigterm_ends_a_running_session(self) -> None:
        supervisor, ui = self.supervise()
        supervisor._handle_signal(signal.SIGTERM, None)
        self.assertEqual(supervisor.run(), 0)
        self.assertLess(ui.frames, FRAME_BUDGET)

    def test_sigint_ends_a_running_session(self) -> None:
        supervisor, ui = self.supervise()
        supervisor._handle_signal(signal.SIGINT, None)
        self.assertEqual(supervisor.run(), 0)
        self.assertLess(ui.frames, FRAME_BUDGET)

    def test_a_signal_mid_session_is_observed(self) -> None:
        """The realistic case: the signal arrives while the gallery is drawing."""
        supervisor, ui = self.supervise()
        real_stop = ui.should_stop

        def stop_after_a_few_frames():
            if ui.frames == 5:
                supervisor._handle_signal(signal.SIGTERM, None)
            return real_stop()

        ui.should_stop = stop_after_a_few_frames
        self.assertEqual(supervisor.run(), 0)
        self.assertLess(ui.frames, FRAME_BUDGET)

    def test_a_signal_does_not_orphan_a_running_child(self) -> None:
        runner = TerminationRecordingRunner()
        supervisor, _ = self.supervise(runner=runner)
        supervisor._handle_signal(signal.SIGTERM, None)
        supervisor.run()
        self.assertGreaterEqual(
            runner.terminated, 1, "the child must be asked to stop, not orphaned"
        )

    def test_the_handler_terminates_the_child_immediately(self) -> None:
        """Not only at cleanup: the child should stop when the signal lands."""
        runner = TerminationRecordingRunner()
        supervisor, _ = self.supervise(runner=runner)
        supervisor._handle_signal(signal.SIGTERM, None)
        self.assertGreaterEqual(runner.terminated, 1)

    def test_the_shutdown_flag_is_shared_with_the_ui(self) -> None:
        """The supervisor and the gallery must read the same flag."""
        shutdown = threading.Event()
        supervisor = Supervisor(
            self.manifest,
            self.cache,
            HangingUi(shutdown.is_set),
            install_signal_handlers=False,
            shutdown=shutdown,
        )
        supervisor.request_shutdown()
        self.assertTrue(shutdown.is_set())


class FakeStick:
    """Stands in for an SDL joystick handle."""

    def __init__(self, device_index: int, instance_id: int, name: str) -> None:
        self.device_index = device_index
        self._instance_id = instance_id
        self._name = name
        self.inited = False
        self.quit_calls = 0

    def init(self) -> None:
        self.inited = True

    def get_instance_id(self) -> int:
        return self._instance_id

    def get_name(self) -> str:
        return self._name

    def get_numbuttons(self) -> int:
        return 0

    def get_button(self, button: int) -> bool:
        return False

    def quit(self) -> None:
        self.quit_calls += 1


class JoystickEnumerationTests(unittest.TestCase):
    """SDL announces each device twice at start-up; we must register it once.

    The cabinet has two DragonRise sticks and logged four attachments. Every
    duplicate opened a second SDL handle and silently replaced the first, which
    was then never closed.
    """

    def setUp(self) -> None:
        self.session = idle_session()
        self.opened: list[FakeStick] = []
        # Two devices, mirroring the cabinet, each answering to a stable
        # instance id however many times it is opened.
        self.instances = {0: 10, 1: 11}

        def factory(device_index: int) -> FakeStick:
            stick = FakeStick(
                device_index, self.instances[device_index], "DragonRise Inc. Generic"
            )
            self.opened.append(stick)
            return stick

        patcher = unittest.mock.patch.object(
            pygame.joystick, "Joystick", side_effect=factory
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enumeration_then_device_added_registers_once(self) -> None:
        """The exact cabinet sequence: get_count() loop, then JOYDEVICEADDED."""
        with self.assertLogs("launcher.gallery", level="INFO") as captured:
            self.session._attach_joystick(0)  # initial enumeration
            self.session._attach_joystick(0)  # SDL's JOYDEVICEADDED for the same one

        attached = [line for line in captured.output if "joystick attached" in line]
        self.assertEqual(
            len(attached),
            1,
            "a device discovered twice must be reported once",
        )
        self.assertEqual(len(self.session._joysticks), 1)
        self.assertEqual(self.session.navigation.axes.device_ids, (10,))

    def test_the_first_handle_is_the_one_kept(self) -> None:
        self.session._attach_joystick(0)
        first = self.session._joysticks[10]
        self.session._attach_joystick(0)
        self.assertIs(
            self.session._joysticks[10],
            first,
            "re-adding a known device must not swap the live handle",
        )

    def test_no_duplicate_handle_is_leaked(self) -> None:
        self.session._attach_joystick(0)
        self.session._attach_joystick(0)
        kept = set(id(stick) for stick in self.session._joysticks.values())
        leaked = [
            stick
            for stick in self.opened
            if id(stick) not in kept and stick.quit_calls == 0
        ]
        self.assertEqual(
            leaked, [], "a duplicate SDL handle was opened and never closed"
        )

    def test_two_real_devices_are_both_kept(self) -> None:
        """Idempotence must not collapse genuinely different sticks."""
        self.session._attach_joystick(0)
        self.session._attach_joystick(1)
        self.session._attach_joystick(0)
        self.session._attach_joystick(1)
        self.assertEqual(len(self.session._joysticks), 2)
        self.assertEqual(self.session.navigation.axes.device_ids, (10, 11))

    def test_a_detached_device_can_be_attached_again(self) -> None:
        self.session._attach_joystick(0)
        self.session._detach_joystick(10)
        self.assertEqual(self.session.navigation.axes.device_ids, ())
        self.session._attach_joystick(0)
        self.assertEqual(self.session.navigation.axes.device_ids, (10,))


class DoubleRegistrationNavigationTests(unittest.TestCase):
    """A duplicated device must not move the selection twice per push.

    This is the symptom that would have been mistaken for the debounce bug:
    identical on a developer machine, wrong only where two sticks exist.
    """

    def test_a_duplicated_device_still_steps_once(self) -> None:
        session = idle_session()
        session.navigation.axes.attach(10)
        session.navigation.axes.attach(10)  # duplicate registration
        from launcher.controls import AXIS_HORIZONTAL

        session.navigation.axes.set_axis(10, AXIS_HORIZONTAL, 1.0)
        steps = session.navigation.poll(0)
        self.assertEqual(len(steps), 1, "one push must be one step")

    def test_two_sticks_pushed_together_still_step_once(self) -> None:
        session = idle_session()
        from launcher.controls import AXIS_HORIZONTAL

        for instance in (10, 11):
            session.navigation.axes.attach(instance)
            session.navigation.axes.set_axis(instance, AXIS_HORIZONTAL, 1.0)
        steps = session.navigation.poll(0)
        self.assertEqual(len(steps), 1)


class ChildProcessShutdownTests(TempDirCase):
    """A signal while a game is running must stop the game too.

    Uses a real child process: the orphan this prevents is a real one, and a
    stubbed runner cannot prove the escalation works.
    """

    def test_terminate_stops_a_real_child_promptly(self) -> None:
        import sys  # noqa: PLC0415
        import time  # noqa: PLC0415

        from launcher.supervisor import ProcessGameRunner  # noqa: PLC0415

        runner = ProcessGameRunner(self.tmp_path / "run")
        command = [sys.executable, "-c", "import time; time.sleep(60)"]
        result: list[object] = []

        worker = threading.Thread(
            target=lambda: result.append(
                runner.run(command, self.tmp_path, game_id="sleeper")
            ),
            daemon=True,
        )
        worker.start()

        deadline = time.monotonic() + 10
        while not runner.is_running and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(runner.is_running, "the child never started")

        child = runner._process
        assert child is not None
        started = time.monotonic()
        runner.terminate()
        worker.join(timeout=10)
        elapsed = time.monotonic() - started

        self.assertFalse(worker.is_alive(), "run() did not return after terminate")
        self.assertLess(elapsed, 5.0, "the child was not stopped promptly")
        self.assertIsNotNone(child.poll(), "the child process was orphaned")
        self.assertEqual(len(result), 1)

    def test_a_signal_while_a_game_runs_terminates_it(self) -> None:
        """The handler itself -- not just cleanup -- must reach the child."""
        import sys  # noqa: PLC0415
        import time  # noqa: PLC0415

        from launcher.supervisor import ProcessGameRunner  # noqa: PLC0415

        runner = ProcessGameRunner(self.tmp_path / "run")
        manifest = build_manifest(dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW))
        cache = RepositoryCache(self.tmp_path / "cache", runner=FakeGitRunner())
        shutdown = threading.Event()
        supervisor = Supervisor(
            manifest,
            cache,
            HangingUi(shutdown.is_set),
            runner=runner,
            install_signal_handlers=False,
            shutdown=shutdown,
        )

        command = [sys.executable, "-c", "import time; time.sleep(60)"]
        worker = threading.Thread(
            target=lambda: runner.run(command, self.tmp_path, game_id="sleeper"),
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 10
        while not runner.is_running and time.monotonic() < deadline:
            time.sleep(0.02)
        child = runner._process
        assert child is not None

        supervisor._handle_signal(signal.SIGTERM, None)
        worker.join(timeout=10)

        self.assertIsNotNone(child.poll(), "the signal handler orphaned the child")


if __name__ == "__main__":
    unittest.main()
