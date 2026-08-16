"""配布用の zip を作る（標準ライブラリのみ）.

配布物は「展開して Start.cmd を押せば使えるフォルダー一式」である。
開発用のもの（Git・CI・テスト・道具・内部設計文書）と、
**実行時データ（userdata）**は入れない。

userdata は空の骨組みだけを入れる。中身は利用者の解析結果であり、
配布物へ混ぜてはいけない。

**入れるものを名指しで決める。** 「これを除く」方式だけだと、後から
増えたフォルダーが黙って配布物へ入る。利用者へ渡すものは、増えるときに
必ず気づける形にしておく。

使い方::

    python tools/make_release.py
    python tools/make_release.py --list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from local_video_catalog import APPLICATION_VERSION      # noqa: E402

PACKAGE_STEM = "local-video-catalog"
PLATFORM = "windows-x64"

INCLUDED_FILES = (
    "Start.cmd",
    "launch.py",
    "app-root.marker",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)
"""配布物の根に置くファイル。**名指しで決める。**"""

INCLUDED_TREES = (
    "src",
    "config",
    "runtime",
)
"""まるごと入れるフォルダー。

``runtime`` は python.org から取り寄せた Python + Tcl/Tk。
これがあるので、利用者は Python を用意しなくてよい。
"""

INCLUDED_DOCS = (
    "docs/QUICKSTART.md",
    "docs/TROUBLESHOOTING.md",
)
"""**利用者向けの文書だけ。** 内部の設計文書・監査記録は配らない。

読む人が違う。設計文書は開発リポジトリに残す。
"""

EXCLUDED_NAMES = frozenset({
    ".git", ".github", ".gitignore", ".gitattributes",
    "tests", "tools", "userdata", "dist", "__pycache__",
    ".venv", "venv", ".vscode", ".idea",
    ".mypy_cache", ".pytest_cache",
})

EXCLUDED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".pdb", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
    ".db", ".log", ".jsonl", ".tmp", ".bak",
})

USERDATA_SKELETON = (
    "config", "catalog", "catalog/exports", "descriptions",
    "cache", "cache/probe", "cache/frames", "cache/vlm", "cache/asr",
    "models", "models/whisper", "logs", "runs", "temp", "control",
)

WHISPER_MODEL_FILE = "ggml-large-v3-turbo-q5_0.bin"
WHISPER_LICENSE_FILE = "WHISPER_MODEL_LICENSE.txt"

BUNDLED_USERDATA = (
    f"models/whisper/{WHISPER_MODEL_FILE}",
    f"models/whisper/{WHISPER_LICENSE_FILE}",
)
"""**userdata に最初から入れてよい唯一のもの。**

ここ以外の userdata は空の骨組みだけ。利用者の解析結果が配布物へ
混ざるのを防ぐためで、その原則は変えない。

