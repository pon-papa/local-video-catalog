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

v1 の簡素化（**映像の解析は必須工程**）:

    映像の解析はこのツールの本体なので、飛ばす選択肢を画面に出さない。
    LM Studio が使えないなら開始できない。「飛ばせば開始できます」とも
    案内しない（案内した先に、意味のある結果が無いため）。
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
    # **画像入力は「確かめて使えた」状態が既定。**
    # 未確認のままでは開始できない（それが v1 の安全側の決まり）。
    "vision": rd.AVAILABLE,
}


def evaluate(**overrides) -> rd.RunReadiness:
    values = dict(ALL_OK)
    skips = {key: overrides.pop(key) for key in
             ("skip_transcription", "checking")
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
    """B / C. LM Studio 未接続のとき — **飛ばせないので開始できない。**"""

    def test_the_run_is_blocked(self) -> None:
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertTrue(readiness.blockers)

    def test_the_blocker_says_to_start_lm_studio(self) -> None:
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE)
        text = "\n".join(readiness.blockers[0].lines())
        self.assertIn("LM Studio", text)
        self.assertIn("ローカルサーバー", text)

    def test_no_advice_to_skip_the_visual_analysis(self) -> None:
        """**「映像の解析を飛ばせば開始できます」と言わない。**"""
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE)
        self.assertEqual(readiness.blockers[0].skip_option, "")
        text = "\n".join(readiness.detail_lines())
        self.assertNotIn("映像の解析を飛ばす", text)

    def test_skipping_transcription_does_not_unblock_it(self) -> None:
        """文字起こしを飛ばしても、映像の解析の不足は解消しない。"""
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE,
                             skip_transcription=True)
        self.assertFalse(readiness.can_start)

    def test_local_ai_never_blocks_transcription(self) -> None:
        """**文字起こしは LM Studio を使わない。** 巻き添えにしない。"""
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE)
        text = "\n".join(readiness.detail_lines())
        self.assertNotIn("文字起こし機能を利用できません", text)

    def test_model_missing_is_treated_like_no_connection(self) -> None:
        readiness = evaluate(visual_model=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertEqual(readiness.blockers[0].skip_option, "")
        self.assertIn("モデル", readiness.blockers[0].problem)

    def test_unknown_local_ai_still_lets_the_run_start(self) -> None:
        """未確認は「使えない」ではない。必須工程でも同じ。"""
        self.assertTrue(evaluate(local_ai=rd.UNKNOWN).can_start)


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

    def test_two_reasons_are_shown(self) -> None:
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE,
                             whisper_feature=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)
        self.assertEqual(len(readiness.blockers), 2)

    def test_skipping_transcription_leaves_the_visual_reason(self) -> None:
        """**飛ばせるのは文字起こしだけ。** 片方だけ消える。"""
        readiness = evaluate(local_ai=rd.UNAVAILABLE,
                             visual_model=rd.UNAVAILABLE,
                             whisper_feature=rd.UNAVAILABLE,
                             skip_transcription=True)
        self.assertFalse(readiness.can_start)
        self.assertEqual(len(readiness.blockers), 1)
        self.assertIn("ローカルAI", readiness.blockers[0].problem)


class FoundationTests(unittest.TestCase):
    """飛ばせない土台。"""

    def test_missing_ffprobe_cannot_be_skipped(self) -> None:
        readiness = evaluate(ffprobe=rd.UNAVAILABLE, skip_transcription=True)
        self.assertFalse(readiness.can_start)
        self.assertEqual(readiness.blockers[0].skip_option, "")

    def test_missing_ffmpeg_cannot_be_skipped(self) -> None:
        readiness = evaluate(ffmpeg=rd.UNAVAILABLE, skip_transcription=True)
        self.assertFalse(readiness.can_start)
        self.assertEqual(readiness.blockers[0].skip_option, "")

    def test_only_the_transcription_is_skippable(self) -> None:
        """**画面に出す「飛ばす」は 1 つだけ。**"""
        self.assertEqual(set(rd.SKIP_LABELS), {rd.SKIP_TRANSCRIPTION})
        self.assertFalse(hasattr(rd, "SKIP_VISUAL"))


