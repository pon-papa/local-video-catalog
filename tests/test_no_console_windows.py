"""内部 CLI がコンソール窓を出さないこと.

**実運用で起きた障害。** 画面は ``pythonw`` で動くのでコンソールを
持たない。その状態でコンソール用のプログラム（ffmpeg / ffprobe）を
素で起動すると、Windows がそのプロセスのために新しいコンソール窓を作る。

329 本の動画を登録した実行では、10 分間ずっと窓が明滅して他の操作が
できなくなった。1 本につき ffprobe を 1 回起動していたためである。

**窓を隠すことと、エラーを隠すことは別。** 出力・終了コード・timeout が
これまでどおり取れることも、ここで固定する。
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from _support import (
    APP_ROOT,
    TempAppRootTestCase,
    find_ffmpeg,
    find_ffprobe,
    make_synthetic_video,
    quiet_logger,
    requires_ffmpeg,
    requires_ffprobe,
)

from local_video_catalog import process_utils

requires_windows = unittest.skipUnless(sys.platform == "win32",
                                       "Windows 専用のためスキップ")


def count_console_windows() -> int:
    """いま開いているコンソール窓の数。

    クラス名で数える。プロセス名で探すと Store 版 Python のように
    名前が変わる環境で当てにならない。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    total = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        nonlocal total
        if not user32.IsWindowVisible(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buffer, 64)
        if buffer.value in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
            total += 1
        return True

    user32.EnumWindows(visit, 0)
    return total


