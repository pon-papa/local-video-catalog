"""解析対象フォルダーから動画を列挙する.

**読み取りだけ。** 元動画フォルダーへは何も書かない。何も作らない。

除外パターンはフォルダー名・ファイル名に対する fnmatch。
既定では隠しフォルダー・ごみ箱・システムフォルダーを避ける。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .source_ref import SourceRef


@dataclass(frozen=True)
class DiscoveredFile:
    """見つかった動画 1 件。**まだ台帳へは入っていない。**"""

    source: SourceRef
    size: int
    creation_time_fs: str | None
    last_write_time_fs: str | None

    @property
    def path(self) -> Path:
        return self.source.absolute


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value).astimezone().isoformat(
            timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def is_excluded(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def discover(
    source_root: Path | str,
    *,
    extensions: tuple[str, ...],
    exclude_patterns: tuple[str, ...] = (),
    recursive: bool = False,
    min_size_bytes: int = 0,
    follow_symlinks: bool = False,
) -> Iterator[DiscoveredFile]:
    """対象フォルダーの動画を列挙する。

    ``recursive`` が False なら直下だけを見る。
    シンボリックリンクは既定で辿らない（ループと、対象外フォルダーへの
    はみ出しを避けるため）。
    """
    root = Path(source_root).resolve()
    if not root.is_dir():
        return

    for directory, subdirectories, file_names in os.walk(
        root, followlinks=follow_symlinks
    ):
        subdirectories[:] = [
            name for name in sorted(subdirectories)
            if not is_excluded(name, exclude_patterns)
        ]
        if not recursive:
            subdirectories[:] = []

        for name in sorted(file_names):
            if is_excluded(name, exclude_patterns):
                continue
            if Path(name).suffix.lower() not in extensions:
                continue

            full = Path(directory) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            if stat.st_size < min_size_bytes:
                continue

            try:
                source = SourceRef.from_absolute(full, root)
            except Exception:
                # 対象フォルダーの外へ出るもの（リンク越しなど）は取り込まない
                continue

            yield DiscoveredFile(
                source=source,
                size=stat.st_size,
                creation_time_fs=_timestamp(getattr(stat, "st_ctime", None)),
                last_write_time_fs=_timestamp(stat.st_mtime),
            )


def count(source_root: Path | str, **kwargs: object) -> int:
    return sum(1 for _ in discover(source_root, **kwargs))  # type: ignore[arg-type]
