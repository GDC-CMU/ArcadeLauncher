"""Supervisor lifecycle: two-level exit, safe child commands, real children.

The supervisor is the piece that makes the cabinet's exit button mean two
different things: in a game it returns you to the gallery, in the gallery it
returns you to the arcade menu. These tests pin that behaviour down, including
one that spawns a genuine child process (acceptance criterion I4).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from launcher.cache import RepositoryCache
from launcher.errors import LaunchError, NotLaunchableError, UiFatalError
from launcher.manifest import Runtime
from launcher.supervisor import (
    ChildResult,
    ProcessGameRunner,
    SessionState,
    Supervisor,
    UiAction,
    UiOutcome,
    build_child_command,
)
from launcher.viewmodes import ViewMode

from support import (
    COMING_SOON_RAW,
    LAUNCHABLE_RAW,
    FakeGitRunner,
    TempDirCase,
    build_manifest,
    child_fixture,
    entry,
)


class ScriptedUi:
    """A gallery stand-in that replays a fixed list of outcomes."""

    def __init__(self, *outcomes: UiOutcome) -> None:
        self.outcomes = list(outcomes)
        self.states: list[SessionState] = []

    def __call__(self, state: SessionState) -> UiOutcome:
        self.states.append(state)
        if not self.outcomes:
            raise AssertionError("supervisor asked for more sessions than scripted")
        return self.outcomes.pop(0)


class RecordingRunner:
    """A child runner that records invocations instead of spawning anything."""

    def __init__(self, result: ChildResult | None = None) -> None:
        self.result = result or ChildResult(0)
        self.calls: list[tuple[list[str], Path, str]] = []
        self.terminated = 0

    def run(self, command, cwd, *, game_id):  # noqa: ANN001 - protocol shape
        self.calls.append((list(command), Path(cwd), game_id))
        return self.result

    def terminate(self) -> None:
        self.terminated += 1


class SupervisorHarness(TempDirCase):
    """Shared setup: a two-game manifest with a ready cached checkout."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest = build_manifest(dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW))
        self.cache = RepositoryCache(self.tmp_path / "cache", runner=FakeGitRunner())
        self.checkout = self.cache.checkout_path(self.manifest.by_id("streetfighter"))
        (self.checkout / ".git").mkdir(parents=True)
        (self.checkout / "main.py").write_text("print('hi')\n", encoding="utf-8")

    def supervise(self, ui, runner=None) -> Supervisor:
        return Supervisor(
            self.manifest,
            self.cache,
            ui,
            runner=runner or RecordingRunner(),
            install_signal_handlers=False,
        )


class ChildCommandTests(TempDirCase):
    """Criterion F3/F4: argv is a list, cwd is the checkout, no shell anywhere."""

    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.tmp_path / "streetfighter"
        self.checkout.mkdir()
        (self.checkout / "main.py").write_text("print('hi')\n", encoding="utf-8")

    def test_command_uses_the_current_interpreter_and_relative_entrypoint(self) -> None:
        command = build_child_command(entry(entrypoint="main.py"), self.checkout)
        self.assertEqual(command, [sys.executable, "main.py"])

    def test_command_is_a_list_of_strings(self) -> None:
        command = build_child_command(entry(entrypoint="main.py"), self.checkout)
        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(part, str) for part in command))

    def test_nested_entrypoint_is_supported(self) -> None:
        nested = self.checkout / "src"
        nested.mkdir()
        (nested / "game.py").write_text("print('hi')\n", encoding="utf-8")
        command = build_child_command(entry(entrypoint="src/game.py"), self.checkout)
        self.assertEqual(command[1], "src/game.py")

    def test_missing_entrypoint_is_a_launch_error(self) -> None:
        with self.assertRaises(LaunchError):
            build_child_command(entry(entrypoint="absent.py"), self.checkout)

    def test_coming_soon_game_can_never_build_a_command(self) -> None:
        with self.assertRaises(NotLaunchableError):
            build_child_command(entry(launchable=False, entrypoint=None), self.checkout)

    def test_non_python_runtime_is_refused(self) -> None:
        class OtherRuntime:
            value = "godot"

        with self.assertRaises(LaunchError):
            build_child_command(
                entry(runtime=OtherRuntime()), self.checkout  # type: ignore[arg-type]
            )

    def test_python_runtime_is_the_supported_one(self) -> None:
        self.assertIs(entry().runtime, Runtime.PYTHON)


class TwoLevelExitTests(SupervisorHarness):
    def test_quit_from_the_gallery_exits_the_launcher(self) -> None:
        ui = ScriptedUi(UiOutcome(UiAction.QUIT, ViewMode.GRID, 0))
        self.assertEqual(self.supervise(ui).run(), 0)

    def test_child_exit_returns_to_the_gallery_not_the_arcade(self) -> None:
        """Criterion F5/F6: one game, then the gallery again, then out."""
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 0, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.GRID, 0),
        )
        runner = RecordingRunner()
        supervisor = self.supervise(ui, runner)

        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(supervisor.sessions_run, 2, "the gallery must come back")

    def test_selection_and_view_survive_a_launch(self) -> None:
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.COVER_FLOW, 1, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.COVER_FLOW, 1),
        )
        self.supervise(ui).run()
        second = ui.states[1]
        self.assertEqual(second.selected_index, 1)
        self.assertIs(second.view_mode, ViewMode.COVER_FLOW)

    def test_successful_child_produces_a_friendly_notice(self) -> None:
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 0, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.GRID, 0),
        )
        self.supervise(ui).run()
        notice = ui.states[1].notice
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertFalse(notice.is_error)

    def test_nonzero_child_exit_becomes_an_error_notice(self) -> None:
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 0, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.GRID, 0),
        )
        runner = RecordingRunner(ChildResult(3, tail="Traceback: boom"))
        self.supervise(ui, runner).run()

        notice = ui.states[1].notice
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertTrue(notice.is_error)
        self.assertIn("3", notice.title)
        self.assertIn("boom", notice.detail)

    def test_child_is_started_in_its_own_checkout(self) -> None:
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 0, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.GRID, 0),
        )
        runner = RecordingRunner()
        self.supervise(ui, runner).run()
        _, cwd, game_id = runner.calls[0]
        self.assertEqual(cwd, self.checkout)
        self.assertEqual(game_id, "streetfighter")


