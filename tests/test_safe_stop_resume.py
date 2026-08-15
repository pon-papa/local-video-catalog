"""安全停止 → 続きから（Resume）を、実運用と同じ筋書きで確かめる.

これから利用者が実動画で行う試験と同じ形を、合成動画で先に通す。

    1 本目は完走させる
    2 本目の途中（代表画像＋映像解析まで終わったところ）で安全停止
    もう一度実行すると、
        1 本目      … 触らない
        2 本目      … 終わった工程は再利用し、**説明文から続ける**
        3 本目以降  … 新しく着手する

**このとき「中間ファイルをゴミ箱へ移動する」は OFF。** cleanup を同時に
動かすと、うまくいかなかったときに Resume の問題なのか cleanup の問題
なのか切り分けられなくなる。cleanup は別の試験で確かめる。

``pipeline.run()`` を通すのは、``processing_runs`` への停止理由の記録まで
含めて確かめたいため（利用者が停止後に最初に見るのがそこ）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from _support import file_state, find_ffmpeg, find_ffprobe, make_synthetic_video
from test_end_to_end import FAKE_MODEL, FakeClient, EndToEndTestCase

from local_video_catalog import database as db_module
from local_video_catalog import html_catalog, paths, pipeline
from local_video_catalog.stages import description as description_stage
from local_video_catalog.stages import visual as visual_stage

VIDEO_COUNT = 4


class SafeStopResumeTestCase(EndToEndTestCase):
    """合成動画を複数本そろえて、止めて、続きから動かす。"""

    def setUp(self) -> None:
        super().setUp()
        # 土台は 1 本しか作らないので、残りをここで足す。
        self.videos = [self.video]
        for index in range(2, VIDEO_COUNT + 1):
            extra = self.source_root / f"clip_{index}.mp4"
            self.assertTrue(make_synthetic_video(
                find_ffmpeg(), extra, duration=3.0, with_audio=True))
            self.videos.append(extra)
        self.video_states = {p: file_state(p) for p in self.videos}

    # -- 実行 --------------------------------------------------------------

    def run_module(self, *, max_videos: int = 0, recycle_cache: bool = False,
                   runners: pipeline.StageRunners | None = None) -> int:
        """``pipeline.run()`` を通す。**台帳への記録まで含めて動かす。**"""
        arguments = [
            "--source-folder", str(self.source_root),
            "--time-budget-minutes", "0",
            "--max-videos", str(max_videos),
            "--skip-transcription",
            # 偽のローカルAIに合わせる。**利用者の設定を持ち込まない。**
            "--visual-model", FAKE_MODEL,
            "--description-model", FAKE_MODEL,
        ]
        if recycle_cache:
            arguments.append("--recycle-cache")
        args = pipeline.build_parser().parse_args(arguments)
        return pipeline.run(args, runners or self.runners())

    def stop_after(self, stage_name: str,
                   *, on_video: int) -> pipeline.StageRunners:
        """指定の工程が ``on_video`` 本目で終わった直後に停止要求を出す。

        **強制終了ではない。** 実際の「安全停止」と同じで、次の工程へ
        入る前の判定で止まる。
        """
        runners = self.runners()
        original = getattr(runners, stage_name)
        seen: list[str] = []

        def wrapper(asset_id, ctx):
            outcome = original(asset_id, ctx)
            if asset_id not in seen:
                seen.append(asset_id)
            if len(seen) == on_video:
                pipeline.request_stop()
            return outcome

        setattr(runners, stage_name, wrapper)
        return runners

    # -- 台帳の読み取り ----------------------------------------------------

    def assets_in_order(self) -> list[str]:
        return [row["asset_id"]
                for row in self.db.list_assets_under(self.source_root)]

    def stages_of(self, asset_id: str) -> dict[str, bool]:
        """今回行う工程だけの完了状況。

        **飛ばした工程を「未完了」に数えない。** 数えると、文字起こしを
        飛ばしている試験で永久に「完走していない」ことになる。
        """
        return {stage: self.db.is_stage_done(asset_id, stage)
                for stage, _label in db_module.PIPELINE_STAGES
                if stage not in self.skip_stages}

    def is_complete(self, asset_id: str) -> bool:
        return all(self.stages_of(asset_id).values())

    def latest_run(self):
        return self.db.connection.execute(
            "SELECT * FROM processing_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()


class StopIsRecordedTests(SafeStopResumeTestCase):
    """A / B. 停止したことが、あとから読める形で残ること。"""

    def test_the_stop_reason_reaches_the_database(self) -> None:
        """**利用者が停止後に最初に見るのがここ。**"""
        self.run_module(max_videos=3,
                        runners=self.stop_after("visual_analysis", on_video=1))
        row = self.latest_run()
        self.assertIsNotNone(row)
        self.assertEqual(row["stop_reason"], pipeline.STOP_REQUESTED)

    def test_the_run_ends_normally(self) -> None:
        """止めても異常終了にしない。**成果は保存済み。**"""
        code = self.run_module(
            max_videos=3, runners=self.stop_after("visual_analysis",
                                                  on_video=1))
        self.assertEqual(code, pipeline.EXIT_OK)

    def test_the_stop_request_is_cleared_afterwards(self) -> None:
        """次回の実行が、いきなり止まらないこと。"""
        self.run_module(max_videos=3,
                        runners=self.stop_after("visual_analysis", on_video=1))
        self.assertFalse(paths.stop_request_path().exists())

    def test_the_finished_stage_is_kept(self) -> None:
        self.run_module(max_videos=3,
                        runners=self.stop_after("visual_analysis", on_video=1))
        first = self.assets_in_order()[0]
        stages = self.stages_of(first)
        self.assertTrue(stages[db_module.STAGE_FRAME_EXTRACTION])
        self.assertTrue(stages[db_module.STAGE_VISUAL_ANALYSIS])
        self.assertFalse(stages[db_module.STAGE_DESCRIPTION],
                         "停止要求のあとの工程まで進んでいます。")


class ResumeContinuesTests(SafeStopResumeTestCase):
    """C / D / K. 途中の動画を、途中から続けること。"""

    def setUp(self) -> None:
        super().setUp()
        # 1 本目は完走、2 本目は映像解析まででとめる
        self.run_module(max_videos=1)
        self.run_module(max_videos=1,
                        runners=self.stop_after("visual_analysis", on_video=1))
        self.first, self.second = self.assets_in_order()[:2]

    def test_the_setup_is_what_we_think_it_is(self) -> None:
        self.assertTrue(self.is_complete(self.first),
                        "1 本目が完走していません。")
        second = self.stages_of(self.second)
        self.assertTrue(second[db_module.STAGE_FRAME_EXTRACTION])
        self.assertTrue(second[db_module.STAGE_VISUAL_ANALYSIS])
        self.assertFalse(second[db_module.STAGE_DESCRIPTION])

    def test_resume_finishes_the_half_done_video(self) -> None:
        self.run_module(max_videos=3)
        self.assertTrue(self.is_complete(self.second),
                        "途中だった動画が完了していません。")

    def test_finished_stages_are_not_run_again(self) -> None:
        """C. **終わった工程は再利用する。** やり直さない。"""
        before = self.db.get_stage_status(
            self.second, db_module.STAGE_VISUAL_ANALYSIS)["attempt_count"]
        self.run_module(max_videos=3)
        after = self.db.get_stage_status(
            self.second, db_module.STAGE_VISUAL_ANALYSIS)["attempt_count"]
        self.assertEqual(before, after, "映像の解析がやり直されています。")

    def test_the_completed_video_is_left_alone(self) -> None:
        """E. 完了済みの動画に触らない。"""
        def attempts() -> dict[str, int]:
            return {stage: self.db.get_stage_status(
                        self.first, stage)["attempt_count"]
                    for stage in self.stages_of(self.first)}

        before = attempts()
        self.run_module(max_videos=3)
        after = attempts()
        self.assertEqual(before, after, "完了済みの動画を作り直しています。")

    def test_the_limit_counts_the_resumed_video(self) -> None:
        """K. 上限 2 本なら、**途中の 1 本＋新しい 1 本**。

        **途中の動画を「ただ」にしない。** 数に入れないと、上限を
        指定しているのに毎回それより多く動くことになる。

        ここでは 1 本目は前回完走済み、2 本目が途中。上限 2 で再開すると
        2 本目と 3 本目が終わり、**4 本目には手をつけない**のが期待動作。
        """
        self.run_module(max_videos=2)
        assets = self.assets_in_order()
        self.assertTrue(self.is_complete(assets[1]), "途中の動画が終わっていません。")
        self.assertTrue(self.is_complete(assets[2]), "新しい 1 本が終わっていません。")
        self.assertFalse(self.is_complete(assets[3]),
                         "上限を超えて 4 本目まで処理しています。")

    def test_the_previous_run_is_not_counted_again(self) -> None:
        """前回完走した動画は、今回の本数に数えない。"""
        self.run_module(max_videos=2)
        done = [a for a in self.assets_in_order() if self.is_complete(a)]
        # 前回の 1 本 ＋ 今回の 2 本
        self.assertEqual(len(done), 3, f"完了本数が想定と違います: {done}")

    def test_the_visual_result_is_reused_not_regenerated(self) -> None:
        """G. cleanup OFF なので、中間成果がそのまま使える。"""
        rows = self.db.connection.execute(
            "SELECT visual_run_id FROM visual_analysis_runs WHERE asset_id = ?",
            (self.second,)).fetchall()
        self.run_module(max_videos=3)
        after = self.db.connection.execute(
            "SELECT visual_run_id FROM visual_analysis_runs WHERE asset_id = ?",
            (self.second,)).fetchall()
        self.assertEqual(len(rows), len(after),
                         "映像解析が作り直されています。")


class NoDuplicationTests(SafeStopResumeTestCase):
    """H / I. 止めて続けても、成果物が二重にならないこと。"""

    def setUp(self) -> None:
        super().setUp()
        self.run_module(max_videos=1,
                        runners=self.stop_after("visual_analysis", on_video=1))
        self.asset = self.assets_in_order()[0]
        self.run_module(max_videos=1)

    def test_one_description_row_per_video(self) -> None:
        count = self.db.connection.execute(
            "SELECT COUNT(*) FROM asset_descriptions WHERE asset_id = ?",
            (self.asset,)).fetchone()[0]
        self.assertEqual(count, 1, "説明文が二重に作られています。")

    def test_one_description_file_per_video(self) -> None:
        files = list(paths.descriptions_dir().glob("*.txt"))
        self.assertEqual(len(files), 1, f"説明文ファイルが複数あります: {files}")

    def test_the_catalog_lists_each_video_once(self) -> None:
        """I. HTML に同じ台帳 ID が 2 度出ないこと。"""
        records = html_catalog.collect_records()
        ids = [r.catalog_id for r in records]
        self.assertEqual(len(ids), len(set(ids)), f"重複しています: {ids}")


class CleanupIsOffTests(SafeStopResumeTestCase):
    """F. cleanup OFF のあいだ、中間成果を消さないこと。"""

    def test_the_cache_survives_a_completed_video(self) -> None:
        self.run_module(max_videos=1, recycle_cache=False)
        frames = list(paths.frames_cache_dir().rglob("*.jpg"))
        self.assertTrue(frames, "代表画像が消えています。")

    def test_no_cleanup_is_recorded(self) -> None:
        self.run_module(max_videos=1, recycle_cache=False)
        asset = self.assets_in_order()[0]
        row = self.db.get_description(asset)
        self.assertIsNotNone(row)
        self.assertIn(row["cache_cleanup_status"], (None, "", "pending"),
                      "cleanup OFF なのに整理が記録されています。")

    def test_the_cache_survives_a_stop_and_resume(self) -> None:
        self.run_module(max_videos=1,
                        runners=self.stop_after("frame_extraction",
                                                on_video=1))
        during = sorted(p.name for p in paths.frames_cache_dir().rglob("*.jpg"))
        self.assertTrue(during, "停止後に代表画像が残っていません。")
        self.run_module(max_videos=1, recycle_cache=False)
        after = sorted(p.name for p in paths.frames_cache_dir().rglob("*.jpg"))
        self.assertEqual(during, after, "再開で代表画像が作り直されています。")


class SourceIsUntouchedTests(SafeStopResumeTestCase):
    """J. 止めても続けても、元動画に触らないこと。"""

    def test_the_videos_are_unchanged(self) -> None:
        self.run_module(max_videos=2,
                        runners=self.stop_after("visual_analysis", on_video=2))
        self.run_module(max_videos=2)
        for path, before in self.video_states.items():
            with self.subTest(name=path.name):
                self.assertEqual(file_state(path), before)

    def test_the_source_folder_gains_no_files(self) -> None:
        before = sorted(p.name for p in self.source_root.rglob("*"))
        self.run_module(max_videos=1,
                        runners=self.stop_after("visual_analysis", on_video=1))
        self.run_module(max_videos=1)
        after = sorted(p.name for p in self.source_root.rglob("*"))
        self.assertEqual(before, after)


class JapanesePathTests(SafeStopResumeTestCase):
    """L. 日本語を含む場所でも、止めて続けられること。"""

    app_root_name = "動画カタログ 日本語"

    def test_stop_and_resume(self) -> None:
        self.assertIn("日本語", str(paths.app_root()))
        self.run_module(max_videos=1,
                        runners=self.stop_after("visual_analysis", on_video=1))
        self.assertEqual(self.latest_run()["stop_reason"],
                         pipeline.STOP_REQUESTED)
        self.run_module(max_videos=1)
        asset = self.assets_in_order()[0]
        self.assertTrue(self.is_complete(asset))


if __name__ == "__main__":
    unittest.main()