文字起こしモデルだけは例外にする。無いと、展開直後の標準状態で
文字起こしができず、利用者は 547 MB のファイルを自分で探して
所定の場所へ置くことになる。それは「展開して起動するだけ」から
遠い。MIT なので同梱でき、許諾文を隣へ置く。
"""


def package_name() -> str:
    return f"{PACKAGE_STEM}-v{APPLICATION_VERSION}-{PLATFORM}"


def app_root() -> Path:
    for candidate in [Path(__file__).resolve().parent,
                      *Path(__file__).resolve().parents]:
        if (candidate / "app-root.marker").is_file():
            return candidate
    raise SystemExit("app-root.marker が見つかりません。")


def is_excluded(relative: Path) -> bool:
    parts = Path(relative).parts
    if any(part in EXCLUDED_NAMES for part in parts):
        return True
    return Path(relative).suffix.lower() in EXCLUDED_SUFFIXES


def collect(root: Path, *, require_runtime: bool = True) -> list[Path]:
    """配布物へ入れるファイルを、**入れると決めたものだけ**集める。

    ``require_runtime`` を False にすると、``runtime`` フォルダーが
    無くても一覧を作る。中身を確かめたいだけの場面（一覧表示や試験）で使う。
    **配布物を作るときは必ず True。** runtime の無い ZIP を配ると、
    利用者は Python を自分で入れる羽目になる。
    """
    found: list[Path] = []

    for name in INCLUDED_FILES:
        path = root / name
        if not path.is_file():
            raise SystemExit(f"配布物に必要なファイルがありません: {name}")
        found.append(Path(name))

    for name in INCLUDED_DOCS:
        path = root / name
        if not path.is_file():
            raise SystemExit(f"配布物に必要な文書がありません: {name}")
        found.append(Path(name))

    for tree in INCLUDED_TREES:
        base = root / tree
        if not base.is_dir():
            if tree == "runtime":
                if not require_runtime:
                    continue
                raise SystemExit(
                    "runtime\\ がありません。\n"
                    "  python tools/fetch_runtime_sources.py --version 3.13.14 "
                    "--into <素材の置き場>\n"
                    "  python tools/build_runtime.py --sources <素材の置き場>\n"
                    "を先に実行してください。")
            raise SystemExit(f"配布物に必要なフォルダーがありません: {tree}")
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if is_excluded(relative):
                continue
            found.append(relative)

    return found


def bundled_userdata_sources(root: Path, materials: Path | None
                             ) -> list[tuple[Path, str]]:
    """同梱する userdata 資源を ``(元ファイル, 配布物での位置)`` で返す。

    素材の置き場所が指定されていなければ、``userdata`` の中の実物を使う
    （開発機で既に持っている場合）。**どちらも無ければ空**を返し、
    呼び出し側が判断する。
    """
    found: list[tuple[Path, str]] = []
    for relative in BUNDLED_USERDATA:
        name = Path(relative).name
        candidates = []
        if materials is not None:
            candidates.append(materials / name)
        candidates.append(root / "userdata" / relative)
        for candidate in candidates:
            if candidate.is_file():
                found.append((candidate, relative))
                break
    return found


def verify_model(path: Path) -> None:
    """**記録した SHA-256 と違うモデルは配らない。**

    名前が同じでも中身が違えば、利用者の手元で何が動くのか分からない。
    """
    manifest = app_root() / "tools" / "whisper_model_source.json"
    if not manifest.is_file():
        raise SystemExit("tools/whisper_model_source.json がありません。")
    record = json.loads(manifest.read_text(encoding="utf-8"))

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != record["sha256"]:
        raise SystemExit(
            "文字起こしモデルの SHA-256 が記録と一致しません。\n"
            f"  記録: {record['sha256']}\n  実物: {digest.hexdigest()}\n"
            "tools/fetch_whisper_model.py で取り直してください。")


def build(root: Path, output: Path, *, materials: Path | None = None
          ) -> tuple[int, int]:
    files = collect(root)
    bundled = bundled_userdata_sources(root, materials)
    if not any(source.name == WHISPER_MODEL_FILE for source, _ in bundled):
        raise SystemExit(
            "文字起こしモデルがありません。\n"
            "  python tools/fetch_whisper_model.py --into <素材の置き場>\n"
            "を先に実行し、--materials で置き場所を渡してください。")

    output.parent.mkdir(parents=True, exist_ok=True)
    top = package_name()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(root / relative, str(Path(top) / relative))
        # userdata は空の骨組みだけ。**中身は入れない。**
        for folder in USERDATA_SKELETON:
            archive.writestr(
                str(Path(top) / "userdata" / folder / ".keep"), "")
        # 例外は文字起こしモデルとその許諾文だけ。
        for source, relative in bundled:
            if source.name == WHISPER_MODEL_FILE:
                verify_model(source)
            archive.write(source,
                          str(Path(top) / "userdata" / relative))

    return (len(files) + len(bundled), output.stat().st_size)


def write_checksum(archive: Path) -> tuple[Path, str]:
    """SHA-256 を添える。**受け取った人が中身を確かめられるように。**"""
    digest = hashlib.sha256()
    with open(archive, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    target = archive.with_suffix(archive.suffix + ".sha256")
    target.write_text(f"{value} *{archive.name}\n",
                      encoding="ascii", newline="\n")
    return (target, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/make_release.py",
        description="配布用の zip を作る（実行時データは入れない）")
    parser.add_argument("--output", default=None,
                        help="出力先。既定は dist/<名前>.zip")
    parser.add_argument("--list", action="store_true",
                        help="入るファイルを表示するだけ")
    parser.add_argument("--materials", default=None,
                        help="同梱する文字起こしモデルの置き場所")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = app_root()

    if args.list:
        for relative in collect(root, require_runtime=False):
            print(relative.as_posix())
        return 0

    output = (Path(args.output) if args.output
              else root / "dist" / f"{package_name()}.zip")
    count, size = build(root, output,
                       materials=(Path(args.materials).resolve()
                                  if args.materials else None))
    checksum_file, checksum = write_checksum(output)

    print(f"配布物を作りました: {output}")
    print(f"{count:,} ファイル / {size:,} バイト "
          f"（{size / 1024 / 1024:,.1f} MB）")
    print(f"SHA-256: {checksum}")
    print(f"添付: {checksum_file.name}")
    print("userdata は空の骨組み ＋ 文字起こしモデルだけを入れています。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
