"""登録・列挙・ffprobe と、**元動画を一切変更しないこと**."""

from __future__ import annotations

import unittest
from pathlib import Path

from _support import (
    TempAppRootTestCase,
    file_state,
    find_ffmpeg,
    find_ffprobe,
    make_synthetic_video,
    quiet_logger,
    requires_ffmpeg,
    requires_ffprobe,
)

from local_video_catalog import config as config_module
from local_video_catalog import database as db_module
from local_video_catalog import discovery, fingerprint, paths, probe, register
from local_video_catalog.logging_utils import new_run_id
from local_video_catalog.source_ref import SourceRef


class DiscoveryTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_root = self.make_source_dir()

    def _make(self, relative: str, size: int = 10) -> Path:
        target = self.source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        return target

    def _discover(self, **kwargs: object) -> list[discovery.DiscoveredFile]:
        options = {"extensions": (".mp4", ".mov"), "exclude_patterns": (".*",)}
        options.update(kwargs)
        return list(discovery.discover(self.source_root, **options))  # type: ignore[arg-type]

    def test_matching_extensions_only(self) -> None:
        self._make("a.mp4")
        self._make("b.txt")
        self._make("c.MOV")
        found = {f.source.relative for f in self._discover()}
        self.assertEqual(found, {"a.mp4", "c.MOV"})

    def test_non_recursive_by_default(self) -> None:
        self._make("a.mp4")
        self._make("sub/b.mp4")
        self.assertEqual({f.source.relative for f in self._discover()}, {"a.mp4"})

    def test_recursive(self) -> None:
        self._make("a.mp4")
        self._make("sub/b.mp4")
        found = {f.source.relative for f in self._discover(recursive=True)}
        self.assertEqual(found, {"a.mp4", "sub/b.mp4"})

    def test_exclusions(self) -> None:
        self._make("a.mp4")
        self._make(".hidden/b.mp4")
        found = {f.source.relative for f in self._discover(recursive=True)}
        self.assertEqual(found, {"a.mp4"})

    def test_minimum_size(self) -> None:
        self._make("small.mp4", size=5)
        self._make("big.mp4", size=500)
        found = {f.source.relative
                 for f in self._discover(min_size_bytes=100)}
        self.assertEqual(found, {"big.mp4"})

    def test_results_are_source_refs(self) -> None:
        self._make("a.mp4")
        found = self._discover()[0]
        self.assertIsInstance(found.source, SourceRef)
        self.assertEqual(found.source.root, self.source_root.resolve())

    def test_missing_folder_yields_nothing(self) -> None:
        self.assertEqual(
            list(discovery.discover(self.temp_dir / "nope",
                                    extensions=(".mp4",))), [])

    def test_discovery_creates_nothing(self) -> None:
        """**元動画フォルダーへ何も書かない。**"""
        self._make("a.mp4")
        before = sorted(p.name for p in self.source_root.rglob("*"))
        self._discover(recursive=True)
        self.assertEqual(sorted(p.name for p in self.source_root.rglob("*")),
                         before)


class FingerprintTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_root = self.make_source_dir()

    def test_small_file_is_read_once(self) -> None:
        target = self.source_root / "a.mp4"
        target.write_bytes(b"x" * 100)
        result = fingerprint.compute_file_fingerprint(
            target, head_bytes=1024, tail_bytes=1024)
        self.assertTrue(result.whole_file_read)
        self.assertEqual(result.head_sha256, result.tail_sha256)

    def test_large_file_reads_head_and_tail(self) -> None:
        target = self.source_root / "a.mp4"
        target.write_bytes(b"a" * 100 + b"b" * 100)
        result = fingerprint.compute_file_fingerprint(
            target, head_bytes=50, tail_bytes=50)
        self.assertFalse(result.whole_file_read)
        self.assertNotEqual(result.head_sha256, result.tail_sha256)

    def test_empty_file(self) -> None:
        target = self.source_root / "a.mp4"
        target.write_bytes(b"")
        result = fingerprint.compute_file_fingerprint(target)
        self.assertEqual(result.size, 0)
        self.assertTrue(result.value.startswith("ffp1:"))

    def test_value_is_stable(self) -> None:
        target = self.source_root / "a.mp4"
        target.write_bytes(b"x" * 5000)
        first = fingerprint.compute_file_fingerprint(target).value
        second = fingerprint.compute_file_fingerprint(target).value
        self.assertEqual(first, second)

    def test_content_change_changes_the_value(self) -> None:
        target = self.source_root / "a.mp4"
        target.write_bytes(b"x" * 5000)
        first = fingerprint.compute_file_fingerprint(target).value
        target.write_bytes(b"y" * 5000)
        self.assertNotEqual(fingerprint.compute_file_fingerprint(target).value,
                            first)

    def test_duration_rounding_keeps_the_signature_stable(self) -> None:
        """ffprobe の桁揺れで再解析が起きないこと。"""
        first = fingerprint.stream_signature(
            12.3456789, "h264", 640, 480, 30, 1, "aac", 48000, 2)
        second = fingerprint.stream_signature(
            12.3456123, "h264", 640, 480, 30, 1, "aac", 48000, 2)
        self.assertEqual(first, second)

    def test_source_file_is_opened_read_only(self) -> None:
        """fingerprint 計算が元動画を書き換えないこと。"""
        target = self.source_root / "a.mp4"
        target.write_bytes(b"x" * 5000)
        before = file_state(target)
        fingerprint.compute_file_fingerprint(target)
        fingerprint.compute_full_sha256(target)
        self.assertEqual(file_state(target), before)


