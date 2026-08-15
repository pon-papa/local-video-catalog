"""画面の並びと、いま選ばれているモデルの表示.

実運用で分かったこと:

  - 「ローカルAI設定…」が最下部の「結果を見る / お手入れ」にあった。
    しかしモデルの選択は**始める前に決めること**で、結果を見る道具では
    ない。置き場所が役割と合っていなかった。
  - 押して開くまで、いま何のモデルで動くのか分からなかった。

**画面は実際に組み立てて確かめる。** 文字列だけを見る試験は、
過去に画面が壊れていても通ってしまった。
"""

from __future__ import annotations

import unittest

from _support import APP_ROOT, TempAppRootTestCase

from local_video_catalog import environment_check as ec

RECOMMENDED = ec.RECOMMENDED_VISUAL_MODEL


def build_window(**overrides):
    """画面を組み立てて返す。**環境チェックは走らせない。**

    表示できない環境（CI など）では skip する。
    """
    import tkinter as tk

    try:
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:                       # 表示不可
        raise unittest.SkipTest(f"画面を開けない環境です: {exc}") from None

    from local_video_catalog.gui import app as app_module
    from local_video_catalog.gui import state as state_module

    if overrides:
        state_module.save(state_module.GuiState(**overrides))
    return app_module.CatalogWindow(check_environment_on_start=False)


def widget_texts(widget) -> list[str]:
    """その枠の中にある文字を、入れ子ごと全部集める。"""
    found: list[str] = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except Exception:
            text = ""
        if text:
            found.append(str(text))
        found.extend(widget_texts(child))
    return found


def group_named(window, title: str):
    """``LabelFrame`` を見出しで探す。"""
    import tkinter as tk
    from tkinter import ttk

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, (ttk.LabelFrame, tk.LabelFrame)):
                try:
                    if str(child.cget("text")) == title:
                        return child
                except Exception:
                    pass
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(window.root)


class PlacementTests(TempAppRootTestCase):
    """A / B. ローカルAI設定は「今回の実行条件」にある。"""

    def setUp(self) -> None:
        super().setUp()
        self.window = build_window(visual_model=RECOMMENDED)
        self.addCleanup(self.window.root.destroy)

    def test_the_ai_settings_button_is_in_the_run_conditions(self) -> None:
        group = group_named(self.window, "今回の実行条件")
        self.assertIsNotNone(group, "「今回の実行条件」の枠が見つかりません。")
        texts = widget_texts(group)
        self.assertIn("ローカルAI", texts)
        self.assertTrue(any("設定" in text for text in texts),
                        f"設定ボタンが見つかりません: {texts}")

    def test_it_is_no_longer_in_the_results_group(self) -> None:
        group = group_named(self.window, "結果を見る / お手入れ")
        self.assertIsNotNone(group)
        for text in widget_texts(group):
            self.assertNotIn("ローカルAI", text)

    def test_the_results_group_keeps_its_own_buttons(self) -> None:
        """移動のついでに、結果を見る側を減らしていないこと。"""
        texts = widget_texts(group_named(self.window, "結果を見る / お手入れ"))
        for label in ("解析結果のまとめ", "HTMLカタログを更新",
                      "HTMLカタログを開く", "説明文を開く",
                      "元動画の場所を開く"):
            with self.subTest(label=label):
                self.assertIn(label, texts)


class ModelDisplayTests(TempAppRootTestCase):
    """C / D / E / G. 選ばれているモデルが見えること。"""

    def test_the_selected_model_is_shown(self) -> None:
        window = build_window(visual_model=RECOMMENDED)
        self.addCleanup(window.root.destroy)
        shown = window.visual_model_var.get()
        self.assertIn(RECOMMENDED, shown)

    def test_the_recommended_model_is_marked(self) -> None:
        window = build_window(visual_model=RECOMMENDED)
        self.addCleanup(window.root.destroy)
        self.assertIn("動作確認済み", window.visual_model_var.get())

    def test_another_model_is_shown_plainly(self) -> None:
        window = build_window(visual_model="some-other-model")
        self.addCleanup(window.root.destroy)
        self.assertEqual(window.visual_model_var.get(), "some-other-model")

    def test_nothing_selected_is_readable(self) -> None:
        """G. **未選択でも画面は壊れない。** 何をすればよいかを出す。"""
        window = build_window(visual_model="")
        self.addCleanup(window.root.destroy)
        shown = window.visual_model_var.get()
        self.assertIn("未選択", shown)
        self.assertIn("選んで", shown)

    def test_the_label_stays_short(self) -> None:
        """長すぎる表示で画面を崩さない。"""
        window = build_window(visual_model="a" * 200)
        self.addCleanup(window.root.destroy)
        self.assertLessEqual(len(window.visual_model_var.get()), 220)

    def test_changing_the_model_updates_the_label(self) -> None:
        """E. 選び直したら表示も変わる。"""
        window = build_window(visual_model="")
        self.addCleanup(window.root.destroy)
        self.assertIn("未選択", window.visual_model_var.get())

        window.state.visual_model = RECOMMENDED
        window._refresh_model_label()
        self.assertIn(RECOMMENDED, window.visual_model_var.get())


class OneSourceOfTruthTests(TempAppRootTestCase):
    """D. 表示と、解析へ渡す model id が同じ出どころであること。"""

    def test_the_label_comes_from_the_state(self) -> None:
        window = build_window(visual_model=RECOMMENDED)
        self.addCleanup(window.root.destroy)
        passed = window.state.pipeline_arguments()
        self.assertIn(window.state.visual_model,
                      window.visual_model_var.get())
        self.assertEqual(passed[passed.index("--visual-model") + 1],
                         window.state.visual_model)

    def test_the_gui_keeps_no_separate_model_variable(self) -> None:
        """**画面用に別管理しない。** 別に持つと表示と実際がずれる。"""
        source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                  / "app.py").read_text(encoding="utf-8")
        block = source.split("def _model_label", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("self.state.visual_model", block)
        # 画面側でモデルを決め直していない
        for invented in ("list_local_models", "select_model", "model_match"):
            with self.subTest(name=invented):
                self.assertNotIn(invented, block)


class ProbeWiringTests(unittest.TestCase):
    """F. 移動でモデル変更時の再確認を壊していないこと。"""

    def setUp(self) -> None:
        self.source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                       / "app.py").read_text(encoding="utf-8")
        self.block = self.source.split("def _open_ai_settings", 1)[1]
        self.block = self.block.split("\n    def ", 1)[0]

    def test_the_label_is_refreshed(self) -> None:
        self.assertIn("_refresh_model_label", self.block)

    def test_the_probe_still_runs_again(self) -> None:
        self.assertIn("_start_environment_check", self.block)
        self.assertIn("画像処理能力を確認しています", self.block)

    def test_the_old_result_is_still_discarded(self) -> None:
        block = self.source.split("def _start_environment_check", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("self.availability = None", block)

    def test_the_button_still_opens_the_same_dialog(self) -> None:
        self.assertEqual(self.source.count("def _open_ai_settings"), 1)
        self.assertEqual(self.source.count("command=self._open_ai_settings"), 1)


class JapaneseRootTests(TempAppRootTestCase):
    """H. 日本語を含む場所でも画面が組み上がること。"""

    app_root_name = "日本語の置き場"

    def test_the_window_builds(self) -> None:
        window = build_window(visual_model=RECOMMENDED)
        self.addCleanup(window.root.destroy)
        self.assertIsNotNone(group_named(window, "今回の実行条件"))
        self.assertIn(RECOMMENDED, window.visual_model_var.get())


if __name__ == "__main__":
    unittest.main()
