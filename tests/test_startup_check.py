"""起動時の環境チェックと、「いま何ができるか」の表示."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from _support import APP_ROOT, TempAppRootTestCase

from local_video_catalog import environment_check as ec


def _result(**levels: str) -> ec.CheckResult:
    result = ec.CheckResult()
    for name, level in levels.items():
        readable = name.replace("_", " ")
        result.add(readable, level, "詳細",
                   "" if level == ec.LEVEL_OK else f"{readable} を用意してください。")
    return result


class CapabilityTests(unittest.TestCase):
    """**OK/NG ではなく「何ができるか」で伝える。**"""

    def _full(self, **overrides: str) -> ec.CheckResult:
        levels = {
            "ffmpeg": ec.LEVEL_OK, "ffprobe": ec.LEVEL_OK,
            "whisper 機能": ec.LEVEL_OK, "文字起こしモデル": ec.LEVEL_OK,
            "ローカルAI": ec.LEVEL_OK, "映像解析モデル": ec.LEVEL_OK,
        }
        levels.update(overrides)
        result = ec.CheckResult()
        for name, level in levels.items():
            result.add(name, level, "",
                       "" if level == ec.LEVEL_OK else f"{name} を用意してください。")
        return result

    def test_everything_available(self) -> None:
        capabilities = self._full().capabilities()
        self.assertTrue(capabilities.can_start)
        self.assertTrue(capabilities.can_analyse_visual)
        self.assertTrue(capabilities.can_transcribe)
        self.assertIn("解析を開始できます",
                      "\n".join(capabilities.summary_lines()))

    def test_lm_studio_down_still_allows_starting(self) -> None:
        """**LM Studio が止まっていても、登録と文字起こしはできる。**"""
        capabilities = self._full(
            **{"ローカルAI": ec.LEVEL_NG}).capabilities()
        self.assertTrue(capabilities.can_start)
        self.assertFalse(capabilities.can_analyse_visual)
        text = "\n".join(capabilities.summary_lines())
        self.assertIn("映像の解析はできません", text)
        self.assertIn("それ以外は開始できます", text)

    def test_missing_ffmpeg_blocks_starting(self) -> None:
        capabilities = self._full(ffmpeg=ec.LEVEL_NG).capabilities()
        self.assertFalse(capabilities.can_start)
        text = "\n".join(capabilities.summary_lines())
        self.assertIn("新しい解析を開始できません", text)

    def test_browsing_is_never_blocked(self) -> None:
        """**閲覧まで禁止しない。**"""
        for broken in ("ffmpeg", "ffprobe", "ローカルAI", "文字起こしモデル"):
            with self.subTest(broken=broken):
                capabilities = self._full(**{broken: ec.LEVEL_NG}).capabilities()
                self.assertTrue(capabilities.can_browse)
                self.assertIn("いつでもできます",
                              "\n".join(capabilities.summary_lines()))

    def test_missing_whisper_model_only_blocks_transcription(self) -> None:
        capabilities = self._full(
            **{"文字起こしモデル": ec.LEVEL_NG}).capabilities()
        self.assertTrue(capabilities.can_start)
        self.assertTrue(capabilities.can_analyse_visual)
        self.assertFalse(capabilities.can_transcribe)

    def test_advice_is_carried_into_the_blockers(self) -> None:
        capabilities = self._full(
            **{"ローカルAI": ec.LEVEL_NG}).capabilities()
        self.assertTrue(capabilities.blockers)
        self.assertIn("ローカルAI", " ".join(capabilities.blockers))


class SerialisationTests(TempAppRootTestCase):
    def test_json_carries_the_capabilities(self) -> None:
        from local_video_catalog import config as config_module

        raw = config_module.load_settings_dict()
        settings = config_module.build_settings(raw, require_ffprobe=False)
        result = ec.check_environment(raw=raw, settings=settings, quick=True)
        payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
        self.assertIn("capabilities", payload)
        for key in ("can_register", "can_analyse_visual", "can_transcribe",
                    "can_start", "blockers"):
            self.assertIn(key, payload["capabilities"])

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

    def test_status_area_reports_library_scan_separately(self) -> None:
        block = self.source.split("def _update_status_from", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("動画ライブラリを確認しています", block)
        self.assertIn("現在:", block)


if __name__ == "__main__":
    unittest.main()