class StreamSelectionTests(unittest.TestCase):
    """**カバーアートを本編として選ばないこと。**"""

    def _video(self, index: int, width: int, height: int, *,
               attached: bool = False, default: bool = False) -> dict:
        return {
            "index": index, "codec_type": "video", "codec_name": "h264",
            "width": width, "height": height,
            "disposition": {"attached_pic": 1 if attached else 0,
                            "default": 1 if default else 0},
        }

    def test_no_streams(self) -> None:
        chosen, rule = probe.select_primary_video_stream([])
        self.assertEqual(chosen, {})
        self.assertEqual(rule, probe.RULE_NO_VIDEO_STREAM)

    def test_cover_art_is_excluded(self) -> None:
        streams = [self._video(0, 1522, 2704),
                   self._video(1, 607, 1080, attached=True)]
        chosen, rule = probe.select_primary_video_stream(streams)
        self.assertEqual(chosen["index"], 0)
        self.assertEqual(rule, probe.RULE_SOLE_PLAYABLE)

    def test_cover_art_only_is_not_playable(self) -> None:
        """カバーアートしか無い場合、それを本編として採用しない。"""
        streams = [self._video(0, 600, 800, attached=True)]
        chosen, rule = probe.select_primary_video_stream(streams)
        self.assertEqual(chosen, {})
        self.assertEqual(rule, probe.RULE_NO_PLAYABLE_VIDEO)

    def test_default_disposition_wins(self) -> None:
        streams = [self._video(0, 1920, 1080),
                   self._video(1, 640, 480, default=True)]
        chosen, rule = probe.select_primary_video_stream(streams)
        self.assertEqual(chosen["index"], 1)
        self.assertEqual(rule, probe.RULE_DEFAULT_DISPOSITION)

    def test_largest_area_then_lowest_index(self) -> None:
        streams = [self._video(2, 640, 480), self._video(1, 1920, 1080),
                   self._video(0, 1920, 1080)]
        chosen, rule = probe.select_primary_video_stream(streams)
        self.assertEqual(chosen["index"], 0)
        self.assertEqual(rule, probe.RULE_LOWEST_INDEX)

    def test_selection_is_deterministic(self) -> None:
        streams = [self._video(0, 1920, 1080), self._video(1, 1920, 1080)]
        results = {probe.select_primary_video_stream(streams)[0]["index"]
                   for _ in range(10)}
        self.assertEqual(results, {0})

    def test_primary_audio_prefers_default(self) -> None:
        streams = [
            {"index": 1, "codec_type": "audio", "channels": 6,
             "disposition": {"default": 0}},
            {"index": 2, "codec_type": "audio", "channels": 2,
             "disposition": {"default": 1}},
        ]
        self.assertEqual(probe.select_primary_audio_stream(streams)["index"], 2)

    def test_no_audio(self) -> None:
        self.assertEqual(probe.select_primary_audio_stream([]), {})

    def test_frame_rate_parsing(self) -> None:
        self.assertEqual(probe.parse_frame_rate("30000/1001")[:2], (30000, 1001))
        self.assertEqual(probe.parse_frame_rate("0/0")[2], None)
        self.assertEqual(probe.parse_frame_rate(None), (None, None, None))


