"""オーケストレータ — **止めどきの判断**を外部ソフト無しで検証する.

工程の実体を差し替えられるようにしてあるので、LM Studio も ffmpeg も
使わずに、3 系統の安全停止と連続失敗ガードを確かめられる。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from _support import TempAppRootTestCase, quiet_logger

from local_video_catalog import config as config_module
from local_video_catalog import database as db_module
from local_video_catalog import paths, pipeline, stage_report
from local_video_catalog.logging_utils import new_run_id
from local_video_catalog.source_ref import SourceRef


class PipelineTestCase(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)
        self.logger = quiet_logger(paths.log_dir(), new_run_id())
        self.addCleanup(self.logger.close)
        self.calls: list[tuple[str, str]] = []
        pipeline.clear_stop_request()

    def add_videos(self, count: int) -> list[str]:
        ids = []
        for index in range(count):
            asset_id = self.db.new_asset_id()
            self.db.insert_asset(
                asset_id=asset_id, catalog_id=self.db.next_catalog_id(),
                source=SourceRef(root=self.source_root,
                                 relative=f"clip{index}.mp4"),
                file_size=1, creation_time_fs=None, last_write_time_fs=None,
                file_fingerprint=None, quick_fingerprint=None,
                full_sha256=None, now="t",
                registration_status=db_module.REG_NEW)
            ids.append(asset_id)
        return ids

    def context(self, *, budget_seconds: float | None = None
                ) -> pipeline.RunContext:
        settings = config_module.build_settings(
            config_module.load_settings_dict(), require_ffprobe=False)
        return pipeline.RunContext(
            settings=settings, database=self.db, logger=self.logger,
            run_id="r1",
            deadline=(time.monotonic() + budget_seconds
                      if budget_seconds is not None else None))

    def targets(self) -> list[stage_report.AssetProgress]:
        report = stage_report.collect(self.db, source_root=self.source_root)
        return report.pending

    def recording_runner(self, name: str) -> pipeline.StageRunner:
        def runner(asset_id: str, context: pipeline.RunContext
                   ) -> pipeline.StageOutcome:
            self.calls.append((asset_id, name))
            return pipeline.StageOutcome.ok()
        return runner

    def failing_runner(self, name: str, kind: str) -> pipeline.StageRunner:
        def runner(asset_id: str, context: pipeline.RunContext
                   ) -> pipeline.StageOutcome:
            self.calls.append((asset_id, name))
            return pipeline.StageOutcome.failed(kind)
        return runner

    def all_ok(self) -> pipeline.StageRunners:
        return pipeline.StageRunners(
            frame_extraction=self.recording_runner("frames"),
            visual_analysis=self.recording_runner("visual"),
            audio_transcription=self.recording_runner("asr"),
            description=self.recording_runner("description"))


class HappyPathTests(PipelineTestCase):
    def test_every_stage_runs_for_every_video(self) -> None:
        self.add_videos(2)
        result = pipeline.run_pipeline(self.context(), self.targets(),
                                       self.all_ok())
        self.assertEqual(result.stop_reason, pipeline.STOP_FINISHED)
        self.assertEqual(result.processed, 2)
        self.assertEqual(len(self.calls), 8)
        self.assertTrue(result.ok)

    def test_stage_order_is_fixed(self) -> None:
        self.add_videos(1)
        pipeline.run_pipeline(self.context(), self.targets(), self.all_ok())
        self.assertEqual([name for _asset, name in self.calls],
                         ["frames", "visual", "asr", "description"])

    def test_completed_stages_are_skipped_on_the_next_run(self) -> None:
        """**Resume。** 二度目は残りだけ動く。"""
        self.add_videos(1)
        pipeline.run_pipeline(self.context(), self.targets(), self.all_ok())
        self.calls.clear()
        second = pipeline.run_pipeline(self.context(), self.targets(),
                                       self.all_ok())
        self.assertEqual(self.calls, [])
        self.assertEqual(second.planned, 0)

    def test_skipped_stages_do_not_run(self) -> None:
        self.add_videos(1)
        pipeline.run_pipeline(
            self.context(), self.targets(), self.all_ok(),
            skip_stages=frozenset({db_module.STAGE_VISUAL_ANALYSIS}))
        self.assertNotIn("visual", [name for _a, name in self.calls])

    def test_missing_runner_is_skipped_not_fatal(self) -> None:
        self.add_videos(1)
        runners = pipeline.StageRunners(
            frame_extraction=self.recording_runner("frames"))
        result = pipeline.run_pipeline(self.context(), self.targets(), runners)
        self.assertTrue(result.ok)
        self.assertEqual([name for _a, name in self.calls], ["frames"])


class StopRequestTests(PipelineTestCase):
    """**ファイルの出現で止める。プロセスを殺さない。**"""

    def test_stop_before_the_next_video(self) -> None:
        self.add_videos(3)

        def stopping(asset_id: str, context: pipeline.RunContext
                     ) -> pipeline.StageOutcome:
            self.calls.append((asset_id, "description"))
            pipeline.request_stop()
            return pipeline.StageOutcome.ok()

        runners = self.all_ok()
        runners.description = stopping
        result = pipeline.run_pipeline(self.context(), self.targets(), runners)
        self.assertEqual(result.stop_reason, pipeline.STOP_REQUESTED)
        self.assertEqual(result.processed, 1)

    def test_stop_between_stages(self) -> None:
        self.add_videos(1)

        def stopping(asset_id: str, context: pipeline.RunContext
                     ) -> pipeline.StageOutcome:
            self.calls.append((asset_id, "frames"))
            pipeline.request_stop()
            return pipeline.StageOutcome.ok()

        runners = self.all_ok()
        runners.frame_extraction = stopping
        result = pipeline.run_pipeline(self.context(), self.targets(), runners)
        self.assertEqual(result.stop_reason, pipeline.STOP_REQUESTED)
        self.assertEqual([name for _a, name in self.calls], ["frames"])

    def test_completed_stages_are_kept_after_a_stop(self) -> None:
        self.add_videos(1)
        assets = self.targets()

        def stopping(asset_id: str, context: pipeline.RunContext
                     ) -> pipeline.StageOutcome:
            pipeline.request_stop()
            return pipeline.StageOutcome.ok()

        runners = self.all_ok()
        runners.visual_analysis = stopping
        pipeline.run_pipeline(self.context(), assets, runners)
        self.assertTrue(self.db.is_stage_done(
            assets[0].asset_id, db_module.STAGE_FRAME_EXTRACTION))

    def test_the_request_file_is_cleared_afterwards(self) -> None:
        self.add_videos(1)
        pipeline.request_stop()
        pipeline.run_pipeline(self.context(), self.targets(), self.all_ok())
        self.assertFalse(paths.stop_request_path().exists())

    def test_stop_file_lives_in_userdata(self) -> None:
        target = pipeline.request_stop()
        self.assertTrue(str(target).startswith(str(paths.userdata_dir())))


class TimeBudgetTests(PipelineTestCase):
    def test_expired_budget_stops_before_the_next_video(self) -> None:
        self.add_videos(3)
        result = pipeline.run_pipeline(
            self.context(budget_seconds=-1), self.targets(), self.all_ok())
        self.assertEqual(result.stop_reason, pipeline.STOP_TIME_BUDGET)
        self.assertEqual(result.processed, 0)

    def test_budget_expiring_mid_video_stops_between_stages(self) -> None:
        self.add_videos(1)
        context = self.context(budget_seconds=10)

        def exhausting(asset_id: str, ctx: pipeline.RunContext
                       ) -> pipeline.StageOutcome:
            self.calls.append((asset_id, "frames"))
            ctx.deadline = time.monotonic() - 1
            return pipeline.StageOutcome.ok()

        runners = self.all_ok()
        runners.frame_extraction = exhausting
        result = pipeline.run_pipeline(context, self.targets(), runners)
        self.assertEqual(result.stop_reason, pipeline.STOP_TIME_BUDGET)
        self.assertEqual([name for _a, name in self.calls], ["frames"])

    def test_no_budget_means_no_time_stop(self) -> None:
        self.add_videos(2)
        result = pipeline.run_pipeline(self.context(), self.targets(),
                                       self.all_ok())
        self.assertEqual(result.stop_reason, pipeline.STOP_FINISHED)


class ConsecutiveFailureTests(PipelineTestCase):
    """**同じ設備障害が 3 本続いたら止める。**

    一晩中同じ失敗を繰り返して時間を浪費しないため。
    """

    def _run_with_failure(self, kind: str, videos: int = 5,
                          limit: int = 3) -> pipeline.PipelineResult:
        self.add_videos(videos)
        runners = self.all_ok()
        runners.visual_analysis = self.failing_runner("visual", kind)
        return pipeline.run_pipeline(
            self.context(), self.targets(), runners,
            consecutive_failure_limit=limit)

    def test_three_connection_failures_stop_the_run(self) -> None:
        result = self._run_with_failure(pipeline.FAILURE_CONNECTION)
        self.assertEqual(result.stop_reason, pipeline.STOP_REPEATED_FAILURE)
        self.assertEqual(result.processed, 3)

    def test_two_failures_do_not_stop(self) -> None:
        result = self._run_with_failure(pipeline.FAILURE_CONNECTION, videos=2)
        self.assertNotEqual(result.stop_reason, pipeline.STOP_REPEATED_FAILURE)
        self.assertEqual(len(result.failures), 2)

    def test_timeout_is_treated_as_infrastructure(self) -> None:
        result = self._run_with_failure(pipeline.FAILURE_TIMEOUT)
        self.assertEqual(result.stop_reason, pipeline.STOP_REPEATED_FAILURE)

    def test_per_video_failures_never_stop_the_run(self) -> None:
        """動画ごとの事情は、いくつ続いても止める理由にならない。"""
        result = self._run_with_failure(pipeline.FAILURE_OTHER, videos=5)
        self.assertEqual(result.stop_reason, pipeline.STOP_FINISHED)
        self.assertEqual(len(result.failures), 5)

    def test_no_frames_does_not_stop_the_run(self) -> None:
        result = self._run_with_failure(pipeline.FAILURE_NO_FRAMES, videos=5)
        self.assertEqual(result.stop_reason, pipeline.STOP_FINISHED)

    def test_a_success_resets_the_counter(self) -> None:
        """成功を挟めば「続いている」ことにならない。"""
        self.add_videos(6)
        sequence = [pipeline.FAILURE_CONNECTION, pipeline.FAILURE_CONNECTION,
                    None, pipeline.FAILURE_CONNECTION,
                    pipeline.FAILURE_CONNECTION, pipeline.FAILURE_CONNECTION]
        state = {"index": 0}

        def mixed(asset_id: str, context: pipeline.RunContext
                  ) -> pipeline.StageOutcome:
            kind = sequence[state["index"]]
            state["index"] += 1
            return (pipeline.StageOutcome.ok() if kind is None
                    else pipeline.StageOutcome.failed(kind))

        runners = self.all_ok()
        runners.visual_analysis = mixed
        result = pipeline.run_pipeline(self.context(), self.targets(), runners,
                                       consecutive_failure_limit=3)
        self.assertEqual(result.stop_reason, pipeline.STOP_REPEATED_FAILURE)
        self.assertEqual(result.processed, 6)

    def test_local_only_stages_do_not_reset_the_counter(self) -> None:
        """代表画像の抽出が成功しても、カウンタを戻さないこと。

        代表画像は ffmpeg だけで完結する。それが成功しても LM Studio が
        健全である証拠にはならない。すべての工程の成功で戻すと、毎回
        リセットされてガードが働かなくなる。
        """
        self.add_videos(5)
        runners = self.all_ok()          # frame_extraction は毎回成功する
        runners.visual_analysis = self.failing_runner(
            "visual", pipeline.FAILURE_CONNECTION)
        result = pipeline.run_pipeline(self.context(), self.targets(), runners,
                                       consecutive_failure_limit=3)
        self.assertEqual(result.stop_reason, pipeline.STOP_REPEATED_FAILURE)
        self.assertEqual(result.processed, 3)
        frame_runs = [name for _asset, name in self.calls if name == "frames"]
        self.assertEqual(len(frame_runs), 3,
                         "代表画像の抽出は 3 本とも成功しているはずです。")

    def test_frame_extraction_is_not_an_infrastructure_stage(self) -> None:
        self.assertNotIn(db_module.STAGE_FRAME_EXTRACTION,
                         pipeline.INFRASTRUCTURE_STAGES)
        self.assertIn(db_module.STAGE_VISUAL_ANALYSIS,
                      pipeline.INFRASTRUCTURE_STAGES)

    def test_alternating_kinds_do_not_accumulate(self) -> None:
        """**同じ種類**が続いたときだけ止める。"""
        self.add_videos(4)
        kinds = [pipeline.FAILURE_CONNECTION, pipeline.FAILURE_TIMEOUT,
                 pipeline.FAILURE_CONNECTION, pipeline.FAILURE_TIMEOUT]
        state = {"index": 0}

        def alternating(asset_id: str, context: pipeline.RunContext
                        ) -> pipeline.StageOutcome:
            kind = kinds[state["index"]]
            state["index"] += 1
            return pipeline.StageOutcome.failed(kind)

        runners = self.all_ok()
        runners.visual_analysis = alternating
        result = pipeline.run_pipeline(self.context(), self.targets(), runners,
                                       consecutive_failure_limit=3)
        self.assertEqual(result.stop_reason, pipeline.STOP_FINISHED)

    def test_stop_message_names_the_cause(self) -> None:
        result = self._run_with_failure(pipeline.FAILURE_TIMEOUT)
        text = "\n".join(pipeline.describe_stop(result))
        self.assertIn("制限時間を超えました", text)
        self.assertNotIn("起動していない", text)

    def test_timeout_is_never_reported_as_not_running(self) -> None:
        """**「制限時間を超えた」を「起動していない」と言わない。**"""
        message = pipeline.FAILURE_MESSAGES[pipeline.FAILURE_TIMEOUT]
        self.assertIn("つながっています", message)
        self.assertNotIn("起動し", message)


class FailureRecordingTests(PipelineTestCase):
    def test_failed_stage_is_recorded_and_retried_next_time(self) -> None:
        self.add_videos(1)
        assets = self.targets()
        runners = self.all_ok()
        runners.visual_analysis = self.failing_runner(
            "visual", pipeline.FAILURE_OTHER)
        pipeline.run_pipeline(self.context(), assets, runners)

        self.assertFalse(self.db.is_stage_done(
            assets[0].asset_id, db_module.STAGE_VISUAL_ANALYSIS))
        self.assertTrue(self.db.is_stage_done(
            assets[0].asset_id, db_module.STAGE_FRAME_EXTRACTION))
        self.assertEqual(len(self.targets()), 1)

    def test_later_stages_are_not_attempted_after_a_failure(self) -> None:
        self.add_videos(1)
        runners = self.all_ok()
        runners.visual_analysis = self.failing_runner(
            "visual", pipeline.FAILURE_OTHER)
        pipeline.run_pipeline(self.context(), self.targets(), runners)
        self.assertNotIn("asr", [name for _a, name in self.calls])
        self.assertNotIn("description", [name for _a, name in self.calls])


class CleanupIntegrationTests(PipelineTestCase):
    """**完了した動画だけ**中間ファイルを片付ける。"""

    def _make_cache(self, asset_id: str) -> Path:
        directory = paths.vlm_cache_dir() / asset_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "data.bin").write_bytes(b"x" * 100)
        return directory

    def test_cleanup_is_planned_only_for_finished_videos(self) -> None:
        self.add_videos(2)
        assets = self.targets()
        for item in assets:
            self._make_cache(item.asset_id)

        state = {"first": True}

        def fail_first(asset_id: str, context: pipeline.RunContext
                       ) -> pipeline.StageOutcome:
            if state["first"]:
                state["first"] = False
                return pipeline.StageOutcome.failed(pipeline.FAILURE_OTHER)
            return pipeline.StageOutcome.ok()

        runners = self.all_ok()
        runners.description = fail_first
        pipeline.run_pipeline(self.context(), assets, runners,
                              recycle_cache=True)

        # 失敗した 1 本目のキャッシュは残っている（Resume に必要）
        self.assertTrue((paths.vlm_cache_dir() / assets[0].asset_id).is_dir())

    def test_no_cleanup_without_the_option(self) -> None:
        self.add_videos(1)
        assets = self.targets()
        directory = self._make_cache(assets[0].asset_id)
        pipeline.run_pipeline(self.context(), assets, self.all_ok(),
                              recycle_cache=False)
        self.assertTrue(directory.is_dir())


class MessageTests(unittest.TestCase):
    def test_every_failure_kind_has_a_message(self) -> None:
        for kind in (pipeline.FAILURE_CONNECTION, pipeline.FAILURE_TIMEOUT,
                     pipeline.FAILURE_MODEL, pipeline.FAILURE_PRIVACY,
                     pipeline.FAILURE_NO_FRAMES, pipeline.FAILURE_OTHER):
            with self.subTest(kind=kind):
                self.assertTrue(pipeline.FAILURE_MESSAGES.get(kind))

    def test_infrastructure_set_excludes_per_video_kinds(self) -> None:
        self.assertNotIn(pipeline.FAILURE_OTHER,
                         pipeline.INFRASTRUCTURE_FAILURES)
        self.assertNotIn(pipeline.FAILURE_NO_FRAMES,
                         pipeline.INFRASTRUCTURE_FAILURES)

    def test_stop_descriptions_say_work_is_kept(self) -> None:
        for reason in (pipeline.STOP_REQUESTED, pipeline.STOP_TIME_BUDGET):
            with self.subTest(reason=reason):
                result = pipeline.PipelineResult(stop_reason=reason)
                text = "\n".join(pipeline.describe_stop(result))
                self.assertIn("保存済み", text)


if __name__ == "__main__":
    unittest.main()
