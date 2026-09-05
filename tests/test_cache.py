"""Repository cache: offline fallback, failure handling, and real local clones.

No test here touches the network. The "real git" tests clone from a fixture
repository created in a temporary directory, which exercises exactly the same
code path a GitHub clone would.
"""

from __future__ import annotations

import subprocess
import unittest

from launcher.cache import GitResult, RepositoryCache
from launcher.errors import NotLaunchableError
from launcher.paths import default_cache_root
from launcher.status import GameStatus

from support import (
    COMING_SOON_RAW,
    FakeGitRunner,
    TempDirCase,
    advance_fixture_repo,
    build_manifest,
    entry,
    git_available,
    make_fixture_repo,
)


class LocationTests(TempDirCase):
    def test_checkout_paths_live_under_the_cache(self) -> None:
        cache = RepositoryCache(self.tmp_path, runner=FakeGitRunner())
        path = cache.checkout_path(entry(id="streetfighter"))
        self.assertEqual(path.parent, cache.games_dir.resolve())

    def test_default_cache_root_is_hidden_and_ignorable(self) -> None:
        self.assertEqual(default_cache_root().name, ".arcade-cache")

    def test_state_lives_outside_the_checkouts(self) -> None:
        cache = RepositoryCache(self.tmp_path, runner=FakeGitRunner())
        self.assertNotIn(cache.games_dir, cache.state_dir.parents)


class ComingSoonTests(TempDirCase):
    """Disabled games are never cloned, fetched, or inspected as launchable."""

    def setUp(self) -> None:
        super().setUp()
        self.runner = FakeGitRunner()
        self.cache = RepositoryCache(self.tmp_path, runner=self.runner)
        self.entry = build_manifest(dict(COMING_SOON_RAW)).by_id("flappy-scotty")

    def test_sync_refuses_coming_soon(self) -> None:
        with self.assertRaises(NotLaunchableError):
            self.cache.sync(self.entry)
        self.assertEqual(self.runner.calls, [], "no git command may be issued")

    def test_verify_only_refuses_coming_soon(self) -> None:
        with self.assertRaises(NotLaunchableError):
            self.cache.verify_only(self.entry)
        self.assertEqual(self.runner.calls, [])


class OfflineTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.entry = entry(id="streetfighter")

    def test_missing_git_reports_unavailable_when_nothing_is_cached(self) -> None:
        cache = RepositoryCache(self.tmp_path, runner=FakeGitRunner(available=False))
        state = cache.sync(self.entry)
        self.assertIs(state.status, GameStatus.UNAVAILABLE)
        self.assertIn("git", state.detail.lower())

    def test_missing_git_falls_back_to_the_cached_copy(self) -> None:
        runner = FakeGitRunner(available=False)
        cache = RepositoryCache(self.tmp_path, runner=runner)
        checkout = cache.checkout_path(self.entry)
        (checkout / ".git").mkdir(parents=True)
        (checkout / "main.py").write_text("print('hi')\n", encoding="utf-8")

        state = cache.sync(self.entry)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE)
        self.assertEqual(runner.calls, [])

    def test_network_failure_falls_back_to_the_cached_copy(self) -> None:
        runner = FakeGitRunner(
            {"fetch": GitResult(128, stderr="fatal: unable to access ... could not resolve host")}
        )
        cache = RepositoryCache(self.tmp_path, runner=runner)
        checkout = cache.checkout_path(self.entry)
        (checkout / ".git").mkdir(parents=True)
        (checkout / "main.py").write_text("print('hi')\n", encoding="utf-8")

        state = cache.sync(self.entry)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE)
        self.assertIn("fetch", runner.verbs())

    def test_clone_failure_with_no_cache_is_unavailable(self) -> None:
        runner = FakeGitRunner({"clone": GitResult(128, stderr="fatal: repository not found")})
        cache = RepositoryCache(self.tmp_path, runner=runner)
        state = cache.sync(self.entry)
        self.assertIs(state.status, GameStatus.UNAVAILABLE)
        self.assertIn("clone", runner.verbs())

    def test_checkout_without_entrypoint_is_unavailable(self) -> None:
        cache = RepositoryCache(self.tmp_path, runner=FakeGitRunner())
        checkout = cache.checkout_path(self.entry)
        (checkout / ".git").mkdir(parents=True)  # cloned, but the game file is gone

        state = cache.verify_only(self.entry)
        self.assertIs(state.status, GameStatus.UNAVAILABLE)
        self.assertIn("main.py", state.detail)

    def test_verify_only_never_touches_the_network(self) -> None:
        runner = FakeGitRunner()
        cache = RepositoryCache(self.tmp_path, runner=runner)
        cache.verify_only(self.entry)
        self.assertEqual(runner.verbs(), [])

    def test_a_just_synced_checkout_still_hits_the_network(self) -> None:
        """The other half of the freshness regression below: no timestamp,
        however recent, may talk this method out of checking again -- only
        :meth:`~launcher.cache.RepositoryCache.verify_only` (offline mode)
        skips the network, and only because a caller chose that explicitly.
        """
        runner = FakeGitRunner()
        cache = RepositoryCache(self.tmp_path, runner=runner)
        checkout = cache.checkout_path(self.entry)
        (checkout / ".git").mkdir(parents=True)
        (checkout / "main.py").write_text("print('hi')\n", encoding="utf-8")
        cache.mark_synced(self.entry, "abc1234")  # as fresh as a sync gets

        state = cache.sync(self.entry)
        self.assertIs(state.status, GameStatus.READY)
        self.assertIn("fetch", runner.verbs(), "a just-synced checkout must still be checked")