class RefusalTests(SupervisorHarness):
    def test_launching_a_coming_soon_game_is_fatal(self) -> None:
        """The gallery must never ask; if it does, fail loudly (criterion C4)."""
        ui = ScriptedUi(UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 1, "flappy-scotty"))
        with self.assertRaises(UiFatalError):
            self.supervise(ui).run()

    def test_launching_an_unknown_game_is_fatal(self) -> None:
        ui = ScriptedUi(UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 0, "doom"))
        with self.assertRaises(UiFatalError):
            self.supervise(ui).run()

    def test_launch_without_a_game_id_is_fatal(self) -> None:
        ui = ScriptedUi(UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 0, None))
        with self.assertRaises(UiFatalError):
            self.supervise(ui).run()

    def test_out_of_range_selection_is_fatal(self) -> None:
        ui = ScriptedUi(UiOutcome(UiAction.QUIT, ViewMode.GRID, 99))
        with self.assertRaises(UiFatalError):
            self.supervise(ui).run()

    def test_non_outcome_return_value_is_fatal(self) -> None:
        ui = ScriptedUi()
        ui.outcomes.append("go left")  # type: ignore[arg-type]
        with self.assertRaises(UiFatalError):
            self.supervise(ui).run()

    def test_missing_checkout_reports_an_error_instead_of_launching(self) -> None:
        shutil.rmtree(self.checkout)
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.GRID, 0, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.GRID, 0),
        )
        runner = RecordingRunner()
        self.supervise(ui, runner).run()

        self.assertEqual(runner.calls, [], "nothing may be spawned without a checkout")
        notice = ui.states[1].notice
        assert notice is not None
        self.assertTrue(notice.is_error)


class CrashRecoveryTests(SupervisorHarness):
    def test_a_crashed_gallery_is_restarted_once(self) -> None:
        class FlakyUi:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, state: SessionState) -> UiOutcome:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("display exploded")
                return UiOutcome(UiAction.QUIT, ViewMode.GRID, 0)

        ui = FlakyUi()
        self.assertEqual(self.supervise(ui).run(), 0)
        self.assertEqual(ui.calls, 2)

    def test_repeated_crashes_eventually_give_up(self) -> None:
        class BrokenUi:
            def __call__(self, state: SessionState) -> UiOutcome:
                raise RuntimeError("display exploded")

        with self.assertRaises(UiFatalError):
            self.supervise(BrokenUi()).run()

    def test_cleanup_removes_the_transient_run_directory(self) -> None:
        ui = ScriptedUi(UiOutcome(UiAction.QUIT, ViewMode.GRID, 0))
        self.supervise(ui).run()
        self.assertFalse((self.cache.root / "run").exists())


@unittest.skipIf(sys.platform == "emscripten", "no subprocesses available")
class RealChildProcessTests(SupervisorHarness):
    """Criterion I4: spawn a real child and prove the launcher comes back."""

    def install(self, fixture: str) -> None:
        shutil.copyfile(child_fixture(fixture), self.checkout / "main.py")

    def test_control_returns_after_a_clean_child_exit(self) -> None:
        self.install("child_ok.py")
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.CAROUSEL, 0, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.CAROUSEL, 0),
        )
        supervisor = Supervisor(
            self.manifest,
            self.cache,
            ui,
            runner=ProcessGameRunner(log_dir=self.tmp_path / "run"),
            install_signal_handlers=False,
        )

        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(supervisor.sessions_run, 2)
        notice = ui.states[1].notice
        assert notice is not None
        self.assertFalse(notice.is_error, notice.detail)

    def test_a_failing_child_surfaces_its_output(self) -> None:
        self.install("child_fail.py")
        ui = ScriptedUi(
            UiOutcome(UiAction.LAUNCH, ViewMode.CAROUSEL, 0, "streetfighter"),
            UiOutcome(UiAction.QUIT, ViewMode.CAROUSEL, 0),
        )
        supervisor = Supervisor(
            self.manifest,
            self.cache,
            ui,
            runner=ProcessGameRunner(log_dir=self.tmp_path / "run"),
            install_signal_handlers=False,
        )
        supervisor.run()

        notice = ui.states[1].notice
        assert notice is not None
        self.assertTrue(notice.is_error)
        self.assertIn("on purpose", notice.detail)

    def test_the_child_log_is_always_cleaned_up(self) -> None:
        self.install("child_ok.py")
        run_dir = self.tmp_path / "run"
        runner = ProcessGameRunner(log_dir=run_dir)
        runner.run([sys.executable, "main.py"], self.checkout, game_id="streetfighter")
        self.assertEqual(list(run_dir.glob("*.child.log")), [])

    def test_the_child_runs_inside_its_own_checkout(self) -> None:
        (self.checkout / "main.py").write_text(
            "import os, sys\nprint(os.getcwd())\nsys.exit(0)\n", encoding="utf-8"
        )
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "main.py"],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            Path(completed.stdout.strip()).resolve(), self.checkout.resolve()
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
