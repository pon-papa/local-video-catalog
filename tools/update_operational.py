"""運用フォルダーを、userdata を保ったまま更新する.

配布物のうち**コードだけ**を差し替える。``userdata`` には手を触れない。
そこには台帳・説明文・解析キャッシュ・Whisper モデル（数百 MB）が
入っており、作り直しには時間も手間もかかる。

使い方::

    python tools/update_operational.py "D:\\path\\to\\local-video-catalog"
    python tools/update_operational.py "..." --dry-run

先に ``tools/make_release.py`` を実行して配布物を作っておくこと。
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

USERDATA = "userdata"


def app_root() -> Path:
    for candidate in [Path(__file__).resolve().parent,
                      *Path(__file__).resolve().parents]:
        if (candidate / "app-root.marker").is_file():
            return candidate
    raise SystemExit("app-root.marker が見つかりません。")


def load_packager():
    sys.path.insert(0, str(app_root() / "tools"))
    import make_release

    return make_release


def check_target(target: Path) -> str:
    """更新先として妥当か。よければ空文字。

    **取り違えると利用者のデータを壊す。** 目印を確かめてから動く。
    """
    if not target.is_dir():
        return f"フォルダーがありません: {target}"
    if not (target / "app-root.marker").is_file():
        return (f"このアプリのフォルダーではありません"
                f"（app-root.marker がありません）: {target}")
    if target.resolve() == app_root().resolve():
        return "開発クローン自身は更新先にできません。"
    return ""


def plan(target: Path) -> tuple[list[Path], list[Path]]:
    """``(更新するもの, 変わらないもの)`` を返す。**何も書き換えない。**"""
    packager = load_packager()
    source_root = app_root()

    changed: list[Path] = []
    same: list[Path] = []
    for relative in packager.collect(source_root):
        if relative.parts and relative.parts[0] == USERDATA:
            continue                      # 触らない
        destination = target / relative
        if (destination.is_file()
                and filecmp.cmp(source_root / relative, destination,
                                shallow=False)):
            same.append(relative)
        else:
            changed.append(relative)
    return (changed, same)


def apply(target: Path, changed: list[Path]) -> int:
    source_root = app_root()
    for relative in changed:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    return len(changed)


def describe_userdata(target: Path) -> list[str]:
    """保持されるものを見せる。安心して実行できるように。"""
    lines: list[str] = []
    userdata = target / USERDATA
    if not userdata.is_dir():
        return ["userdata: まだありません"]
    for name in ("catalog", "descriptions", "models", "cache", "logs"):
        directory = userdata / name
        if not directory.is_dir():
            continue
        files = [p for p in directory.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        lines.append(f"  {name:14} {len(files):6,} ファイル "
                     f"{size / 1024 / 1024:10,.1f} MB")
    return lines or ["  （まだ何もありません）"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/update_operational.py",
        description="運用フォルダーのコードだけを更新する（userdata は保持）")
    parser.add_argument("target", help="運用フォルダー（local-video-catalog）")
    parser.add_argument("--dry-run", action="store_true",
                        help="何が変わるか見るだけ")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()

    problem = check_target(target)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    changed, same = plan(target)

    print(f"更新先: {target}")
    print("")
    print("そのまま残すもの（userdata）:")
    for line in describe_userdata(target):
        print(line)
    print("")
    print(f"入れ替えるファイル: {len(changed)} 件"
          f"（変更なし {len(same)} 件）")
    for relative in changed[:20]:
        print(f"  {relative.as_posix()}")
    if len(changed) > 20:
        print(f"  ... 他 {len(changed) - 20} 件")

    if args.dry_run:
        print("")
        print("[確認のみ] 何も変更していません。")
        return 0
    if not changed:
        print("")
        print("更新するものはありません。")
        return 0

    count = apply(target, changed)
    print("")
    print(f"{count} 件を更新しました。userdata には触れていません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
