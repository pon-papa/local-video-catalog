"""ffprobe の実行と結果の解析.

**元動画は読み取り専用の入力としてのみ渡す。** ffprobe は書き込みをしない。

主映像・主音声ストリームの選び方が要。カバーアート（サムネイル）を
本編と取り違えると、解像度もコーデックも誤って記録され、代表画像も
静止画 1 枚から作られてしまう。
"""

from __future__ import annotations

import gzip
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import FFPROBE_IMPL_VERSION
from . import process_utils

# 主映像の選択理由
RULE_NO_VIDEO_STREAM = "no_video_stream"
RULE_NO_PLAYABLE_VIDEO = "no_playable_video"
RULE_SOLE_PLAYABLE = "sole_playable"
RULE_DEFAULT_DISPOSITION = "default_disposition"
RULE_LARGEST_AREA = "largest_area"
RULE_LOWEST_INDEX = "lowest_index"

PROBE_OK = "ok"
PROBE_FAILED = "failed"

ERROR_TIMEOUT = "timeout"
ERROR_FFPROBE_FAILED = "ffprobe_failed"
ERROR_INVALID_JSON = "invalid_json"
ERROR_NOT_FOUND = "not_found"


class ProbeError(Exception):
    def __init__(self, error_type: str, message: str,
                 exit_code: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# ストリームの選択
# --------------------------------------------------------------------------


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None       # NaN を除く


def is_attached_picture(stream: dict[str, Any]) -> bool:
    """カバーアート（埋め込みサムネイル）か。

    音楽ファイルやスマートフォンの動画には、本編とは別にカバーアートが
    映像ストリームとして入っていることがある。**本編ではない。**
    """
    disposition = stream.get("disposition") or {}
    return _to_int(disposition.get("attached_pic")) == 1


def _is_default(stream: dict[str, Any]) -> bool:
    disposition = stream.get("disposition") or {}
    return _to_int(disposition.get("default")) == 1


def _area(stream: dict[str, Any]) -> int:
    width = _to_int(stream.get("width")) or 0
    height = _to_int(stream.get("height")) or 0
    return width * height


def select_primary_video_stream(
    video_streams: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """主映像ストリームを**決定的に**選ぶ。

    規則:
        1. ``disposition.attached_pic == 1`` を候補から除外
        2. 残りが 1 本ならそれ
        3. 複数なら default → 面積最大 → index 最小 の順で絞る

    同じ ffprobe 出力からは常に同じストリームを選ぶ。

    通常映像が 1 本も無い場合、**カバーアートを主映像として採用しない。**
    空の辞書と理由を返す。
    """
    if not video_streams:
        return ({}, RULE_NO_VIDEO_STREAM)

    candidates = [s for s in video_streams if not is_attached_picture(s)]
    if not candidates:
        # 映像ストリームはあるが、すべてカバーアート。
        # 再生できる映像は無い。カバーアートで代用しない。
        return ({}, RULE_NO_PLAYABLE_VIDEO)
    if len(candidates) == 1:
        return (candidates[0], RULE_SOLE_PLAYABLE)

    defaults = [s for s in candidates if _is_default(s)]
    if len(defaults) == 1:
        return (defaults[0], RULE_DEFAULT_DISPOSITION)
    pool = defaults if defaults else candidates

    largest = max(_area(s) for s in pool)
    biggest = [s for s in pool if _area(s) == largest]
    if len(biggest) == 1:
        return (biggest[0], RULE_LARGEST_AREA)

    chosen = min(biggest, key=lambda s: _to_int(s.get("index")) or 0)
    return (chosen, RULE_LOWEST_INDEX)


def select_primary_audio_stream(
    audio_streams: list[dict[str, Any]],
) -> dict[str, Any]:
    """主音声ストリームを決定的に選ぶ。

    default → チャンネル数最大 → index 最小。文字起こしはこの 1 本だけを使う。
    """
    if not audio_streams:
        return {}
    defaults = [s for s in audio_streams if _is_default(s)]
    pool = defaults if defaults else audio_streams
    most_channels = max((_to_int(s.get("channels")) or 0) for s in pool)
    best = [s for s in pool if (_to_int(s.get("channels")) or 0) == most_channels]
    return min(best, key=lambda s: _to_int(s.get("index")) or 0)


def parse_frame_rate(text: str | None) -> tuple[int | None, int | None, float | None]:
    """``30000/1001`` のような表記を分子・分母・小数へ分解する。"""
    if not text or "/" not in str(text):
        return (None, None, None)
    numerator, _, denominator = str(text).partition("/")
    num = _to_int(numerator)
    den = _to_int(denominator)
    if num is None or den is None or den == 0:
        return (num, den, None)
    return (num, den, num / den)


# --------------------------------------------------------------------------
# 結果
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """ffprobe の結果を台帳の列へそのまま渡せる形にしたもの。"""

    values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return self.values.get("probe_status") == PROBE_OK

    @property
    def duration_seconds(self) -> float | None:
        return self.values.get("duration_seconds")

    @property
    def has_playable_video(self) -> bool:
        return bool(self.values.get("playable_video_stream_count"))

    @property
    def has_audio(self) -> bool:
        return bool(self.values.get("audio_stream_count"))


def analyse(raw: dict[str, Any]) -> dict[str, Any]:
    """ffprobe の JSON から台帳の列を組み立てる。

    **ここは純粋関数。** 生 JSON のキャッシュさえ残っていれば、
    元動画を読み直さずに解析規則の更新を適用できる。
    """
    streams = raw.get("streams") or []
    fmt = raw.get("format") or {}

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    attached = [s for s in video_streams if is_attached_picture(s)]

    primary_video, rule = select_primary_video_stream(video_streams)
    primary_audio = select_primary_audio_stream(audio_streams)

    frame_rate = (primary_video.get("avg_frame_rate")
                  or primary_video.get("r_frame_rate"))
    fr_num, fr_den, fr_decimal = parse_frame_rate(frame_rate)

    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    location_present = any(
        key in tags for key in ("location", "com.apple.quicktime.location.iso6709"))

    return {
        "probe_status": PROBE_OK,
        "duration_seconds": _to_float(fmt.get("duration")),
        "format_name": fmt.get("format_name"),
        "format_long_name": fmt.get("format_long_name"),
        "bit_rate": _to_int(fmt.get("bit_rate")),
        "video_stream_count": len(video_streams),
        "playable_video_stream_count": len(video_streams) - len(attached),
        "attached_picture_stream_count": len(attached),
        "primary_video_stream_index": _to_int(primary_video.get("index")),
        "primary_video_selection_rule": rule,
        "audio_stream_count": len(audio_streams),
        "primary_audio_stream_index": _to_int(primary_audio.get("index")),
        "subtitle_stream_count": len(subtitle_streams),
        "chapter_count": len(raw.get("chapters") or []),
        "width": _to_int(primary_video.get("width")),
        "height": _to_int(primary_video.get("height")),
        "video_codec": primary_video.get("codec_name"),
        "pixel_format": primary_video.get("pix_fmt"),
        "frame_rate_num": fr_num,
        "frame_rate_den": fr_den,
        "frame_rate_decimal": fr_decimal,
        "audio_codec": primary_audio.get("codec_name"),
        "sample_rate": _to_int(primary_audio.get("sample_rate")),
        "channel_count": _to_int(primary_audio.get("channels")),
        "creation_time_tag": tags.get("creation_time"),
        "location_tag_present": 1 if location_present else 0,
        "ffprobe_impl_version": FFPROBE_IMPL_VERSION,
    }


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------


def run_ffprobe(
    ffprobe_path: Path, video_path: Path, *, timeout: int = 60
) -> dict[str, Any]:
    """ffprobe を実行して生 JSON を返す。**動画は読むだけ。**"""
    command = [
        str(ffprobe_path), "-hide_banner", "-loglevel", "error",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
        str(video_path),
    ]
    try:
        # process_utils を通すのは、画面から呼ばれたときにコンソール窓を
        # 作らせないため。ffprobe は動画 1 本ごとに起動するので、
        # 素で起動すると窓が何百回も明滅する。
        completed = process_utils.run(command, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(
            ERROR_TIMEOUT,
            f"ffprobe が {timeout} 秒以内に終わりませんでした。") from exc
    except OSError as exc:
        raise ProbeError(ERROR_NOT_FOUND, f"ffprobe を実行できません: {exc}") from exc

    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ProbeError(
            ERROR_FFPROBE_FAILED,
            message or f"ffprobe が終了コード {completed.returncode} で失敗しました。",
            exit_code=completed.returncode)

    try:
        return json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(
            ERROR_INVALID_JSON, f"ffprobe の出力を解釈できません: {exc}") from exc


def write_raw_cache(raw: dict[str, Any], target: Path, *, gzip_enabled: bool = True) -> Path:
    """生 JSON を保存する。**原子的に書く。**

    これがあれば、解析規則を更新したときに動画を読み直さずに
    列を作り直せる。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    temp = target.with_suffix(target.suffix + ".tmp")
    if gzip_enabled:
        with gzip.open(temp, "wb") as handle:
            handle.write(payload)
    else:
        temp.write_bytes(payload)
    temp.replace(target)
    return target


def read_raw_cache(path: Path) -> dict[str, Any] | None:
    """保存済みの生 JSON を読む。壊れていれば None（再取得へ回す）。"""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                return json.loads(handle.read().decode("utf-8"))
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, gzip.BadGzipFile):
        return None


def probe(
    ffprobe_path: Path,
    video_path: Path,
    *,
    timeout: int = 60,
    cache_path: Path | None = None,
    gzip_enabled: bool = True,
) -> ProbeResult:
    """1 本ぶんの ffprobe を実行し、台帳へ渡す値を作る。

    失敗しても例外を投げず、``probe_status = failed`` の値を返す。
    1 本の失敗で全体を止めないため。
    """
    try:
        raw = run_ffprobe(ffprobe_path, video_path, timeout=timeout)
    except ProbeError as exc:
        return ProbeResult(values={
            "probe_status": PROBE_FAILED,
            "error_type": exc.error_type,
            "error_message": str(exc),
            "exit_code": exc.exit_code,
            "ffprobe_impl_version": FFPROBE_IMPL_VERSION,
        })

    values = analyse(raw)
    if cache_path is not None:
        try:
            written = write_raw_cache(raw, cache_path, gzip_enabled=gzip_enabled)
            values["raw_probe_cache_path"] = written
        except OSError:
            # キャッシュに失敗しても解析結果は使える
            pass
    return ProbeResult(values=values, raw=raw)
