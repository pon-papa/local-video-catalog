"""外部プログラムを起動する共通の入口.

**画面から使うアプリなので、内部の CLI がコンソール窓を出してはいけない。**

なぜ窓が出るのか:

    画面は ``pythonw`` で動くのでコンソールを持たない。解析本体もコンソール
    無しで起動している。**その状態でコンソール用のプログラム（ffmpeg /
    ffprobe）を起動すると、Windows がそのプロセスのために新しい
    コンソール窓を作る。** 1 回ごとに窓が開いて閉じるため、329 本の動画を
    登録した実運用では、10 分間ずっと窓が明滅して他の操作ができなくなった。

    ``CREATE_NO_WINDOW`` を渡せば窓は作られない。渡し忘れると出る。
    **呼び出し側ごとに書くと必ず忘れるので、ここへ集約する。**

**窓を隠すことと、エラーを隠すことは別。** このモジュールは
stdout / stderr / 終了コード / timeout をこれまでどおり返す。
異常が見えなくなる作りにはしない。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

WINDOWS = sys.platform == "win32"


def no_window_flags() -> int:
    """コンソール窓を作らせないための creationflags。

    Windows 以外では 0（そもそも窓の概念がない）。
    """
    if not WINDOWS:
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def hidden_startupinfo() -> Any:
    """窓を表示しない STARTUPINFO。Windows 以外では None。

    ``CREATE_NO_WINDOW`` だけで足りるが、``STARTF_USESHOWWINDOW`` を
    併せて渡しておくと、コンソール用でないプログラムを起動した場合にも
    窓が前に出てこない。**利用者のフォーカスを奪わないため。**
    """
    if not WINDOWS:
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def _hidden_options(options: dict[str, Any]) -> dict[str, Any]:
    """窓を出さない指定を足す。**既に指定があれば尊重する。**"""
    merged = dict(options)
    merged["creationflags"] = merged.get("creationflags", 0) | no_window_flags()
    if WINDOWS and merged.get("startupinfo") is None:
        merged["startupinfo"] = hidden_startupinfo()
    return merged


def run(
    command: Sequence[str | Path],
    *,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    text: bool = False,
    encoding: str | None = None,
    **options: Any,
) -> subprocess.CompletedProcess:
    """外部プログラムを実行して終わりまで待つ。**窓は出さない。**

    ``subprocess.run`` の薄い包み。既定で stdout / stderr を捕まえる。
    **異常を握りつぶさない**ので、失敗の内容は呼び出し側で読める。

    ``check`` は既定で False。1 本の失敗で全体を止めない方針のため、
    呼び出し側が終了コードを見て判断する。
    """
    merged = _hidden_options(options)
    merged.setdefault("capture_output", True)
    merged.setdefault("check", False)
    return subprocess.run(
        [str(part) for part in command],
        timeout=timeout,
        cwd=str(cwd) if cwd is not None else None,
        env=env, text=text, encoding=encoding, **merged)


def popen(
    command: Sequence[str | Path],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    **options: Any,
) -> subprocess.Popen:
    """外部プログラムを起動して、待たずに戻る。**窓は出さない。**

    出力を読み続けたい場合に使う（画面が解析本体を動かす経路）。
    **パイプは呼び出し側が閉じること。** 開いたままだと、長時間の運転で
    ハンドルが溜まる。
    """
    merged = _hidden_options(options)
    return subprocess.Popen(
        [str(part) for part in command],
        cwd=str(cwd) if cwd is not None else None,
        env=env, **merged)


def open_in_file_manager(target: Path, *, select: bool = False) -> None:
    """エクスプローラーで開く。**開くだけで、何も変更しない。**

    ここは**窓を出すのが目的**なので隠さない。利用者が押したときだけ
    呼ばれる。
    """
    if not WINDOWS:
        return
    target = Path(target)
    if select and target.exists():
        subprocess.Popen(["explorer", "/select,", str(target)])
    else:
        subprocess.Popen(["explorer", str(target)])