class AnalyseTests(unittest.TestCase):
    def test_counts_and_primary_selection(self) -> None:
        raw = {
            "format": {"duration": "12.5", "format_name": "mov",
                       "tags": {"creation_time": "2009-08-15T14:30:05Z"}},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264",
                 "width": 1920, "height": 1080, "avg_frame_rate": "30/1",
                 "disposition": {}},
                {"index": 1, "codec_type": "video", "codec_name": "mjpeg",
                 "width": 600, "height": 800,
                 "disposition": {"attached_pic": 1}},
                {"index": 2, "codec_type": "audio", "codec_name": "aac",
                 "channels": 2, "sample_rate": "48000", "disposition": {}},
            ],
            "chapters": [],
        }
        values = probe.analyse(raw)
        self.assertEqual(values["video_stream_count"], 2)
        self.assertEqual(values["playable_video_stream_count"], 1)
        self.assertEqual(values["attached_picture_stream_count"], 1)
        self.assertEqual(values["width"], 1920)
        self.assertEqual(values["video_codec"], "h264")
        self.assertEqual(values["primary_audio_stream_index"], 2)
        self.assertEqual(values["creation_time_tag"], "2009-08-15T14:30:05Z")

    def test_analyse_is_pure(self) -> None:
        """生 JSON さえあれば動画を読み直さずに再解析できる。"""
        raw = {"format": {"duration": "1.0"}, "streams": []}
        self.assertEqual(probe.analyse(raw), probe.analyse(raw))


class RawCacheTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()

    def test_round_trip_gzip(self) -> None:
        target = paths.probe_cache_dir() / "a.json.gz"
        probe.write_raw_cache({"format": {"duration": "1.0"}}, target)
        self.assertEqual(probe.read_raw_cache(target)["format"]["duration"], "1.0")

    def test_round_trip_plain(self) -> None:
        target = paths.probe_cache_dir() / "a.json"
        probe.write_raw_cache({"a": 1}, target, gzip_enabled=False)
        self.assertEqual(probe.read_raw_cache(target), {"a": 1})

    def test_broken_cache_returns_none(self) -> None:
        target = paths.probe_cache_dir() / "a.json.gz"
        target.write_bytes(b"not gzip")
        self.assertIsNone(probe.read_raw_cache(target))

    def test_no_temp_file_is_left(self) -> None:
        target = paths.probe_cache_dir() / "a.json.gz"
        probe.write_raw_cache({"a": 1}, target)
        self.assertFalse(target.with_suffix(target.suffix + ".tmp").exists())


