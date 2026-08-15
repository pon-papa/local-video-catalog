"""起動時の環境チェックと、「いま何ができるか」の表示."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from _support import APP_ROOT, TempAppRootTestCase

from local_video_catalog import environment_check as ec
from local_video_catalog import readiness as rd


def _result(**levels: str) -> ec.CheckResult:
    result = ec.CheckResult()
    for name, level in levels.items():
        readable = name.replace("_", " ")
        result.add(readable, level, "詳細",
                   "" if level == ec.LEVEL_OK else f"{readable} を用意してください。")
    return result


class AvailabilityTests(unittest.TestCase):
    """環境チェックの 3 段階を、3 値の可用性へ正しく直すこと。

    **「注意（未確認）」を「利用不可」にしない。** 実運用では起動時の
    確認が --quick だったせいで whisper 機能が「未確認」になり、それを
    「利用不可」と読んで「文字起こしはできません」と誤表示していた。
    """

    def _result(self, **levels: str) -> ec.CheckResult:
        result = ec.CheckResult()
        for name, level in levels.items():
            result.add(name, level, "", "" if level == ec.LEVEL_OK else "対処")
        return result

    def test_ok_becomes_available(self) -> None:
        result = self._result(ffmpeg=ec.LEVEL_OK)
        self.assertEqual(result.availability("ffmpeg"), rd.AVAILABLE)

    def test_ng_becomes_unavailable(self) -> None:
        result = self._result(ffmpeg=ec.LEVEL_NG)
        self.assertEqual(result.availability("ffmpeg"), rd.UNAVAILABLE)

    def test_warn_becomes_unknown_not_unavailable(self) -> None:
        result = self._result(**{ec.WHISPER_FEATURE: ec.LEVEL_WARN})
        self.assertEqual(result.availability(ec.WHISPER_FEATURE), rd.UNKNOWN)

    def test_absent_item_is_unknown(self) -> None:
        self.assertEqual(ec.CheckResult().availability("ffmpeg"), rd.UNKNOWN)

    def test_readiness_uses_the_availabilities(self) -> None:
        result = ec.CheckResult()
        for name in ("ffmpeg", "ffprobe", ec.WHISPER_FEATURE,
                     ec.WHISPER_MODEL, ec.LOCAL_AI, ec.VISUAL_MODEL,
                     ec.VISION):
            result.add(name, ec.LEVEL_OK)
        self.assertTrue(result.readiness().can_start)

    def test_an_unchecked_vision_capability_blocks(self) -> None:
        """**画像入力を確かめないまま「開始できます」と言わない。**"""
        result = ec.CheckResult()
        for name in ("ffmpeg", "ffprobe", ec.WHISPER_FEATURE,
                     ec.WHISPER_MODEL, ec.LOCAL_AI, ec.VISUAL_MODEL):
            result.add(name, ec.LEVEL_OK)
        self.assertFalse(result.readiness().can_start)

    def test_local_ai_down_blocks_the_run(self) -> None:
        """**映像の解析は必須。** 飛ばして開始する道は用意しない。"""
        result = ec.CheckResult()
        for name in ("ffmpeg", "ffprobe", ec.WHISPER_FEATURE,
                     ec.WHISPER_MODEL):
            result.add(name, ec.LEVEL_OK)
        result.add(ec.LOCAL_AI, ec.LEVEL_NG, "未接続", "LM Studio を起動")
        self.assertFalse(result.readiness().can_start)
        self.assertFalse(result.readiness(skip_transcription=True).can_start)

    def test_connection_failure_is_unavailable(self) -> None:
        """繋がらなければ「利用不可」。未確認と混ぜない。"""
        from local_video_catalog import config as config_module

        raw = config_module.load_settings_dict()
        raw["vlm"] = {**raw["vlm"], "base_url": "http://127.0.0.1:9/v1"}
        result = ec.CheckResult()
        ec.check_local_ai(result, raw)
        self.assertEqual(result.availability(ec.LOCAL_AI), rd.UNAVAILABLE)


class SerialisationTests(TempAppRootTestCase):
    def test_json_carries_the_availability(self) -> None:
        from local_video_catalog import config as config_module

        raw = config_module.load_settings_dict()
        settings = config_module.build_settings(raw, require_ffprobe=False)
        result = ec.check_environment(raw=raw, settings=settings, quick=True)
        payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
        self.assertIn("availability", payload)
        for key in ("ffmpeg", "ffprobe", "whisper_feature", "whisper_model",
                    "local_ai", "visual_model"):
            self.assertIn(key, payload["availability"])

    def test_check_changes_nothing(self) -> None:
        from local_video_catalog import config as config_module
        from local_video_catalog import paths

        paths.ensure_userdata_tree()
        before = sorted(p.name for p in paths.userdata_dir().rglob("*"))
        raw = config_module.load_settings_dict()
        settings = config_module.build_settings(raw, require_ffprobe=False)
        ec.check_environment(raw=raw, settings=settings, quick=True)
        self.assertEqual(sorted(p.name for p in paths.userdata_dir().rglob("*")),
                         before)


class DisplayTests(unittest.TestCase):
    def test_marks_are_defined_for_every_level(self) -> None:
        for level in (ec.LEVEL_OK, ec.LEVEL_WARN, ec.LEVEL_NG):
            self.assertIn(level, ec.MARKS)

    def test_lines_start_with_a_mark(self) -> None:
        result = ec.CheckResult()
        result.add("ffmpeg", ec.LEVEL_OK, "C:/ffmpeg.exe")
        result.add("ローカルAI", ec.LEVEL_NG, "未接続", "起動してください。")
        lines = ec.format_lines(result)
        self.assertTrue(lines[0].startswith(ec.MARKS[ec.LEVEL_OK]))
        self.assertTrue(any(line.startswith(ec.MARKS[ec.LEVEL_NG])
                            for line in lines))


class GuiWiringTests(unittest.TestCase):
    """画面が起動時に確認し、**画面スレッドを塞がない**こと。"""

    def setUp(self) -> None:
        self.source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                       / "app.py").read_text(encoding="utf-8")

    def test_check_runs_at_startup(self) -> None:
        constructor = self.source.split("def __init__", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_start_environment_check", constructor)

    def test_check_runs_off_the_main_thread(self) -> None:
        block = self.source.split("def _start_environment_check", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("threading.Thread", block)
        self.assertIn("daemon=True", block)

    def test_results_are_collected_without_blocking(self) -> None:
        block = self.source.split("def _poll_environment", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("get_nowait", block)
        self.assertIn("after", block)

    def test_manual_button_is_kept(self) -> None:
        self.assertIn('text="環境チェック"', self.source)

    def test_start_is_gated_but_browsing_is_not(self) -> None:
        block = self.source.split("def _set_running", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("can_start", block)
        # 閲覧系のボタンは無効化の対象に入っていない
        self.assertNotIn("_open_catalog", block)
        self.assertNotIn("_show_summary", block)

    def test_preview_shows_what_will_be_done(self) -> None:
        """「対象確認」で**今回行う工程**が出ること。"""
        block = self.source.split("def _preview", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("stage_lines", block)

    def test_start_shows_what_will_be_done(self) -> None:
        block = self.source.split("def _start(self", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("stage_lines", block)

    def test_status_area_reports_library_scan_separately(self) -> None:
        block = self.source.split("def _update_status_from", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("動画ライブラリを確認しています", block)
        self.assertIn("現在:", block)


if __name__ == "__main__":
    unittest.main()
