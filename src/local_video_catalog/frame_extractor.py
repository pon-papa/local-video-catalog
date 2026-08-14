"""代表画像の抽出（工程 2/5）.

**元動画は ffmpeg の入力としてのみ渡す。** 書き込み・改名・移動をしない。
出力先は必ず ``userdata/cache/frames/`` 配下。

抽出時刻は決定的に決める。同じ動画・同じ設定なら常に同じ時刻の画像を作る。
そうでないと再利用の判定ができない。
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import FRAME_EXTRACTION_IMPL_VERSION, paths

DEFAULT_TARGET_INTERVAL_SECONDS = 30.0
DEFAULT_MINIMUM_FRAME_COUNT = 6
DEFAULT_MAXIMUM_FRAME_COUNT = 24
"""上限 24 枚。

映像の視覚概要は 1 枚あたり約 16 秒かかる（実測）。24 枚で約 390 秒。
これ以上増やすと 1 本あたりの待ち時間が現実的でなくなる。
"""

DEFAULT_EDGE_MARGIN_SECONDS = 1.0
DEFAULT_MAXIMUM_IMAGE_DIMENSION = 768
DEFAULT_JPEG_QUALITY = 2

TAIL_GUARD_MILLISECONDS = 100
"""末尾からこれだけ手前までを抽出対象にする。

再生時間ちょうどの 1ms 手前を指定すると、**復号できる最終フレームを
越えてしまい、ffmpeg が画像を出さない**。フレームレートが低い動画ほど
起こりやすく、短い動画では最後の 1 枚が必ず失敗していた。