class UnknownTests(unittest.TestCase):
    """K. **「未確認」を「利用不可」と混同しない。**"""

    def test_unknown_does_not_block(self) -> None:
        readiness = evaluate(whisper_feature=rd.UNKNOWN)
        self.assertTrue(readiness.can_start,
                        "未確認なだけで開始を止めてはいけません。")

    def test_unknown_local_ai_does_not_block(self) -> None:
        self.assertTrue(evaluate(local_ai=rd.UNKNOWN).can_start)

    def test_unknown_vision_does_block(self) -> None:
        """**画像入力だけは例外。** 確かめていないなら始めない。

        始めてしまうと、全部の動画が映像解析で落ちて時間だけが失われる。
        他の項目と違い、やり直しの費用が桁違いに大きい。
        """
        readiness = evaluate(vision=rd.UNKNOWN)
        self.assertFalse(readiness.can_start)
        self.assertIn("確認できませんでした",
                      "\n".join(readiness.detail_lines()))

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
        self.assertNotIn("チェックを入れる", text)

    def test_browsing_is_always_offered(self) -> None:
        for readiness in (evaluate(), evaluate(ffmpeg=rd.UNAVAILABLE)):
            with self.subTest(can_start=readiness.can_start):
                self.assertIn("いつでもできます",
                              "\n".join(readiness.detail_lines()))


class StageListTests(unittest.TestCase):
    """I. **今回なにを行うのかが見えること。**

    「対象確認」と開始前に、行う工程と行わない工程を並べて出す。
    """

    def test_every_stage_is_named(self) -> None:
        text = "\n".join(evaluate().stage_lines())
        for stage in ("動画ライブラリの確認", "代表画像の抽出", "映像の解析",
                      "文字起こし", "説明文の作成"):
            with self.subTest(stage=stage):
                self.assertIn(stage, text)

    def test_the_visual_analysis_is_always_listed_when_startable(self) -> None:
        self.assertIn("映像の解析", evaluate(skip_transcription=True).performed)

    def test_a_skipped_stage_is_shown_as_not_performed(self) -> None:
        readiness = evaluate(skip_transcription=True)
        self.assertNotIn("文字起こし", readiness.performed)
        text = "\n".join(readiness.stage_lines())
        self.assertIn("文字起こしは行いません", text)

    def test_detail_lines_include_the_stage_list(self) -> None:
        text = "\n".join(evaluate().detail_lines())
        self.assertIn("今回行う工程", text)
        self.assertIn("映像の解析", text)


class InternalOptionTests(unittest.TestCase):
    """J. 画面から消しても、**内部の飛ばす仕組みは残す。**

    LM Studio を用意できない試験環境で処理全体を通すために必要。
    利用者へは見せない。
    """

    def _read(self, *parts: str) -> str:
        return (APP_ROOT.joinpath("src", "local_video_catalog", *parts)
                ).read_text(encoding="utf-8")

    def test_the_pipeline_still_accepts_the_internal_flag(self) -> None:
        self.assertIn("--skip-visual", self._read("pipeline.py"))

    def test_the_gui_state_no_longer_carries_it(self) -> None:
        source = self._read("gui", "state.py")
        tree = ast.parse(source)
        state = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.ClassDef) and node.name == "GuiState")
        names = [node.target.id for node in state.body
                 if isinstance(node, ast.AnnAssign)]
        self.assertNotIn("skip_visual_analysis", names)

    def test_the_gui_never_passes_the_internal_flag(self) -> None:
        self.assertNotIn("--skip-visual", self._read("gui", "state.py"))
        self.assertNotIn("--skip-visual", self._read("gui", "app.py"))

    def test_an_old_saved_state_cannot_re_enable_it(self) -> None:
        """古い設定ファイルに残っていても、黙って飛ばしたりしない。"""
        from local_video_catalog.gui import state as state_module

        state = state_module.GuiState.from_dict({"skip_visual_analysis": True})
        self.assertFalse(hasattr(state, "skip_visual_analysis"))
        self.assertNotIn("--skip-visual", state.pipeline_arguments())


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

    def test_the_visual_skip_checkbox_is_gone(self) -> None:
        """G. **画面に「映像の解析を飛ばす」を出さない。**"""
        self.assertNotIn("映像の解析を飛ばす", self.source)
        self.assertNotIn("skip_visual_var", self.source)

    def test_the_transcription_checkbox_remains(self) -> None:
        self.assertIn('text="文字起こしを飛ばす"', self.source)
        self.assertIn("skip_asr_var", self.source)

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
