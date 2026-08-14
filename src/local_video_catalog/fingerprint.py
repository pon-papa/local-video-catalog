"""quick fingerprint と完全 SHA-256.

**全動画へ毎回完全 SHA-256 を実行しない。** 動画のコーパスは数百 GB に
なりうる。「変わっていないことを確認する」ためだけに毎回全読みするのは、
外付けドライブへの負荷が大きく割に合わない。

二段構えにする。

  file_fingerprint  ffprobe より前に計算できる部分
                    = ファイルサイズ + 先頭 1MiB の SHA-256 + 末尾 1MiB の SHA-256
                    変更検出（再処理が要るか）の判定に使う。

  quick_fingerprint file_fingerprint に ffprobe 由来の情報を加えたもの
                    = 上記 + 再生時間 + 映像／音声ストリーム構成
                    台帳上の同一性判定（移動の追跡）と、各工程の
                    再利用キーに使う。

ファイルが head_bytes + tail_bytes 未満なら、同じ範囲を二重に読まない。

**元ファイルは必ず読み取り専用（"rb"）で開く。** 書き込みモードでは開かない。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import FINGERPRINT_IMPL_VERSION

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_FULL_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class FileFingerprint:
    """ffprobe 前に計算できるファイル同一性の材料。"""

    size: int
    head_sha256: str
    tail_sha256: str
    head_bytes_read: int
    tail_bytes_read: int
    whole_file_read: bool
    impl_version: int = FINGERPRINT_IMPL_VERSION

    @property
    def value(self) -> str:
        """安定した文字列表現を SHA-256 で畳んだ値。"""
        material = "|".join([
            f"v{self.impl_version}",
            f"size={self.size}",
            f"head={self.head_sha256}",
            f"tail={self.tail_sha256}",
        ])
        return "ffp1:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_file_fingerprint(
    path: Path,
    *,
    head_bytes: int = 1024 * 1024,
    tail_bytes: int = 1024 * 1024,
    size: int | None = None,
) -> FileFingerprint:
    """ファイルの先頭・末尾を読んで file_fingerprint を作る。

    head + tail 未満のファイルでは全体を 1 回だけ読み、先頭・末尾ハッシュの
    双方をそのバイト列から求める（二重読み込みをしない）。
    """
    path = Path(path)
    actual_size = path.stat().st_size if size is None else size

    if actual_size == 0:
        return FileFingerprint(
            size=0, head_sha256=_EMPTY_SHA256, tail_sha256=_EMPTY_SHA256,
            head_bytes_read=0, tail_bytes_read=0, whole_file_read=True)

    with open(path, "rb") as handle:
        if actual_size <= head_bytes + tail_bytes:
            data = handle.read()
            digest = hashlib.sha256(data).hexdigest()
            return FileFingerprint(
                size=actual_size, head_sha256=digest, tail_sha256=digest,
                head_bytes_read=len(data), tail_bytes_read=0,
                whole_file_read=True)

        head = handle.read(head_bytes)
        handle.seek(-tail_bytes, 2)          # ファイル末尾から
        tail = handle.read(tail_bytes)

    return FileFingerprint(
        size=actual_size,
        head_sha256=hashlib.sha256(head).hexdigest(),
        tail_sha256=hashlib.sha256(tail).hexdigest(),
        head_bytes_read=len(head), tail_bytes_read=len(tail),
        whole_file_read=False)


def stream_signature(
    duration_seconds: float | None,
    video_codec: str | None,
    width: int | None,
    height: int | None,
    frame_rate_num: int | None,
    frame_rate_den: int | None,
    audio_codec: str | None,
    sample_rate: int | None,
    channel_count: int | None,
) -> str:
    """ffprobe 由来のストリーム構成を安定した文字列にする。

    duration は小数第 3 位で丸める。ffprobe の桁揺れで fingerprint が
    変わり、解析結果を無駄に作り直さないため。
    """
    duration_text = "na" if duration_seconds is None else f"{duration_seconds:.3f}"
    video_part = "|".join([
        f"vc={video_codec or 'na'}",
        f"w={width if width is not None else 'na'}",
        f"h={height if height is not None else 'na'}",
        f"fr={frame_rate_num if frame_rate_num is not None else 'na'}"
        f"/{frame_rate_den if frame_rate_den is not None else 'na'}",
    ])
    audio_part = "|".join([
        f"ac={audio_codec or 'na'}",
        f"sr={sample_rate if sample_rate is not None else 'na'}",
        f"ch={channel_count if channel_count is not None else 'na'}",
    ])
    return f"dur={duration_text}|{video_part}|{audio_part}"


def compute_quick_fingerprint(
    file_fingerprint: FileFingerprint, signature: str
) -> str:
    """file_fingerprint と ffprobe 由来のストリーム構成を合成する。"""
    material = "|".join([
        f"v{FINGERPRINT_IMPL_VERSION}",
        f"size={file_fingerprint.size}",
        f"head={file_fingerprint.head_sha256}",
        f"tail={file_fingerprint.tail_sha256}",
        signature,
    ])
    return "qfp1:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_full_sha256(path: Path, *, chunk_size: int = _FULL_HASH_CHUNK) -> str:
    """ファイル全体の SHA-256。

    **明示的に要求されたときだけ呼ぶこと。** 数百 GB のコーパスへ
    無条件に適用してはならない。
    """
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
