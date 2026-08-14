"""解析処理を別プロセスで動かし、出力を取り込む.

**tkinter を import しない。** 画面を起動せずに検証できるようにするため。

なぜ別プロセスなのか:

  - 解析は何時間もかかる。同じプロセスで回すと画面が固まる。
  - 出力を 1 行ずつ受け取れるので、進み具合をそのまま見せられる。
  - 停止は**ファイルを置くだけ**で済む。プロセスを殺さないので、
    台帳も元動画も壊れない。

日本語 Windows での文字化け対策が要る。子プロセスの出力を
リダイレクトして受け取ると、既定では CP932 として復号されて壊れる。
``PYTHONUTF8`` と ``PYTHONIOENCODING`` を明示して渡す。
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .. import paths, pipeline
from ..logging_utils import child_process_environment

MODULE_PIPELINE = "local_video_catalog.pipeline"
MODULE_ENVIRONMENT = "local_video_catalog.environment_check"
MODULE_CATALOG = "local_video_catalog.html_catalog"
MODULE_PROGRESS = "local_video_catalog.stage_report"
MODULE_SUMMARY = "local_video_catalog.run_summary"


def python_executable() -> str:
    """いま動いている Python。

    配布物のどこかにある python.exe を探し回らない。画面自体がこの
    Python で動いているのだから、同じものを使うのが確実。
    """
    return sys.executable


def build_command(module: str, arguments: list[str]) -> list[str]:
    return [python_executable(), "-X", "utf8", "-m", module, *arguments]


def package_source_root() -> Path:
    """**いま動いているパッケージ**が置かれているフォルダー。

    APP_ROOT から ``src`` を組み立てない。APP_ROOT はテストや検証で
    差し替えられるうえ、将来 zip 化などで配置が変わりうる。
    子プロセスには「親と同じパッケージ」を読ませたい。
    """
    import local_video_catalog

    return Path(local_video_catalog.__file__).resolve().parents[1]


def child_environment() -> dict[str, str]:
    """子プロセスの環境変数。

    **UTF-8 を明示する。** 明示しないと日本語 Windows で出力が
    CP932 として復号され、画面のログが文字化けする。
    """
    env = child_process_environment()
    separator = ";" if sys.platform == "win32" else ":"
    existing = env.get("PYTHONPATH", "")
    source_root = str(package_source_root())
    env["PYTHONPATH"] = (source_root if not existing
                         else f"{source_root}{separator}{existing}")
    return env


@dataclass
class TaskResult:
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class BackgroundTask:
    """子プロセスを動かし、出力行を queue へ流す。

    画面側は ``drain()`` を定期的に呼ぶだけでよい。**画面スレッドを
    ブロックしない。**
    """

    def __init__(self, module: str, arguments: list[str]) -> None:
        self.command = build_command(module, arguments)
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.result = TaskResult()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    # -- 起動と監視 -------------------------------------------------------

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=child_environment(), cwd=str(paths.app_root()),
                creationflags=_no_window_flag())
        except OSError as exc:
            self.result.error = f"処理を開始できません: {exc}"
            self.result.exit_code = -1
            return

        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._process is not None
        stream = self._process.stdout
        if stream is not None:
            try:
                for line in stream:
                    text = line.rstrip("\r\n")
                    self.result.lines.append(text)
                    self.queue.put(text)
            finally:
                # 読み切ったパイプは閉じる。長時間運転で何百回も
                # 起動するため、開きっぱなしにしない。
                try:
                    stream.close()
                except OSError:
                    pass
        self.result.exit_code = self._process.wait()

    # -- 画面から呼ぶ -----------------------------------------------------

    def drain(self, limit: int = 200) -> list[str]:
        """溜まっている出力行を取り出す。**待たない。**"""
        lines: list[str] = []
        for _ in range(limit):
            try:
                lines.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return lines

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: float | None = None) -> TaskResult:
        if self._thread is not None:
            self._thread.join(timeout)
        return self.result

    # -- 停止 -------------------------------------------------------------

    def request_stop(self) -> Path:
        """安全停止を要求する。

        **プロセスを終了させない。** 区切りのよいところまで進んでから
        自分で止まる。台帳も元動画も壊れない。
        """
        return pipeline.request_stop()


def _no_window_flag() -> int:
    """Windows でコンソール窓を出さない。"""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def run_and_collect(module: str, arguments: list[str],
                    timeout: float | None = None) -> TaskResult:
    """短い処理を実行して結果をまとめて受け取る。

    環境チェックや HTML 更新のように、すぐ終わるものに使う。
    """
    task = BackgroundTask(module, arguments)
    task.start()
    if task.result.error:
        return task.result
    return task.wait(timeout)


# --------------------------------------------------------------------------
# 画面から呼ぶ入口
# --------------------------------------------------------------------------


def start_analysis(arguments: list[str]) -> BackgroundTask:
    task = BackgroundTask(MODULE_PIPELINE, arguments)
    task.start()
    return task


def check_environment(arguments: list[str] | None = None) -> TaskResult:
    return run_and_collect(MODULE_ENVIRONMENT, arguments or [], timeout=180)


def preview_targets(arguments: list[str]) -> TaskResult:
    return run_and_collect(MODULE_PIPELINE, [*arguments, "--dry-run"],
                           timeout=600)


def update_catalog() -> TaskResult:
    return run_and_collect(MODULE_CATALOG, [], timeout=180)


def show_summary(source_folder: str | None = None) -> TaskResult:
    """解析結果のまとめ。試験後の確認に使う。**読み取りだけ。**"""
    arguments = ["--source-folder", source_folder] if source_folder else []
    return run_and_collect(MODULE_SUMMARY, arguments, timeout=180)


def retry_failed(arguments: list[str], catalog_ids: list[str]) -> BackgroundTask:
    """失敗した動画だけをやり直す。

    **工程の再利用ルールは変えない。** 対象を絞るだけなので、選ばれた
    動画の中でも完了済みの工程はこれまでどおり飛ばされる。
    """
    extra: list[str] = []
    for catalog_id in catalog_ids:
        if catalog_id:
            extra += ["--only-catalog-id", catalog_id]
    return start_analysis([*arguments, *extra])
