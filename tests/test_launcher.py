"""起動経路（Start.cmd → launch.py → GUI）.

**この経路だけが試されないまま残っていた。** 実際に起きた障害:

    Start.cmd が ``python src\\local_video_catalog\\gui\\app.py`` を実行して
    いた。モジュールのファイルを直接実行すると package の一部ではなく
    __main__ になるため、``from .. import config`` が
    ``ImportError: attempted relative import with no known parent package``
    で即死し、コンソールが一瞬出て消えるだけだった。

開発中は ``-m`` か import 経由でしか起動しないので気づけない。
ここでは**配布物と同じ呼び方**で確かめる。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from _support import APP_ROOT, TempDirTestCase

LAUNCH = APP_ROOT / "launch.py"
START_CMD = APP_ROOT / "Start.cmd"

requires_windows = unittest.skipUnless(sys.platform == "win32",
                                       "Windows 専用のためスキップ")


def _child_env(app_root: Path) -> dict[str, str]:
    """配布物の起動と同じ環境。**PYTHONPATH を渡さない。**

    渡してしまうと、開発環境でだけ通る状態を再現できない。
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "LOCAL_VIDEO_CATALOG_ROOT")}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_launch(app_root: Path, *, probe: str, timeout: int = 120
               ) -> subprocess.CompletedProcess:
    """launch.py の準備だけを行い、画面は開かずに ``probe`` を実行する。

    ``launch.main()`` を呼ぶと画面が開いて閉じないため、import path の
    組み立てまでを再現して確かめる。
    """
    code = (
        "import runpy, sys\n"
        f"sys.argv = [r'{app_root / 'launch.py'}']\n"
        "spec = runpy.run_path("
        f"r'{app_root / 'launch.py'}', run_name='not_main')\n"
        "sys.path.insert(0, str(spec['SOURCE_ROOT']))\n"
        + probe
    )
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_child_env(app_root), cwd=str(app_root), timeout=timeout)


class LauncherContractTests(unittest.TestCase):
    """Start.cmd が何を起動するか。"""

    def setUp(self) -> None:
        if not START_CMD.is_file():
            self.skipTest("Start.cmd がありません")
        self.text = START_CMD.read_bytes().decode("cp932", errors="replace")
        # rem 行は「なぜこう直したか」の説明で、実行されるものではない。
        # 説明文に出てくる語を検出対象にすると意味のない失敗になる。
        self.commands = "\n".join(
            line for line in self.text.splitlines()
            if not line.strip().lower().startswith("rem"))

    def test_start_goes_through_launch_py(self) -> None:
        """**モジュールのファイルを直接実行しないこと。**"""
        self.assertIn("launch.py", self.commands)
        self.assertNotIn("gui\\app.py", self.commands)
        self.assertNotIn("app.py", self.commands.replace("launch.py", ""))

    def test_bundled_runtime_is_preferred(self) -> None:
        runtime = self.commands.index("runtime\\pythonw.exe")
        launcher = self.commands.index("pyw.exe")
        self.assertLess(runtime, launcher,
                        "同梱 runtime が先に見られていません。")

    def test_bundled_runtime_gets_tcl_paths(self) -> None:
        """embeddable の Tcl/Tk は環境変数で指す必要がある。"""
        self.assertIn("TCL_LIBRARY", self.commands)
        self.assertIn("TK_LIBRARY", self.commands)

    def test_console_free_interpreter_is_preferred(self) -> None:
        """起動に成功したらコンソールを残さない。"""
        self.assertIn("pythonw.exe", self.commands)

    def test_missing_python_is_not_silent(self) -> None:
        self.assertIn("Python 3.13", self.commands)
        self.assertIn("pause", self.commands)

    def test_broken_layout_is_not_silent(self) -> None:
        self.assertIn("app-root.marker", self.commands)
        self.assertIn("構成が壊れています", self.commands)

    def test_encoded_as_cp932(self) -> None:
        """cmd.exe は OEM コードページで読む。UTF-8 だと文字化けする。"""
        raw = START_CMD.read_bytes()
        raw.decode("cp932")
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("ascii")


