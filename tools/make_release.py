"""配布用の zip を作る（標準ライブラリのみ）.

配布物は「展開して Start.cmd を押せば使えるフォルダー一式」である。
開発用のもの（Git・CI・テスト）と、**実行時データ（userdata）**は入れない。

userdata は空の骨組みだけを入れる。中身は利用者の解析結果であり、
配布物へ混ぜてはいけない。

使い方::

    python tools/make_release.py
    python tools/make_release.py --output C:\\path\\to\\local-video-catalog.zip
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

EXCLUDED_NAMES = frozenset({
    ".git", ".github", ".gitignore", ".gitattributes",
    "tests", "userdata", "__pycache__",
    ".venv", "venv", ".vscode", ".idea",
    ".mypy_cache", ".pytest_cache",
})
"""配布物へ入れないもの。

``tests`` を外すのは、配布物を小さくするため（開発クローンでは残る）。
``userdata`` を外すのは、**利用者の解析結果が混ざらないようにするため**。
"""

EXCLUDED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
    ".db", ".log", ".jsonl", ".tmp", ".bak",
})

USERDATA_SKELETON = (
    "config", "catalog", "catalog/exports", "descriptions",
    "cache", "cache/probe", "cache/frames", "cache/vlm", "cache/asr",
    "models", "models/whisper", "logs", "runs", "temp", "control",
)


def app_root() -> Path:
    for candidate in [Path(__file__).resolve().parent,
                      *Path(__file__).resolve().parents]:
        if (candidate / "app-root.marker").is_file():
            return candidate
    raise SystemExit("app-root.marker が見つかりません。")


def is_excluded(relative: Path) -> bool:
    """配布物から外すか。``relative`` は APP_ROOT からの相対パス。"""
    parts = Path(relative).parts
    if any(part in EXCLUDED_NAMES for part in parts):
        return True
    return Path(relative).suffix.lower() in EXCLUDED_SUFFIXES


def collect(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if is_excluded(relative):
            continue
        found.append(relative)
    return found


def build(root: Path, output: Path) -> tuple[int, int]:
    """zip を作る。``(ファイル数, バイト数)`` を返す。"""
    files = collect(root)
    output.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(root / relative, str(Path("local-video-catalog")
                                               / relative))
        # userdata は空の骨組みだけ。中身は入れない。
        #
        # .gitignore は入れない。配布先は Git 管理下ではないので、
        # 利用者にとって意味の分からないファイルが増えるだけになる。
        for folder in USERDATA_SKELETON:
            archive.writestr(
                str(Path("local-video-catalog") / "userdata" / folder
                    / ".keep"), "")

    return (len(files), output.stat().st_size)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/make_release.py",
        description="配布用の zip を作る（実行時データは入れない）")
    parser.add_argument("--output", default=None,
                        help="出力先。既定は userdata/temp/local-video-catalog.zip")
    parser.add_argument("--list", action="store_true",
                        help="入るファイルを表示するだけ")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = app_root()

    if args.list:
        for relative in collect(root):
            print(relative.as_posix())
        return 0

    output = (Path(args.output) if args.output
              else root / "userdata" / "temp" / "local-video-catalog.zip")
    count, size = build(root, output)
    print(f"配布物を作りました: {output}")
    print(f"{count} ファイル / {size:,} バイト")
    print("userdata は空の骨組みだけを入れています。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
