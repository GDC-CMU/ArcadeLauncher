"""Preparation versus Play: fake pip/imports, persisted readiness and safe argv."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from support import TempDirCase, entry
from runtime_support import godot_entry, managed_source, runtime_fixture, write_pack

from launcher.commands import build_child_command
from launcher.errors import LaunchError, ManifestError, PreparationError, RuntimeProvisionError
from launcher.manifest import GodotVersion
from launcher.preparation import PreparationService


class PreparationHarness(TempDirCase):
    def setUp(self):
        super().setUp()
        self.source = managed_source(self.tmp_path / "source with spaces")
        self.store, self.runner, self.downloads = runtime_fixture(self, self.tmp_path / "runtimes")
        self.service = PreparationService(
            self.tmp_path / "prepared", runtimes=self.store, runner=self.runner,
            data_root=self.tmp_path / "persistent saves",
        )

    def python_source(self):
        (self.source / "main.py").write_text("print('fixture')\n", encoding="utf-8")
        (self.source / "requirements.txt").write_text("fixture-dep==1.0\n", encoding="utf-8")
        return entry(python_requirements="requirements.txt")

    def pack_source(self):
        game = godot_entry(startup_script="arcade bootstrap.gd")
        write_pack(self.source / "Flappy Scotty.pck")
        (self.source / "arcade bootstrap.gd").write_text("extends SceneTree\n", encoding="utf-8")
        return game


class PythonPreparationTests(PreparationHarness):
    def test_legacy_python_still_uses_current_interpreter_without_receipt(self):
        game = self.python_source()
        legacy = replace(game, python_requirements=None)
        ready = self.service.resolve(legacy, self.source)
        self.assertEqual(ready.executable, Path(sys.executable))
        self.assertEqual(self.runner.calls, [])

    def test_new_python_cannot_fall_back_to_the_launcher_interpreter(self):
        game = self.python_source()
        with self.assertRaisesRegex(PreparationError, "not been prepared"):
            self.service.resolve(game, self.source)
        with self.assertRaises(LaunchError):
            build_child_command(game, self.source)

    def test_install_is_isolated_and_has_a_dependency_check(self):
        game = self.python_source()
        ready = self.service.prepare(game, self.source)
        self.assertNotEqual(ready.executable, Path(sys.executable))
        venv = self.runner.calls[0][0]
        self.assertIn("--copies", venv)
        self.assertIn("--without-pip", venv)
        self.assertNotIn("--system-site-packages", venv)
        self.assertTrue(any("ensurepip" in args and "-I" in args for args, _, _ in self.runner.calls))
        install = next(args for args, _, _ in self.runner.calls if "install" in args)
        self.assertEqual(install[0], str(ready.executable))
        self.assertIn("--isolated", install)
        self.assertTrue(any("check" in args for args, _, _ in self.runner.calls))
        env = ready.child_environment()
        self.assertEqual(env["ARCADE_MODE"], "1")
        self.assertEqual(env["ARCADE_GAME_DATA_DIR"], str(self.service.data_root / game.id))
        self.assertEqual(env["VIRTUAL_ENV"], str(ready.environment))
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertNotIn(self.service.root, ready.data_dir.parents)

    def test_repeated_prepare_and_offline_resolve_never_reinstall(self):
        game = self.python_source()
        first = self.service.prepare(game, self.source)
        count = len(self.runner.calls)
        with mock.patch.object(self.runner, "run", side_effect=AssertionError("process on reuse")):
            second = self.service.prepare(game, self.source, allow_download=False)
            resolved = self.service.resolve(game, self.source)
        self.assertEqual(first.executable, second.executable)
        self.assertEqual(first, resolved)
        self.assertEqual(len(self.runner.calls), count)

    def test_matching_dependency_files_reuse_one_environment_across_games(self):
        game = self.python_source()
        first = self.service.prepare(game, self.source)
        other = managed_source(self.tmp_path / "another source")
        (other / "main.py").write_text("print('different game')\n", encoding="utf-8")
        (other / "requirements.txt").write_text("fixture-dep==1.0\n", encoding="utf-8")
        second = self.service.prepare(replace(game, id="another-game"), other)
        self.assertEqual(first.executable, second.executable)
        self.assertNotEqual(first.data_dir, second.data_dir)
        self.assertEqual(sum("install" in args for args, _, _ in self.runner.calls), 1)

    def test_nested_requirements_are_part_of_the_fingerprint(self):
        game = self.python_source()
        nested = self.source / "deps"
        nested.mkdir()
        (self.source / "requirements.txt").write_text("-r deps/base.txt\n", encoding="utf-8")
        included = nested / "base.txt"
        included.write_text("fixture-dep==1.0\n", encoding="utf-8")
        first = self.service.prepare(game, self.source)
        included.write_text("fixture-dep==2.0\n", encoding="utf-8")
        with self.assertRaisesRegex(PreparationError, "requirements changed"):
            self.service.resolve(game, self.source)
        second = self.service.prepare(game, self.source)
        self.assertNotEqual(first.executable, second.executable)
        self.assertTrue(first.executable.exists())

    def test_unsafe_pip_inputs_fail_before_a_subprocess(self):
        game = self.python_source()
        for line in ("-r ../outside.txt", "-r requirements.txt", "--target /tmp/escape",
                     "-e .", "thing @ https://example.invalid/a.whl", "${SECRET}",
                     "fixture-dep==1 --prefix=/elsewhere", "file:///tmp/package.whl"):
            (self.source / "requirements.txt").write_text(line + "\n", encoding="utf-8")
            with self.subTest(line=line), self.assertRaises((PreparationError, ManifestError)):
                self.service.prepare(game, self.source)
        self.assertEqual(self.runner.calls, [])

    def test_failed_install_does_not_publish_a_ready_receipt(self):
        game = self.python_source()
        self.runner.fail_install = True
        with self.assertRaisesRegex(PreparationError, "installation failed"):
            self.service.prepare(game, self.source)
        self.assertFalse((self.source / ".git" / "arcade-launcher-prepared.json").exists())
        with self.assertRaises(PreparationError):
            self.service.resolve(game, self.source)
        self.assertEqual(list((self.service.root / "python").iterdir()), [])

    def test_requirements_changed_during_install_cannot_publish_the_old_key(self):
        game = self.python_source()
        original = self.runner.run

        def change_requirements(command, **kwargs):
            result = original(command, **kwargs)
            if "install" in command:
                (self.source / "requirements.txt").write_text("fixture-dep==2.0\n", encoding="utf-8")
            return result

        with mock.patch.object(self.runner, "run", side_effect=change_requirements):
            with self.assertRaisesRegex(PreparationError, "requirements changed during preparation"):
                self.service.prepare(game, self.source)
        self.assertFalse((self.source / ".git" / "arcade-launcher-prepared.json").exists())
        self.assertEqual(list((self.service.root / "python").iterdir()), [])

    def test_offline_unprepared_requirements_do_not_spawn_pip_or_venv(self):
        game = self.python_source()
        with self.assertRaisesRegex(PreparationError, "offline"):
            self.service.prepare(game, self.source, allow_download=False)
        self.assertEqual(self.runner.calls, [])

    def test_missing_or_corrupt_dependency_cannot_be_declared_ready(self):
        game = self.python_source()
        ready = self.service.prepare(game, self.source)
        assert ready.environment is not None
        module = ready.environment / "fixture-site-packages" / "fixture_dep" / "__init__.py"
        module.write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(PreparationError, "corrupt"):
            self.service.resolve(game, self.source)
        module.unlink()
        with self.assertRaises(PreparationError):
            self.service.resolve(game, self.source)

    def test_missing_isolated_interpreter_does_not_use_system_python(self):
        game = self.python_source()
        ready = self.service.prepare(game, self.source)
        ready.executable.unlink()
        with self.assertRaises(PreparationError):
            self.service.resolve(game, self.source)

    def test_receipt_corruption_is_explicit_not_a_legacy_fallback(self):
        game = self.python_source()
        self.service.prepare(game, self.source)
        receipt = self.source / ".git" / "arcade-launcher-prepared.json"
        receipt.write_text("{oops", encoding="utf-8")
        with self.assertRaisesRegex(PreparationError, "receipt"):
            self.service.resolve(game, self.source)

    def test_play_argument_cannot_be_an_interpreter_option(self):
        (self.source / "-c").write_text("print('file')\n", encoding="utf-8")
        self.assertEqual(build_child_command(entry(entrypoint="-c"), self.source)[1], "./-c")

    def test_python_children_keep_inherited_os_userdata_roots(self):
        game = self.python_source()
        for candidate in (replace(game, python_requirements=None), game):
            ready = self.service.prepare(candidate, self.source)
            with self.subTest(requirements=candidate.python_requirements), mock.patch.dict(
                os.environ, {"APPDATA": "inherited-roaming", "XDG_DATA_HOME": "inherited-data"}
            ):
                before = dict(os.environ)
                env = ready.child_environment()
                self.assertEqual(env["APPDATA"], "inherited-roaming")
                self.assertEqual(env["XDG_DATA_HOME"], "inherited-data")
                self.assertEqual(dict(os.environ), before)


class GodotPreparationTests(PreparationHarness):
    def test_dirty_sources_are_content_keyed_and_last_good_is_still_resolvable(self):
        game = self.pack_source()
        head = self.source / ".git" / "HEAD"
        head.write_text("unchanged fixture head\n", encoding="utf-8")
        first = self.service.prepare(game, self.source)
        self.assertTrue(self.service.matches(game, self.source, include_source=True))
        bootstrap = self.source / "arcade bootstrap.gd"
        bootstrap.write_text("extends SceneTree\n# dirty adapter change\n", encoding="utf-8")
        extra = self.source / "untracked asset.txt"
        extra.write_text("candidate content\n", encoding="utf-8")
        self.assertTrue(self.service.matches(game, self.source), "Play only compares launch metadata")
        self.assertFalse(self.service.matches(game, self.source, include_source=True))
        self.assertEqual(self.service.resolve(game, self.source), first)
        second = self.service.prepare(game, self.source, allow_download=False)
        self.assertNotEqual(first.checkout, second.checkout)
        self.assertEqual((second.checkout / extra.name).read_bytes(), extra.read_bytes())
        self.assertEqual((first.checkout / bootstrap.name).read_text(encoding="utf-8"), "extends SceneTree\n")
        self.assertEqual(first.data_dir, second.data_dir)
        self.assertEqual(head.read_text(encoding="utf-8"), "unchanged fixture head\n")
        self.assertTrue(self.service.matches(game, self.source, include_source=True))
        extra.unlink()
        self.assertFalse(self.service.matches(game, self.source, include_source=True))

    def test_edit_during_copy_cannot_publish_under_a_stale_fingerprint(self):
        game = self.pack_source()
        original = shutil.copy2

        def change_copy(source, destination, *args, **kwargs):
            result = original(source, destination, *args, **kwargs)
            if Path(destination).name == "arcade bootstrap.gd":
                Path(destination).write_text("extends SceneTree\n# copy changed\n", encoding="utf-8")
            return result

        with mock.patch("launcher.preparation.shutil.copy2", side_effect=change_copy):
            with self.assertRaisesRegex(PreparationError, "corrupt or modified"):
                self.service.prepare(game, self.source)
        self.assertFalse((self.source / ".git" / "arcade-launcher-prepared.json").exists())
        self.assertFalse(any("--headless" in args for args, _, _ in self.runner.calls))

    def test_source_changed_during_probe_preserves_previous_receipt(self):
        game = self.pack_source()
        first = self.service.prepare(game, self.source)
        receipt = self.source / ".git" / "arcade-launcher-prepared.json"
        old = receipt.read_bytes()
        (self.source / "arcade bootstrap.gd").write_text("extends SceneTree\n# candidate\n", encoding="utf-8")
        original = self.runner.run

        def edit_source(command, **kwargs):
            result = original(command, **kwargs)
            if "--headless" in command:
                (self.source / "late asset.txt").write_text("late edit\n", encoding="utf-8")
            return result

        with mock.patch.object(self.runner, "run", side_effect=edit_source):
            with self.assertRaisesRegex(PreparationError, "source files changed during preparation"):
                self.service.prepare(game, self.source, allow_download=False)
        self.assertEqual(receipt.read_bytes(), old)
        self.assertEqual(self.service.resolve(game, self.source), first)
        self.assertFalse(self.service.matches(game, self.source, include_source=True))

    def test_explicit_checkout_must_not_overlap_caches_or_persistent_data(self):
        game = self.pack_source()
        for root_name in ("root", "data_root"):
            original = getattr(self.service, root_name)
            for overlap in (self.source, self.source / "nested", self.source.parent):
                with self.subTest(root=root_name, overlap=overlap):
                    setattr(self.service, root_name, overlap)
                    with self.assertRaisesRegex(PreparationError, "checkout must not overlap"):
                        self.service.prepare(game, self.source)
                    with self.assertRaisesRegex(PreparationError, "checkout must not overlap"):
                        self.service.resolve(game, self.source)
            setattr(self.service, root_name, original)
        self.assertEqual(self.runner.calls, [])

    def test_godot_userdata_is_child_only_per_game_on_windows_and_linux(self):
        game = self.pack_source()
        ready = self.service.prepare(game, self.source)
        other = replace(ready, entry=replace(game, id="other-game"),
                        data_dir=self.service.data_root / "other-game")
        for host, redirected, unchanged in (
            ("nt", "APPDATA", "XDG_DATA_HOME"),
            ("posix", "XDG_DATA_HOME", "APPDATA"),
        ):
            with self.subTest(host=host), mock.patch.dict(
                os.environ, {"APPDATA": "inherited-roaming", "XDG_DATA_HOME": "inherited-data"}
            ), mock.patch("launcher.preparation.os.name", host):
                before = dict(os.environ)
                env = ready.child_environment()
                self.assertEqual(env[redirected], str(ready.data_dir))
                self.assertEqual(env[unchanged], before[unchanged])
                self.assertEqual(env["ARCADE_GAME_DATA_DIR"], str(ready.data_dir))
                self.assertNotEqual(env[redirected], other.child_environment()[redirected])
                env[redirected] = "child-local-change"
                self.assertEqual(ready.child_environment()[redirected], str(ready.data_dir))
                self.assertEqual(dict(os.environ), before)
        self.assertFalse(ready.data_dir.exists(), "building an environment must not write files")

    def test_space_containing_pack_and_bootstrap_are_single_argv_items(self):
        game = self.pack_source()
        ready = self.service.prepare(game, self.source)
        command = build_child_command(ready.entry, ready.checkout, executable=ready.executable)
        self.assertEqual(command[0], str(ready.executable))
        self.assertEqual(command[command.index("--main-pack") + 1], str(ready.checkout / "Flappy Scotty.pck"))
        self.assertEqual(command[command.index("--script") + 1], str(ready.checkout / "arcade bootstrap.gd"))
        self.assertIn("gl_compatibility", command)
        self.assertNotIn("--editor", command)
        self.assertNotIn("--import", command)
        self.assertNotIn(sys.executable, command)

    def test_pack_probe_runs_once_not_on_play_or_gallery_return(self):
        game = self.pack_source()
        ready = self.service.prepare(game, self.source)
        count = len(self.runner.calls)
        with mock.patch.object(self.runner, "run", side_effect=AssertionError("process on Play")):
            for _ in range(3):
                again = self.service.resolve(game, self.source)
                build_child_command(again.entry, again.checkout, executable=again.executable)
            self.service.prepare(game, self.source, allow_download=False)
        self.assertEqual(ready, again)
        self.assertEqual(len(self.runner.calls), count)

    def test_source_imports_only_in_a_snapshot_and_probes_use_disposable_saves(self):
        game = godot_entry(entrypoint="project.godot")
        (self.source / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        data = self.service.data_root / game.id
        data.mkdir(parents=True)
        save = data / "score.json"
        save.write_text('{"score": 17}', encoding="utf-8")
        ready = self.service.prepare(game, self.source)
        self.assertFalse((self.source / ".godot").exists())
        self.assertTrue((ready.checkout / ".godot" / "imported" / "fixture.ctex").is_file())
        self.assertEqual(save.read_text(encoding="utf-8"), '{"score": 17}')
        for args, _, env in self.runner.calls:
            if "--headless" in args:
                self.assertNotEqual(env["ARCADE_GAME_DATA_DIR"], str(data))
                self.assertEqual(env["ARCADE_MODE"], "1")
                for variable in ("ARCADE_GAME_DATA_DIR", "APPDATA", "XDG_DATA_HOME"):
                    isolated = Path(env[variable])
                    self.assertIn(self.service.root, isolated.parents)
                    self.assertNotIn(self.service.data_root, isolated.parents)
                    self.assertFalse(isolated.exists(), "probe userdata must be disposed")
        self.assertEqual(ready.child_environment()["APPDATA" if os.name == "nt" else "XDG_DATA_HOME"],
                         str(data))

    def test_import_error_even_with_exit_zero_cannot_publish_readiness(self):
        game = godot_entry(entrypoint="project.godot")
        (self.source / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        self.runner.fail_import = True
        with self.assertRaisesRegex(PreparationError, "import failed"):
            self.service.prepare(game, self.source)
        self.assertFalse((self.source / ".godot").exists())
        self.assertFalse((self.source / ".git" / "arcade-launcher-prepared.json").exists())

    def test_failed_repreparation_keeps_last_good_contract_and_snapshot(self):
        game = self.pack_source()
        first = self.service.prepare(game, self.source)
        receipt = self.source / ".git" / "arcade-launcher-prepared.json"
        old = receipt.read_bytes()
        # Metadata/source candidate now needs another engine, but its pack
        # initialization fails. The old contract and immutable artifact survive.
        candidate = replace(game, godot_version=GodotVersion.V4_5_2)
        write_pack(self.source / "Flappy Scotty.pck", GodotVersion.V4_5_2)
        self.runner.fail_probe = True
        with self.assertRaises(PreparationError):
            self.service.prepare(candidate, self.source)
        self.assertEqual(receipt.read_bytes(), old)
        last_good = self.service.resolve(candidate, self.source)
        self.assertEqual(last_good.executable, first.executable)
        self.assertEqual(last_good.checkout, first.checkout)
        self.assertIs(last_good.entry.godot_version, GodotVersion.V4_4_1)
        self.assertFalse(self.service.matches(candidate, self.source))

    def test_pack_header_requires_the_exact_engine_release(self):
        game = self.pack_source()
        write_pack(self.source / "Flappy Scotty.pck", GodotVersion.V4_5_2)
        with self.assertRaisesRegex(PreparationError, "header"):
            self.service.prepare(game, self.source)
        self.assertFalse(any("--headless" in args for args, _, _ in self.runner.calls))

    def test_missing_runtime_disables_only_native_readiness(self):
        game = self.pack_source()
        ready = self.service.prepare(game, self.source)
        ready.executable.unlink()
        with self.assertRaises(RuntimeProvisionError):
            self.service.resolve(game, self.source)
        (self.source / "main.py").write_text("print('legacy')\n", encoding="utf-8")
        separate = managed_source(self.tmp_path / "legacy")
        (separate / "main.py").write_text("print('legacy')\n", encoding="utf-8")
        self.assertEqual(self.service.resolve(entry(), separate).executable, Path(sys.executable))

    def test_changed_prepared_pack_or_import_is_not_ready(self):
        game = self.pack_source()
        ready = self.service.prepare(game, self.source)
        (ready.checkout / "Flappy Scotty.pck").write_bytes(b"damaged")
        with self.assertRaisesRegex(PreparationError, "corrupt"):
            self.service.resolve(game, self.source)

    def test_saved_artifact_pointer_cannot_escape_the_preparation_root(self):
        game = self.pack_source()
        self.service.prepare(game, self.source)
        receipt = self.source / ".git" / "arcade-launcher-prepared.json"
        record = json.loads(receipt.read_text(encoding="utf-8"))
        record["godot_key"] = "../../elsewhere"
        receipt.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(PreparationError, "fingerprint"):
            self.service.resolve(game, self.source)
