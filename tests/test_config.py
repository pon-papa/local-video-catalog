"""config — 設定のマージ・検証と、保存先を設定で変えられないこと."""

from __future__ import annotations

import json
import unittest

from _support import TempAppRootTestCase

from local_video_catalog import config as config_module
from local_video_catalog import paths


class MergeTests(TempAppRootTestCase):
    """3 層マージの挙動。"""

    def test_defaults_are_returned_without_a_settings_file(self) -> None:
        data = config_module.load_settings_dict()
        self.assertEqual(data["workers"], 8)
        self.assertEqual(data["vlm"]["base_url"], config_module.DEFAULT_VLM_BASE_URL)

    def test_user_settings_override_defaults(self) -> None:
        config_module.save_settings_dict({"workers": 4})
        self.assertEqual(config_module.load_settings_dict()["workers"], 4)

    def test_nested_merge_keeps_untouched_keys(self) -> None:
        config_module.save_settings_dict({"vlm": {"timeout_seconds": 111}})
        vlm = config_module.load_settings_dict()["vlm"]
        self.assertEqual(vlm["timeout_seconds"], 111)
        self.assertEqual(vlm["model_match"], config_module.DEFAULT_VISUAL_MODEL)

    def test_none_values_do_not_erase_defaults(self) -> None:
        config_module.save_settings_dict({"workers": None})
        self.assertEqual(config_module.load_settings_dict()["workers"], 8)

    def test_extra_config_file_wins(self) -> None:
        config_module.save_settings_dict({"workers": 4})
        extra = self.temp_dir / "extra.json"
        extra.write_text(json.dumps({"workers": 2}), encoding="utf-8")
        self.assertEqual(config_module.load_settings_dict(extra)["workers"], 2)

    def test_missing_extra_config_raises(self) -> None:
        with self.assertRaises(config_module.ConfigError):
            config_module.load_settings_dict(self.temp_dir / "nope.json")

    def test_broken_json_reports_the_file(self) -> None:
        broken = paths.settings_path()
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(config_module.ConfigError) as ctx:
            config_module.load_settings_dict()
        self.assertIn("settings.json", str(ctx.exception))

    def test_settings_are_written_atomically(self) -> None:
        target = config_module.save_settings_dict({"workers": 3})
        self.assertEqual(target, paths.settings_path())
        self.assertFalse(target.with_suffix(target.suffix + ".tmp").exists())


class RemovedKeyTests(TempAppRootTestCase):
    """保存先を設定で変えられないこと。**cleanup の基点を守る。**"""

    def test_data_root_is_ignored_not_honoured(self) -> None:
        elsewhere = self.make_source_dir("elsewhere")
        config_module.save_settings_dict({"data_root": str(elsewhere)})
        data = config_module.load_settings_dict()
        self.assertNotIn("data_root", data)

    def test_data_root_is_stripped_before_being_saved(self) -> None:
        """古い設定を持ち込まれても、保存し直したときに残らない。"""
        elsewhere = self.make_source_dir("elsewhere")
        config_module.save_settings_dict({"data_root": str(elsewhere),
                                          "workers": 5})
        written = json.loads(paths.settings_path().read_text(encoding="utf-8"))
        self.assertNotIn("data_root", written)
        self.assertEqual(written["workers"], 5)

    def test_database_path_stays_inside_userdata(self) -> None:
        elsewhere = self.make_source_dir("elsewhere")
        config_module.save_settings_dict({"data_root": str(elsewhere)})
        config_module.load_settings_dict()
        self.assertTrue(
            str(paths.database_path()).startswith(str(paths.userdata_dir())))

    def test_settings_object_has_no_data_root(self) -> None:
        """Settings に保存先を持たせない（また設定で上書きしたくなるため）。"""
        settings = config_module.build_settings(
            config_module.load_settings_dict(), require_ffprobe=False)
        for forbidden in ("data_root", "catalog_dir", "cache_dir", "log_dir"):
            self.assertFalse(hasattr(settings, forbidden),
                             f"Settings に {forbidden} があります。")


