"""GUI から起動した処理が、実際に全工程を動かすこと.

**画面は起動しない。** GUI が使う ``runner`` の経路をそのまま呼ぶ。
子プロセスとして本物の ``pipeline`` が動くので、「処理開始が登録だけで
終わっていないか」をここで確かめられる。

ローカル AI は使わないので映像解析は飛ばす。**接続そのものの検証が
目的**であり、解析内容は test_end_to_end で見ている。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _support import (
    TempAppRootTestCase,
    file_state,
    find_ffmpeg,
    find_ffprobe,
    make_synthetic_video,
)

from local_video_catalog import config as config_module
from local_video_catalog import database as db_module
from local_video_catalog import paths, pipeline
from local_video_catalog.gui import runner as gui_runner
from local_video_catalog.gui import state as gui_state


class GuiPipelineTestCase(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        if find_ffmpeg() is None or find_ffprobe() is None:
            self.skipTest("ffmpeg / ffprobe が必要")
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.video = self.source_root / "clip.mp4"
        self.assertTrue(make_synthetic_video(
            find_ffmpeg(), self.video, duration=3.0))
        self.video_state = file_state(self.video)

        config_module.save_settings_dict({
            "ffmpeg_path": str(find_ffmpeg()),
            "ffprobe_path": str(find_ffprobe()),
            "frames": {"minimum_frame_count": 2, "maximum_frame_count": 2},
        })
        pipeline.clear_stop_request()

    def state(self, **overrides: object) -> gui_state.GuiState:
        values = {
            "source_folder": str(self.source_root),
            "no_time_limit": True, "no_video_limit": True,
            "skip_transcription": True,
        }
        values.update(overrides)
        return gui_state.GuiState(**values)  # type: ignore[arg-type]

    def arguments(self, **overrides: object) -> list[str]:
        """試験用の引数。

        ``--skip-visual`` は**内部専用**で、画面からは出せない。ここでは
        LM Studio を用意せずに処理全体を通すために直接付ける。
        """
        return self.state(**overrides).pipeline_arguments() + ["--skip-visual"]


class StartRunsTheWholePipelineTests(GuiPipelineTestCase):
    def test_start_reaches_the_description_stage(self) -> None:
        """**登録だけで終わらないこと。**"""
        task = gui_runner.start_analysis(self.arguments())
        self.assertFalse(task.result.error, task.result.error)
        result = task.wait(timeout=300)
        self.assertEqual(result.exit_code, 0, result.text)

        with db_module.CatalogDatabase() as database:
            asset_id = database.list_assets_under(
                self.source_root)[0]["asset_id"]
            self.assertTrue(database.is_stage_done(
                asset_id, db_module.STAGE_FRAME_EXTRACTION))
            self.assertTrue(database.is_stage_done(
                asset_id, db_module.STAGE_DESCRIPTION))
            self.assertIsNotNone(database.get_description(asset_id))

    def test_progress_is_visible_in_the_output(self) -> None:
        task = gui_runner.start_analysis(self.arguments())
        result = task.wait(timeout=300)
        for expected in ("動画の登録と基本情報", "代表画像", "説明文"):
            with self.subTest(text=expected):
                self.assertIn(expected, result.text)

    def test_japanese_output_is_not_mangled(self) -> None:
        task = gui_runner.start_analysis(self.arguments())
        result = task.wait(timeout=300)
        self.assertIn("解析", result.text)
        self.assertNotIn("�", result.text)

    def test_source_video_is_untouched(self) -> None:
        task = gui_runner.start_analysis(self.arguments())
        task.wait(timeout=300)
        self.assertEqual(file_state(self.video), self.video_state)


class GuiResumeTests(GuiPipelineTestCase):
    def test_second_run_reports_nothing_left(self) -> None:
        gui_runner.start_analysis(
            self.arguments()).wait(timeout=300)
        second = gui_runner.start_analysis(
            self.arguments()).wait(timeout=300)
        self.assertEqual(second.exit_code, 0)
        self.assertIn("完了しています", second.text)

    def test_preview_changes_nothing(self) -> None:
        result = gui_runner.preview_targets(self.arguments())
        self.assertEqual(result.exit_code, 0, result.text)
        self.assertIn("変更していません", result.text)
        with db_module.CatalogDatabase() as database:
            rows = database.list_assets_under(self.source_root)
            for row in rows:
                self.assertFalse(database.is_stage_done(
                    row["asset_id"], db_module.STAGE_DESCRIPTION))


class GuiSafeStopTests(GuiPipelineTestCase):
    def test_stop_request_ends_the_run_without_killing_it(self) -> None:
        """**プロセスを殺さずに止まること。**"""
        for index in range(4):
            make_synthetic_video(find_ffmpeg(),
                                 self.source_root / f"extra{index}.mp4",
                                 duration=2.0 + index)

        task = gui_runner.start_analysis(self.arguments())
        self.assertFalse(task.result.error)

        # 登録が終わって解析へ入るのを待ってから停止を要求する
        deadline = 120
        seen = ""
        while task.running and deadline > 0:
            seen += "\n".join(task.drain())
            if "代表画像" in seen:
                break
            task.wait(timeout=0.5)
            deadline -= 0.5

        task.request_stop()
        result = task.wait(timeout=300)

        # 強制終了ではないので、終了コードは通常の範囲に収まる
        self.assertIn(result.exit_code, (0, 1), result.text[-2000:])
        self.assertFalse(paths.stop_request_path().exists())

    def test_catalog_update_from_the_gui(self) -> None:
        gui_runner.start_analysis(
            self.arguments()).wait(timeout=300)
        result = gui_runner.update_catalog()
        self.assertEqual(result.exit_code, 0, result.text)
        self.assertTrue(paths.catalog_html_path().is_file())


class GuiRetryTests(GuiPipelineTestCase):
    def test_retry_only_touches_the_named_video(self) -> None:
        make_synthetic_video(find_ffmpeg(), self.source_root / "other.mp4",
                             duration=4.0)
        gui_runner.start_analysis(
            self.arguments()).wait(timeout=300)

        with db_module.CatalogDatabase() as database:
            rows = database.list_assets_under(self.source_root)
            target = rows[0]
            database.set_stage_status(target["asset_id"],
                                      db_module.STAGE_DESCRIPTION,
                                      db_module.STATUS_FAILED)
            other_before = database.get_stage_status(
                rows[1]["asset_id"], db_module.STAGE_DESCRIPTION)["attempt_count"]

        task = gui_runner.retry_failed(self.arguments(),
                                       [target["catalog_id"]])
        result = task.wait(timeout=300)
        self.assertEqual(result.exit_code, 0, result.text)

        with db_module.CatalogDatabase() as database:
            self.assertTrue(database.is_stage_done(
                target["asset_id"], db_module.STAGE_DESCRIPTION))
            other_after = database.get_stage_status(
                rows[1]["asset_id"], db_module.STAGE_DESCRIPTION)["attempt_count"]
        self.assertEqual(other_before, other_after,
                         "指定していない動画が再処理されています。")


class GuiEnvironmentCheckTests(GuiPipelineTestCase):
    def test_environment_check_runs_and_reports(self) -> None:
        result = gui_runner.check_environment(
            ["--quick", "--skip-transcription",
             "--source-folder", str(self.source_root)])
        self.assertIn(result.exit_code, (0, 3))
        self.assertIn("ffmpeg", result.text)
        self.assertIn("保存先", result.text)


if __name__ == "__main__":
    unittest.main()
