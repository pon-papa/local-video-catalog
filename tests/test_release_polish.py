"""公開前の仕上げ 5 点を、実運用で起きた形のまま固定する.

どれも解析のやり方は変えない。**利用者から見た挙動のずれ**を直したもの。

1. 整理する設定なら、以前 OFF で終わった動画の中間ファイルも片づく
2. 解析が終わったら HTML カタログが勝手に新しくなる
3. 経過時間・残り時間が画面に出る（ログは汚さない）
4. 止めた／時間に達しただけの中断を「失敗」と数えない
5. 本数制限なしのとき、候補を全部並べて画面を埋めない
"""

from __future__ import annotations

import unittest
from pathlib import Path

from _support import APP_ROOT, file_state, find_ffmpeg, make_synthetic_video
from test_end_to_end import FAKE_MODEL
from test_safe_stop_resume import SafeStopResumeTestCase

from local_video_catalog import database as db_module
from local_video_catalog import html_catalog, paths, pipeline, recycle, selection

NEWLINE = "\n"


class CleanupSweepTests(SafeStopResumeTestCase):
    """1. 整理は「完了済みの動画すべて」を見る。"""

    def frames_of(self, asset_id: str) -> list[Path]:
        return [p for p in paths.frames_cache_dir().rglob("*.jpg")
                if asset_id in str(p)]

    def test_a_video_finished_while_cleanup_was_off_is_cleaned_later(self
                                                                    ) -> None:
        """**これが今回の本題。** 昔の残りが片づくこと。"""
        self.run_module(max_videos=1, recycle_cache=False)
        first = self.assets_in_order()[0]
        self.assertTrue(self.frames_of(first), "前提: 中間ファイルが残ること")

        # 次の実行で整理を入れる。1 本目は今回の対象ではない。
        self.run_module(max_videos=1, recycle_cache=True)
        self.assertFalse(self.frames_of(first),
                         "以前 OFF で完了した動画が整理されていません。")

    def test_running_it_again_is_safe(self) -> None:
        """3. 何度実行しても壊れない（冪等）。"""
        self.run_module(max_videos=1, recycle_cache=True)
        first = self.assets_in_order()[0]
        self.assertFalse(self.frames_of(first))
        self.run_module(max_videos=1, recycle_cache=True)
        self.assertFalse(self.frames_of(first))

    def test_the_second_pass_reports_nothing_new(self) -> None:
        self.run_module(max_videos=2, recycle_cache=True)
        self.run_module(max_videos=0, recycle_cache=True)
        # 2 回目は「すでに整理済み」に寄る
        summary = self.last_cleanup()
        self.assertIsNotNone(summary)
        self.assertEqual(summary.failed, 0)
        self.assertGreaterEqual(summary.already_clean, 1)

    def test_an_unfinished_video_is_never_cleaned(self) -> None:
        """4 / 5 / 6. 途中の動画は絶対に触らない。

        1 本目は完走、2 本目は映像解析までで停止。その状態で整理を
        走らせ、**完走した方だけが片づくこと**を見る。
        """
        self.run_module(max_videos=1)
        self.run_module(max_videos=1,
                        runners=self.stop_after("visual_analysis", on_video=1))
        first, second = self.assets_in_order()[:2]
        self.assertTrue(self.is_complete(first), "前提: 1 本目が完走")
        self.assertFalse(self.is_complete(second), "前提: 2 本目が途中")
        before = sorted(p.name for p in self.frames_of(second))
        self.assertTrue(before, "前提: 途中の動画にも中間ファイルがあること")

        summary = pipeline.cleanup_completed_assets(
            self.context(), skip_stages=self.skip_stages)

        self.assertEqual(sorted(p.name for p in self.frames_of(second)), before,
                         "再開に必要な中間成果が消えています。")
        self.assertFalse(self.frames_of(first), "完走した動画が片づいていません。")
        self.assertEqual(summary.checked, 1, "途中の動画まで数えています。")

    def test_the_resumed_video_still_reuses_its_cache(self) -> None:
        """整理を入れても、途中の動画は続きから再利用できる。

        **順序が要。** 整理は実行の最後に走るので、再開のときには
        中間成果がまだ残っている。
        """
        self.run_module(max_videos=1,
                        runners=self.stop_after("visual_analysis", on_video=1))
        first = self.assets_in_order()[0]
        self.assertTrue(self.frames_of(first), "前提: 中間ファイルがあること")

        self.run_module(max_videos=1, recycle_cache=True)
        attempts = self.db.get_stage_status(
            first, db_module.STAGE_FRAME_EXTRACTION)["attempt_count"]
        self.assertEqual(attempts, 1, "代表画像がやり直されています。")
        self.assertTrue(self.is_complete(first), "続きから完了していません。")

    def test_the_summary_is_reported(self) -> None:
        self.run_module(max_videos=2, recycle_cache=True)
        summary = self.last_cleanup()
        self.assertIsNotNone(summary)
        text = "\n".join(summary.lines())
        for word in ("完了済み動画", "ゴミ箱へ移動", "すでに整理済み"):
            with self.subTest(word=word):
                self.assertIn(word, text)

    def test_nothing_is_cleaned_when_the_setting_is_off(self) -> None:
        self.run_module(max_videos=1, recycle_cache=False)
        self.assertIsNone(self.last_cleanup())
        self.assertTrue(self.frames_of(self.assets_in_order()[0]))

    def test_no_hard_delete_path_exists(self) -> None:
        """9. 消せなかったときに完全削除へ逃げないこと。"""
        source = (APP_ROOT / "src" / "local_video_catalog"
                  / "recycle.py").read_text(encoding="utf-8")
        for forbidden in ("shutil.rmtree", "os.remove", "unlink(",
                          "os.unlink"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_source_videos_are_untouched(self) -> None:
        """7. 元動画は 1 バイトも変わらない。"""
        self.run_module(max_videos=2, recycle_cache=True)
        for path, before in self.video_states.items():
            with self.subTest(name=path.name):
                self.assertEqual(file_state(path), before)

    def test_nothing_outside_the_app_folder_is_touched(self) -> None:
        """8. APP_ROOT の外へ出ない。"""
        before = sorted(p.name for p in self.source_root.rglob("*"))
        self.run_module(max_videos=2, recycle_cache=True)
        self.assertEqual(sorted(p.name for p in self.source_root.rglob("*")),
                         before)

    def test_protected_places_are_not_cleanable(self) -> None:
        """台帳・説明文・HTML・モデル・ログ・設定は対象外のまま。"""
        for directory in (paths.descriptions_dir(), paths.catalog_dir(),
                          paths.log_dir(), paths.config_dir(),
                          paths.whisper_models_dir()):
            with self.subTest(name=directory.name):
                self.assertFalse(paths.is_cleanable(directory))


class CatalogRefreshTests(SafeStopResumeTestCase):
    """2. 解析が終わったら HTML が新しくなる。"""

    def catalog_ids(self) -> list[str]:
        if not paths.catalog_html_path().is_file():
            return []
        text = paths.catalog_html_path().read_text(encoding="utf-8")
        return sorted({r.catalog_id for r in html_catalog.collect_records()
                       if r.catalog_id in text})

    def test_a_normal_finish_updates_the_catalog(self) -> None:
        """10. ボタンを押さなくても出来ている。"""
        self.run_module(max_videos=1)
        self.assertTrue(paths.catalog_html_path().is_file())
        self.assertEqual(len(self.catalog_ids()), 1)

    def test_the_video_limit_also_updates_it(self) -> None:
        """11."""
        self.run_module(max_videos=2)
        self.assertEqual(len(self.catalog_ids()), 2)

    def test_a_stopped_run_publishes_what_is_finished(self) -> None:
        """13. 止めても、出来た説明文までは載る。途中の動画は載らない。"""
        self.run_module(max_videos=2,
                        runners=self.stop_after("visual_analysis", on_video=2))
        finished = [a for a in self.assets_in_order() if self.is_complete(a)]
        self.assertEqual(len(finished), 1, "前提: 1 本だけ完走していること")
        self.assertEqual(len(self.catalog_ids()), 1,
                         "途中の動画まで載っています。")

    def test_no_duplicate_entries(self) -> None:
        """14."""
        self.run_module(max_videos=1)
        self.run_module(max_videos=1)
        ids = [r.catalog_id for r in html_catalog.collect_records()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_broken_catalog_does_not_break_the_run(self) -> None:
        """15. HTML が書けなくても、台帳と説明文は無事。"""
        original = html_catalog.write_catalog

        def explode(*_args, **_kwargs):
            raise OSError("書けません")

        html_catalog.write_catalog = explode
        try:
            code = self.run_module(max_videos=1)
        finally:
            html_catalog.write_catalog = original

        self.assertEqual(code, pipeline.EXIT_OK)
        asset = self.assets_in_order()[0]
        self.assertTrue(self.is_complete(asset), "解析結果が壊れています。")
        self.assertEqual(len(list(paths.descriptions_dir().glob("*.txt"))), 1)

    def test_the_manual_button_still_exists(self) -> None:
        source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                  / "app.py").read_text(encoding="utf-8")
        self.assertIn("HTMLカタログを更新", source)

    def test_the_same_builder_is_reused(self) -> None:
        """二重実装しない。"""
        source = (APP_ROOT / "src" / "local_video_catalog"
                  / "pipeline.py").read_text(encoding="utf-8")
        block = source.split("def refresh_catalog", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("html_catalog.collect_records", block)
        self.assertIn("html_catalog.write_catalog", block)
        self.assertNotIn("<html", block)


class InterruptionIsNotFailureTests(SafeStopResumeTestCase):
    """4. 止めた／時間に達しただけを「失敗」と数えない。

    **工程の途中で止まったとき**の話。工程と工程の境目で止まった場合は
    そもそも何も中断していないので、ここでは扱わない。
    """

    def interrupt_inside(self, after_calls: int = 3):
        """``stop_requested`` を、途中から True にする。

        利用者が工程の実行中に「安全停止」を押した状況そのもの。
        時計に頼らないので、いつ動かしても同じ結果になる。

        既定の 3 回は、**押される前に通る判定の数**:
        動画のループ 1 回 → 工程に入る前 1 回 → 代表画像 1 枚目 1 回。
        4 回目（2 枚目）で True になり、1 枚できた状態で途中終了する。
        """
        calls = {"n": 0}
        original = pipeline.stop_requested

        def counted() -> bool:
            calls["n"] += 1
            return calls["n"] > after_calls

        pipeline.stop_requested = counted
        self.addCleanup(lambda: setattr(pipeline, "stop_requested", original))
        return calls

    def test_a_stage_stopped_midway_is_not_a_failure(self) -> None:
        """17. **これが実運用で「失敗 1 件」と出ていた形。**"""
        self.interrupt_inside()
        result = self.run_pipeline_directly()
        self.assertEqual(result.failures, [],
                         f"失敗に数えています: {result.failures}")
        self.assertTrue(result.interrupted_notes,
                        "途中で終わったことが記録されていません。")

    def test_the_message_says_it_was_stopped(self) -> None:
        self.interrupt_inside()
        result = self.run_pipeline_directly()
        text = NEWLINE.join(result.interrupted_notes)
        self.assertIn("途中で終了", text)
        self.assertNotIn("失敗", text)

    def test_the_summary_separates_finished_partial_and_failed(self) -> None:
        """16. 完了・途中・失敗を分けて数える。"""
        self.interrupt_inside()
        result = self.run_pipeline_directly()
        text = NEWLINE.join(pipeline.describe_stop(result))
        self.assertIn("完了した動画", text)
        self.assertIn("途中まで処理 : 1 本", text)
        self.assertIn("失敗         : 0 件", text)

    def test_an_interruption_never_triggers_the_failure_guard(self) -> None:
        """止めたことを「設備が壊れている」と誤解しないこと。"""
        self.interrupt_inside()
        result = self.run_pipeline_directly()
        self.assertNotEqual(result.stop_reason,
                            pipeline.STOP_REPEATED_FAILURE)

    def test_a_stopped_outcome_is_classified_as_interruption(self) -> None:
        """工程がどう返せば中断になるかを固定する。"""
        runners = self.runners()
        runners.frame_extraction = lambda asset_id, ctx: (
            pipeline.StageOutcome.stopped(
                db_module.STATUS_PARTIAL, "止めたため途中で終了しました。"))
        result = self.run_pipeline_directly(runners=runners)
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.interrupted_notes), 1)
        self.assertEqual(result.completed, 0)

    def test_a_real_failure_is_still_a_failure(self) -> None:
        """18. **本物のエラーを隠さない。**"""
        runners = self.runners()
        runners.visual_analysis = lambda asset_id, ctx: (
            pipeline.StageOutcome.failed(
                pipeline.FAILURE_CONNECTION, "LM Studio へ接続できません。"))
        result = self.run_pipeline_directly(runners=runners)
        self.assertTrue(result.failures, "本物の失敗が数えられていません。")
        self.assertEqual(result.interrupted_notes, [])

    def test_a_failure_is_not_excused_by_a_pending_stop(self) -> None:
        """**状況で判定しない。** 止める要求が出ていても、失敗は失敗。

        「工程が終わったときに、たまたま期限を過ぎていたから中断扱い」
        にすると、本物のエラーが隠れてしまう。判断するのは工程自身。
        """
        runners = self.runners()

        def fails_then_stop(asset_id, ctx):
            pipeline.request_stop()          # 直後に停止要求が出た状況
            return pipeline.StageOutcome.failed(
                pipeline.FAILURE_CONNECTION, "LM Studio へ接続できません。")

        runners.frame_extraction = fails_then_stop
        result = self.run_pipeline_directly(runners=runners)
        self.assertTrue(result.failures, "本物の失敗が消えています。")
        self.assertEqual(result.interrupted_notes, [])

    def test_every_stage_uses_the_same_way_of_saying_it(self) -> None:
        """文字起こしだけの話にしない。"""
        for name in ("frames", "visual", "transcription"):
            source = (APP_ROOT / "src" / "local_video_catalog" / "stages"
                      / f"{name}.py").read_text(encoding="utf-8")
            with self.subTest(stage=name):
                self.assertIn("StageOutcome.stopped", source)

    def test_the_visual_stage_does_not_finish_on_partial_frames(self) -> None:
        """**中断した映像解析を completed にしない。**

        してしまうと少ない枚数のまま「完了」になり、二度と作り直され
        ないので、黙って質が落ちる。
        """
        self.register_video()
        asset = self.assets_in_order()[0]
        runners = self.runners()
        result = pipeline.run_pipeline(
            self.context(), [t for t in self.targets() if t.asset_id == asset],
            runners, skip_stages=self.skip_stages)
        self.assertTrue(result.ok, f"前提: 一度は完走すること {result.failures}")

        # 代表画像はあるが映像解析をやり直させ、その途中で止める
        self.db.set_stage_status(asset, db_module.STAGE_VISUAL_ANALYSIS,
                                 db_module.STATUS_FAILED)
        self.interrupt_inside()
        pipeline.run_pipeline(
            self.context(), [t for t in self.targets() if t.asset_id == asset],
            self.runners(), skip_stages=self.skip_stages)
        self.assertFalse(
            self.db.is_stage_done(asset, db_module.STAGE_VISUAL_ANALYSIS),
            "途中で止めた映像の解析が「完了」になっています。")


