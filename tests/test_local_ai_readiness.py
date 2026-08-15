"""**「LM Studio が起動している」だけでは解析を始めさせない.**

実運用で起きうる食い違い:

    LM Studio は起動している。接続もできる。それでも
      - 使うモデルが選ばれていない
      - 前に選んだモデルが今は読み込まれていない
      - そのモデルが画像を受け取れない
    のどれかなら、映像の解析は 1 本目から必ず失敗する。

だから開始条件を 4 段にして、**実際に画像を 1 枚送るところまで**
確かめてから「開始できます」と言う。

A 接続できない            → 開始不可
B 接続済み・モデル未選択  → 開始不可
C 選んだモデルが今は無い  → 開始不可（**別モデルへ勝手に替えない**）
D テキスト専用モデル      → 開始不可
E 画像を受け取れた        → 開始できる
F 確かめられなかった      → 開始不可（**「たぶん使える」にしない**）
"""

from __future__ import annotations

import unittest

from _fake_lm_studio import SLOW, TEXT_ONLY, FakeLmStudio
from _support import APP_ROOT, TempAppRootTestCase

from local_video_catalog import environment_check as ec
from local_video_catalog import readiness as rd

RECOMMENDED = ec.RECOMMENDED_VISUAL_MODEL


def raw_for(server: FakeLmStudio | None, model: str | None) -> dict:
    """LM Studio の接続先と、選んだモデルだけを差し替えた設定。"""
    from local_video_catalog import config as config_module

    raw = config_module.load_settings_dict()
    base_url = server.base_url if server else "http://127.0.0.1:9/v1"
    raw["vlm"] = {**dict(raw.get("vlm") or {}), "base_url": base_url,
                  "timeout_seconds": 10}
    ec.apply_model_choices(raw, visual_model=model)
    return raw


def local_ai_result(server: FakeLmStudio | None, model: str | None, *,
                    probe_vision: bool = True) -> ec.CheckResult:
    result = ec.CheckResult()
    ec.check_local_ai(result, raw_for(server, model), probe_vision=probe_vision)
    return result


def readiness_for(result: ec.CheckResult, *, skip_transcription: bool = False,
                  whisper: str = rd.AVAILABLE) -> rd.RunReadiness:
    """土台と文字起こしは揃っている前提で、ローカルAI側だけを見る。"""
    availability = result.availabilities()
    availability.update(ffmpeg=rd.AVAILABLE, ffprobe=rd.AVAILABLE,
                        whisper_feature=whisper, whisper_model=whisper)
    return rd.evaluate_run_readiness(**availability,
                                     skip_transcription=skip_transcription)


class ConnectionTests(TempAppRootTestCase):
    """A. 接続できない。"""

    def test_no_connection_blocks(self) -> None:
        result = local_ai_result(None, RECOMMENDED)
        self.assertEqual(result.availability(ec.LOCAL_AI), rd.UNAVAILABLE)
        readiness = readiness_for(result)
        self.assertFalse(readiness.can_start)
        self.assertIn("LM Studio", "\n".join(readiness.detail_lines()))

    def test_no_connection_does_not_probe(self) -> None:
        """繋がらないのに画像を送りに行かない。"""
        result = local_ai_result(None, RECOMMENDED)
        self.assertIsNone(result.find(ec.VISION))


