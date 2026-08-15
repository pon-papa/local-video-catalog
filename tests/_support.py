"""テスト共通のヘルパー.

方針:
  - **実際の動画を一切使わない。** ffmpeg の testsrc（カラーバー）と
    sine（正弦波）で作る合成データだけを使う。
  - ffmpeg が無くても走るテストと、必要なテストを分ける。
  - **すべて一時フォルダーで完結する。** 開発クローンの userdata へも、
    利用者の実データへも触れない。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# --- src をインポートパスへ ------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_APP_ROOT = _TESTS_DIR.parent
_SRC = _APP_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

APP_ROOT = _APP_ROOT
SRC_DIR = _SRC
TOOLS_DIR = _APP_ROOT / "tools"


def _find_tool(env_var: str, settings_key: str, command: str) -> Path | None:
    """テスト用の外部ツールを探す。

    1. 環境変数（**明示的な上書き**）
    2. ユーザー設定
    3. PATH

    環境変数が設定されている場合は、それを**最終決定**として扱う。
    指し示す先が存在しなければ ``None`` を返し、以降のフォールバックを
    行わない。これにより CI 側から「ツールが無い状態」を再現できる
    （ubuntu ランナーには ffmpeg が入っているため、PATH への
    フォールバックが残っていると無効化できない）。
    """
    env_path = os.environ.get(env_var)
    if env_path:
        candidate = Path(env_path)
        return candidate if candidate.is_file() else None

    try:
        from local_video_catalog import config as config_module

        settings = config_module.load_settings_dict()
        configured = settings.get(settings_key)
        if configured and Path(configured).is_file():
            return Path(configured)
    except Exception:
        pass

    found = shutil.which(command)
    return Path(found) if found else None


def find_ffmpeg() -> Path | None:
    return _find_tool("LOCAL_VIDEO_CATALOG_FFMPEG", "ffmpeg_path", "ffmpeg")


def find_ffprobe() -> Path | None:
    return _find_tool("LOCAL_VIDEO_CATALOG_FFPROBE", "ffprobe_path", "ffprobe")


HAS_FFMPEG = find_ffmpeg() is not None
HAS_FFPROBE = find_ffprobe() is not None

requires_ffmpeg = unittest.skipUnless(
    HAS_FFMPEG, "ffmpeg が見つからないためスキップ（CI では想定内）")
requires_ffprobe = unittest.skipUnless(
    HAS_FFPROBE, "ffprobe が見つからないためスキップ（CI では想定内）")
requires_windows = unittest.skipUnless(
    sys.platform == "win32", "Windows 専用のためスキップ")


class TempDirTestCase(unittest.TestCase):
    """一時フォルダーを用意し、終了時に必ず片付けるテストの土台。"""

    def setUp(self) -> None:
        super().setUp()
        # 子プロセスを起動するテストでは、こちらが片付けている最中に
        # 相手が __pycache__ を書くことがある。片付けの失敗でテストを
        # 落とさない（一時フォルダーは OS が後で回収する）。
        self._temp = tempfile.TemporaryDirectory(
            prefix="lvc_test_", ignore_cleanup_errors=True)
        self.temp_dir = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def make_file(self, relative: str, content: bytes = b"") -> Path:
        target = self.temp_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def make_dir(self, relative: str) -> Path:
        target = self.temp_dir / relative
        target.mkdir(parents=True, exist_ok=True)
        return target


class TempAppRootTestCase(TempDirTestCase):
    """一時フォルダーを APP_ROOT に見立てて動かすテストの土台.

    **開発クローンの userdata を汚さないため**、環境変数で APP_ROOT を
    差し替える。片付けは ``TempDirTestCase`` が行う。

    ``app_root_name`` を変えると、**日本語を含む APP_ROOT** など特殊な
    場所での挙動を確かめられる。
    """

    app_root_name = "app"

    def setUp(self) -> None:
        super().setUp()
        from local_video_catalog import paths

        self.app_root = self.temp_dir / self.app_root_name
        self.app_root.mkdir(parents=True, exist_ok=True)
        (self.app_root / paths.APP_ROOT_MARKER).write_text(
            "test marker\n", encoding="utf-8")

        previous = os.environ.get(paths.ROOT_ENVIRONMENT_VARIABLE)
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(self.app_root)

        def restore() -> None:
            if previous is None:
                os.environ.pop(paths.ROOT_ENVIRONMENT_VARIABLE, None)
            else:
                os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = previous

        self.addCleanup(restore)
        self.paths = paths

    def make_source_dir(self, name: str = "videos") -> Path:
        """APP_ROOT の**外**にある元動画フォルダーを作る。"""
        target = self.temp_dir / name
        target.mkdir(parents=True, exist_ok=True)
        return target


def make_synthetic_video(
    ffmpeg: Path,
    target: Path,
    *,
    duration: float = 1.0,
    with_audio: bool = True,
    size: str = "160x120",
    container_args: list[str] | None = None,
) -> bool:
    """個人情報を含まない合成動画を作る。

    testsrc（カラーバー）と sine（正弦波）のみを使う。
    **実際の動画は一切使用しない。**
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate=10",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]

    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac", "-b:a", "64k", "-shortest"]
    if container_args:
        command += container_args
    command += [str(target)]

    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return (completed.returncode == 0 and target.is_file()
            and target.stat().st_size > 0)


def code_strings_and_calls(module) -> tuple[list[str], list[str]]:
    """docstring を除いた文字列定数と、呼び出している関数名を集める。

    文字列を素朴に grep すると、**方針を説明した docstring** まで
    引っかかって役に立たない（「%LOCALAPPDATA% は使わない」と書いた
    説明が、使っている証拠として検出されてしまう）。構文木を見て
    「実際に使っているか」だけを調べる。
    """
    import ast

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        for statement in getattr(node, "body", []):
            if (isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)):
                docstrings.add(id(statement.value))

    strings = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]

    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            calls.append(node.func.attr)
        elif isinstance(node.func, ast.Name):
            calls.append(node.func.id)
    return (strings, calls)


def quiet_logger(log_dir: Path, run_id: str):
    """テスト用のログ。**ファイルへは書くが、コンソールへは出さない。**

    テスト出力の中に本番の進捗表示が混ざると、失敗の位置が見えなくなる。
    ログの内容そのものを検証したいときは text_log_path を読む。
    """
    import logging

    from local_video_catalog.logging_utils import RunLogger

    return RunLogger(log_dir, run_id, console_level=logging.CRITICAL + 1)


def file_state(path: Path) -> tuple[int, int, bytes]:
    """元動画が変更されていないことを確かめるための状態。

    サイズ・更新時刻・内容の 3 点。**1 つでも変わったら書き込みが
    起きている。**
    """
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, path.read_bytes())
