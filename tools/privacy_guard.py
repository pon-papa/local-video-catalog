"""個人データが Git へ混入していないかを検査する（標準ライブラリのみ）.

local-video-catalog は One-Folder 完結型で、実行時データを
``APP_ROOT\\userdata\\`` 配下に置く。つまり **実データがリポジトリの
作業ツリーの中に現れうる**。旧個人版（実データを別ドライブへ完全分離）
とは構造的にリスクが異なるため、``.gitignore`` だけに頼らない。

このスクリプトは ``git ls-files``（= 実際に追跡されているファイル）を
対象に検査する。``.gitignore`` が壊れていても、追跡されてしまった
時点で検出できる。

検査項目:

  1. userdata/ 配下が追跡されていない
  2. メディア（動画・音声・画像）が追跡されていない
  3. 台帳・モデル（sqlite/db/bin/gguf 等）が追跡されていない
  4. 解析成果物（説明文・カタログ・文字起こし・解析キャッシュ）が追跡されていない
  5. 元動画の内容ごとの名前空間（src_<hex>）が追跡されていない
  6. ユーザー設定ファイルが追跡されていない
  7. 追跡ファイルの中身にユーザー固有の絶対パスが含まれていない

使い方::

    python tools/privacy_guard.py            # 検査して結果を表示
    python tools/privacy_guard.py --json     # JSON で出す（CI 用）

終了コード:
    0 = 問題なし
    1 = 個人データの混入を検出
    2 = 検査できなかった（git が無い等）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_CANNOT_CHECK = 2

# --------------------------------------------------------------------------
# 検査ルール
# --------------------------------------------------------------------------

MEDIA_SUFFIXES = frozenset({
    ".mp4", ".m4v", ".mov", ".mts", ".m2ts", ".avi", ".mkv", ".mpg",
    ".mpeg", ".vob", ".webm", ".ts", ".wav", ".mp3", ".flac", ".aac",
    ".m4a", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff",
})
"""解析対象そのもの、または解析対象から作られた画像・音声。"""

DATA_SUFFIXES = frozenset({
    ".sqlite", ".sqlite3", ".db", ".bin", ".gguf", ".safetensors",
    ".pt", ".onnx", ".srt", ".vtt",
})
"""台帳と AI モデル。中身が個人データ、またはサイズが大きすぎるもの。"""

ALLOWED_TRACKED_PATHS = frozenset({
    "userdata/.gitignore",
})
"""禁止パターンに一致するが、追跡してよい正確なパス。

