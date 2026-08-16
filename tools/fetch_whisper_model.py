"""同梱する文字起こしモデルを取り寄せ、素性を記録する.

**ここだけがネットワークを使う。** ``make_release.py`` は手元の素材だけで
動く。配布物を作るたびに外から取りに行く形にしない（同じ素材から同じ
ものが出来る状態を保つため）。runtime と同じ考え方。

取り寄せるのは 1 つだけ::

    ggml-large-v3-turbo-q5_0.bin

**アプリの既定値と同じ名前でなければならない。** 違う名前を置いても、
利用者の画面では「モデルがありません」のままになる。

ライセンス（一次情報で確認したもの）::

    配布元  https://huggingface.co/ggerganov/whisper.cpp
            モデルカードに license: mit
            「OpenAI's Whisper models converted to ggml format」
    上流    https://github.com/openai/whisper  MIT / Copyright (c) 2022 OpenAI

MIT なので再配布できる。ただし**著作権表示と許諾文を添える**必要が
あるので、``THIRD_PARTY_NOTICES.md`` に記載し、ライセンス本文を
モデルの隣へ置く。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = APP_ROOT / "tools" / "whisper_model_source.json"

MODEL_FILE = "ggml-large-v3-turbo-q5_0.bin"
BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
"""**この配布元だけ。** 設定で差し替えられるようにしない。"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    if not url.startswith(BASE + "/"):
        raise SystemExit(f"この配布元以外からは取得しません: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=600) as response:
        with open(target, "wb") as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)


def fetch(into: Path) -> dict:
    url = f"{BASE}/{MODEL_FILE}"
    target = into / MODEL_FILE

    if target.is_file():
        print(f"すでにあります: {target}")
    else:
        print(f"取得: {url}")
        download(url, target)

    record = {
        "file_name": MODEL_FILE,
        "source_url": url,
        "upstream_project": "whisper.cpp (ggerganov) / OpenAI Whisper",
        "upstream_model": "OpenAI Whisper large-v3-turbo（ggml 形式・q5_0 量子化）",
        "license": "MIT",
        "license_notes": (
            "配布元 https://huggingface.co/ggerganov/whisper.cpp の"
            "モデルカードが license: mit を宣言。上流の OpenAI Whisper も"
            "MIT（Copyright (c) 2022 OpenAI）。再配布には著作権表示と"
            "許諾文の同梱が必要。"),
        "sha256": sha256_of(target),
        "bytes": target.stat().st_size,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(f"  {record['bytes']:,} バイト")
    print(f"  SHA-256 {record['sha256']}")
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/fetch_whisper_model.py",
        description="同梱する文字起こしモデルを取り寄せる")
    parser.add_argument("--into", required=True, help="素材の置き場所")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = fetch(Path(args.into).resolve())
    MANIFEST.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("")
    print(f"記録しました: {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
