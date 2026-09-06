"""Background synchronisation: threading, offline mode, and disabled games."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import main as entrypoint
from launcher.cache import GitResult, RepositoryCache
from launcher.settings import Settings
from launcher.status import GameStatus
from launcher.supervisor import UiAction, UiOutcome
from launcher.sync import SyncService, initial_states

from support import (
    COMING_SOON_RAW,
    LAUNCHABLE_RAW,
    FakeGitRunner,
    TempDirCase,
    build_manifest,
)


class InitialStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = build_manifest(dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW))

    def test_coming_soon_is_pinned(self) -> None:
        states = initial_states(self.manifest, sync_enabled=True)
        self.assertIs(states["flappy-scotty"].status, GameStatus.COMING_SOON)

    def test_launchable_starts_pending_when_syncing(self) -> None:
        states = initial_states(self.manifest, sync_enabled=True)
        self.assertIs(states["streetfighter"].status, GameStatus.PENDING)
        self.assertIn("update", states["streetfighter"].detail)

    def test_offline_start_says_it_is_only_checking_the_cache(self) -> None:
        states = initial_states(self.manifest, sync_enabled=False)
        self.assertIs(states["streetfighter"].status, GameStatus.PENDING)
        self.assertIn("cache", states["streetfighter"].detail)

    def test_every_game_has_a_state(self) -> None:
        states = initial_states(self.manifest)
        self.assertEqual(set(states), {game.id for game in self.manifest})


class SyncServiceTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest = build_manifest(dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW))
        self.runner = FakeGitRunner(on_clone=self._materialise)
        self.cache = RepositoryCache(self.tmp_path, runner=self.runner)

    @staticmethod
    def _materialise(path) -> None:
        (path / ".git").mkdir(parents=True, exist_ok=True)
        (path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    @staticmethod
    def _final(results, game_id: str):
        """The last state published for *game_id*.

        The service publishes an interim ``UPDATING`` state so the gallery can
        show progress; the caller cares about where it settled.
        """
        matching = [state for state in results if state.game_id == game_id]
        assert matching, f"no state published for {game_id}"
        return matching[-1]

    def test_results_are_delivered_to_the_caller(self) -> None:
        with SyncService(self.cache) as sync:
            sync.request_all(self.manifest.launchable)
            sync.wait_idle()
            results = sync.drain()
        self.assertEqual({state.game_id for state in results}, {"streetfighter"})
        self.assertIs(self._final(results, "streetfighter").status, GameStatus.READY)

    def test_progress_is_reported_before_the_final_state(self) -> None:
        with SyncService(self.cache) as sync:
            sync.request_all(self.manifest.launchable)
            sync.wait_idle()
            results = sync.drain()
        self.assertIs(results[0].status, GameStatus.UPDATING)
        self.assertGreaterEqual(len(results), 2)

    def test_disabled_games_are_never_requested(self) -> None:
        with SyncService(self.cache) as sync:
            queued = sync.request_all(self.manifest.launchable)
            sync.wait_idle()
            sync.drain()
        self.assertEqual(queued, 1)
        cloned = [call for call in self.runner.calls if call[0] == "clone"]
        self.assertEqual(len(cloned), 1)
        self.assertNotIn("flappy-scotty", " ".join(cloned[0]))

    def test_offline_mode_never_calls_git(self) -> None:
        with SyncService(self.cache, online=False) as sync:
            sync.request_all(self.manifest.launchable)
            sync.wait_idle()
            results = sync.drain()
        self.assertEqual(self.runner.verbs(), [])
        self.assertTrue(all("GitHub" not in state.detail for state in results))
        self.assertIs(self._final(results, "streetfighter").status, GameStatus.UNAVAILABLE)

    def test_duplicate_queued_requests_are_coalesced(self) -> None:
        sync = SyncService(self.cache)
        game = self.manifest.launchable[0]
        self.assertTrue(sync.request(game))
        self.assertFalse(sync.request(game))
        self.assertEqual(sync.request_all(self.manifest.launchable), 0)
        with sync:
            self.assertTrue(sync.wait_idle())
            results = sync.drain()
        self.assertEqual(self.runner.verbs().count("clone"), 1)
        self.assertEqual(sum(state.status is GameStatus.UPDATING for state in results), 1)

    def test_completed_games_are_not_checked_again_in_the_same_service(self) -> None:
        with SyncService(self.cache) as sync:
            self.assertEqual(sync.request_all(self.manifest.launchable), 1)
            self.assertTrue(sync.wait_idle())
            sync.drain()
            self.assertEqual(sync.request_all(self.manifest.launchable), 0)
            self.assertEqual(sync.drain(), [])
        self.assertEqual(self.runner.verbs(), ["clone", "rev-parse"])

    def test_failed_checks_are_not_retried_on_every_gallery_return(self) -> None:
        self.runner.results["clone"] = GitResult(128, stderr="offline")
        with SyncService(self.cache) as sync:
            sync.request_all(self.manifest.launchable)
            self.assertTrue(sync.wait_idle())
            self.assertEqual(sync.request_all(self.manifest.launchable), 0)
        self.assertEqual(self.runner.verbs().count("clone"), 1)

    def test_new_service_checks_the_same_checkout_again(self) -> None:
        for _ in range(2):
            with SyncService(self.cache) as sync:
                self.assertEqual(sync.request_all(self.manifest.launchable), 1)
                self.assertTrue(sync.wait_idle())
        self.assertEqual(self.runner.verbs().count("clone"), 1)
        self.assertEqual(self.runner.verbs().count("fetch"), 1)

    def test_offline_existing_copy_is_usable_without_git(self) -> None:
        self._materialise(self.cache.checkout_path(self.manifest.launchable[0]))
        with SyncService(self.cache, online=False) as sync:
            sync.request_all(self.manifest.launchable)
            self.assertTrue(sync.wait_idle())
            results = sync.drain()
        self.assertEqual(self.runner.verbs(), [])
        self.assertIs(self._final(results, "streetfighter").status, GameStatus.CACHED_OFFLINE)

    def test_failures_become_states_not_exceptions(self) -> None:
        runner = FakeGitRunner({"clone": GitResult(128, stderr="fatal: no network")})
        cache = RepositoryCache(self.tmp_path / "b", runner=runner)
        with SyncService(cache) as sync:
            sync.request_all(self.manifest.launchable)
            sync.wait_idle()
            results = sync.drain()
        self.assertIs(self._final(results, "streetfighter").status, GameStatus.UNAVAILABLE)

    def test_stop_is_idempotent(self) -> None:
        sync = SyncService(self.cache)
        sync.start()
        sync.stop()
        sync.stop()
        self.assertFalse(sync.is_running)

    def test_drain_is_empty_before_any_request(self) -> None:
        with SyncService(self.cache) as sync:
            self.assertEqual(sync.drain(), [])


class EntrypointStartupSyncTests(TempDirCase):
    def test_each_entrypoint_run_queues_one_startup_check(self) -> None:
        manifest = build_manifest(dict(LAUNCHABLE_RAW), dict(COMING_SOON_RAW))
        runner = FakeGitRunner(on_clone=SyncServiceTests._materialise)
        cache = RepositoryCache(self.tmp_path, runner=runner)
        observed = []

        def gallery_factory(_manifest, _settings, states, sync, **kwargs):
            def ui(state):
                self.assertTrue(sync.wait_idle())
                for update in sync.drain():
                    states[update.game_id] = update
                observed.append(states["streetfighter"].status)
                return UiOutcome(UiAction.QUIT, state.view_mode, state.selected_index)
            return ui

        with (
            patch.object(entrypoint, "load_manifest", return_value=manifest),
            patch.object(entrypoint, "RepositoryCache", return_value=cache),
            patch.object(entrypoint, "load_gallery", return_value=gallery_factory),
            patch.object(entrypoint, "load_settings", return_value=Settings()) as settings,
        ):
            self.assertEqual(entrypoint.main([]), 0)
            self.assertEqual(entrypoint.main([]), 0)
            self.assertEqual(entrypoint.main(["--no-sync"]), 0)
            settings.return_value = Settings(sync_on_start=False)
            self.assertEqual(entrypoint.main([]), 0)

        self.assertEqual(
            observed,
            [GameStatus.READY, GameStatus.READY, GameStatus.CACHED_OFFLINE, GameStatus.CACHED_OFFLINE],
        )
        self.assertEqual(runner.verbs().count("clone"), 1)
        self.assertEqual(runner.verbs().count("fetch"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
