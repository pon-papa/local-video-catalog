"""配布用 runtime の素材を python.org から取り寄せ、素性を記録する.

**ここだけがネットワークを使う。** ``build_runtime.py`` と
``make_release.py`` は手元の素材だけで動く。配布物を作るたびに外へ
取りに行く形にしない（同じ素材から同じものが出来る状態を保つため）。

**取得先は python.org 公式だけ。** ミラーや再配布サイトは使わない。

取り寄せたら ``tools/runtime_sources.json`` へ

    バージョン / ファイル名 / 取得元 URL / SHA-256 / 取得日

を残す。以後の組み立ては、この SHA-256 と一致する素材でしか行わない。
**何を配ったのかを後から追えるようにするため。**
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = APP_ROOT / "tools" / "runtime_sources.json"

BASE = "https://www.python.org/ftp/python"
"""**python.org 公式のみ。** ここを設定で変えられるようにしない。"""


def download(url: str, target: Path) -> None:
    if not url.startswith(BASE + "/"):
        raise SystemExit(f"python.org 以外からは取得しません: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        target.write_bytes(response.read())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(version: str, into: Path) -> dict:
    embed_name = f"python-{version}-embed-amd64.zip"
    embed_url = f"{BASE}/{version}/{embed_name}"
    tcltk_name = "tcltk.msi"
    tcltk_url = f"{BASE}/{version}/amd64/{tcltk_name}"

    into.mkdir(parents=True, exist_ok=True)
    record: dict = {
        "python_version": version,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": ("embeddable package には tkinter が含まれないため、"
                 "Tcl/Tk は同じ版の tcltk.msi から補う。"),
    }

    for key, name, url in (("embed", embed_name, embed_url),
                           ("tcltk", tcltk_name, tcltk_url)):
        target = into / name
        print(f"取得: {url}")
        download(url, target)
        record[key] = {
            "file_name": name,
            "source_url": url,
            "sha256": sha256_of(target),
            "bytes": target.stat().st_size,
        }
        print(f"  {record[key]['bytes']:,} バイト")
        print(f"  SHA-256 {record[key]['sha256']}")

    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/fetch_runtime_sources.py",
        description="runtime の素材を python.org から取り寄せる")
    parser.add_argument("--version", required=True,
                        help="Python のバージョン（例: 3.13.14）")
    parser.add_argument("--into", required=True, help="素材の置き場所")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = fetch(args.version, Path(args.into).resolve())
    SOURCES_FILE.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("")
    print(f"記録しました: {SOURCES_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