class HelperContractTests(unittest.TestCase):
    """共通ヘルパーの約束事。"""

    @requires_windows
    def test_no_window_flag_is_set_on_windows(self) -> None:
        self.assertEqual(process_utils.no_window_flags(),
                         subprocess.CREATE_NO_WINDOW)

    @unittest.skipIf(sys.platform == "win32", "非 Windows の挙動")
    def test_no_flags_elsewhere(self) -> None:
        self.assertEqual(process_utils.no_window_flags(), 0)
        self.assertIsNone(process_utils.hidden_startupinfo())

    @requires_windows
    def test_startupinfo_hides_the_window(self) -> None:
        info = process_utils.hidden_startupinfo()
        self.assertTrue(info.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(info.wShowWindow, subprocess.SW_HIDE)

    def test_caller_flags_are_kept(self) -> None:
        merged = process_utils._hidden_options({"creationflags": 0x00000200})
        self.assertTrue(merged["creationflags"] & 0x00000200)
        if sys.platform == "win32":
            self.assertTrue(
                merged["creationflags"] & subprocess.CREATE_NO_WINDOW)


class OutputIsStillVisibleTests(unittest.TestCase):
    """**窓を消してもエラーは消さない。**"""

    # 子側にも ``-X utf8`` を渡す。渡さないと子は日本語版 Windows の
    # 既定（CP932）で書き出すので、UTF-8 として読むと復号に失敗する。
    # **process_utils の問題ではなく、試験の書き方の問題。**

    def test_stdout_is_captured(self) -> None:
        done = process_utils.run(
            [sys.executable, "-X", "utf8", "-c", "print('こんにちは')"],
            text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0)
        self.assertIn("こんにちは", done.stdout)

    def test_stderr_is_captured(self) -> None:
        done = process_utils.run(
            [sys.executable, "-X", "utf8", "-c",
             "import sys; print('失敗の内容', file=sys.stderr)"],
            text=True, encoding="utf-8")
        self.assertIn("失敗の内容", done.stderr)

    def test_exit_code_is_reported(self) -> None:
        done = process_utils.run([sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(done.returncode, 7)

    def test_failure_does_not_raise_by_default(self) -> None:
        """1 本の失敗で全体を止めないため、例外にしない。"""
        done = process_utils.run([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(done.returncode, 3)

    def test_timeout_still_works(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            process_utils.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=2)

    def test_non_ascii_arguments_survive(self) -> None:
        done = process_utils.run(
            [sys.executable, "-X", "utf8", "-c",
             "import sys; print(sys.argv[1])", "日本語の引数"],
            text=True, encoding="utf-8")
        self.assertIn("日本語の引数", done.stdout)

    def test_cwd_is_honoured(self) -> None:
        done = process_utils.run(
            [sys.executable, "-X", "utf8", "-c", "import os; print(os.getcwd())"],
            cwd=APP_ROOT, text=True, encoding="utf-8")
        self.assertIn(str(APP_ROOT), done.stdout)

    def test_popen_pipes_can_be_closed(self) -> None:
        """**ResourceWarning を再発させない。**"""
        process = process_utils.popen(
            [sys.executable, "-c", "print('ok')"],
            stdout=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            output = process.stdout.read()
        finally:
            process.stdout.close()
            process.wait(timeout=30)
        self.assertIn("ok", output)


class EveryCallSiteUsesTheHelperTests(unittest.TestCase):
    """**呼び出し側ごとに書くと必ず忘れる。** 直書きを許さない。"""

    ANALYSIS_MODULES = (
        "probe", "frame_extractor", "asr_engine", "config",
    )
    GUI_MODULES = ("gui/runner", "gui/app")

    def _direct_launches(self, relative: str) -> list[str]:
        source = (APP_ROOT / "src" / "local_video_catalog"
                  / f"{relative}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            owner = getattr(func.value, "id", "")
            if owner == "subprocess" and func.attr in (
                    "run", "Popen", "call", "check_call", "check_output"):
                found.append(f"subprocess.{func.attr}")
            if owner == "os" and func.attr == "system":
                found.append("os.system")
        return found

    def test_analysis_modules_do_not_launch_directly(self) -> None:
        for name in self.ANALYSIS_MODULES:
            with self.subTest(module=name):
                self.assertEqual(
                    self._direct_launches(name), [],
                    f"{name}.py が subprocess を直接呼んでいます。"
                    "process_utils を通してください"
                    "（通さないとコンソール窓が出ます）。")

    def test_gui_modules_do_not_launch_directly(self) -> None:
        for name in self.GUI_MODULES:
            with self.subTest(module=name):
                self.assertEqual(self._direct_launches(name), [])

    def test_no_shell_execution_anywhere(self) -> None:
        for path in (APP_ROOT / "src").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("shell=True", source)
                self.assertNotIn("os.system", source)

    def test_no_powershell_dependency(self) -> None:
        """一般配布版は PowerShell を必須にしない。"""
        for path in (APP_ROOT / "src").rglob("*.py"):
            source = path.read_text(encoding="utf-8").lower()
            with self.subTest(path=path.name):
                self.assertNotIn("powershell", source)
                self.assertNotIn("pwsh.exe", source)


@requires_windows
class RealWindowTests(TempAppRootTestCase):
    """**窓を持たない親の下で**、実際に窓が増えないことを数える。

    ここが要。テスト自身が ``python.exe``（コンソールあり）で動いていると、
    子はその窓へ相乗りするので新しい窓は作られず、**何を測っても 0** に
    なる。それでは修正前のコードでも通ってしまう。

    そこで測定は ``pythonw`` の子プロセスへ任せる。
    ``test_the_measurement_can_detect_windows`` が、その測り方で本当に
    窓を検出できることを先に確かめる。
    """

    PROBE = Path(__file__).resolve().parent / "_window_probe.py"

    def setUp(self) -> None:
        super().setUp()
        from local_video_catalog import paths

        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.launcher = self._windowless_python()
        if self.launcher is None:
            self.skipTest("窓なしで起動できる Python が見つかりません")

    def _windowless_python(self) -> list[str] | None:
        for candidate, prefix in (("pyw.exe", ["-3"]), ("pythonw.exe", [])):
            found = shutil.which(candidate)
            if found:
                return [found, *prefix]
        return None

    def _measure(self, mode: str, *arguments: str) -> int:
        """窓なしの親で ``mode`` を実行し、増えた窓の最大数を返す。"""
        output = self.temp_dir / f"probe_{mode}.json"
        command = [
            *self.launcher, "-X", "utf8", str(self.PROBE), mode,
            str(output), str(APP_ROOT / "src"), *arguments,
        ]
        finished = subprocess.run(command, capture_output=True, timeout=300)
        self.assertTrue(
            output.is_file(),
            f"測定できませんでした: {finished.stderr.decode('utf-8', 'replace')}")

        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertFalse(payload["parent_has_console"],
                         "測定する親がコンソールを持っています。"
                         "この条件では窓が出ないので測定になりません。")
        return int(payload["peak"])

    def _synthetic_video(self) -> Path:
        video = self.source_root / "clip.mp4"
        if not make_synthetic_video(find_ffmpeg(), video, duration=2.0):
            self.skipTest("合成動画を作れません")
        return video

    # -- 測り方そのものの確認 -------------------------------------------

    @requires_ffprobe
    def test_the_measurement_can_detect_windows(self) -> None:
        """**修正前の書き方なら窓が出る**ことを確かめる。

        これが 0 だと、以降のテストは何も保証しない。
        """
        video = self.source_root / "clip.mp4"
        video.write_bytes(b"x" * 2000)
        peak = self._measure("control", str(find_ffprobe()), str(video))
        self.assertGreater(
            peak, 0,
            "修正前の書き方でも窓が検出されませんでした。"
            "この測り方では窓の有無を判定できません。")

    # -- 本題 -------------------------------------------------------------

    @requires_ffprobe
    def test_ffprobe_opens_no_window(self) -> None:
        """**今回の障害そのもの。** 動画 1 本ごとに窓が出ていた。"""
        video = self.source_root / "clip.mp4"
        video.write_bytes(b"x" * 2000)
        self.assertEqual(
            self._measure("ffprobe", str(find_ffprobe()), str(video)), 0,
            "ffprobe がコンソール窓を出しています。")

    @requires_ffmpeg
    def test_ffmpeg_frame_extraction_opens_no_window(self) -> None:
        from local_video_catalog import paths

        video = self._synthetic_video()
        self.assertEqual(
            self._measure("ffmpeg", str(find_ffmpeg()), str(video),
                          str(paths.temp_dir())), 0,
            "ffmpeg がコンソール窓を出しています。")

    @requires_ffmpeg
    def test_whisper_check_opens_no_window(self) -> None:
        self.assertEqual(
            self._measure("whisper_check", str(find_ffmpeg())), 0,
            "whisper 機能の確認が窓を出しています。")

    def test_pipeline_child_opens_no_window(self) -> None:
        self.assertEqual(self._measure("pipeline_child"), 0,
                         "解析本体の起動が窓を出しています。")

    @requires_ffprobe
    def test_registering_many_videos_opens_no_window(self) -> None:
        """**実運用と同じ形。** 何本もの登録で窓が明滅しないこと。"""
        for index in range(8):
            (self.source_root / f"clip{index}.mp4").write_bytes(
                b"x" * (2000 + index))
        self.assertEqual(
            self._measure("register", str(self.app_root),
                          str(self.source_root), str(find_ffprobe())), 0,
            "登録工程がコンソール窓を出しています。")

    @requires_ffprobe
    def test_japanese_paths_open_no_window(self) -> None:
        folder = self.temp_dir / "日本語の動画フォルダー"
        folder.mkdir()
        video = folder / "動画_2014.mp4"
        video.write_bytes(b"x" * 2000)
        self.assertEqual(
            self._measure("ffprobe", str(find_ffprobe()), str(video)), 0,
            "日本語パスでコンソール窓が出ています。")