class LaunchModuleTests(unittest.TestCase):
    """launch.py の中身。"""

    def setUp(self) -> None:
        self.text = LAUNCH.read_text(encoding="utf-8")

    def test_lives_at_the_app_root(self) -> None:
        self.assertTrue(LAUNCH.is_file())
        self.assertEqual(LAUNCH.parent, APP_ROOT)

    def test_failures_are_shown_and_logged(self) -> None:
        for expected in ("MessageBoxW", "startup-error", "userdata"):
            with self.subTest(name=expected):
                self.assertIn(expected, self.text)

    def test_error_path_does_not_depend_on_tkinter(self) -> None:
        """tkinter が壊れていることが原因の場合にも出せること。"""
        self.assertIn("MessageBoxW", self.text)
        # 画面部品を import していないこと（それ自体が壊れている場合に備える）
        show = self.text.split("def show_error", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("import tkinter", show)
        self.assertIn("ctypes", show)

    def test_logs_stay_inside_the_app_root(self) -> None:
        directory = self.text.split("def _log_directory", 1)[1].split("def ", 1)[0]
        self.assertIn("APP_ROOT", directory)
        for forbidden in ("LOCALAPPDATA", "APPDATA", "gettempdir"):
            self.assertNotIn(forbidden, directory)


class DistributionStartupTests(TempDirTestCase):
    """**配布物と同じ形**に展開して、同じ呼び方で起動できること。"""

    def _make_distribution(self, folder_name: str = "local-video-catalog") -> Path:
        """release と同じ構成を temp へ作る。"""
        sys.path.insert(0, str(APP_ROOT / "tools"))
        import make_release

        destination = self.temp_dir / folder_name
        for relative in make_release.collect(APP_ROOT):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(APP_ROOT / relative, target)
        (destination / "userdata").mkdir(exist_ok=True)
        return destination

    def test_package_imports_without_pythonpath(self) -> None:
        """**PYTHONPATH 無しで動くこと。** これが今回の障害の本体。"""
        app = self._make_distribution()
        result = run_launch(app, probe=(
            "import local_video_catalog.gui.app as a\n"
            "print('IMPORT OK', a.WINDOW_TITLE)\n"))
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertIn("IMPORT OK", result.stdout)

    def test_relative_imports_resolve(self) -> None:
        """モジュールを直接実行したときに壊れた箇所を明示的に確認する。"""
        app = self._make_distribution()
        result = run_launch(app, probe=(
            "from local_video_catalog.gui import app, runner, state\n"
            "from local_video_catalog import pipeline, paths\n"
            "print('RELATIVE IMPORTS OK')\n"))
        self.assertIn("RELATIVE IMPORTS OK", result.stdout,
                      result.stdout + result.stderr)

    def test_running_the_module_file_directly_still_fails(self) -> None:
        """**直接実行は今も壊れる。** だからこそ launch.py を通す。

        この前提が変わったら（package 化の方法を変えたら）気づけるように
        しておく。
        """
        app = self._make_distribution()
        result = subprocess.run(
            [sys.executable, "-X", "utf8",
             str(app / "src" / "local_video_catalog" / "gui" / "app.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_child_env(app), cwd=str(app), timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("relative import", result.stderr.lower())

    def test_app_root_resolves_to_the_distribution(self) -> None:
        app = self._make_distribution()
        result = run_launch(app, probe=(
            "from local_video_catalog import paths\n"
            "print('APP_ROOT', paths.app_root())\n"))
        self.assertIn(str(app), result.stdout, result.stderr)

    def test_japanese_path_works(self) -> None:
        """日本語のフォルダー名でも起動準備が通ること。"""
        app = self._make_distribution("動画カタログ 配布版")
        result = run_launch(app, probe=(
            "from local_video_catalog import paths\n"
            "import local_video_catalog.gui.app\n"
            "print('APP_ROOT', paths.app_root())\n"))
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertIn("動画カタログ 配布版", result.stdout)

    def test_missing_source_is_reported(self) -> None:
        """本体が欠けていたら、黙って終わらず理由を残すこと。"""
        app = self._make_distribution()
        shutil.rmtree(app / "src")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c",
             "import runpy, sys;"
             f"m = runpy.run_path(r'{app / 'launch.py'}', run_name='x');"
             "sys.exit(m['main']())"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**_child_env(app),
                 "LOCAL_VIDEO_CATALOG_NO_DIALOG": "1"},
            cwd=str(app), timeout=60)
        self.assertNotEqual(result.returncode, 0)
        logs = list((app / "userdata" / "logs").glob("startup-error_*.log"))
        self.assertTrue(logs, "起動エラーのログが残っていません。")
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("プログラム本体が見つかりません", text)

    def test_startup_error_log_lands_in_userdata(self) -> None:
        app = self._make_distribution()
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c",
             "import runpy;"
             f"m = runpy.run_path(r'{app / 'launch.py'}', run_name='x');"
             "print(m['write_startup_error']('テスト用の内容'))"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_child_env(app), cwd=str(app), timeout=60)
        self.assertIn("userdata", result.stdout, result.stderr)
        self.assertIn("logs", result.stdout)


def _gui_windows() -> list[int]:
    """開いている画面の PID。

    **プロセス名で探さない。** Microsoft Store 版の Python は
    ``pythonw3.13`` のような名前になり、``pythonw`` を探すと
    「起動していない」と誤判定する（実際に一度これで誤診した）。
    """
    import ctypes
    from ctypes import wintypes

    from local_video_catalog.gui.app import WINDOW_TITLE

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if WINDOW_TITLE in buffer.value:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append(int(pid.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


@requires_windows
class RealStartupTests(TempDirTestCase):
    """**本物の Start.cmd を実行して**、画面が出ることを確かめる。

    ここが今回の障害で抜けていた検証。Start.cmd は ``start`` で子を
    切り離すため終了を待てないので、画面が現れたかで判定する。
    """

    def _distribute(self, folder_name: str) -> Path:
        sys.path.insert(0, str(APP_ROOT / "tools"))
        import make_release

        app = self.temp_dir / folder_name
        for relative in make_release.collect(APP_ROOT):
            target = app / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(APP_ROOT / relative, target)
        (app / "userdata").mkdir(exist_ok=True)
        return app

    def _launch_and_wait(self, app: Path, seconds: int = 25) -> list[int]:
        import time

        known = set(_gui_windows())
        # Start.cmd は start で子を切り離してすぐ終わる。
        # ここで待たないと cmd.exe が残り、ResourceWarning になる。
        # **出力は捕捉しない。** 捕捉すると切り離した子がパイプを継承し、
        # 画面を閉じるまで戻らなくなる（調査中に実際に踏んだ）。
        launcher = subprocess.Popen(
            ["cmd.exe", "/c", str(app / "Start.cmd")],
            cwd=str(app), env=_child_env(app),
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        launcher.wait(timeout=seconds)

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            opened = [pid for pid in _gui_windows() if pid not in known]
            if opened:
                return opened
            time.sleep(0.5)
        return []

    def _close(self, pids: list[int]) -> None:
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, check=False)

    def test_start_cmd_opens_the_window(self) -> None:
        app = self._distribute("local-video-catalog")
        opened = self._launch_and_wait(app)
        self.addCleanup(self._close, opened)

        errors = list((app / "userdata" / "logs").glob("startup-error_*.log"))
        detail = errors[0].read_text(encoding="utf-8") if errors else ""
        self.assertTrue(opened,
                        f"Start.cmd から画面が開きませんでした。\n{detail}")
        self.assertEqual(errors, [], f"起動エラーが記録されています:\n{detail}")

    def test_start_cmd_works_from_a_japanese_path(self) -> None:
        app = self._distribute("動画カタログ 配布版")
        opened = self._launch_and_wait(app)
        self.addCleanup(self._close, opened)

        errors = list((app / "userdata" / "logs").glob("startup-error_*.log"))
        detail = errors[0].read_text(encoding="utf-8") if errors else ""
        self.assertTrue(opened,
                        f"日本語パスで画面が開きませんでした。\n{detail}")

    def test_userdata_is_created_by_the_launch(self) -> None:
        app = self._distribute("local-video-catalog")
        opened = self._launch_and_wait(app)
        self.addCleanup(self._close, opened)
        self.assertTrue(opened)
        self.assertTrue((app / "userdata" / "config").is_dir())
        self.assertTrue((app / "userdata" / "logs").is_dir())


if __name__ == "__main__":
    unittest.main()