class CloneArgumentTests(TempDirCase):
    def test_clone_is_shallow_and_pinned_to_the_ref(self) -> None:
        runner = FakeGitRunner(on_clone=lambda path: _fake_checkout(path))
        cache = RepositoryCache(self.tmp_path, runner=runner)
        cache.sync(entry(id="streetfighter", ref="main"))

        clone = next(call for call in runner.calls if call[0] == "clone")
        self.assertIn("--depth", clone)
        self.assertIn("--single-branch", clone)
        self.assertIn("--branch", clone)
        self.assertEqual(clone[clone.index("--branch") + 1], "main")

    def test_no_git_argument_is_ever_a_shell_string(self) -> None:
        runner = FakeGitRunner(on_clone=lambda path: _fake_checkout(path))
        cache = RepositoryCache(self.tmp_path, runner=runner)
        cache.sync(entry(id="streetfighter"))
        for call in runner.calls:
            self.assertIsInstance(call, tuple)
            for argument in call:
                self.assertIsInstance(argument, str)
                self.assertNotIn("&&", argument)
                self.assertNotIn(";", argument)


def _fake_checkout(path) -> None:
    """Materialise what a successful clone would have produced."""
    (path / ".git").mkdir(parents=True, exist_ok=True)
    (path / "main.py").write_text("print('hi')\n", encoding="utf-8")


@unittest.skipUnless(git_available(), "git is not installed")
class RealGitTests(TempDirCase):
    """End-to-end clone and refresh against a local fixture repository."""

    def setUp(self) -> None:
        super().setUp()
        self.origin = make_fixture_repo(self.tmp_path / "origin")
        self.cache = RepositoryCache(self.tmp_path / "cache")
        self.entry = entry(id="fixture-game", repository=str(self.origin), ref="main")

    def test_clone_then_launchable(self) -> None:
        state = self.cache.sync(self.entry)
        self.assertIs(state.status, GameStatus.READY, state.detail)
        self.assertTrue((self.cache.checkout_path(self.entry) / "main.py").is_file())

    def test_second_sync_reuses_the_checkout(self) -> None:
        self.cache.sync(self.entry)
        state = self.cache.sync(self.entry)
        self.assertTrue(state.status.is_playable, state.detail)

    def test_verify_only_after_clone_reports_a_usable_cache(self) -> None:
        self.cache.sync(self.entry)
        state = self.cache.verify_only(self.entry)
        self.assertTrue(state.status.is_playable, state.detail)

    def test_missing_origin_falls_back_to_unavailable(self) -> None:
        broken = entry(
            id="ghost", repository=str(self.tmp_path / "no-such-repo"), ref="main"
        )
        state = self.cache.sync(broken)
        self.assertIs(state.status, GameStatus.UNAVAILABLE)


@unittest.skipUnless(git_available(), "git is not installed")
class FreshnessRegressionTests(TempDirCase):
    """Reproduces the stale-checkout bug directly: a checkout synced only
    moments ago must still pick up a commit the remote gained since.

    This is the incident from the bug report, minus the six-hour wait: a
    checkout that syncs successfully, then the remote moves on, must not be
    served as "up to date" the next time the launcher asks -- freshness by
    the clock was exactly the mechanism that let a three-commits-stale build
    through for hours while reporting itself current.
    """

    def setUp(self) -> None:
        super().setUp()
        self.origin = make_fixture_repo(self.tmp_path / "origin")
        self.cache = RepositoryCache(self.tmp_path / "cache")
        self.entry = entry(id="fixture-game", repository=str(self.origin), ref="main")

    def _checked_out_commit(self) -> str:
        checkout = self.cache.checkout_path(self.entry)
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return result.stdout.strip()

    def test_a_freshly_synced_checkout_still_picks_up_a_moved_remote(self) -> None:
        first = self.cache.sync(self.entry)
        self.assertIs(first.status, GameStatus.READY, first.detail)
        old_commit = self._checked_out_commit()

        # The remote gains a real commit -- a fix landing mid-fair -- while
        # this checkout's own sync bookkeeping is only moments old: well
        # inside even the old six-hour "fresh" window this regression test
        # is guarding against.
        new_commit = advance_fixture_repo(self.origin)
        self.assertNotEqual(old_commit, new_commit, "the fixture must actually advance")

        second = self.cache.sync(self.entry)
        self.assertIs(second.status, GameStatus.READY, second.detail)
        self.assertEqual(
            self._checked_out_commit(),
            new_commit,
            "sync() served a stale checkout instead of re-fetching",
        )
        self.assertIn(new_commit, second.detail, "the new commit should be visible in the UI")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
