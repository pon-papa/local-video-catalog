"""文字起こし（ffmpeg 内蔵 whisper フィルター）.

実測で判明した制約（ffmpeg 8.1.1-full_build）:

  1. **whisper.cpp は非 ASCII を含むパスのファイルを開けない。**
     日本語を含む絶対パスを渡すと ``failed to open`` になる。
     8.3 短縮名でも回避できなかった。
     → **ffmpeg の作業ディレクトリを APP_ROOT にし、model と出力先を
        ASCII の相対パスで渡す**ことで回避する。
     入力動画のパスは ffmpeg 自身が扱うため、日本語のままで問題ない。

     One-Folder 化により model も出力先も必ず APP_ROOT 配下へ来るので、
     ``userdata/models/whisper/...`` のような ASCII 相対で常に表せる。
     **APP_ROOT 自身が日本語を含んでいても成立する。**

  2. フィルター引数内の ``:`` は区切り文字。Windows のドライブ文字は
     エスケープが要る（ただし 1 の回避により通常は不要）。

  3. 出力は **JSON Lines**。``format=json`` でも JSON 配列にはならない。

  4. ``queue``（既定 3 秒）が転写窓のサイズ。小さいと窓が重なり
     **同じ内容が繰り返し出力される**。実測では 30 秒が良好だった。

  5. **VAD は幻覚を抑制しない。** 実測では逆に悪化した
     （無音 60 秒: 約 3 秒 → 約 598 秒、日本語 CER: 0.000 → 0.737）。
     **既定では無効。有効化しないこと。**

  6. 無音・非音声でも幻覚文を生成する。後段の正規化で印を付ける。

長時間の動画は 1 回の ffmpeg プロセスで処理しない。チャンクへ分けて
1 個ずつ処理し、**中断時の損失を最大 1 チャンクに限定する。**
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import ASR_IMPL_VERSION, paths

ENGINE_NAME = "ffmpeg-whisper-filter"

DEFAULT_LANGUAGE = "ja"
DEFAULT_QUEUE_SECONDS = 30
DEFAULT_CHUNK_DURATION_SECONDS = 300.0
DEFAULT_CHUNK_OVERLAP_SECONDS = 1.0
DEFAULT_MODEL_NAME = "ggml-large-v3-turbo-q5_0.bin"

ERROR_MODEL_MISSING = "model_missing"
ERROR_MODEL_NOT_ASCII = "model_path_not_ascii"
ERROR_FFMPEG_FAILED = "ffmpeg_failed"
ERROR_TIMEOUT = "timeout"
ERROR_NO_OUTPUT = "no_output"
ERROR_INVALID_OUTPUT = "invalid_output"

CHUNK_PENDING = "pending"
CHUNK_COMPLETED = "completed"
CHUNK_NO_SPEECH = "no_speech"
CHUNK_REUSED = "reused"
CHUNK_FAILED = "failed"


class AsrConfigurationError(Exception):
    """文字起こしを始められない。**推測で進めずに止まる。**"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class AsrConfig:
    model_name: str = DEFAULT_MODEL_NAME
    language: str = DEFAULT_LANGUAGE
    queue_seconds: int = DEFAULT_QUEUE_SECONDS
    vad_enabled: bool = False
    """**既定は無効。** 有効にすると処理時間と誤りが大きく悪化する（実測）。"""

    vad_threshold: float = 0.5
    chunk_duration_seconds: float = DEFAULT_CHUNK_DURATION_SECONDS
    chunk_overlap_seconds: float = DEFAULT_CHUNK_OVERLAP_SECONDS
    max_len: int = 0
    use_gpu: bool = True

    def validate(self) -> None:
        if not self.model_name:
            raise ValueError("model_name を指定してください。")
        if Path(self.model_name).name != self.model_name:
            raise ValueError(
                "モデルはファイル名だけで指定してください"
                "（フォルダーは userdata/models/whisper/ に固定です）。")
        if self.queue_seconds < 1:
            raise ValueError("queue_seconds は 1 以上にしてください。")
        if self.chunk_duration_seconds <= 0:
            raise ValueError("chunk_duration_seconds は 0 より大きくしてください。")
        if self.chunk_overlap_seconds < 0:
            raise ValueError("chunk_overlap_seconds は 0 以上にしてください。")
        if self.chunk_overlap_seconds >= self.chunk_duration_seconds:
            raise ValueError("chunk_overlap_seconds はチャンク長より短くしてください。")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        """**認識結果に影響する設定だけ**から決まるハッシュ。

        これが変わると既存チャンクは再処理される。
        """
        material = json.dumps({
            "impl": ASR_IMPL_VERSION,
            "language": self.language,
            "queue_seconds": self.queue_seconds,
            "vad_enabled": self.vad_enabled,
            "vad_threshold": self.vad_threshold if self.vad_enabled else None,
            "chunk_duration_seconds": self.chunk_duration_seconds,
            "chunk_overlap_seconds": self.chunk_overlap_seconds,
            "max_len": self.max_len,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @classmethod
    def from_settings(cls, raw: dict[str, Any]) -> "AsrConfig":
        section = dict(raw.get("asr") or {})
        return cls(
            model_name=str(section.get("model_name", DEFAULT_MODEL_NAME)),
            language=str(section.get("language", DEFAULT_LANGUAGE)),
            queue_seconds=int(section.get("queue_seconds", DEFAULT_QUEUE_SECONDS)),
            vad_enabled=bool(section.get("vad_enabled", False)),
            vad_threshold=float(section.get("vad_threshold", 0.5)),
            chunk_duration_seconds=float(section.get(
                "chunk_duration_seconds", DEFAULT_CHUNK_DURATION_SECONDS)),
            chunk_overlap_seconds=float(section.get(
                "chunk_overlap_seconds", DEFAULT_CHUNK_OVERLAP_SECONDS)),
            max_len=int(section.get("max_len", 0)),
            use_gpu=bool(section.get("use_gpu", True)),
        )


@dataclass(frozen=True)
class PlannedChunk:
    chunk_index: int
    absolute_start_seconds: float
    duration_seconds: float
    overlap_seconds: float


def plan_chunks(duration_seconds: float, config: AsrConfig) -> list[PlannedChunk]:
    """動画をチャンクへ分ける。

    重なりを持たせるのは、チャンク境界で語が切れるのを避けるため。
    **中断したときの損失は最大 1 チャンク。**
    """
    config.validate()
    if duration_seconds <= 0:
        return []

    chunks: list[PlannedChunk] = []
    step = config.chunk_duration_seconds - config.chunk_overlap_seconds
    position = 0.0
    index = 0
    while position < duration_seconds:
        remaining = duration_seconds - position
        length = min(config.chunk_duration_seconds, remaining)
        chunks.append(PlannedChunk(
            chunk_index=index,
            absolute_start_seconds=round(position, 3),
            duration_seconds=round(length, 3),
            overlap_seconds=config.chunk_overlap_seconds if index else 0.0))
        if length < config.chunk_duration_seconds:
            break
        position += step
        index += 1
    return chunks


# --------------------------------------------------------------------------
# 非 ASCII パス対策
# --------------------------------------------------------------------------


def model_path(config: AsrConfig) -> Path:
    """モデルの実体。**userdata/models/whisper/ に固定。**"""
    return paths.whisper_models_dir() / config.model_name


def check_model(config: AsrConfig) -> tuple[bool, str]:
    """モデルを安全に使えるか。理由つきで返す。

    条件（**どれも外せない**）:
        1. ファイル名だけで指定されていること
        2. userdata/models/whisper/ の実ファイルであること
        3. 1 MiB 以上であること（壊れたファイルを掴まない）
        4. APP_ROOT からの ASCII 相対パスで表せること
           （whisper.cpp が非 ASCII パスを開けないため）
    """
    if Path(config.model_name).name != config.model_name:
        return (False, "モデルはファイル名だけで指定してください。")

    target = model_path(config)
    if not target.is_file():
        return (False, f"{target} がありません。"
                       "userdata/models/whisper/ へモデルを置いてください。")
    if target.stat().st_size < 1024 * 1024:
        return (False, "ファイルが小さすぎます（壊れている可能性があります）。")
    if paths.to_relative_ascii(target, paths.app_root()) is None:
        return (False, "モデル名に ASCII 以外の文字が含まれています。"
                       "whisper は非 ASCII のパスを開けません。")
    return (True, "")


def escape_filter_value(value: str) -> str:
    """ffmpeg のフィルター引数用のエスケープ。

    ``:`` は区切り文字なので ``\\:`` にする（ドライブ文字対策）。
    ``\\`` は ``/`` へ寄せる。
    """
    return value.replace("\\", "/").replace(":", r"\:")


def build_whisper_filter(
    *, model_arg: str, destination_arg: str, config: AsrConfig
) -> str:
    """whisper フィルターの記述を組み立てる。

    実オプション名は ``ffmpeg -h filter=whisper`` に基づく::

        model / language / queue / use_gpu / gpu_device /
        destination / format / max_len /
        vad_model / vad_threshold / ...

    ``translate`` や ``temperature`` は **存在しない**。
    """
    parts = [
        f"model={model_arg}",
        f"language={config.language}",
        "format=json",
        f"queue={config.queue_seconds}",
        f"destination={destination_arg}",
    ]
    if config.max_len > 0:
        parts.append(f"max_len={config.max_len}")
    if not config.use_gpu:
        parts.append("use_gpu=0")
    if config.vad_enabled:
        # 既定では通らない経路。実測では有効化が悪化を招いた。
        parts.append(f"vad_threshold={config.vad_threshold}")
    return "whisper=" + ":".join(parts)


def chunk_output_directory(asset_id: str, config: AsrConfig,
                           source_fingerprint: str | None) -> Path:
    """チャンク結果の保存先。

        userdata/cache/asr/<asset_id>/<impl>/<config_hash12>/src_<fingerprint>/

    元ファイルの内容ごとに名前空間を分ける。差し替えられても新旧が
    同じ場所を奪い合わない。
    """
    namespace = f"src_{(source_fingerprint or 'unknown').split(':')[-1][:32]}"
    return (paths.asr_cache_dir() / asset_id / ASR_IMPL_VERSION
            / config.config_hash[:12] / namespace)


@dataclass
class ChunkRunResult:
    chunk: PlannedChunk
    status: str
    items: list[dict[str, Any]] = field(default_factory=list)
    processing_duration_ms: int = 0
    ffmpeg_exit_code: int | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (CHUNK_COMPLETED, CHUNK_NO_SPEECH, CHUNK_REUSED)


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    """whisper フィルターの出力（JSON Lines）を読む。

    ``format=json`` でも JSON 配列にはならないので、1 行ずつ読む。
    壊れた行は飛ばす（後段が警告として扱う）。
    """
    if not Path(path).is_file():
        return []
    items: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
        elif isinstance(parsed, list):
            items.extend(item for item in parsed if isinstance(item, dict))
    return items


def build_ffmpeg_command(
    ffmpeg_path: Path,
    source: Path,
    chunk: PlannedChunk,
    *,
    model_arg: str,
    destination_arg: str,
    config: AsrConfig,
    audio_stream_index: int | None = None,
) -> list[str]:
    """1 チャンクぶんの ffmpeg 引数。

    ``model_arg`` と ``destination_arg`` は **APP_ROOT からの ASCII 相対パス**。
    作業ディレクトリを APP_ROOT にして実行すること。
    """
    command = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{chunk.absolute_start_seconds:.3f}",
        "-t", f"{chunk.duration_seconds:.3f}",
        "-i", str(source),
    ]
    if audio_stream_index is not None:
        command += ["-map", f"0:{audio_stream_index}"]
    command += [
        "-vn", "-sn", "-dn",
        "-af", build_whisper_filter(
            model_arg=model_arg, destination_arg=destination_arg, config=config),
        "-f", "null", "-",
    ]
    return command


def run_chunk(
    ffmpeg_path: Path,
    source: Path,
    chunk: PlannedChunk,
    output_dir: Path,
    config: AsrConfig,
    *,
    audio_stream_index: int | None = None,
    timeout: int = 3600,
) -> ChunkRunResult:
    """1 チャンクを処理する。**元動画は入力としてのみ渡す。**"""
    model = model_path(config)
    model_arg = paths.to_relative_ascii(model, paths.app_root())
    if model_arg is None:
        return ChunkRunResult(
            chunk=chunk, status=CHUNK_FAILED, error_type=ERROR_MODEL_NOT_ASCII,
            error_message="モデルのパスを ASCII の相対パスで表せません。")

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"chunk_{chunk.chunk_index:06d}.jsonl"
    destination_arg = paths.to_relative_ascii(
        destination.parent, paths.app_root())
    if destination_arg is None:
        return ChunkRunResult(
            chunk=chunk, status=CHUNK_FAILED, error_type=ERROR_MODEL_NOT_ASCII,
            error_message="出力先を ASCII の相対パスで表せません。")
    destination_arg = f"{destination_arg}/{destination.name}"

    command = build_ffmpeg_command(
        ffmpeg_path, source, chunk, model_arg=escape_filter_value(model_arg),
        destination_arg=escape_filter_value(destination_arg), config=config,
        audio_stream_index=audio_stream_index)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False,
            # 非 ASCII パス対策の要。ここを APP_ROOT にすることで
            # model と destination を ASCII 相対で渡せる。
            cwd=str(paths.app_root()))
    except subprocess.TimeoutExpired:
        return ChunkRunResult(
            chunk=chunk, status=CHUNK_FAILED, error_type=ERROR_TIMEOUT,
            error_message=f"{timeout} 秒以内に終わりませんでした。")
    except OSError as exc:
        return ChunkRunResult(
            chunk=chunk, status=CHUNK_FAILED, error_type=ERROR_FFMPEG_FAILED,
            error_message=f"ffmpeg を実行できません: {exc}")

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        return ChunkRunResult(
            chunk=chunk, status=CHUNK_FAILED,
            processing_duration_ms=elapsed_ms,
            ffmpeg_exit_code=completed.returncode,
            error_type=ERROR_FFMPEG_FAILED,
            error_message=message or "ffmpeg が失敗しました。")

    items = read_json_lines(destination)
    return ChunkRunResult(
        chunk=chunk,
        status=CHUNK_COMPLETED if items else CHUNK_NO_SPEECH,
        items=items, processing_duration_ms=elapsed_ms,
        ffmpeg_exit_code=completed.returncode)
