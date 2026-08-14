"""中間ファイルを Windows のゴミ箱へ送る（標準ライブラリのみ）.

**完全削除は行わない。** ゴミ箱へ送れなかった場合はファイルを残し、
呼び出し側へ失敗として返す。消えて困るものを黙って消さないため。

Windows の ``SHFileOperationW``（shell32）を ctypes 越しに使う。
外部パッケージは導入しない。

**片付けてよい場所の判定は ``paths.is_cleanable`` が持つ。**
このモジュールは判定を自前で持たない。境界は APP_ROOT からのみ決まり、
設定値・引数・台帳の記録値では変わらない。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

CLEANUP_OK = "ok"
CLEANUP_FAILED = "failed"
CLEANUP_NOTHING = "nothing_to_clean"


class RecycleError(Exception):
    """ゴミ箱へ送れなかった。**完全削除へは進まない。**"""


@dataclass
class CleanupResult:
    moved_paths: list[Path] = field(default_factory=list)
    skipped_paths: list[Path] = field(default_factory=list)
    freed_bytes: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def status(self) -> str:
        if self.error:
            return CLEANUP_FAILED
        return CLEANUP_OK if self.moved_paths else CLEANUP_NOTHING


def directory_size(path: Path) -> int:
    total = 0
    for entry in Path(path).rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def send_to_recycle_bin(targets: list[Path]) -> None:
    """ゴミ箱へ送る。失敗したら ``RecycleError`` を投げる。

    **削除へフォールバックしない。**

    3 段階で確認する。戻り値・中断フラグ・実際に消えたかどうか。
    どれか 1 つでも駄目なら失敗として扱う。
    """
    existing = [str(Path(p).resolve()) for p in targets if Path(p).exists()]
    if not existing:
        return
    if sys.platform != "win32":
        raise RecycleError("ゴミ箱への移動は Windows でのみ対応しています。")

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040        # ← これがゴミ箱行きの指定
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    FOF_SILENT = 0x0004

    # pFrom は NUL 区切り + 末尾二重 NUL
    joined = "\0".join(existing) + "\0\0"

    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = joined
    operation.pTo = None
    operation.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION
                        | FOF_NOERRORUI | FOF_SILENT)

    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    code = shell32.SHFileOperationW(ctypes.byref(operation))

    if code != 0:
        raise RecycleError(
            f"ゴミ箱へ移動できませんでした（SHFileOperation コード {code}）。"
            "ファイルはそのまま残しています。")
    if operation.fAnyOperationsAborted:
        raise RecycleError(
            "ゴミ箱への移動が中断されました。ファイルはそのまま残しています。")

    remaining = [t for t in existing if os.path.exists(t)]
    if remaining:
        raise RecycleError(
            f"{len(remaining)} 件がゴミ箱へ移動できていません。"
            "ファイルはそのまま残しています。")


def cleanup_intermediate_cache(
    asset_id: str, *, dry_run: bool = False
) -> CleanupResult:
    """1 本ぶんの中間キャッシュをゴミ箱へ送る。

    対象は ``paths.is_cleanable`` が True を返す場所だけ。
    元動画・台帳・最終テキスト・HTML カタログ・モデル・ログ・
    probe キャッシュは対象外。

    **呼ぶ側の責任**: 最終テキストが正常に出来た動画にだけ使うこと。
    処理中・失敗した動画のキャッシュを消すと Resume が壊れる。

    失敗しても完全削除はせず、ファイルを残したまま error を返す。
    """
    result = CleanupResult()
    targets: list[Path] = []

    for candidate in paths.cache_directories_for_asset(asset_id):
        # 二重の確認。cache_directories_for_asset は APP_ROOT から
        # 組み立てているが、判定はもう一度通す。
        if not paths.is_cleanable(candidate):
            result.skipped_paths.append(candidate)
            continue
        targets.append(candidate)
        result.freed_bytes += directory_size(candidate)

    if not targets:
        result.freed_bytes = 0
        return result

    if dry_run:
        result.moved_paths = targets
        return result

    try:
        send_to_recycle_bin(targets)
    except RecycleError as exc:
        result.error = str(exc)
        result.freed_bytes = 0
        return result

    result.moved_paths = targets
    return result