class ModelSelectionTests(TempAppRootTestCase):
    """B / C / P. モデルが選ばれているか、今も使えるか。"""

    def test_connected_but_nothing_selected_blocks(self) -> None:
        with FakeLmStudio([RECOMMENDED]) as server:
            result = local_ai_result(server, "")
        self.assertEqual(result.availability(ec.LOCAL_AI), rd.AVAILABLE)
        self.assertEqual(result.visual_model_availability(), rd.NOT_SELECTED)

        readiness = readiness_for(result)
        self.assertFalse(readiness.can_start)
        text = "\n".join(readiness.detail_lines())
        self.assertIn("選択されていません", text)
        self.assertIn("選択してください", text)

    def test_nothing_selected_does_not_probe(self) -> None:
        """選ぶ前に画像を送りに行かない。"""
        with FakeLmStudio([RECOMMENDED]) as server:
            local_ai_result(server, "")
            self.assertEqual(server.requests, [])

    def test_a_selected_model_that_is_gone_blocks(self) -> None:
        with FakeLmStudio(["something-else"]) as server:
            result = local_ai_result(server, RECOMMENDED)
        self.assertEqual(result.visual_model_availability(), rd.UNAVAILABLE)

        readiness = readiness_for(result)
        self.assertFalse(readiness.can_start)
        text = "\n".join(readiness.detail_lines())
        self.assertIn("前回使用したモデルを利用できません", text)

    def test_a_missing_model_is_never_swapped_for_another(self) -> None:
        """P. **勝手に別モデルへ切り替えない。**

        似た名前が 1 つだけあっても選ばない。利用者が選んだつもりの
        ないモデルで解析が進むと、結果の出所が分からなくなる。
        """
        with FakeLmStudio([f"{RECOMMENDED}-mlx-4bit"]) as server:
            result = local_ai_result(server, RECOMMENDED)
            self.assertEqual(server.requests, [])
        item = result.find(ec.VISUAL_MODEL)
        self.assertEqual(item.level, ec.LEVEL_NG)
        self.assertIn(RECOMMENDED, item.detail)
        self.assertNotIn("mlx", str(result.find(ec.VISUAL_MODEL).detail
                                    ).replace(RECOMMENDED, ""))


class VisionCapabilityTests(TempAppRootTestCase):
    """D / E / F / I. 画像を受け取れるか。"""

    def test_a_text_only_model_blocks(self) -> None:
        with FakeLmStudio(["plain-llm"], behaviour=TEXT_ONLY) as server:
            result = local_ai_result(server, "plain-llm")
        self.assertEqual(result.availability(ec.VISION), rd.UNAVAILABLE)

        readiness = readiness_for(result)
        self.assertFalse(readiness.can_start)
        text = "\n".join(readiness.detail_lines())
        self.assertIn("画像入力", text)
        self.assertIn("別のモデル", text)

    def test_a_vision_model_can_start(self) -> None:
        with FakeLmStudio(["some-vision-model"]) as server:
            result = local_ai_result(server, "some-vision-model")
        self.assertEqual(result.availability(ec.VISION), rd.AVAILABLE)
        self.assertTrue(readiness_for(result).can_start)

    def test_timeout_blocks(self) -> None:
        with FakeLmStudio(["slow"], behaviour=SLOW, delay_seconds=3) as server:
            raw = raw_for(server, "slow")
            # 待ち時間は設定で変えられる（試験を待たせないため 1 秒）
            raw["vlm"] = {**raw["vlm"], "vision_probe_timeout_seconds": 1}
            result = ec.CheckResult()
            ec.check_local_ai(result, raw)

        # **「確かめられなかった」は「非対応」ではない。** 対処が違う。
        self.assertEqual(result.availability(ec.VISION), rd.UNKNOWN)
        readiness = readiness_for(result)
        self.assertFalse(readiness.can_start, "確認できないまま始めてはいけません。")
        text = "\n".join(readiness.detail_lines())
        self.assertIn("確認できませんでした", text)
        self.assertNotIn("このモデルでは画像入力を利用できません", text)

    def test_a_rejected_model_and_an_unconfirmed_one_read_differently(self
                                                                     ) -> None:
        """C と D を混ぜない。片方は「別のモデルへ」、片方は「やり直す」。"""
        with FakeLmStudio(["plain-llm"], behaviour=TEXT_ONLY) as server:
            rejected = local_ai_result(server, "plain-llm")
        with FakeLmStudio(["slow"], behaviour=SLOW, delay_seconds=3) as server:
            raw = raw_for(server, "slow")
            raw["vlm"] = {**raw["vlm"], "vision_probe_timeout_seconds": 1}
            unconfirmed = ec.CheckResult()
            ec.check_local_ai(unconfirmed, raw)

        self.assertNotEqual(rejected.availability(ec.VISION),
                            unconfirmed.availability(ec.VISION))
        self.assertIn("別のモデル",
                      "\n".join(readiness_for(rejected).detail_lines()))
        self.assertIn("やり直して",
                      "\n".join(readiness_for(unconfirmed).detail_lines()))

    def test_the_recommended_model_is_probed_too(self) -> None:
        """I. **名前が qwen3-vl でも probe を省かない。**"""
        with FakeLmStudio([RECOMMENDED]) as server:
            result = local_ai_result(server, RECOMMENDED)
            self.assertEqual(len(server.requests), 1,
                             "推奨モデルでも実際に画像を送って確かめること。")
        self.assertEqual(result.availability(ec.VISION), rd.AVAILABLE)
        self.assertTrue(readiness_for(result).can_start)

    def test_the_recommended_model_would_be_rejected_if_it_failed(self) -> None:
        """名前を理由に通さない。同じ名前でも拒めば開始不可。"""
        with FakeLmStudio([RECOMMENDED], behaviour=TEXT_ONLY) as server:
            result = local_ai_result(server, RECOMMENDED)
        self.assertFalse(readiness_for(result).can_start)

    def test_unprobed_is_not_treated_as_usable(self) -> None:
        """H. 確認していない状態では開始できない。"""
        with FakeLmStudio([RECOMMENDED]) as server:
            result = local_ai_result(server, RECOMMENDED, probe_vision=False)
        self.assertEqual(result.availability(ec.VISION), rd.UNKNOWN)
        self.assertFalse(readiness_for(result).can_start)

    def test_probing_state_blocks_starting(self) -> None:
        readiness = rd.evaluate_run_readiness(
            ffmpeg=rd.AVAILABLE, ffprobe=rd.AVAILABLE,
            whisper_feature=rd.AVAILABLE, whisper_model=rd.AVAILABLE,
            local_ai=rd.AVAILABLE, visual_model=rd.AVAILABLE,
            vision=rd.CHECKING)
        self.assertFalse(readiness.can_start)
        self.assertIn("確認しています", "\n".join(readiness.detail_lines()))


