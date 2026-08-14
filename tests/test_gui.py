"""GUI のロジック — **画面を起動せずに**検証する.

旧個人版の GUI（PowerShell WinForms・1,871行）は自動テストが事実上
不可能だった。新版は状態管理と子プロセス制御を tkinter から分離して
あるので、ここで押さえられる。
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import paths, pipeline
from local_video_catalog.gui import runner as gui_runner
from local_video_catalog.gui import state as gui_state


class SeparationTests(unittest.TestCase):
    """**tkinter に依存しないこと。** 依存すると試験できなくなる。"""

    def _imports(self, module) -> set[str]:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        return found

    def test_state_does_not_import_tkinter(self) -> None:
        self.assertNotIn("tkinter", self._imports(gui_state))

    def test_runner_does_not_import_tkinter(self) -> None:
        self.assertNotIn("tkinter", self._imports(gui_runner))


class StatePersistenceTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()

    def test_defaults_when_nothing_saved(self) -> None:
        state = gui_state.load()
        self.assertEqual(state.source_folder, "")
        self.assertEqual(state.time_budget_minutes, 60)

    def test_round_trip(self) -> None:
        state = gui_state.GuiState(
            source_folder="X:/videos", recursive=True,
            time_budget_minutes=120, skip_visual_analysis=True)
        self.assertTrue(gui_state.save(state))
        self.assertEqual(gui_state.load(), state)

    def test_state_lives_in_userdata(self) -> None:
        gui_state.save(gui_state.GuiState())
        self.assertTrue(paths.gui_state_path().is_file())
        self.assertTrue(
            str(paths.gui_state_path()).startswith(str(paths.userdata_dir())))

    def test_no_hidden_location_is_used(self) -> None:
        """**%LOCALAPPDATA% を使わない。** 旧版はここに書いていた。

        方針を説明した docstring は除いて、実際のコードだけを見る。
        """
        from _support import code_strings_and_calls

        strings, calls = code_strings_and_calls(gui_state)
        for text in strings:
            self.assertNotIn("LOCALAPPDATA", text.upper())
            self.assertNotIn("APPDATA", text.upper())
        for forbidden in ("expanduser", "home", "getenv"):
            self.assertNotIn(forbidden, calls)

    def test_broken_file_falls_back_to_defaults(self) -> None:
        paths.gui_state_path().write_text("{ not json", encoding="utf-8")
        self.assertEqual(gui_state.load(), gui_state.GuiState())

    def test_unknown_keys_are_ignored(self) -> None:
        """古い版が保存した状態を読んでも落ちないこと。"""
        paths.gui_state_path().write_text(
            json.dumps({"source_folder": "X:/v", "DataRoot": "D:/old",
                        "removed_option": True}), encoding="utf-8")
        state = gui_state.load()
        self.assertEqual(state.source_folder, "X:/v")
        self.assertFalse(hasattr(state, "DataRoot"))

    def test_wrong_types_fall_back_to_defaults(self) -> None:
        paths.gui_state_path().write_text(
            json.dumps({"time_budget_minutes": "abc"}), encoding="utf-8")
        self.assertEqual(gui_state.load().time_budget_minutes, 60)

    def test_save_is_atomic(self) -> None:
        gui_state.save(gui_state.GuiState())
        temp = paths.gui_state_path().with_suffix(
            paths.gui_state_path().suffix + ".tmp")
        self.assertFalse(temp.exists())

    def test_save_failure_is_reported_not_raised(self) -> None:
        """保存に失敗しても解析には影響しない。黙って続けられること。"""
        unwritable = self.temp_dir / "nope" / "deep" / "gui-state.json"
        result = gui_state.save(gui_state.GuiState(),
                                path=unwritable / "impossible" / "x.json")
        self.assertIsInstance(result, bool)


class RunConditionTests(unittest.TestCase):
    """画面の設定が、実行条件へ正しく変換されること。"""

    def test_no_time_limit_means_zero(self) -> None:
        state = gui_state.GuiState(time_budget_minutes=60, no_time_limit=True)
        self.assertEqual(state.effective_time_budget(), 0.0)

    def test_no_video_limit_means_zero(self) -> None:
        state = gui_state.GuiState(max_videos=10, no_video_limit=True)
        self.assertEqual(state.effective_max_videos(), 0)

    def test_limits_are_passed_when_enabled(self) -> None:
        state = gui_state.GuiState(time_budget_minutes=120, max_videos=5,
                                   no_time_limit=False, no_video_limit=False)
        self.assertEqual(state.effective_time_budget(), 120.0)
        self.assertEqual(state.effective_max_videos(), 5)

    def test_negative_values_are_clamped(self) -> None:
        state = gui_state.GuiState(time_budget_minutes=-5, max_videos=-1,
                                   no_time_limit=False, no_video_limit=False)
        self.assertEqual(state.effective_time_budget(), 0.0)
        self.assertEqual(state.effective_max_videos(), 0)

    def test_arguments_include_the_source_folder(self) -> None:
        args = gui_state.GuiState(source_folder="X:/videos").pipeline_arguments()
        self.assertIn("--source-folder", args)
        self.assertIn("X:/videos", args)

    def test_skip_flags_are_passed(self) -> None:
        args = gui_state.GuiState(skip_visual_analysis=True,
                                  skip_transcription=True,
                                  recycle_cache=True).pipeline_arguments()
        for flag in ("--skip-visual", "--skip-transcription", "--recycle-cache"):
            self.assertIn(flag, args)

    def test_storage_location_is_never_passed(self) -> None:
        """**保存先を画面から指定させない。** One-Folder 原則を守る。"""
        args = gui_state.GuiState(source_folder="X:/v").pipeline_arguments()
        text = " ".join(args)
        for forbidden in ("--data-root", "--output", "--userdata",
                          "--catalog-dir"):
            self.assertNotIn(forbidden, text)


class ChildProcessTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()

    def test_utf8_is_forced_for_children(self) -> None:
        """**日本語 Windows で出力を取り込むと化けるため必須。**"""
        env = gui_runner.child_environment()
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_source_directory_is_on_the_path(self) -> None:
        env = gui_runner.child_environment()
        self.assertIn(str(paths.app_root() / "src"), env["PYTHONPATH"])

    def test_command_uses_the_running_interpreter(self) -> None:
        """python.exe を探し回らない。画面が動いている Python を使う。"""
        command = gui_runner.build_command("some.module", ["--flag"])
        self.assertEqual(command[0], sys.executable)
        self.assertIn("-m", command)
        self.assertIn("some.module", command)
        self.assertIn("--flag", command)

    def test_utf8_mode_flag_is_present(self) -> None:
        self.assertIn("-X", gui_runner.build_command("m", []))
        self.assertIn("utf8", gui_runner.build_command("m", []))


class BackgroundTaskTests(TempAppRootTestCase):
    """出力を 1 行ずつ受け取り、画面をブロックしないこと。"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()

    def _task(self, code: str) -> gui_runner.BackgroundTask:
        task = gui_runner.BackgroundTask("x", [])
        task.command = [sys.executable, "-X", "utf8", "-c", code]
        return task

    def test_output_is_collected(self) -> None:
        task = self._task("print('一行目'); print('二行目')")
        task.start()
        result = task.wait(timeout=30)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("一行目", result.text)
        self.assertIn("二行目", result.text)

    def test_japanese_is_not_mangled(self) -> None:
        task = self._task("print('代表画像の抽出 … 完了')")
        task.start()
        result = task.wait(timeout=30)
        self.assertIn("代表画像の抽出", result.text)

    def test_drain_does_not_block(self) -> None:
        task = self._task("import time; time.sleep(0.2); print('done')")
        task.start()
        self.assertIsInstance(task.drain(), list)   # 待たずに返る
        task.wait(timeout=30)
        self.assertIn("done", task.result.text)

    def test_exit_code_is_reported(self) -> None:
        task = self._task("raise SystemExit(3)")
        task.start()
        self.assertEqual(task.wait(timeout=30).exit_code, 3)

    def test_stderr_is_merged(self) -> None:
        task = self._task("import sys; print('エラー内容', file=sys.stderr)")
        task.start()
        self.assertIn("エラー内容", task.wait(timeout=30).text)

    def test_missing_program_is_reported_not_raised(self) -> None:
        task = gui_runner.BackgroundTask("x", [])
        task.command = [str(self.temp_dir / "does-not-exist.exe")]
        task.start()
        self.assertTrue(task.result.error)
        self.assertFalse(task.result.ok)