class SelectionDisplayTests(unittest.TestCase):
    """5. 本数制限なしのとき、画面を埋めない。"""

    def plan(self, count: int, *, limit: int = 0,
             minutes: float = 0.0) -> selection.SelectionPlan:
        videos = [selection.SelectedVideo(
            catalog_id=f"VID-{index:06d}", file_name=f"clip_{index}.mp4",
            asset_id=f"a{index}", reason=selection.REASON_NEW)
            for index in range(1, count + 1)]
        return selection.SelectionPlan(
            library_total=count + 10, outstanding_total=count, limit=limit,
            videos=videos, time_budget_minutes=minutes)

    def test_a_long_list_is_truncated(self) -> None:
        """24."""
        lines = self.plan(319).detail_lines()
        listed = [line for line in lines if line.strip().startswith(
            tuple(f"{i}." for i in range(1, 11)))]
        self.assertLessEqual(len(listed), selection.SelectionPlan.DETAIL_LIMIT)
        self.assertIn("ほか 309 本", "\n".join(lines))

    def test_a_small_limit_shows_everything(self) -> None:
        """25. 3 本なら 3 本とも出す。"""
        lines = "\n".join(self.plan(3, limit=3).detail_lines())
        for index in (1, 2, 3):
            with self.subTest(index=index):
                self.assertIn(f"VID-{index:06d}", lines)
        self.assertNotIn("ほか", lines)

    def test_unlimited_says_candidates_not_a_promise(self) -> None:
        """**「今回解析する 319 本」と言い切らない。**"""
        lines = "\n".join(self.plan(319, minutes=360).summary_lines())
        self.assertIn("処理本数       : 制限なし", lines)
        self.assertIn("解析候補", lines)
        self.assertIn("稼働時間       : 360 分", lines)
        self.assertNotIn("今回解析する   : 319 本", lines)

    def test_a_limit_still_says_how_many(self) -> None:
        lines = "\n".join(self.plan(3, limit=3).summary_lines())
        self.assertIn("最大 3 本", lines)
        self.assertIn("今回解析する   : 3 本", lines)

    def test_the_structured_record_keeps_every_video(self) -> None:
        """記録は減らさない。**画面の都合で履歴を削らない。**"""
        payload = self.plan(319).to_dict()
        self.assertEqual(len(payload["videos"]), 319)

    def test_the_order_is_untouched(self) -> None:
        plan = self.plan(50)
        self.assertEqual([v.catalog_id for v in plan.videos],
                         sorted(v.catalog_id for v in plan.videos))