0.1 秒は 10fps でも 1 フレームぶんにあたる。
"""

IMAGE_FORMAT = "jpeg"

FRAME_OK = "ok"
FRAME_FAILED = "failed"
FRAME_REUSED = "reused"

SKIP_NO_PLAYABLE_VIDEO = "no_playable_video"
SKIP_NO_DURATION = "no_duration"
SKIP_NO_PROBE = "no_probe_result"
SKIP_SOURCE_MISSING = "source_file_missing"


class ExtractionNotPossible(Exception):
    """抽出できない。**理由を持って止まる。**"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ExtractionConfig:
    target_interval_seconds: float = DEFAULT_TARGET_INTERVAL_SECONDS
    minimum_frame_count: int = DEFAULT_MINIMUM_FRAME_COUNT
    maximum_frame_count: int = DEFAULT_MAXIMUM_FRAME_COUNT
    edge_margin_seconds: float = DEFAULT_EDGE_MARGIN_SECONDS
    maximum_image_dimension: int = DEFAULT_MAXIMUM_IMAGE_DIMENSION
    jpeg_quality: int = DEFAULT_JPEG_QUALITY

    def validate(self) -> None:
        if self.target_interval_seconds <= 0:
            raise ValueError("target_interval_seconds は 0 より大きくしてください。")
        if self.minimum_frame_count < 1:
            raise ValueError("minimum_frame_count は 1 以上にしてください。")
        if self.maximum_frame_count < self.minimum_frame_count:
            raise ValueError(
                "maximum_frame_count は minimum_frame_count 以上にしてください。")
        if self.edge_margin_seconds < 0:
            raise ValueError("edge_margin_seconds は 0 以上にしてください。")
        if self.maximum_image_dimension < 2:
            raise ValueError("maximum_image_dimension は 2 以上にしてください。")
        if not (1 <= self.jpeg_quality <= 31):
            raise ValueError("jpeg_quality は 1〜31 にしてください（小さいほど高品質）。")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        """設定と実装バージョンから決まる安定したハッシュ。

        設定が 1 つでも違えば別の抽出結果になるため、再利用の判定と
        出力先の分離に使う。
        """
        material = json.dumps(
            {"impl": FRAME_EXTRACTION_IMPL_VERSION, **self.to_dict()},
            ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @classmethod
    def from_settings(cls, raw: dict[str, Any]) -> "ExtractionConfig":
        section = dict(raw.get("frames") or {})
        return cls(
            target_interval_seconds=float(section.get(
                "target_interval_seconds", DEFAULT_TARGET_INTERVAL_SECONDS)),
            minimum_frame_count=int(section.get(
                "minimum_frame_count", DEFAULT_MINIMUM_FRAME_COUNT)),
            maximum_frame_count=int(section.get(
                "maximum_frame_count", DEFAULT_MAXIMUM_FRAME_COUNT)),
            edge_margin_seconds=float(section.get(
                "edge_margin_seconds", DEFAULT_EDGE_MARGIN_SECONDS)),
            maximum_image_dimension=int(section.get(
                "maximum_image_dimension", DEFAULT_MAXIMUM_IMAGE_DIMENSION)),
            jpeg_quality=int(section.get("jpeg_quality", DEFAULT_JPEG_QUALITY)),
        )


@dataclass(frozen=True)
class PlannedFrame:
    sequence_index: int
    target_time_seconds: float
    target_time_milliseconds: int
    relative_position: float


def decide_frame_count(duration_seconds: float, config: ExtractionConfig) -> int:
    """再生時間から枚数を決める。短い動画でも下限、長い動画でも上限で止める。"""
    if duration_seconds <= 0:
        return 0
    wanted = math.ceil(duration_seconds / config.target_interval_seconds)
    return max(config.minimum_frame_count,
               min(config.maximum_frame_count, wanted))


def usable_range(duration_seconds: float,
                 config: ExtractionConfig) -> tuple[float, float]:
    """端の余白を除いた抽出可能範囲。

    冒頭・末尾の暗転や無地を拾わないための余白。動画が短くて余白を
    取れない場合は、全体を使う。
    """
    margin = config.edge_margin_seconds
    if duration_seconds <= margin * 2:
        return (0.0, duration_seconds)
    return (margin, duration_seconds - margin)


def plan_frames(duration_seconds: float,
                config: ExtractionConfig) -> list[PlannedFrame]:
    """抽出予定時刻を**決定的に**生成する。

    利用可能範囲へ両端を含めて均等配置する::

        t_i = start + (end - start) * i / (count - 1)

    これで「冒頭付近・中盤・終盤付近」が必ず含まれる。1 枚なら中央 1 点。
    ミリ秒へ丸めたあと重複を取り除く（非常に短い動画で起こる）。
    """
    config.validate()
    if duration_seconds <= 0:
        return []

    count = decide_frame_count(duration_seconds, config)
    start, end = usable_range(duration_seconds, config)

    raw_times: list[float] = []
    if count <= 1 or end <= start:
        raw_times.append((start + end) / 2.0)
    else:
        span = end - start
        raw_times = [start + span * i / (count - 1) for i in range(count)]

    limit_ms = int(duration_seconds * 1000)
    # 末尾ぎりぎりを指すと最終フレームを越えて取りこぼす。手前で止める。
    safe_limit_ms = max(0, limit_ms - TAIL_GUARD_MILLISECONDS)
    seen: set[int] = set()
    frames: list[PlannedFrame] = []
    for seconds in raw_times:
        clamped = min(max(seconds, 0.0), duration_seconds)
        ms = int(round(clamped * 1000))
        if ms > safe_limit_ms:
            ms = safe_limit_ms
        if ms in seen:
            continue
        seen.add(ms)
        frames.append(PlannedFrame(
            sequence_index=len(frames) + 1,
            target_time_seconds=round(ms / 1000.0, 3),
            target_time_milliseconds=ms,
            relative_position=round(
                (ms / 1000.0) / duration_seconds if duration_seconds else 0.0, 6),
        ))
    return frames


def output_directory(asset_id: str, config: ExtractionConfig) -> Path:
    """代表画像の保存先。

        userdata/cache/frames/<asset_id>/<impl>/<config_hash の先頭12桁>/

    実装バージョンと設定ハッシュで階層を分けるので、同じ動画でも設定が
    違えば混ざらない。**必ず APP_ROOT 配下。**
    """
    return (paths.frames_cache_dir() / asset_id
            / FRAME_EXTRACTION_IMPL_VERSION / config.config_hash[:12])


def frame_file_name(frame: PlannedFrame) -> str:
    return f"frame_{frame.sequence_index:04d}_{frame.target_time_milliseconds:09d}ms.jpg"


def build_ffmpeg_command(
    ffmpeg_path: Path,
    source: Path,
    frame: PlannedFrame,
    target: Path,
    config: ExtractionConfig,
    *,
    stream_index: int | None = None,
) -> list[str]:
    """1 枚ぶんの ffmpeg 引数。

    ``-ss`` を入力の前に置いて高速シークする。``-map`` で主映像を明示し、
    カバーアートを掴まないようにする。
    """
    scale = (f"scale='min({config.maximum_image_dimension},iw)':-2"
             ":force_original_aspect_ratio=decrease")
    command = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{frame.target_time_seconds:.3f}",
        "-i", str(source),
    ]
    if stream_index is not None:
        command += ["-map", f"0:{stream_index}"]
    command += [
        "-frames:v", "1", "-an", "-sn", "-dn",
        "-vf", scale,
        "-q:v", str(config.jpeg_quality),
        str(target),
    ]
    return command


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_one(
    ffmpeg_path: Path,
    source: Path,
    frame: PlannedFrame,
    target: Path,
    config: ExtractionConfig,
    *,
    stream_index: int | None = None,
    timeout: int = 120,
) -> tuple[bool, int | None, str]:
    """1 枚抽出する。``(成功したか, ffmpeg の終了コード, エラー文)``。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    command = build_ffmpeg_command(
        ffmpeg_path, source, frame, target, config, stream_index=stream_index)
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return (False, None, f"{timeout} 秒以内に終わりませんでした。")
    except OSError as exc:
        return (False, None, f"ffmpeg を実行できません: {exc}")

    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        return (False, completed.returncode, message or "ffmpeg が失敗しました。")
    if not target.is_file() or target.stat().st_size == 0:
        return (False, completed.returncode, "画像が作られませんでした。")
    return (True, completed.returncode, "")