class BuildSettingsTests(TempAppRootTestCase):
    def _build(self, **overrides: object) -> config_module.Settings:
        data = config_module.load_settings_dict()
        data.update(overrides)
        return config_module.build_settings(data, require_ffprobe=False)

    def test_zero_workers_means_use_the_default(self) -> None:
        self.assertEqual(self._build(workers=0).workers,
                         config_module.DEFAULT_SETTINGS["workers"])

    def test_negative_workers_are_clamped(self) -> None:
        self.assertEqual(self._build(workers=-4).workers, config_module.MIN_WORKERS)

    def test_workers_above_the_limit_raise(self) -> None:
        with self.assertRaises(config_module.ConfigError):
            self._build(workers=config_module.MAX_WORKERS + 1)

    def test_extensions_are_normalised(self) -> None:
        settings = self._build(extensions=["MP4", ".MOV", "mkv", ""])
        self.assertEqual(settings.extensions, (".mkv", ".mov", ".mp4"))

    def test_empty_extension_list_means_use_the_defaults(self) -> None:
        self.assertEqual(self._build(extensions=[]).extensions,
                         tuple(sorted(config_module.DEFAULT_EXTENSIONS)))

    def test_extensions_that_normalise_to_nothing_raise(self) -> None:
        """指定した結果 0 件になる場合は、黙って全種類を対象にしない。"""
        with self.assertRaises(config_module.ConfigError):
            self._build(extensions=["", "   "])

    def test_bad_fingerprint_sizes_raise(self) -> None:
        with self.assertRaises(config_module.ConfigError):
            self._build(fingerprint={"head_bytes": 0, "tail_bytes": 1})

    def test_missing_ffprobe_raises_when_required(self) -> None:
        data = config_module.load_settings_dict()
        data["ffprobe_path"] = str(self.temp_dir / "nope.exe")
        with self.assertRaises(config_module.ConfigError):
            config_module.build_settings(data, require_ffprobe=True)

    def test_snapshot_contains_no_app_root_path(self) -> None:
        """実行記録に APP_ROOT の絶対パスを焼き込まない（移動耐性）。"""
        snapshot = self._build().config_snapshot()
        text = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn(str(paths.app_root()), text)
        self.assertNotIn("userdata", text)


class ProvenDefaultTests(TempAppRootTestCase):
    """実運用で確かめた既定値を、うっかり変えないための固定。"""

    def test_vad_is_disabled_by_default(self) -> None:
        """有効にすると無音 60 秒が 3 秒→598 秒、日本語 CER が 0.000→0.737。"""
        self.assertFalse(config_module.DEFAULT_SETTINGS["asr"]["vad_enabled"])
        self.assertFalse(config_module.load_settings_dict()["asr"]["vad_enabled"])

    def test_summary_timeout_is_separate_and_longer(self) -> None:
        """視覚概要はフレーム枚数に比例して伸びる（実測 約16秒/枚）。

        フレーム 1 枚と同じ 300 秒を適用すると、22〜24 枚の動画が
        まとめて失敗する。
        """
        vlm = config_module.load_settings_dict()["vlm"]
        self.assertIn("summary_timeout_seconds", vlm)
        self.assertGreater(vlm["summary_timeout_seconds"], vlm["timeout_seconds"])
        self.assertGreaterEqual(vlm["summary_timeout_seconds"], 1200)

    def test_queue_seconds_avoids_overlapping_windows(self) -> None:
        """既定 3 秒だと窓が重なり同じ内容が繰り返し出る。"""
        self.assertGreaterEqual(
            config_module.load_settings_dict()["asr"]["queue_seconds"], 30)

    def test_single_concurrent_vlm_request(self) -> None:
        self.assertEqual(
            config_module.load_settings_dict()["vlm"]["maximum_concurrent_requests"], 1)

    def test_consecutive_failure_limit_is_three(self) -> None:
        self.assertEqual(
            config_module.load_settings_dict()["run"]["consecutive_failure_limit"], 3)

    def test_base_url_is_loopback(self) -> None:
        self.assertIn("127.0.0.1", config_module.DEFAULT_VLM_BASE_URL)

    def test_chunking_limits_the_loss_on_interruption(self) -> None:
        self.assertEqual(
            config_module.load_settings_dict()["asr"]["chunk_duration_seconds"], 300)


class VerifyUserDataTests(TempAppRootTestCase):
    def test_verify_creates_and_reports(self) -> None:
        info = config_module.verify_userdata()
        self.assertTrue(info["writable"])
        self.assertGreater(info["total_bytes"], 0)
        self.assertTrue(paths.descriptions_dir().is_dir())

    def test_verify_reports_the_app_root(self) -> None:
        info = config_module.verify_userdata()
        self.assertEqual(info["app_root"], str(paths.app_root()))

    def test_write_check_file_is_removed(self) -> None:
        config_module.verify_userdata()
        self.assertFalse((paths.userdata_dir() / ".write_check.tmp").exists())


class WhisperFilterDetectionTests(TempAppRootTestCase):
    """whisper フィルターの判定が 1 箇所であること、緩すぎないこと。"""

    def test_missing_ffmpeg_is_false(self) -> None:
        self.assertFalse(config_module.ffmpeg_has_whisper(None))
        self.assertFalse(
            config_module.ffmpeg_has_whisper(self.temp_dir / "nope.exe"))


if __name__ == "__main__":
    unittest.main()
