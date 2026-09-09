"""Explicit dirty-checkout staging through the real operator CLI, offline."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from support import COMING_SOON_RAW, LAUNCHABLE_RAW, TempDirCase, manifest_document
from runtime_support import managed_source, runtime_fixture, write_pack

from launcher.manifest import GodotVersion
from launcher.preparation import PreparationService
from launcher.status import GameStatus
from tools.prepare_games import main


class ExplicitCheckoutTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.source = managed_source(self.tmp_path / "dirty candidate with spaces")
        write_pack(self.source / "Space Game.pck")
        (self.source / "bootstrap.gd").write_text("extends SceneTree\n", encoding="utf-8")
        self.cache_root = self.tmp_path / "stage cache"
        self.data_root = self.tmp_path / "durable data"
        self.manifest = self.tmp_path / "staged manifest.json"
        self.raw = {
            **LAUNCHABLE_RAW, "id": "candidate-game", "runtime": "godot",
            "godot_version": "4.4.1-stable", "entrypoint": "Space Game.pck",
            "startup_script": "bootstrap.gd",
        }
        self.write_manifest()
        self.store, self.runner, self.downloads = runtime_fixture(self, self.tmp_path / "engines")
        self.store.ensure(GodotVersion.V4_4_1)
        self.args = [
            "--manifest", str(self.manifest), "--game", "candidate-game",
            "--checkout", str(self.source), "--cache", str(self.cache_root),
            "--game-data-root", str(self.data_root),
        ]
        for target in ("launcher.cache.SubprocessGitRunner.run", "launcher.cache.SubprocessGitRunner.available"):
            patcher = mock.patch(target, side_effect=AssertionError("git during explicit checkout staging"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("tools.prepare_games.RuntimeStore", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "tools.prepare_games.PreparationService",
            side_effect=lambda root, **kwargs: PreparationService(root, runner=self.runner, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_manifest(self):
        self.manifest.write_text(json.dumps(manifest_document(self.raw, dict(COMING_SOON_RAW))), encoding="utf-8")

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            result = main([*self.args, *extra])
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        return result, records[0]

    def test_explicit_roots_prepare_current_bytes_without_git_or_cache_promotion(self):
        (self.source / "untracked.txt").write_text("dirty working bytes\n", encoding="utf-8")
        code, record = self.invoke("--offline")
        self.assertEqual(code, 0, record)
        self.assertTrue(record["requested_release_prepared"])
        self.assertEqual(record["source_checkout"], str(self.source.resolve()))
        snapshot = Path(record["checkout"])
        self.assertIn(self.cache_root, snapshot.parents)
        self.assertEqual((snapshot / "untracked.txt").read_text(encoding="utf-8"), "dirty working bytes\n")
        self.assertEqual(record["game_data_dir"], str(self.data_root / "candidate-game"))
        self.assertFalse((self.cache_root / "games").exists(), "explicit sources are not silently installed")
        self.assertFalse(self.data_root.exists(), "preparation must not touch durable saves")
        self.assertEqual(len(self.downloads), 1, "only the pre-seeded fixture engine was downloaded")

    def test_dirty_source_verify_reports_last_good_not_a_prepared_candidate(self):
        code, first = self.invoke("--offline")
        self.assertEqual(code, 0, first)
        (self.source / "bootstrap.gd").write_text("extends SceneTree\n# edited without a commit\n", encoding="utf-8")
        code, stale = self.invoke("--verify-only")
        self.assertEqual(code, 1, stale)
        self.assertFalse(stale["requested_release_prepared"])
        self.assertEqual(stale["status"], GameStatus.CACHED_OFFLINE.value)
        self.assertEqual(stale["checkout"], first["checkout"])
        code, prepared = self.invoke("--offline")
        self.assertEqual(code, 0, prepared)
        self.assertNotEqual(prepared["checkout"], first["checkout"])
        self.assertEqual(prepared["game_data_dir"], first["game_data_dir"])

    def test_verify_only_does_not_prepare_spawn_or_write_receipts(self):
        code, first = self.invoke("--offline")
        self.assertEqual(code, 0, first)
        with mock.patch.object(self.runner, "run", side_effect=AssertionError("process during verify")), \
                mock.patch.object(self.store, "ensure", side_effect=AssertionError("ensure during verify")), \
                mock.patch("launcher.preparation.atomic_json", side_effect=AssertionError("receipt write during verify")):
            code, record = self.invoke("--verify-only")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["checkout"], first["checkout"])

    def test_failed_candidate_returns_nonzero_and_reports_preserved_last_good(self):
        code, first = self.invoke("--offline")
        self.assertEqual(code, 0, first)
        receipt = self.source / ".git" / "arcade-launcher-prepared.json"
        previous = receipt.read_bytes()
        (self.source / "bootstrap.gd").write_text("extends SceneTree\n# bad candidate\n", encoding="utf-8")
        self.runner.fail_probe = True
        code, record = self.invoke("--offline")
        self.assertEqual(code, 1, record)
        self.assertFalse(record["requested_release_prepared"])
        self.assertEqual(record["checkout"], first["checkout"])
        self.assertEqual(receipt.read_bytes(), previous)

    def test_unprepared_verify_fails_without_materializing_any_cache(self):
        with mock.patch.object(self.runner, "run", side_effect=AssertionError("process during verify")):
            code, record = self.invoke("--verify-only")
        self.assertEqual(code, 1, record)
        self.assertEqual(record["status"], GameStatus.UNAVAILABLE.value)
        self.assertFalse(self.cache_root.exists())

    def test_python_candidate_uses_the_explicit_checkout_and_isolated_dependencies(self):
        self.raw = {
            **LAUNCHABLE_RAW, "id": "candidate-game", "entrypoint": "main.py",
            "python_requirements": "requirements.txt",
        }
        self.write_manifest()
        (self.source / "main.py").write_text("print('dirty Python candidate')\n", encoding="utf-8")
        (self.source / "requirements.txt").write_text("fixture-dep==1.0\n", encoding="utf-8")
        code, record = self.invoke()
        self.assertEqual(code, 0, record)
        self.assertEqual(record["checkout"], str(self.source))
        self.assertIn(self.cache_root / "prepared" / "python", Path(record["executable"]).parents)
        self.assertEqual(self.invoke("--offline")[0], 0)

    def test_checkout_requires_explicit_roots_and_one_enabled_game(self):
        for flag in ("--game", "--cache", "--game-data-root"):
            args = list(self.args)
            index = args.index(flag)
            del args[index:index + 2]
            with self.subTest(missing=flag), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    main(args)
                self.assertEqual(stopped.exception.code, 2)
        for extra in (
            ["--game", "flappy-scotty"],
            ["--game", "candidate-game"],
            ["--rollback", str(self.tmp_path / "backup")],
        ):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    main([*self.args, *extra])
                self.assertEqual(stopped.exception.code, 2)
        self.assertFalse((self.source / ".git" / "arcade-launcher-prepared.json").exists())
