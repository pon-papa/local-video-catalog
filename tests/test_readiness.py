"""開始してよいかの判定 — **失敗すると分かっている処理を始めさせない**.

実運用で見つかった不整合:

    LM Studio 未接続なのに「処理開始」が押せた。押せば映像解析で必ず
    失敗する。しかも表示は「文字起こしもできません」と出ていたが、
    **文字起こしは LM Studio を使わない**ので、これは誤りだった。

原因は 2 つ。
  1. 起動時の確認が ``--quick`` で whisper 機能を「未確認」にしていた
  2. 「未確認」を「利用不可」として扱っていた

規則:
    足りない環境 ＋ その工程を行う設定  → 開始できない
    足りない環境 ＋ 「飛ばす」を明示     → 開始できる
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from _support import APP_ROOT

from local_video_catalog import readiness as rd

ALL_OK = {
    "ffmpeg": rd.AVAILABLE, "ffprobe": rd.AVAILABLE,
    "whisper_feature": rd.AVAILABLE, "whisper_model": rd.AVAILABLE,
    "local_ai": rd.AVAILABLE, "visual_model": rd.AVAILABLE,
}


def evaluate(**overrides) -> rd.RunReadiness:
    values = dict(ALL_OK)
    skips = {key: overrides.pop(key) for key in
             ("skip_visual", "skip_transcription", "checking")
             if key in overrides}
    values.update(overrides)
    return rd.evaluate_run_readiness(**values, **skips)


class HappyPathTests(unittest.TestCase):
    """A. すべて揃っていて skip なし → 開始できる。"""

    def test_everything_available(self) -> None:
        readiness = evaluate()
        self.assertTrue(readiness.can_start)
        self.assertEqual(readiness.blockers, [])
        self.assertIn("解析を開始できます", readiness.status_line())

    def test_stages_are_listed(self) -> None:
        readiness = evaluate()
        self.assertIn("映像の解析", readiness.performed)
        self.assertIn("文字起こし", readiness.performed)
        self.assertIn("説明文の作成", readiness.performed)


class LocalAiTests(unittest.TestCase):
    """B / C. LM Studio 未接続のとき。"""

    def test_without_skip_the_run_is_blocked(self) -> None:
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertTrue(readiness.blockers)

    def test_the_blocker_names_the_skip_that_resolves_it(self) -> None:
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE)
        blocker = readiness.blockers[0]
        self.assertEqual(blocker.skip_option, rd.SKIP_VISUAL)
        text = "\n".join(blocker.lines())
        self.assertIn("映像の解析を飛ばす", text)
        self.assertIn("LM Studio", text)

    def test_skipping_visual_allows_starting(self) -> None:
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE, skip_visual=True)
        self.assertTrue(readiness.can_start)

    def test_skipping_visual_warns_that_descriptions_are_templates(self) -> None:
        """**説明文は LM Studio 無しでも完了する。** ただし定型文になる。"""
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE, skip_visual=True)
        text = "\n".join(readiness.warnings)
        self.assertIn("決まった文面", text)

    def test_local_ai_never_blocks_transcription(self) -> None:
        """**文字起こしは LM Studio を使わない。** 巻き添えにしない。"""
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE, skip_visual=True)
        self.assertIn("文字起こし", readiness.performed)
        text = "\n".join(readiness.detail_lines())
        self.assertNotIn("文字起こし機能を利用できません", text)

    def test_model_missing_is_treated_like_no_connection(self) -> None:
        readiness = evaluate(visual_model=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertEqual(readiness.blockers[0].skip_option, rd.SKIP_VISUAL)


class TranscriptionTests(unittest.TestCase):
    """D / E. 文字起こしを使えないとき。"""

    def test_without_skip_the_run_is_blocked(self) -> None:
        readiness = evaluate(whisper_feature=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertEqual(readiness.blockers[0].skip_option,
                         rd.SKIP_TRANSCRIPTION)

    def test_skipping_transcription_allows_starting(self) -> None:
        readiness = evaluate(whisper_feature=rd.UNAVAILABLE,
                             skip_transcription=True)
        self.assertTrue(readiness.can_start)

    def test_missing_model_blocks_too(self) -> None:
        readiness = evaluate(whisper_model=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertIn("モデル", readiness.blockers[0].problem)

    def test_transcription_problems_never_mention_lm_studio(self) -> None:
        readiness = evaluate(whisper_feature=rd.UNAVAILABLE)
        self.assertNotIn("LM Studio",
                         "\n".join(readiness.blockers[0].lines()))


class BothMissingTests(unittest.TestCase):
    """F / G. 両方使えないとき。"""

    def test_neither_skipped_blocks_with_two_reasons(self) -> None:
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE,
                             whisper_feature=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertEqual(len(readiness.blockers), 2)

    def test_both_skipped_still_starts(self) -> None:
        """登録・代表画像・説明文（定型文）は成立する。"""
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE,
                             whisper_feature=rd.UNAVAILABLE,
                             skip_visual=True, skip_transcription=True)
        self.assertTrue(readiness.can_start)
        self.assertIn("動画の登録", readiness.performed)
        self.assertIn("代表画像の抽出", readiness.performed)
        self.assertIn("説明文の作成", readiness.performed)


class FoundationTests(unittest.TestCase):
    """飛ばせない土台。"""

    def test_missing_ffprobe_cannot_be_skipped(self) -> None:
        readiness = evaluate(ffprobe=rd.UNAVAILABLE, skip_visual=True,
                             skip_transcription=True)
        self.assertFalse(readiness.can_start)
        self.assertEqual(readiness.blockers[0].skip_option, "")

    def test_missing_ffmpeg_cannot_be_skipped(self) -> None:
        readiness = evaluate(ffmpeg=rd.UNAVAILABLE, skip_visual=True,
                             skip_transcription=True)
        self.assertFalse(readiness.can_start)
        self.assertEqual(readiness.blockers[0].skip_option, "")


class UnknownTests(unittest.TestCase):
    """K. **「未確認」を「利用不可」と混同しない。**"""

    def test_unknown_does_not_block(self) -> None:
        readiness = evaluate(whisper_feature=rd.UNKNOWN)
        self.assertTrue(readiness.can_start,
                        "未確認なだけで開始を止めてはいけません。")

    def test_unknown_local_ai_does_not_block(self) -> None:
        self.assertTrue(evaluate(local_ai=rd.UNKNOWN).can_start)

    def test_warn_level_maps_to_unknown(self) -> None:
        from local_video_catalog import environment_check as ec

        self.assertEqual(
            rd.availability_from_level(ec.LEVEL_WARN, ok_level=ec.LEVEL_OK,
                                       warn_level=ec.LEVEL_WARN),
            rd.UNKNOWN)
        self.assertEqual(
            rd.availability_from_level(ec.LEVEL_NG, ok_level=ec.LEVEL_OK,
                                       warn_level=ec.LEVEL_WARN),
            rd.UNAVAILABLE)


class CheckingTests(unittest.TestCase):
    """J. 確認中は開始させない。"""

    def test_checking_blocks_starting(self) -> None:
        readiness = evaluate(checking=True)
        self.assertFalse(readiness.can_start)
        self.assertTrue(readiness.checking)
        self.assertIn("確認しています", readiness.status_line())

    def test_checking_says_to_wait(self) -> None:
        self.assertIn("お待ちください",
                      "\n".join(evaluate(checking=True).detail_lines()))


class MessageTests(unittest.TestCase):
    """状況説明欄に、理由と対処が出ること。"""

    def test_blocked_message_says_why_and_how(self) -> None:
        text = "\n".join(evaluate(local_ai=rd.UNAVAILABLE,
                                  visual_model=rd.UNAVAILABLE).detail_lines())
        self.assertIn("現在は解析を開始できません", text)
        self.assertIn("LM Studio を起動", text)
        self.assertIn("チェックを入れる", text)

    def test_browsing_is_always_offered(self) -> None:
        for readiness in (evaluate(), evaluate(ffmpeg=rd.UNAVAILABLE)):
            with self.subTest(can_start=readiness.can_start):
                self.assertIn("いつでもできます",
                              "\n".join(readiness.detail_lines()))


class StageDependencyTests(unittest.TestCase):
    """L. 判定と、実際の工程が必要とするものが一致すること。

    **実装を読んで確かめた依存関係:**

        代表画像   ffmpeg
        映像の解析 LM Studio ＋ モデル
        文字起こし ffmpeg ＋ whisper 機能 ＋ モデル（**LM Studio 不要**）
        説明文     LM Studio があれば使い、無ければ定型文
    """

    def _source(self, name: str) -> str:
        return (APP_ROOT / "src" / "local_video_catalog" / "stages"
                / f"{name}.py").read_text(encoding="utf-8")

    def test_transcription_stage_does_not_use_the_local_ai(self) -> None:
        source = self._source("transcription")
        self.assertNotIn("vlm_client", source)
        self.assertNotIn("LocalVlmClient", source)

    def test_frames_stage_needs_only_ffmpeg(self) -> None:
        source = self._source("frames")
        self.assertIn("ffmpeg_path", source)
        self.assertNotIn("vlm_client", source)

    def test_visual_stage_needs_the_local_ai(self) -> None:
        self.assertIn("vlm_client", self._source("visual"))

    def test_description_falls_back_without_the_local_ai(self) -> None:
        """**説明文は LM Studio 無しでも完了する。** だから開始は止めない。"""
        source = self._source("description")
        self.assertIn("GENERATOR_FALLBACK", source)
        self.assertIn("fallback_content_text", source)

    def test_readiness_matches_those_dependencies(self) -> None:
        # 文字起こしだけが壊れているとき、映像の解析は止まらない
        readiness = evaluate(whisper_feature=rd.UNAVAILABLE,
                             skip_transcription=True)
        self.assertTrue(readiness.can_start)
        self.assertIn("映像の解析", readiness.performed)


class SingleSourceOfTruthTests(unittest.TestCase):
    """H. 画面が独自に条件を書いていないこと。"""

    def setUp(self) -> None:
        self.source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                       / "app.py").read_text(encoding="utf-8")

    def test_gui_delegates_to_evaluate_run_readiness(self) -> None:
        self.assertIn("evaluate_run_readiness", self.source)

    def test_checkbox_changes_refresh_immediately(self) -> None:
        self.assertIn("command=self._on_skip_changed", self.source)
        block = self.source.split("def _on_skip_changed", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("_refresh_readiness", block)

    def test_start_re_checks_before_running(self) -> None:
        # "def _start" だと _start_environment_check に当たる
        block = self.source.split("def _start(self", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("_readiness()", block)
        self.assertIn("can_start", block)

    def test_gui_does_not_reimplement_the_rules(self) -> None:
        """条件を画面へ書き写すと、表示と実際が食い違う。"""
        for invented in ("can_analyse_visual", "can_transcribe",
                         "local_ai ==", "whisper_feature =="):
            with self.subTest(name=invented):
                self.assertNotIn(invented, self.source)

    def test_startup_check_is_not_quick(self) -> None:
        """``--quick`` だと whisper 機能が未確認のまま残る。"""
        block = self.source.split("def _start_environment_check", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertNotIn('"--quick"', block)


if __name__ == "__main__":
    unittest.main()
