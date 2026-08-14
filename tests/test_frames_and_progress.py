"""代表画像の計画・抽出と、進み具合／Resume の判定."""

from __future__ import annotations

import unittest
from pathlib import Path

from _support import (
    TempAppRootTestCase,
    file_state,
    find_ffmpeg,
    make_synthetic_video,
    requires_ffmpeg,
)

from local_video_catalog import FRAME_EXTRACTION_IMPL_VERSION
from local_video_catalog import database as db_module
from local_video_catalog import frame_extractor as fx
from local_video_catalog import paths, stage_report
from local_video_catalog.source_ref import SourceRef


class FramePlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = fx.ExtractionConfig()

    def test_zero_duration_plans_nothing(self) -> None:
        self.assertEqual(fx.plan_frames(0, self.config), [])

    def test_short_video_gets_the_minimum(self) -> None:
        frames = fx.plan_frames(10.0, self.config)
        self.assertEqual(len(frames), self.config.minimum_frame_count)

    def test_long_video_is_capped(self) -> None:
        """1 枚あたり約 16 秒かかるので、枚数の上限が待ち時間の上限になる。"""
        frames = fx.plan_frames(60 * 60 * 3, self.config)
        self.assertEqual(len(frames), self.config.maximum_frame_count)

    def test_plan_is_deterministic(self) -> None:
        first = fx.plan_frames(300.0, self.config)
        second = fx.plan_frames(300.0, self.config)
        self.assertEqual([f.target_time_milliseconds for f in first],
                         [f.target_time_milliseconds for f in second])

    def test_times_are_sorted_and_unique(self) -> None:
        frames = fx.plan_frames(300.0, self.config)
        times = [f.target_time_milliseconds for f in frames]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), len(set(times)))

    def test_beginning_middle_and_end_are_covered(self) -> None:
        duration = 600.0
        frames = fx.plan_frames(duration, self.config)
        positions = [f.relative_position for f in frames]
        self.assertLess(positions[0], 0.05)
        self.assertGreater(positions[-1], 0.95)

    def test_never_reaches_the_very_end(self) -> None:
        """動画長ちょうどを指定すると最後の 1 枚が取れないことがある。"""
        duration = 12.0
        for frame in fx.plan_frames(duration, self.config):
            self.assertLess(frame.target_time_milliseconds, int(duration * 1000))

    def test_tail_guard_keeps_the_last_frame_decodable(self) -> None:
        """**末尾ぎりぎりを指すと、復号できる最終フレームを越える。**

        1ms 手前では低フレームレートの動画で必ず取りこぼしていた。
        """
        for duration in (2.0, 3.0, 10.0, 600.0):
            with self.subTest(duration=duration):
                frames = fx.plan_frames(duration, self.config)
                last = frames[-1].target_time_milliseconds
                self.assertLessEqual(
                    last, int(duration * 1000) - fx.TAIL_GUARD_MILLISECONDS)

    def test_tail_guard_never_goes_negative(self) -> None:
        for duration in (0.05, 0.1, 0.2):
            with self.subTest(duration=duration):
                for frame in fx.plan_frames(duration, self.config):
                    self.assertGreaterEqual(frame.target_time_milliseconds, 0)

    def test_very_short_video_deduplicates(self) -> None:
        frames = fx.plan_frames(0.05, self.config)
        times = [f.target_time_milliseconds for f in frames]
        self.assertEqual(len(times), len(set(times)))

    def test_sequence_indexes_are_contiguous(self) -> None:
        frames = fx.plan_frames(300.0, self.config)
        self.assertEqual([f.sequence_index for f in frames],
                         list(range(1, len(frames) + 1)))

    def test_invalid_config_is_refused(self) -> None:
        for bad in (
            fx.ExtractionConfig(target_interval_seconds=0),
            fx.ExtractionConfig(minimum_frame_count=0),
            fx.ExtractionConfig(minimum_frame_count=10, maximum_frame_count=5),
            fx.ExtractionConfig(jpeg_quality=99),
            fx.ExtractionConfig(maximum_image_dimension=1),
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    bad.validate()


class ConfigHashTests(unittest.TestCase):
    def test_same_config_same_hash(self) -> None:
        self.assertEqual(fx.ExtractionConfig().config_hash,
                         fx.ExtractionConfig().config_hash)

    def test_different_config_different_hash(self) -> None:
        self.assertNotEqual(
            fx.ExtractionConfig().config_hash,
            fx.ExtractionConfig(maximum_frame_count=12).config_hash)

    def test_hash_covers_the_implementation_version(self) -> None:
        material = fx.ExtractionConfig().config_hash
        self.assertEqual(len(material), 64)


class OutputLocationTests(TempAppRootTestCase):
    def test_output_is_inside_the_frames_cache(self) -> None:
        config = fx.ExtractionConfig()
        target = fx.output_directory("asset123", config)
        self.assertTrue(str(target).startswith(str(paths.frames_cache_dir())))
        self.assertIn(FRAME_EXTRACTION_IMPL_VERSION, target.parts)

    def test_output_is_cleanable(self) -> None:
        """代表画像は完了後に片付けられる場所に置く。"""
        paths.ensure_userdata_tree()
        target = fx.output_directory("asset123", fx.ExtractionConfig())
        target.mkdir(parents=True)
        self.assertTrue(paths.is_cleanable(target))

    def test_different_settings_do_not_mix(self) -> None:
        first = fx.output_directory("asset123", fx.ExtractionConfig())
        second = fx.output_directory(
            "asset123", fx.ExtractionConfig(maximum_frame_count=12))
        self.assertNotEqual(first, second)


class FfmpegCommandTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.frame = fx.PlannedFrame(1, 1.5, 1500, 0.1)
        self.config = fx.ExtractionConfig()

    def test_source_is_only_an_input(self) -> None:
        """元動画が出力先として現れないこと。"""
        source = self.make_source_dir() / "clip.mp4"
        target = paths.frames_cache_dir() / "a" / "f.jpg"
        command = fx.build_ffmpeg_command(
            Path("ffmpeg"), source, self.frame, target, self.config)
        self.assertEqual(command[command.index("-i") + 1], str(source))
        self.assertEqual(command[-1], str(target))
        self.assertNotEqual(command[-1], str(source))

    def test_primary_stream_is_mapped(self) -> None:
        command = fx.build_ffmpeg_command(
            Path("ffmpeg"), Path("in.mp4"), self.frame, Path("out.jpg"),
            self.config, stream_index=0)
        self.assertIn("-map", command)
        self.assertIn("0:0", command)

    def test_audio_and_subtitles_are_dropped(self) -> None:
        command = fx.build_ffmpeg_command(
            Path("ffmpeg"), Path("in.mp4"), self.frame, Path("out.jpg"),
            self.config)
        for flag in ("-an", "-sn", "-dn"):
            self.assertIn(flag, command)


class ExtractionTests(TempAppRootTestCase):
    @requires_ffmpeg
    def test_extracts_and_leaves_the_source_untouched(self) -> None:
        paths.ensure_userdata_tree()
        source_root = self.make_source_dir()
        video = source_root / "clip.mp4"
        self.assertTrue(make_synthetic_video(find_ffmpeg(), video, duration=3.0))
        before = file_state(video)
        listing_before = sorted(p.name for p in source_root.rglob("*"))

        config = fx.ExtractionConfig()
        frames = fx.plan_frames(3.0, config)
        directory = fx.output_directory("asset123", config)

        successes = 0
        for frame in frames[:3]:
            target = directory / fx.frame_file_name(frame)
            ok, _code, message = fx.extract_one(
                find_ffmpeg(), video, frame, target, config)
            if ok:
                successes += 1
                self.assertTrue(target.is_file())
                self.assertGreater(target.stat().st_size, 0)

        self.assertGreater(successes, 0, "1 枚も抽出できませんでした。")
        self.assertEqual(file_state(video), before,
                         "元動画が変更されています。")
        self.assertEqual(sorted(p.name for p in source_root.rglob("*")),
                         listing_before,
                         "元動画フォルダーにファイルが作られています。")

    @requires_ffmpeg
    def test_failure_is_reported_not_raised(self) -> None:
        paths.ensure_userdata_tree()
        missing = self.make_source_dir() / "nope.mp4"
        frame = fx.PlannedFrame(1, 1.0, 1000, 0.1)
        target = paths.frames_cache_dir() / "a" / "f.jpg"
        ok, _code, message = fx.extract_one(
            find_ffmpeg(), missing, frame, target, fx.ExtractionConfig())
        self.assertFalse(ok)
        self.assertTrue(message)


class ProgressTests(TempAppRootTestCase):
    """Resume の判定。"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)
        self.assets = [self._add(f"clip{i}.mp4") for i in range(3)]

    def _add(self, relative: str) -> str:
        asset_id = self.db.new_asset_id()
        self.db.insert_asset(
            asset_id=asset_id, catalog_id=self.db.next_catalog_id(),
            source=SourceRef(root=self.source_root, relative=relative),
            file_size=1, creation_time_fs=None, last_write_time_fs=None,
            file_fingerprint=None, quick_fingerprint=None, full_sha256=None,
            now="t", registration_status=db_module.REG_NEW)
        return asset_id

    def _complete(self, asset_id: str, *stages: str) -> None:
        for stage in stages:
            self.db.set_stage_status(asset_id, stage, db_module.STATUS_COMPLETED)

    def test_everything_is_pending_at_first(self) -> None:
        report = stage_report.collect(self.db, source_root=self.source_root)
        self.assertEqual(len(report.pending), 3)

    def test_a_fully_processed_video_drops_out(self) -> None:
        self._complete(self.assets[0],
                       *[stage for stage, _ in db_module.PIPELINE_STAGES])
        report = stage_report.collect(self.db, source_root=self.source_root)
        self.assertEqual(len(report.pending), 2)

    def test_ignored_stages_do_not_count_as_pending(self) -> None:
        """飛ばす設定の工程が、いつまでも未完了と表示され続けないこと。"""
        for asset_id in self.assets:
            self._complete(asset_id, db_module.STAGE_FRAME_EXTRACTION,
                           db_module.STAGE_AUDIO_TRANSCRIPTION,
                           db_module.STAGE_DESCRIPTION)
        report = stage_report.collect(
            self.db, source_root=self.source_root,
            ignored_stages=frozenset({db_module.STAGE_VISUAL_ANALYSIS}))
        self.assertEqual(len(report.pending), 0)

    def test_unavailable_videos_are_not_pending(self) -> None:
        self.db.mark_assets_unavailable([self.assets[0]], "t")
        report = stage_report.collect(self.db, source_root=self.source_root)
        self.assertEqual(len(report.pending), 2)
        self.assertEqual(len(report.unavailable), 1)

    def test_max_videos_limits_new_work_only(self) -> None:
        self._complete(self.assets[0],
                       *[stage for stage, _ in db_module.PIPELINE_STAGES])
        report = stage_report.collect(self.db, source_root=self.source_root)
        selected = stage_report.select_pending(report, max_videos=1)
        self.assertEqual(len(selected), 1)
        self.assertNotIn(self.assets[0], [item.asset_id for item in selected])

    def test_only_catalog_ids_narrows_the_target(self) -> None:
        report = stage_report.collect(
            self.db, source_root=self.source_root,
            only_catalog_ids=("VID-000002",))
        self.assertEqual([item.catalog_id for item in report.items],
                         ["VID-000002"])

    def test_no_speech_counts_as_finished(self) -> None:
        for asset_id in self.assets:
            self._complete(asset_id, db_module.STAGE_FRAME_EXTRACTION,
                           db_module.STAGE_VISUAL_ANALYSIS,
                           db_module.STAGE_DESCRIPTION)
            self.db.set_stage_status(asset_id,
                                     db_module.STAGE_AUDIO_TRANSCRIPTION,
                                     db_module.STATUS_NO_SPEECH)
        report = stage_report.collect(self.db, source_root=self.source_root)
        self.assertEqual(len(report.pending), 0)

    def test_summary_mentions_every_stage(self) -> None:
        report = stage_report.collect(self.db, source_root=self.source_root)
        text = "\n".join(stage_report.format_summary(report, found_count=3))
        for _stage, label in db_module.PIPELINE_STAGES:
            self.assertIn(label, text)

    def test_collect_changes_nothing(self) -> None:
        before = self.db.connection.execute(
            "SELECT COUNT(*) AS c FROM stage_status").fetchone()["c"]
        stage_report.collect(self.db, source_root=self.source_root)
        after = self.db.connection.execute(
            "SELECT COUNT(*) AS c FROM stage_status").fetchone()["c"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
