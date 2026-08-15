"""アプリの起動口（配布物のダブルクリック起動はここを通る）.

**このファイルは APP_ROOT の直下に置く。** 自分の場所から APP_ROOT を
決め、``src`` を import path へ足してから画面を開く。

なぜ専用の起動口が要るのか:

    ``python src\\local_video_catalog\\gui\\app.py`` のように**モジュールの
    ファイルを直接実行すると、それは package の一部ではなく __main__ に
    なる**。``from .. import config`` のような相対 import は解決できず、
    ``ImportError: attempted relative import with no known parent package``
    で即座に落ちる。開発中は ``-m`` や import 経由でしか起動しないため、
    この経路だけが試されないまま残りやすい。

**起動に失敗したら黙って消えない。** 画面に日本語で理由を出し、
同じ内容をログへ残す。配布物では利用者がコンソールを見られないため、
ここで捕まえられなかった例外は「一瞬ウィンドウが出て消えるだけ」になる。
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = APP_ROOT / "src"

TITLE = "動画カタログ — 起動できませんでした"

NO_DIALOG_VARIABLE = "LOCAL_VIDEO_CATALOG_NO_DIALOG"
"""ダイアログを出さずに標準エラーへ書く。**自動テスト専用。**

失敗経路は本来モーダルダイアログを出すので、そのままでは誰も押す人が
いない自動テストが固まる。通常運用では設定しない。
"""

_ADVICE = {
    "python": (
        "Python 3.13 以降が必要です。\n"
        "Microsoft Store または python.org からインストールしてください。"),
    "tkinter": (
        "画面を表示する部品（tkinter）が使えません。\n"
        "python.org 版の Python では標準で入っています。\n"
        "Microsoft Store 版で問題が続く場合は python.org 版をお試しください。"),
    "source": (
        "プログラム本体が見つかりません。\n"
        "配布されたフォルダー一式を、そのまま展開し直してください。"),
    "settings": (
        "設定を読み込めませんでした。\n"
        "userdata\\config\\settings.json を削除すると初期状態で起動します。"),
}


def _log_directory() -> Path | None:
    """起動エラーの保存先。用意できなければ None。

    **APP_ROOT の外へは書かない。** userdata を作れないほど早い段階の
    障害では、画面表示を優先してログをあきらめる。
    """
    try:
        target = APP_ROOT / "userdata" / "logs"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        return None


def write_startup_error(detail: str) -> Path | None:
    """起動エラーをログへ残す。書けなければ None。"""
    directory = _log_directory()
    if directory is None:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = directory / f"startup-error_{stamp}.log"
    try:
        target.write_text(detail, encoding="utf-8", newline="\n")
    except OSError:
        return None
    return target


def show_error(message: str) -> None:
    """利用者へ知らせる。

    画面まわりが壊れていること自体が原因の場合もあるため、
    **Windows の MessageBox を直接呼ぶ**。それも駄目なら標準エラーへ。
    """
    import os

    if os.environ.get(NO_DIALOG_VARIABLE):
        print(message, file=sys.stderr)
        return

    if sys.platform == "win32":
        try:
            import ctypes

            MB_OK = 0x0
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(
                None, message, TITLE, MB_OK | MB_ICONERROR)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def classify(error: BaseException) -> str:
    """原因の見当をつけて、利用者向けの助言を選ぶ。"""
    text = f"{type(error).__name__}: {error}".lower()
    if "tkinter" in text or "_tkinter" in text or "tcl" in text:
        return _ADVICE["tkinter"]
    if "local_video_catalog" in text or "no module named" in text:
        return _ADVICE["source"]
    if "settings" in text or "json" in text:
        return _ADVICE["settings"]
    return ""


def fail(summary: str, detail: str, advice: str = "") -> int:
    """起動失敗を、画面とログの両方へ残して終わる。"""
    saved = write_startup_error(
        f"{summary}\n\n{advice}\n\n{'-' * 60}\n{detail}\n")

    message = [summary]
    if advice:
        message.append("")
        message.append(advice)
    message.append("")
    if saved is not None:
        message.append(f"詳しい内容を次のファイルへ保存しました:\n{saved}")
    else:
        message.append("ログを保存できませんでした。"
                       "フォルダーの書き込み権限を確認してください。")
        message.append("")
        message.append(detail.strip().splitlines()[-1] if detail.strip() else "")
    show_error("\n".join(message))
    return 1


def main() -> int:
    if sys.version_info < (3, 13):
        current = ".".join(str(part) for part in sys.version_info[:3])
        return fail(f"Python のバージョンが古すぎます（{current}）。",
                    f"sys.executable = {sys.executable}\n"
                    f"sys.version = {sys.version}",
                    _ADVICE["python"])

    if not (SOURCE_ROOT / "local_video_catalog" / "__init__.py").is_file():
        return fail("プログラム本体が見つかりません。",
                    f"探した場所: {SOURCE_ROOT / 'local_video_catalog'}",
                    _ADVICE["source"])

    # **package として import できるようにする。**
    # ファイルを直接実行すると相対 import が壊れるため、必ずここを通す。
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))

    try:
        from local_video_catalog.gui import app
    except BaseException as error:            # noqa: BLE001
        return fail("画面を読み込めませんでした。",
                    traceback.format_exc(), classify(error))

    try:
        return app.main()
    except BaseException as error:            # noqa: BLE001
        return fail("起動中に問題が起きました。",
                    traceback.format_exc(), classify(error))


if __name__ == "__main__":
    raise SystemExit(main())