class SafeStopTests(TempAppRootTestCase):
    """**停止はファイル生成。プロセスを殺さない。**"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        pipeline.clear_stop_request()

    def test_request_creates_the_file(self) -> None:
        task = gui_runner.BackgroundTask("x", [])
        target = task.request_stop()
        self.assertTrue(target.is_file())
        self.assertEqual(target, paths.stop_request_path())

    def test_runner_never_kills_a_process(self) -> None:
        source = Path(gui_runner.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for forbidden in ("kill", "terminate", "send_signal"):
            with self.subTest(name=forbidden):
                self.assertNotIn(
                    forbidden, called,
                    f"runner が {forbidden}() を呼んでいます。"
                    "安全停止はファイル生成で行う方針に反します。")


class RetryTests(TempAppRootTestCase):
    def test_retry_narrows_the_target_only(self) -> None:
        """**工程の再利用ルールは変えない。** 対象を絞るだけ。"""
        base = gui_state.GuiState(source_folder="X:/v").pipeline_arguments()
        task = gui_runner.BackgroundTask(
            gui_runner.MODULE_PIPELINE,
            [*base, "--only-catalog-id", "VID-000003"])
        self.assertIn("--only-catalog-id", task.command)
        self.assertIn("VID-000003", task.command)
        for forbidden in ("--force", "--no-resume", "--rebuild"):
            self.assertNotIn(forbidden, task.command)


class FeatureParityTests(unittest.TestCase):
    """機能パリティ表が、実際の入口と食い違っていないこと。"""

    def test_every_documented_module_exists(self) -> None:
        for module in (gui_runner.MODULE_PIPELINE, gui_runner.MODULE_ENVIRONMENT,
                       gui_runner.MODULE_CATALOG, gui_runner.MODULE_PROGRESS):
            with self.subTest(module=module):
                __import__(module)

    def test_parity_document_lists_the_dropped_features(self) -> None:
        from _support import APP_ROOT

        text = (APP_ROOT / "docs" / "GUI_FEATURE_PARITY.md").read_text(
            encoding="utf-8")
        self.assertIn("削除する機能", text)
        self.assertIn("GUI作業履歴の整理", text)
        self.assertIn("保存先の選択", text)


if __name__ == "__main__":
    unittest.main()
