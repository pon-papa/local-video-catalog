"""配布 ZIP そのものを、別の場所へ展開して確かめる.

**リポジトリのテストが通るだけでは足りない。** 利用者が受け取るのは
ZIP であって、リポジトリではない。開発環境の都合（PYTHONPATH、
インストール済み Python、作業ディレクトリ）に助けられていないことを、
展開したフォルダーだけで確かめる。

確かめること:

  中身    開発用のもの・実データが入っていないか
  起動    同梱 runtime だけで tkinter と本体が動くか
  境界    userdata を自分の下へ作り、外へ書かないか
  移動    フォルダーを移しても動くか
  場所    日本語と空白を含むパスでも動くか

**このツールは配布 ZIP には入れない。** 開発リポジトリだけに置く。

使い方::

    python tools/release_smoke_test.py
    python tools/release_smoke_test.py --archive dist/....zip
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from local_video_catalog import APPLICATION_VERSION      # noqa: E402

APP_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_SUFFIXES = (
    ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db",
    ".log", ".jsonl", ".pyc", ".pyo",
    ".mp4", ".m4v", ".mov", ".mts", ".m2ts", ".mkv", ".avi",
    ".jpg", ".jpeg", ".png", ".bin", ".gguf",
)
"""配布物に**あってはならない**種類。実データと開発の副産物。"""

FORBIDDEN_NAMES = (
    ".git", ".github", ".gitignore", ".gitattributes",
    "tests", "tools", "__pycache__", "dist",
    "settings.json", "gui-state.json", "catalog.html",
)

FORBIDDEN_TEXT = (
    "C:\\Users\\User",
    "HomeVideo",
    "動画編集関係",
    "local-video-catalog-運用",
)
"""**個人環境の痕跡。** 1 文字でも配布物に混ぜない。

開発機の利用者名・実際の動画フォルダー・運用コピーの場所が、
文書やコードのコメントへ紛れ込んでいないかを見る。