class ClockTests(unittest.TestCase):
    """3. 経過時間の表示。"""

    def setUp(self) -> None:
        from local_video_catalog.gui import app as app_module

        self.app_module = app_module

    def test_format_is_stable_for_long_runs(self) -> None:
        self.assertEqual(self.app_module.format_duration(0), "00:00:00")
        self.assertEqual(self.app_module.format_duration(59), "00:00:59")
        self.assertEqual(self.app_module.format_duration(3600), "01:00:00")
        self.assertEqual(self.app_module.format_duration(8076), "02:14:36")

    def test_negative_values_do_not_break_it(self) -> None:
        self.assertEqual(self.app_module.format_duration(-5), "00:00:00")

    def test_the_clock_does_not_write_to_the_log(self) -> None:
        """23. **ログを毎秒汚さない。**"""
        source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                  / "app.py").read_text(encoding="utf-8")
        for name in ("_tick", "_clock_text", "_start_clock", "_stop_clock"):
            block = source.split(f"def {name}", 1)[1].split("\n    def ", 1)[0]
            with self.subTest(name=name):
                self.assertNotIn("self._append", block)

    def test_the_budget_comes_from_the_same_place_as_the_run(self) -> None:
        """画面だけ別の時計を作らないこと。"""
        source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                  / "app.py").read_text(encoding="utf-8")
        block = source.split("def _start(self", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("state.effective_time_budget()", block)
        self.assertIn("_start_clock", block)


if __name__ == "__main__":
    unittest.main()