``userdata/.gitignore`` は二重防御の内側そのものであり、これが追跡されて
いないと Git 上に userdata フォルダーが残らず、防御が 1 層に減る。
**完全一致のみ**を許可する（前方一致にしない）。
"""

# パス（POSIX 表記）に対する禁止パターン
FORBIDDEN_PATH_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(^|/)userdata/", "実行時データ（userdata 配下）"),
    (r"(^|/)descriptions/", "最終説明文"),
    (r"(^|/)VID-\d+.*\.txt$", "最終説明文"),
    (r"(^|/)catalog\.html(\.tmp)?$", "HTML カタログ"),
    (r"(^|/)cache/(probe|frames|scenes|vlm|asr)/", "解析キャッシュ"),
    (r"(^|/)src_[0-9a-f]{8,}(/|$)", "元動画ごとの名前空間（実データ）"),
    (r"(^|/)transcript(_[A-Za-z0-9_]+)?\.(json|txt)$", "文字起こし結果"),
    (r"(^|/)segments(_[A-Za-z0-9_]+)?\.jsonl$", "文字起こしセグメント"),
    (r"(^|/)chunk_\d+.*\.(json|txt)$", "ASR チャンク結果"),
    (r"(^|/)raw_engine_output", "ASR の生出力"),
    (r"(^|/)frame_.*\.analysis\.json$", "フレーム解析結果"),
    (r"(^|/)asset_visual_summary\.json$", "視覚概要"),
    (r"(^|/)run_manifest(_[A-Za-z0-9_]+)?\.json$", "実行マニフェスト"),
    (r"(^|/)settings\.json$", "ユーザー設定（実在パスを含む）"),
    (r"(^|/)settings\.local\.[A-Za-z0-9]+$", "ユーザー設定（実在パスを含む）"),
    (r"(^|/)gui-state\.json$", "画面状態（実在パスを含む）"),
    (r"(^|/)\.env(\.|$)", "環境変数ファイル"),
    (r"(^|/)models/", "AI モデル"),
    (r"(^|/)logs/", "実行ログ"),
    (r"(^|/)runs/", "実行マニフェスト"),
)

# --- 追跡ファイルの「中身」に対する検査 ------------------------------------

# Windows のユーザープロファイル配下の絶対パス。
# 例: C:\Users\Taro\... / C:/Users/Taro/...
USER_PROFILE_PATTERN = re.compile(
    r"[A-Za-z]:[\\/]+Users[\\/]+(?!<)(?!\{)[^\\/\s\"'<>|]+[\\/]",
)

# ドライブ直下の非 ASCII フォルダー。個人の保存先を指していることが多い。
# 例: D:\動画内容解析システムデータ\ / F:\ホームビデオ\
NON_ASCII_DRIVE_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/]+[^\x00-\x7F][^\\/\s\"'<>|]*",
)

# 内容検査から除外するファイル。
#
# **検査規則そのもの、またはその試験値を含むファイルだけ**を挙げる。
# ここへ安易に追加すると防御が穴だらけになるので、追加時は理由を必ず書く。
CONTENT_SCAN_SKIP = (
    # パターン定義そのものを含む
    "tools/privacy_guard.py",
    # 「検出されるべき悪い値」を試験値として持つ。値はすべて合成。
    "tests/test_privacy_guard.py",
    # 遮断パターンの一覧であって、実在パスではない
    ".gitignore",
    "userdata/.gitignore",
)

CONTENT_SCAN_SUFFIXES = frozenset({
    ".py", ".json", ".md", ".cmd", ".bat", ".ps1", ".yml", ".yaml",
    ".txt", ".cfg", ".ini", ".toml",
})

# 内容検査の許可リスト。ドキュメント上どうしても必要な参照。
CONTENT_ALLOWLIST: tuple[str, ...] = (
    # 調査文書は旧個人版のパスを「持ち込み禁止対象」として列挙する必要がある。
    "docs/CURRENT_SYSTEM_AUDIT.md",
)

MAX_CONTENT_BYTES = 2 * 1024 * 1024
"""これより大きい追跡ファイルは中身を読まない（本来そんなファイルは無いはず）。"""


# --------------------------------------------------------------------------
# 結果
# --------------------------------------------------------------------------


@dataclass
class Violation:
    path: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason, "detail": self.detail}


@dataclass
class GuardResult:
    checked_files: int = 0
    violations: list[Violation] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked_files": self.checked_files,
            "violations": [v.to_dict() for v in self.violations],
            "error": self.error,
        }


# --------------------------------------------------------------------------
# 検査
# --------------------------------------------------------------------------


def list_tracked_files(repo_root: Path) -> tuple[list[str], str]:
    """git が追跡しているファイルの一覧（POSIX 表記）を返す。"""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"git を実行できませんでした: {exc}"
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        return [], f"git ls-files が失敗しました: {message}"

    raw = completed.stdout.decode("utf-8", errors="replace")
    return [p for p in raw.split("\0") if p], ""


def check_path(path: str) -> Violation | None:
    """1 件のパスを検査する。問題なければ None。"""
    if path in ALLOWED_TRACKED_PATHS:
        return None

    lowered = path.lower()
    suffix = Path(lowered).suffix

    if suffix in MEDIA_SUFFIXES:
        return Violation(path, "メディアファイルが追跡されています",
                         f"拡張子 {suffix}")
    if suffix in DATA_SUFFIXES:
        return Violation(path, "台帳・モデル等が追跡されています",
                         f"拡張子 {suffix}")

    for pattern, label in FORBIDDEN_PATH_PATTERNS:
        if re.search(pattern, path):
            return Violation(path, f"{label}が追跡されています", pattern)
    return None


def check_content(repo_root: Path, path: str) -> list[Violation]:
    """追跡ファイルの中身に個人環境の絶対パスが無いかを検査する。"""
    if path in CONTENT_SCAN_SKIP or path in CONTENT_ALLOWLIST:
        return []
    if Path(path).suffix.lower() not in CONTENT_SCAN_SUFFIXES:
        return []

    target = repo_root / path
    try:
        if target.stat().st_size > MAX_CONTENT_BYTES:
            return []
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    found: list[Violation] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = USER_PROFILE_PATTERN.search(line)
        if match:
            found.append(Violation(
                path, "ユーザープロファイル配下の絶対パスが含まれています",
                f"{line_number} 行目: {match.group(0)}"))
            continue
        match = NON_ASCII_DRIVE_PATH_PATTERN.search(line)
        if match:
            found.append(Violation(
                path, "個人環境と思われる絶対パスが含まれています",
                f"{line_number} 行目: {match.group(0)}"))
    return found


def run_guard(repo_root: Path, *, scan_content: bool = True) -> GuardResult:
    """リポジトリ全体を検査する。**何も変更しない。**"""
    result = GuardResult()
    tracked, error = list_tracked_files(repo_root)
    if error:
        result.error = error
        return result

    result.checked_files = len(tracked)
    for path in tracked:
        violation = check_path(path)
        if violation:
            result.violations.append(violation)
            continue
        if scan_content:
            result.violations.extend(check_content(repo_root, path))
    return result


# --------------------------------------------------------------------------
# コマンドライン
# --------------------------------------------------------------------------


def find_repo_root(start: Path) -> Path:
    """app-root.marker を持つ最初の祖先を返す。無ければ start。"""
    for candidate in [start, *start.parents]:
        if (candidate / "app-root.marker").is_file():
            return candidate
    return start


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/privacy_guard.py",
        description="個人データが Git へ混入していないかを検査する（読み取り専用）",
    )
    parser.add_argument("--repo-root", default=None,
                        help="検査するリポジトリ（既定: app-root.marker のある祖先）")
    parser.add_argument("--skip-content", action="store_true",
                        help="追跡ファイルの中身の検査を省く")
    parser.add_argument("--json", action="store_true", help="結果を JSON で出す")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = (Path(args.repo_root) if args.repo_root
                 else find_repo_root(Path(__file__).resolve().parent))

    result = run_guard(repo_root, scan_content=not args.skip_content)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        print(f"検査対象   : {repo_root}")
        print(f"追跡ファイル: {result.checked_files} 件")
        if result.error:
            print(f"エラー     : {result.error}", file=sys.stderr)
        elif result.violations:
            print("")
            print(f"個人データの混入を {len(result.violations)} 件検出しました:")
            for violation in result.violations:
                print(f"  {violation.path}")
                print(f"    → {violation.reason}")
                if violation.detail:
                    print(f"      {violation.detail}")
            print("")
            print("これらのファイルを追跡対象から外してから commit してください。")
        else:
            print("個人データの混入なし: OK")

    if result.error:
        return EXIT_CANNOT_CHECK
    return EXIT_VIOLATION if result.violations else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