class TranscriptionInteractionTests(TempAppRootTestCase):
    """J / K. 文字起こしと映像解析は独立していること。"""

    def test_missing_whisper_blocks_when_not_skipped(self) -> None:
        with FakeLmStudio([RECOMMENDED]) as server:
            result = local_ai_result(server, RECOMMENDED)
        readiness = readiness_for(result, whisper=rd.UNAVAILABLE)
        self.assertFalse(readiness.can_start)

    def test_missing_whisper_is_fine_when_skipped(self) -> None:
        with FakeLmStudio([RECOMMENDED]) as server:
            result = local_ai_result(server, RECOMMENDED)
        readiness = readiness_for(result, whisper=rd.UNAVAILABLE,
                                  skip_transcription=True)
        self.assertTrue(readiness.can_start)
        self.assertIn("映像の解析", readiness.performed)

    def test_skipping_transcription_never_unblocks_the_vision_problem(self
                                                                      ) -> None:
        with FakeLmStudio(["plain-llm"], behaviour=TEXT_ONLY) as server:
            result = local_ai_result(server, "plain-llm")
        self.assertFalse(readiness_for(result, skip_transcription=True
                                       ).can_start)


class SameAnswerEverywhereTests(TempAppRootTestCase):
    """L / O. 判定とモデル指定が、どの入口でも一致すること。"""

    def test_one_result_gives_one_readiness(self) -> None:
        """起動時も手動も、同じ ``CheckResult`` から同じ答えになる。"""
        with FakeLmStudio([RECOMMENDED]) as server:
            result = local_ai_result(server, RECOMMENDED)
        first = readiness_for(result)
        second = readiness_for(result)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_the_gui_uses_one_argument_builder(self) -> None:
        """起動時チェックと手動チェックが同じ引数を組み立てること。"""
        source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                  / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("environment_arguments()"), 1)
        block = source.split("def _check_environment", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("_start_environment_check", block)

    def test_the_selected_model_reaches_the_pipeline(self) -> None:
        """O. 画面に出ている model id が、そのまま解析へ渡ること。"""
        from local_video_catalog.gui import state as state_module

        state = state_module.GuiState(source_folder="X:/v",
                                      visual_model=RECOMMENDED)
        args = state.pipeline_arguments()
        self.assertIn("--visual-model", args)
        self.assertEqual(args[args.index("--visual-model") + 1], RECOMMENDED)

    def test_the_same_model_reaches_the_environment_check(self) -> None:
        from local_video_catalog.gui import state as state_module

        state = state_module.GuiState(source_folder="X:/v",
                                      visual_model=RECOMMENDED)
        checked = state.environment_arguments()
        analysed = state.pipeline_arguments()
        self.assertEqual(checked[checked.index("--visual-model") + 1],
                         analysed[analysed.index("--visual-model") + 1])

    def test_an_unselected_model_is_passed_as_empty(self) -> None:
        """**「未指定」と「未選択」を混ぜない。** 空文字で明示する。"""
        from local_video_catalog.gui import state as state_module

        args = state_module.GuiState().environment_arguments()
        self.assertEqual(args[args.index("--visual-model") + 1], "")

    def test_the_pipeline_applies_the_choice_through_one_function(self) -> None:
        source = (APP_ROOT / "src" / "local_video_catalog"
                  / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("apply_model_choices", source)


class ModelChangeTests(unittest.TestCase):
    """G. モデルを変えたら、前の判定を使い回さないこと。"""

    def setUp(self) -> None:
        self.source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                       / "app.py").read_text(encoding="utf-8")

    def test_changing_the_model_starts_a_new_check(self) -> None:
        block = self.source.split("def _open_ai_settings", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("_start_environment_check", block)
        self.assertIn("visual_model", block)

    def test_the_previous_result_is_discarded(self) -> None:
        """**古い可用性を残さない。** 残すと旧モデルの結果で開始できる。"""
        block = self.source.split("def _start_environment_check", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("self.availability = None", block)

    def test_the_user_is_told_what_is_being_checked(self) -> None:
        block = self.source.split("def _open_ai_settings", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("画像処理能力を確認しています", block)


class NonAsciiRootTests(TempAppRootTestCase):
    """Q. 日本語を含む場所でも同じように動くこと。"""

    app_root_name = "日本語の置き場"

    def test_the_check_works_under_a_japanese_path(self) -> None:
        from local_video_catalog import paths

        self.assertIn("日本語", str(paths.app_root()),
                      "この試験は日本語を含む APP_ROOT で動かすこと。")
        with FakeLmStudio([RECOMMENDED]) as server:
            result = local_ai_result(server, RECOMMENDED)
        self.assertTrue(readiness_for(result).can_start)

    def test_a_full_check_works_under_a_japanese_path(self) -> None:
        """環境チェック一式（画像 probe を含む）が通ること。"""
        from local_video_catalog import config as config_module

        raw = raw_for_japanese_root(RECOMMENDED)
        with FakeLmStudio([RECOMMENDED]) as server:
            raw["vlm"] = {**raw["vlm"], "base_url": server.base_url}
            settings = config_module.build_settings(raw, require_ffprobe=False)
            result = ec.check_environment(raw=raw, settings=settings)
        self.assertEqual(result.availability(ec.VISION), rd.AVAILABLE)


def raw_for_japanese_root(model: str) -> dict:
    from local_video_catalog import config as config_module

    raw = config_module.load_settings_dict()
    raw["vlm"] = {**dict(raw.get("vlm") or {}), "timeout_seconds": 10}
    ec.apply_model_choices(raw, visual_model=model)
    return raw


if __name__ == "__main__":
    unittest.main()
