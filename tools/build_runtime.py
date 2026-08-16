"""配布用の Python runtime を組み立てる（Windows x64）.

**目的**: 利用者に Python をインストールさせないこと。
``Start.cmd`` は ``runtime\\pythonw.exe`` があればそれを最優先で使う。

**素材は python.org 公式のものだけ。** 2 つ要る。

    python-3.13.14-embed-amd64.zip   本体（Windows embeddable package）
    tcltk.msi                        Tcl/Tk 一式

**embeddable package には tkinter が入っていない。** 中身 34 件を
実際に確認したが ``_tkinter`` も ``tcl`` も無い。画面が出ないので、
``tcltk.msi``（フルインストーラーと同じ構成部品で、python.org が
個別に配布している）から次を足す。

    DLLs\\_tkinter.pyd
    DLLs\\tcl86t.dll
    DLLs\\tk86t.dll
    DLLs\\zlib1.dll
    Lib\\tkinter\\
    tcl\\                            Tcl/Tk のスクリプト一式

``tcltk.msi`` の取り出しは ``msiexec /a``（管理インストール）で行う。
**インストーラーを実行しない**ので、このPCの環境は何も変わらない。

**このスクリプトはネットワークを使わない。** 素材は手元にある前提。
入手は ``fetch_runtime_sources.py`` が行い、SHA-256 を記録する。
記録した SHA-256 が一致しない素材では組み立てを断る。

**同梱するのは実行に要るものだけ。** idlelib・turtledemo・test は入れない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = APP_ROOT / "tools" / "runtime_sources.json"

# runtime に足りない Tcl/Tk のうち、**動かすために要るものだけ**
TCLTK_FILES = (
    "DLLs/_tkinter.pyd",
    "DLLs/tcl86t.dll",
    "DLLs/tk86t.dll",
    "DLLs/zlib1.dll",
)

TCLTK_TREES = (
    ("Lib/tkinter", "Lib/tkinter"),
    ("tcl", "tcl"),
)

SKIP_TREES = ("idlelib", "turtledemo", "test", "tests", "demos", "nmake")
"""配らないもの。IDLE も亀のデモも、このアプリでは使わない。"""

SKIP_SUFFIXES = (".pyc", ".pdb", ".lib", ".sh", ".c", ".vc")
"""**動かすのに要らないもの。** .lib や nmake は開発用で、
配ってもファイルが増えるだけ。ライセンス文書（license.terms）は残す。
"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_sources() -> dict:
    if not SOURCES_FILE.is_file():
        raise SystemExit(
            f"素材の記録がありません: {SOURCES_FILE}\n"
            "先に tools/fetch_runtime_sources.py を実行してください。")
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def verify(path: Path, expected: str, label: str) -> None:
    """**記録と違う素材では作らない。** 何を配ったか分からなくなる。"""
    if not path.is_file():
        raise SystemExit(f"{label} が見つかりません: {path}")
    actual = sha256_of(path)
    if actual != expected:
        raise SystemExit(
            f"{label} の SHA-256 が記録と一致しません。\n"
            f"  記録: {expected}\n  実物: {actual}\n"
            "素材を取り直してください。")


def extract_tcltk(msi: Path, into: Path) -> Path:
    """``msiexec /a`` で取り出す。**インストールはしない。**"""
    into.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["msiexec.exe", "/a", str(msi), f"TARGETDIR={into}", "/qn"],
        capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f"tcltk.msi を取り出せませんでした（{result.returncode}）")
    return into


def copy_tree(source: Path, target: Path) -> int:
    """要らないものを外しながら写す。"""
    count = 0
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        if any(part in SKIP_TREES for part in relative.parts):
            continue
        if item.suffix.lower() in SKIP_SUFFIXES:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        count += 1
    return count


def write_path_file(runtime: Path) -> None:
    """``python313._pth`` を書き、**アプリの src を見えるようにする**。

    embeddable package は既定で site を無効にしている。ここで
    ``..\\src`` を足しておけば、``Start.cmd`` から
    ``python -m local_video_catalog...`` がそのまま動く。

    **利用者の環境変数や既存 Python を見に行かせない。** ここに書いた
    ものだけが sys.path になる。
    """
    target = next(runtime.glob("python3*._pth"), None)
    if target is None:
        raise SystemExit("._pth ファイルが見つかりません。")
    lines = [
        "python313.zip",
        ".",
        "DLLs",
        "Lib",
        "..\\src",
        "",
        "# Do not enable site: keep the user's site-packages out of this app.",
        "#import site",
        "",
    ]
    # **ASCII だけで書く。** ._pth は Python の起動前に読まれるので、
    # 非 ASCII を混ぜると環境によっては読み損ねる。
    target.write_text("\r\n".join(lines), encoding="ascii")


def build(destination: Path, *, sources_dir: Path) -> dict:
    record = load_sources()
    embed = sources_dir / record["embed"]["file_name"]
    tcltk = sources_dir / record["tcltk"]["file_name"]
    verify(embed, record["embed"]["sha256"], "embeddable package")
    verify(tcltk, record["tcltk"]["sha256"], "tcltk.msi")

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    with zipfile.ZipFile(embed) as archive:
        archive.extractall(destination)

    work = destination.parent / "_tcltk_extract"
    if work.exists():
        shutil.rmtree(work)
    extract_tcltk(tcltk, work)

    copied = 0
    for relative in TCLTK_FILES:
        source = work / relative
        if not source.is_file():
            raise SystemExit(f"tcltk.msi に {relative} がありません。")
        target = destination / Path(relative).name
        shutil.copy2(source, target)
        copied += 1
    for source_name, target_name in TCLTK_TREES:
        copied += copy_tree(work / source_name, destination / target_name)

    write_path_file(destination)
    shutil.rmtree(work, ignore_errors=True)

    files = [p for p in destination.rglob("*") if p.is_file()]
    return {
        "python_version": record["python_version"],
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "added_from_tcltk": copied,
    }


def verify_runtime(runtime: Path) -> list[str]:
    """**組み上がった runtime だけで画面が出せるか確かめる。**

    「python.exe があるから大丈夫」では足りない。``tkinter.Tk()`` まで
    通して初めて、利用者の画面が開くと言える。
    """
    python = runtime / "python.exe"
    if not python.is_file():
        return ["python.exe がありません。"]

    script = (
        "import sys, tkinter, _tkinter\n"
        "root = tkinter.Tk(); root.destroy()\n"
        "print('python', sys.version.split()[0])\n"
        "print('tcl', _tkinter.TCL_VERSION, 'tk', _tkinter.TK_VERSION)\n"
        "print('tkinter ok')\n"
    )
    result = subprocess.run([str(python), "-c", script],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        return ["tkinter を動かせませんでした:",
                (result.stderr or "").strip()[:500]]
    return [line for line in (result.stdout or "").splitlines() if line]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/build_runtime.py",
        description="配布用 Python runtime を組み立てる（ネットワークを使わない）")
    parser.add_argument("--sources", required=True,
                        help="素材（embed zip と tcltk.msi）のあるフォルダー")
    parser.add_argument("--destination", default=str(APP_ROOT / "runtime"),
                        help="組み立て先（既定: APP_ROOT\\runtime）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    destination = Path(args.destination).resolve()
    summary = build(destination, sources_dir=Path(args.sources).resolve())

    print(f"組み立てました: {destination}")
    print(f"  Python {summary['python_version']}")
    print(f"  {summary['files']:,} ファイル / "
          f"{summary['bytes'] / 1024 / 1024:,.1f} MB")
    print("")
    print("確認:")
    for line in verify_runtime(destination):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
