"""Real local-git update failures, rollback, userdata and source-edit safety."""

from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from support import FakeGitRunner, TempDirCase, advance_fixture_repo, entry, git_available, make_fixture_repo
from runtime_support import runtime_fixture

from launcher.cache import GitResult, RepositoryCache, SubprocessGitRunner
from launcher.integrity import atomic_json
from launcher.preparation import PreparationService
from launcher.status import GameStatus


@unittest.skipUnless(git_available(), "git is not installed")
class PreparedCacheTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.origin = make_fixture_repo(self.tmp_path / "origin")
        self.game = entry(repository=str(self.origin), python_requirements="requirements.txt")
        (self.origin / "requirements.txt").write_text("fixture-dep==1.0\n", encoding="utf-8")
        (self.origin / ".gitignore").write_text("saves/\n", encoding="utf-8")
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-m", "fixture dependencies and ignored saves")
        self.store, self.runner, _ = runtime_fixture(self, self.tmp_path / "runtimes")
        self.preparation = PreparationService(
            self.tmp_path / "cache" / "prepared", runtimes=self.store, runner=self.runner,
            data_root=self.tmp_path / "userdata",
        )
        self.cache = RepositoryCache(self.tmp_path / "cache", preparation=self.preparation)
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.READY, state.detail)
        self.checkout = self.cache.checkout_path(self.game)
        self.first_commit = self.head(self.checkout)
        self.first = self.cache.prepared_game(self.game)
        self.save = self.checkout / "saves" / "progress.json"
        self.save.parent.mkdir()
        self.save.write_text('{"score": 123}', encoding="utf-8")
        self.first.data_dir.mkdir(parents=True)
        self.external_save = self.first.data_dir / "score.json"
        self.external_save.write_text('{"best": 42}', encoding="utf-8")

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()

    def head(self, cwd):
        return self.git(cwd, "rev-parse", "HEAD")

    def test_failed_new_dependencies_leave_old_commit_and_environment_playable(self):
        (self.origin / "requirements.txt").write_text("fixture-dep==2.0\n", encoding="utf-8")
        advance_fixture_repo(self.origin)
        self.runner.fail_install = True
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        self.assertIn("installation failed", state.detail)
        self.assertEqual(self.head(self.checkout), self.first_commit)
        self.assertEqual(self.cache.prepared_game(self.game).executable, self.first.executable)
        self.assertEqual(self.save.read_text(encoding="utf-8"), '{"score": 123}')
        self.assertEqual(self.external_save.read_text(encoding="utf-8"), '{"best": 42}')
        self.assertTrue(self.cache.verify_only(self.game).status.is_playable)
        self.assertEqual(list((self.cache.root / "staging").iterdir()), [])

    def test_removed_candidate_entrypoint_never_replaces_last_good(self):
        self.git(self.origin, "rm", "main.py")
        self.git(self.origin, "commit", "-m", "fixture broken release")
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        self.assertIn("entrypoint", state.detail)
        self.assertEqual(self.head(self.checkout), self.first_commit)
        self.assertTrue((self.checkout / "main.py").is_file())

    def test_successful_update_preserves_ignored_saves_and_keeps_full_rollback(self):
        new_commit = advance_fixture_repo(self.origin)
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.READY, state.detail)
        self.assertTrue(self.head(self.checkout).startswith(new_commit))
        self.assertEqual(self.save.read_text(encoding="utf-8"), '{"score": 123}')
        backups = list((self.cache.root / "rollback").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.head(backups[0]), self.first_commit)
        self.assertTrue((backups[0] / ".git" / "arcade-launcher-prepared.json").is_file())
        self.assertEqual(self.cache.prepared_game(self.game).executable, self.first.executable)

    def test_rollback_keeps_newest_ignored_and_external_userdata_without_preparation(self):
        advance_fixture_repo(self.origin)
        self.assertIs(self.cache.sync(self.game).status, GameStatus.READY)
        backup = next((self.cache.root / "rollback").iterdir())
        self.save.write_text('{"score": 999}', encoding="utf-8")
        self.external_save.write_text('{"best": 99}', encoding="utf-8")
        with mock.patch.object(self.preparation, "prepare", side_effect=AssertionError("prepare on rollback")):
            state = self.cache.rollback(self.game, backup)
        self.assertIs(state.status, GameStatus.READY, state.detail)
        self.assertEqual(self.head(self.checkout), self.first_commit)
        self.assertEqual(self.save.read_text(encoding="utf-8"), '{"score": 999}')
        self.assertEqual(self.external_save.read_text(encoding="utf-8"), '{"best": 99}')
        self.assertTrue(backup.is_dir(), "rollback must not consume its backup")

    def test_current_source_edits_block_update_and_are_preserved_byte_for_byte(self):
        target = self.checkout / "main.py"
        target.write_text("# local work must survive\n", encoding="utf-8")
        advance_fixture_repo(self.origin)
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        self.assertIn("source edits", state.detail)
        self.assertEqual(target.read_text(encoding="utf-8"), "# local work must survive\n")
        self.assertEqual(self.head(self.checkout), self.first_commit)

    def test_untracked_source_file_blocks_promotion(self):
        source = self.checkout / "experimental.py"
        source.write_text("# local untracked work\n", encoding="utf-8")
        advance_fixture_repo(self.origin)
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        self.assertTrue(source.is_file())
        self.assertEqual(self.head(self.checkout), self.first_commit)

    def test_new_revision_cannot_take_over_an_ignored_save_path(self):
        (self.origin / "saves").mkdir()
        (self.origin / "saves" / "progress.json").write_text("upstream replacement", encoding="utf-8")
        self.git(self.origin, "add", "-f", "saves/progress.json")
        self.git(self.origin, "commit", "-m", "fixture colliding save")
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        self.assertIn("overwrite ignored user data", state.detail)
        self.assertEqual(self.save.read_text(encoding="utf-8"), '{"score": 123}')
        self.assertEqual(self.head(self.checkout), self.first_commit)

    def test_failed_directory_swap_restores_the_original_checkout(self):
        advance_fixture_repo(self.origin)
        original = Path.replace
        target = self.checkout

        def fail_candidate(path, destination):
            if Path(path).parent.name == "staging" and Path(destination) == target:
                raise PermissionError("fixture promotion denied")
            return original(path, destination)

        with mock.patch.object(Path, "replace", fail_candidate):
            state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        self.assertEqual(self.head(self.checkout), self.first_commit)
        self.assertTrue(self.cache.verify_only(self.game).status.is_playable)
        self.assertEqual(self.save.read_text(encoding="utf-8"), '{"score": 123}')

    def test_interrupted_swap_is_readable_offline_without_recovery_writes(self):
        backup = self.cache.root / "rollback" / f"{self.game.id}-interrupted"
        backup.parent.mkdir()
        self.checkout.replace(backup)
        journal = self.cache.root / "state" / f"{self.game.id}.promotion.json"
        atomic_json(journal, {"format": 1, "backup": backup.relative_to(self.cache.root).as_posix()})
        with mock.patch.object(self.cache.runner, "run", side_effect=AssertionError("git on Play")):
            state = self.cache.verify_only(self.game)
            prepared = self.cache.prepared_game(self.game)
        self.assertTrue(state.status.is_playable, state.detail)
        self.assertEqual(prepared.checkout, backup)
        self.assertTrue(journal.is_file(), "readiness must not do recovery writes")
        self.assertFalse(self.checkout.exists())

    def test_offline_failure_and_restart_reuse_actual_prepared_release(self):
        self.cache.runner = FakeGitRunner({"fetch": GitResult(128, stderr="offline fixture")})
        state = self.cache.sync(self.game)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        restarted = RepositoryCache(
            self.cache.root, runner=FakeGitRunner(available=False),
            preparation=PreparationService(
                self.preparation.root, runtimes=self.store, runner=self.runner,
                data_root=self.preparation.data_root,
            ),
        )
        with mock.patch.object(self.runner, "run", side_effect=AssertionError("process on offline readiness")):
            self.assertTrue(restarted.verify_only(self.game).status.is_playable)
            self.assertEqual(restarted.prepared_game(self.game).executable, self.first.executable)

    def test_full_commit_ref_can_be_cloned_without_a_branch_name(self):
        full = self.head(self.origin)
        other = replace(self.game, id="pinned-game", ref=full)
        state = self.cache.sync(other)
        self.assertIs(state.status, GameStatus.READY, state.detail)
        self.assertEqual(self.head(self.cache.checkout_path(other)), full)

    def test_rollback_outside_managed_cache_is_refused(self):
        state = self.cache.rollback(self.game, self.origin)
        self.assertIs(state.status, GameStatus.CACHED_OFFLINE, state.detail)
        self.assertIn("managed cache", state.detail)
        self.assertEqual(self.head(self.checkout), self.first_commit)