``VID-000001`` のような台帳 ID は**ここに入れない**。ID の書式は
アプリが自分で発行するもので、``database.py`` に必ず現れる。
禁じると正しいコードを弾いてしまう（実際に弾いた）。漏れて困るのは
ID そのものではなく、**実在の場所と結びついた情報**なので上の 4 つで見る。
"""

TEXT_SUFFIXES = (".md", ".py", ".cmd", ".json", ".txt", ".marker")


class Failure(Exception):
    """確かめて駄目だったこと。"""


def check_contents(archive: Path) -> list[str]:
    """ZIP の中身を見る。**展開する前に、入っているものだけで判断する。**"""
    notes: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()

        for name in names:
            parts = Path(name).parts[1:]          # 先頭は配布フォルダー名
            if any(part in FORBIDDEN_NAMES for part in parts):
                raise Failure(f"配布物に入ってはいけないもの: {name}")
            if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES:
                raise Failure(f"配布物に入ってはいけない種類: {name}")

        userdata = [n for n in names if "/userdata/" in n.replace("\\", "/")]
        not_keep = [n for n in userdata if not n.endswith(".keep")]
        if not_keep:
            raise Failure(f"userdata に中身が入っています: {not_keep[:5]}")
        notes.append(f"userdata は空の骨組み {len(userdata)} か所のみ")

        required = ("Start.cmd", "launch.py", "app-root.marker", "README.md",
                    "LICENSE", "THIRD_PARTY_NOTICES.md",
                    "docs/QUICKSTART.md", "docs/TROUBLESHOOTING.md")
        for name in required:
            if not any(n.replace("\\", "/").endswith("/" + name)
                       for n in names):
                raise Failure(f"配布物に必要なものがありません: {name}")
        notes.append(f"必要なファイル {len(required)} 件をすべて確認")

        if not any("/runtime/" in n.replace("\\", "/") for n in names):
            raise Failure("runtime\\ が入っていません。")
        for needed in ("pythonw.exe", "_tkinter.pyd", "tk86t.dll"):
            if not any(n.replace("\\", "/").endswith("/runtime/" + needed)
                       for n in names):
                raise Failure(f"runtime に {needed} がありません。")
        notes.append("runtime に python / tkinter / Tcl・Tk を確認")

        # 文書・コードに個人環境の痕跡が残っていないか
        for name in names:
            if Path(name).suffix.lower() not in TEXT_SUFFIXES:
                continue
            if "/runtime/" in name.replace("\\", "/"):
                continue          # python.org の配布物はそのまま
            body = zf.read(name).decode("utf-8", errors="replace")
            for forbidden in FORBIDDEN_TEXT:
                if forbidden in body:
                    raise Failure(
                        f"個人環境の痕跡が残っています: {name} に "
                        f"{forbidden!r}")
        notes.append("個人環境の痕跡なし")

    return notes


def run_in(extracted: Path, script: str, *, cwd: Path) -> str:
    """**同梱 runtime だけで**動かす。開発環境の影響を断つ。"""
    python = extracted / "runtime" / "python.exe"
    if not python.is_file():
        raise Failure("runtime\\python.exe がありません。")

    environment = {
        k: v for k, v in os.environ.items()
        if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
                             "LOCAL_VIDEO_CATALOG_ROOT")
    }
    # **子にも -X utf8 を渡す。** 渡さないと日本語版 Windows の既定
    # （CP932）で書き出され、UTF-8 として読むところで壊れる。
    result = subprocess.run(
        [str(python), "-X", "utf8", "-c", script], capture_output=True,
        text=True, encoding="utf-8", cwd=str(cwd), env=environment)
    if result.returncode != 0:
        raise Failure(f"同梱 runtime で失敗しました:\n{result.stderr.strip()[:800]}")
    return (result.stdout or "").strip()


def check_startup(extracted: Path, *, cwd: Path) -> list[str]:
    script = (
        "import sys, tkinter, _tkinter\n"
        "root = tkinter.Tk(); root.destroy()\n"
        "import local_video_catalog as m\n"
        "from local_video_catalog.gui import app\n"
        "from local_video_catalog import paths\n"
        "print('python', sys.version.split()[0])\n"
        "print('tcl', _tkinter.TCL_VERSION, 'tk', _tkinter.TK_VERSION)\n"
        "print('version', m.APPLICATION_VERSION)\n"
        "print('title', app.WINDOW_TITLE)\n"
        "print('root', paths.app_root())\n"
    )
    lines = run_in(extracted, script, cwd=cwd).splitlines()
    found = dict(line.split(" ", 1) for line in lines if " " in line)

    if found.get("version") != APPLICATION_VERSION:
        raise Failure(f"版が一致しません: {found.get('version')}")
    resolved = Path(found.get("root", ""))
    if resolved != extracted:
        raise Failure(f"APP_ROOT が展開先と違います: {resolved}")
    return lines


def outside_candidates() -> list[Path]:
    """アプリが状態を置きそうな「外」の場所。"""
    found: list[Path] = []
    for variable in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(variable)
        if not base:
            continue
        for name in ("local-video-catalog", "LocalVideoCatalog",
                     "FamilyVideoCatalog"):
            found.append(Path(base) / name)
    return found


def check_userdata_boundary(extracted: Path, *, cwd: Path) -> list[str]:
    """**自分の下だけに書く**ことを確かめる。

    「その場所が在るか」ではなく「**この実行で増えたか**」を見る。
    在るかどうかで判定すると、無関係な別のアプリが昔に作ったフォルダーで
    落ちる（実際に落ちた）。
    """
    before = {path for path in outside_candidates() if path.exists()}

    script = (
        "from local_video_catalog import paths\n"
        "paths.ensure_userdata_tree()\n"
        "print('userdata', paths.userdata_dir())\n"
    )
    output = run_in(extracted, script, cwd=cwd)
    userdata = Path(output.split(" ", 1)[1])
    if extracted not in userdata.parents:
        raise Failure(f"userdata が配布フォルダーの外です: {userdata}")

    appeared = [str(path) for path in outside_candidates()
                if path.exists() and path not in before]
    if appeared:
        raise Failure(f"アプリの外へ状態を作っています: {appeared}")

    return [f"userdata は配布フォルダーの中: {userdata.relative_to(extracted)}",
            "%APPDATA% / %LOCALAPPDATA% / ユーザーフォルダーへ何も増やさない"]


def check_start_cmd(extracted: Path) -> list[str]:
    """``Start.cmd`` が cmd.exe で読める文字コードで書かれていること。"""
    raw = (extracted / "Start.cmd").read_bytes()
    try:
        raw.decode("cp932")
    except UnicodeDecodeError as exc:
        raise Failure(f"Start.cmd を cmd.exe が読めません（CP932 不可）: {exc}")
    if b"runtime" not in raw:
        raise Failure("Start.cmd が同梱 runtime を見ていません。")
    return ["Start.cmd は CP932 で読める / runtime を優先する"]


def smoke(archive: Path) -> list[str]:
    notes = [f"配布物: {archive.name}",
             f"大きさ: {archive.stat().st_size:,} バイト"]
    notes += check_contents(archive)

    with tempfile.TemporaryDirectory(prefix="lvc_smoke_",
                                     ignore_cleanup_errors=True) as temp:
        # **日本語と空白を含む場所**へ展開する。ここで壊れる作りが多い。
        base = Path(temp) / "配布テスト" / "動画 カタログ β版"
        base.mkdir(parents=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(base)
        extracted = next(p for p in base.iterdir() if p.is_dir())
        notes.append(f"展開先: ...\\配布テスト\\動画 カタログ β版\\{extracted.name}")

        notes += check_start_cmd(extracted)
        # **作業ディレクトリを別の場所にして**動かす
        notes += check_startup(extracted, cwd=Path(temp))
        notes += check_userdata_boundary(extracted, cwd=Path(temp))

        # **フォルダーを移しても動くこと**
        moved = base.parent / "移動先 folder"
        shutil.move(str(extracted), str(moved))
        notes += ["移動しても動く:"]
        notes += check_startup(moved, cwd=Path(temp))

    return notes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/release_smoke_test.py",
        description="配布 ZIP そのものを別の場所で確かめる")
    parser.add_argument("--archive", default=None, help="確かめる ZIP")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.archive:
        archive = Path(args.archive)
    else:
        name = (f"local-video-catalog-v{APPLICATION_VERSION}-windows-x64.zip")
        archive = APP_ROOT / "dist" / name
    if not archive.is_file():
        print(f"配布物がありません: {archive}", file=sys.stderr)
        print("先に python tools/make_release.py を実行してください。",
              file=sys.stderr)
        return 1

    try:
        for line in smoke(archive):
            print(f"  {line}")
    except Failure as exc:
        print("")
        print(f"✕ {exc}", file=sys.stderr)
        return 1

    print("")
    print("✓ 配布物だけで起動できます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