class RegistrationTests(TempAppRootTestCase):
    """台帳への登録。ffprobe が無くても走る部分。"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)
        self.logger = quiet_logger(paths.log_dir(), new_run_id())
        self.addCleanup(self.logger.close)

    def _settings(self, **overrides: object) -> config_module.Settings:
        raw = config_module.load_settings_dict()
        raw["ffprobe_path"] = str(find_ffprobe() or "ffprobe")
        raw.update(overrides)
        return config_module.build_settings(raw, require_ffprobe=False)

    def _make_video(self, relative: str) -> Path:
        target = self.source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 3000)
        return target

    @requires_ffprobe
    def test_registers_and_reuses(self) -> None:
        self._make_video("a.mp4")
        settings = self._settings()

        first = register.register_folder(
            self.source_root, settings, self.db, self.logger, run_id="r1")
        self.assertEqual(first.discovered, 1)
        self.assertEqual(first.registered, 1)

        second = register.register_folder(
            self.source_root, settings, self.db, self.logger, run_id="r2")
        self.assertEqual(second.registered, 0)
        self.assertEqual(second.reused, 1)

    @requires_ffprobe
    def test_catalog_ids_are_stable_across_runs(self) -> None:
        self._make_video("a.mp4")
        settings = self._settings()
        register.register_folder(self.source_root, settings, self.db,
                                 self.logger, run_id="r1")
        before = self.db.list_assets_under(self.source_root)[0]["catalog_id"]
        register.register_folder(self.source_root, settings, self.db,
                                 self.logger, run_id="r2")
        after = self.db.list_assets_under(self.source_root)[0]["catalog_id"]
        self.assertEqual(before, after)

    @requires_ffprobe
    def test_dry_run_writes_nothing(self) -> None:
        self._make_video("a.mp4")
        summary = register.register_folder(
            self.source_root, self._settings(), self.db, self.logger,
            run_id="r1", dry_run=True)
        self.assertEqual(summary.discovered, 1)
        self.assertEqual(self.db.list_assets_under(self.source_root), [])

    @requires_ffprobe
    def test_identical_files_are_separate_assets(self) -> None:
        """**内容が同じでも、両方あるなら別の動画として登録する。**

        「同じ内容 = 移動」と決めつけると、同一内容の動画が同じフォルダーに
        複数あるとき、2 本目以降が 1 本目の行を奪い合って台帳から消える。
        """
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            target = self.source_root / name
            target.write_bytes(b"x" * 3000)

        summary = register.register_folder(
            self.source_root, self._settings(), self.db, self.logger,
            run_id="r1")
        self.assertEqual(summary.discovered, 3)
        self.assertEqual(len(self.db.list_assets_under(self.source_root)), 3)
        self.assertEqual(summary.moved, 0)

    @requires_ffprobe
    def test_a_real_move_is_still_detected(self) -> None:
        """元の場所から消えていれば、移動として同じ行を使い続ける。"""
        original = self.source_root / "a.mp4"
        original.write_bytes(b"x" * 3000)
        settings = self._settings()
        register.register_folder(self.source_root, settings, self.db,
                                 self.logger, run_id="r1")
        first = self.db.list_assets_under(self.source_root)[0]

        original.rename(self.source_root / "renamed.mp4")
        summary = register.register_folder(self.source_root, settings, self.db,
                                           self.logger, run_id="r2")

        rows = self.db.list_assets_under(self.source_root)
        self.assertEqual(len(rows), 1, "移動で行が増えています。")
        self.assertEqual(rows[0]["asset_id"], first["asset_id"])
        self.assertEqual(rows[0]["source_relative"], "renamed.mp4")
        self.assertEqual(rows[0]["original_source_relative"], "a.mp4")
        self.assertEqual(summary.moved, 1)

    @requires_ffprobe
    def test_vanished_videos_are_flagged_not_deleted(self) -> None:
        video = self._make_video("a.mp4")
        settings = self._settings()
        register.register_folder(self.source_root, settings, self.db,
                                 self.logger, run_id="r1")
        video.unlink()
        summary = register.register_folder(self.source_root, settings, self.db,
                                           self.logger, run_id="r2")
        self.assertEqual(summary.missing, 1)
        rows = self.db.list_assets_under(self.source_root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_available"], 0)


class SourceVideoIsUntouchedTests(TempAppRootTestCase):
    """**最重要の安全仕様。** 元動画は 1 バイトも変わらない。"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)
        self.logger = quiet_logger(paths.log_dir(), new_run_id())
        self.addCleanup(self.logger.close)

    @requires_ffmpeg
    @requires_ffprobe
    def test_registration_does_not_modify_the_source(self) -> None:
        video = self.source_root / "clip.mp4"
        self.assertTrue(make_synthetic_video(find_ffmpeg(), video, duration=1.0))
        before = file_state(video)
        listing_before = sorted(p.name for p in self.source_root.rglob("*"))

        raw = config_module.load_settings_dict()
        raw["ffprobe_path"] = str(find_ffprobe())
        settings = config_module.build_settings(raw)
        register.register_folder(self.source_root, settings, self.db,
                                 self.logger, run_id="r1")

        self.assertEqual(file_state(video), before,
                         "元動画のサイズ・更新時刻・内容が変わっています。")
        self.assertEqual(sorted(p.name for p in self.source_root.rglob("*")),
                         listing_before,
                         "元動画フォルダーへファイルが作られています。")

    @requires_ffmpeg
    @requires_ffprobe
    def test_all_outputs_land_inside_userdata(self) -> None:
        video = self.source_root / "clip.mp4"
        self.assertTrue(make_synthetic_video(find_ffmpeg(), video, duration=1.0))

        raw = config_module.load_settings_dict()
        raw["ffprobe_path"] = str(find_ffprobe())
        settings = config_module.build_settings(raw)
        register.register_folder(self.source_root, settings, self.db,
                                 self.logger, run_id="r1")

        produced = [p for p in paths.app_root().rglob("*") if p.is_file()]
        self.assertTrue(produced)
        for path in produced:
            if path.name == paths.APP_ROOT_MARKER:
                continue
            self.assertTrue(
                str(path).startswith(str(paths.userdata_dir())),
                f"userdata の外に生成物があります: {path}")


if __name__ == "__main__":
    unittest.main()
