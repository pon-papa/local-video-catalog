"""設定の読み込み・検証・既定値.

優先順位（後のものが前を上書きする。``None`` は無視して下位を残す）:

    1. 本モジュールの DEFAULT_SETTINGS
    2. userdata\\config\\settings.json （存在すれば）
    3. --config で指定されたファイル
    4. コマンドライン引数

**保存先は設定に書かない。** 台帳・キャッシュ・ログ・説明文の位置は
``paths`` が APP_ROOT から導出する。設定で保存先を上書きできるように
すると、cleanup（消す機能）の基点を設定値へ委ねることになり、設定が
壊れたときに被害が外へ広がる。

設定に書くのは **APP_ROOT の外にあるもの**（元動画フォルダー・ffmpeg・
LM Studio の URL）と、**動作のパラメーター**だけである。

ffmpeg / ffprobe は PATH に頼らず、設定された絶対パスを優先する。
1 台の PC に複数の ffmpeg があり、**whisper フィルターを持つのは
片方だけ**ということが起こるため。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths, process_utils

# --------------------------------------------------------------------------
# 既定値
# --------------------------------------------------------------------------

DEFAULT_EXTENSIONS = [
    ".mp4", ".m4v", ".mov", ".mts", ".m2ts", ".avi",
    ".mkv", ".mpg", ".mpeg", ".vob", ".webm", ".ts",
]

DEFAULT_EXCLUDE_PATTERNS = [
    ".*",
    "$RECYCLE.BIN",
    "System Volume Information",
    "__pycache__",
]

DEFAULT_VLM_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_VISUAL_MODEL = "qwen3-vl-8b-instruct"
DEFAULT_WHISPER_MODEL = "ggml-large-v3-turbo-q5_0.bin"

DEFAULT_SETTINGS: dict[str, Any] = {
    "config_version": 1,

    # -- 外部入力（APP_ROOT の外）-----------------------------------------
    "source_path": None,
    "ffmpeg_path": None,
    "ffprobe_path": None,

    # -- 列挙 --------------------------------------------------------------
    "recursive": False,
    "workers": 8,
    "extensions": DEFAULT_EXTENSIONS,
    "exclude_patterns": DEFAULT_EXCLUDE_PATTERNS,
    "min_size_bytes": 0,
    "follow_symlinks": False,

    # -- 解析 --------------------------------------------------------------
    "ffprobe_timeout_sec": 60,
    "full_hash": False,
    "resume": True,
    "fingerprint": {
        "head_bytes": 1024 * 1024,
        "tail_bytes": 1024 * 1024,
    },
    "probe_cache": {
        "enabled": True,
        "gzip": True,
    },

    # -- 映像解析（ローカル VLM）------------------------------------------
    "vlm": {
        "base_url": DEFAULT_VLM_BASE_URL,
        "model_match": DEFAULT_VISUAL_MODEL,
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens_per_frame": 1000,
        "max_tokens_summary": 1600,
        # フレーム 1 枚あたりの待ち時間（実測 20〜40 秒）
        "timeout_seconds": 300,
        # 視覚概要の待ち時間。フレーム枚数に比例して伸びる（実測 約16秒/枚）。
        # 24 枚なら 390 秒前後になり、300 秒では足りない。
        # **待ち時間は生成内容を変えないので config_hash には含めない。**
        "summary_timeout_seconds": 1200,
        # VRAM の少ない環境で同時送信しない
        "maximum_concurrent_requests": 1,
    },

    # -- 説明文（文章だけ。画像は送らない）--------------------------------
    "description": {
        # 空なら「映像解析と同じモデル」
        "model_match": "",
    },

    # -- 文字起こし（ローカル ASR）----------------------------------------
    "asr": {
        "model_name": DEFAULT_WHISPER_MODEL,
        "language": "ja",
        # 転写窓。既定 3 秒だと窓が重なり同じ内容が繰り返し出るため 30。
        "queue_seconds": 30,
        # **VAD は既定で無効。**
        # 実測では有効時に無音 60 秒の処理が 3 秒 → 598 秒へ悪化し、
        # 日本語 CER が 0.000 → 0.737 へ悪化した。幻覚も抑制されなかった。
        "vad_enabled": False,
        "vad_threshold": 0.5,
        # 長時間動画をこの長さへ分割する。中断時の損失を 1 チャンクに限定する。
        "chunk_duration_seconds": 300,
        "chunk_overlap_seconds": 1.0,
        "max_len": 0,
        "use_gpu": True,
    },

    # -- 実行条件 ----------------------------------------------------------
    "run": {
        "time_budget_minutes": 60,
        "max_videos": 0,
        # **内部専用。** 画面からは変えられない（映像の解析は必須工程）。
        "skip_visual_analysis": False,
        "skip_transcription": False,
        "recycle_cache": False,
        # 同じ設備側の故障が続いたら安全停止する本数。
        # 1 本目は個別の動画の問題かもしれず、2 本目でも偶然が残るが、
        # 成功を 1 本も挟まずに 3 本続けて同じ種類で落ちるのは設備側の問題。
        "consecutive_failure_limit": 3,
    },
}

MAX_WORKERS = 32
MIN_WORKERS = 1

REMOVED_KEYS = ("data_root",)
"""受け付けない設定キー。

``data_root`` は旧個人版にあったが、一般配布版では**保存先を設定で
変えられてはいけない**。cleanup の基点が設定値になると、設定が壊れた
ときに消す範囲が外へ広がる。書かれていても黙って無視する
（エラーにはしない。古い設定ファイルを持ち込んだ利用者を止めないため）。
"""


class ConfigError(Exception):
    """設定が不正、または必須の外部ツールが見つからない。"""


# --------------------------------------------------------------------------
# 設定オブジェクト
# --------------------------------------------------------------------------


@dataclass
class Settings:
    """検証済みの実行設定。

    **保存先を持たない。** 保存先が必要なら ``paths`` を直接呼ぶ。
    ここに ``data_root`` 相当を置くと、また設定で上書きしたくなる。
    """

    source_path: Path | None
    ffmpeg_path: Path | None
    ffprobe_path: Path
    recursive: bool
    workers: int
    extensions: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    min_size_bytes: int
    follow_symlinks: bool
    ffprobe_timeout_sec: int
    full_hash: bool
    resume: bool
    head_bytes: int
    tail_bytes: int
    probe_cache_enabled: bool
    probe_cache_gzip: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- 便利な参照 -------------------------------------------------------

    @property
    def vlm(self) -> dict[str, Any]:
        return dict(self.raw.get("vlm") or {})

    @property
    def asr(self) -> dict[str, Any]:
        return dict(self.raw.get("asr") or {})

    @property
    def run(self) -> dict[str, Any]:
        return dict(self.raw.get("run") or {})

    def config_snapshot(self) -> dict[str, Any]:
        """台帳の processing_runs へ保存する設定スナップショット。

        **APP_ROOT の絶対パスを含めない。** フォルダーごと移動しても
        過去の実行記録が古い場所を指したままにならないようにする。
        認証情報の類は元々保持していない。
        """
        return {
            "source_path": str(self.source_path) if self.source_path else None,
            "ffprobe_path": str(self.ffprobe_path),
            "ffmpeg_path": str(self.ffmpeg_path) if self.ffmpeg_path else None,
            "recursive": self.recursive,
            "workers": self.workers,
            "extensions": list(self.extensions),
            "exclude_patterns": list(self.exclude_patterns),
            "min_size_bytes": self.min_size_bytes,
            "ffprobe_timeout_sec": self.ffprobe_timeout_sec,
            "full_hash": self.full_hash,
            "resume": self.resume,
            "head_bytes": self.head_bytes,
            "tail_bytes": self.tail_bytes,
        }


# --------------------------------------------------------------------------
# 読み込み
# --------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """辞書を再帰的にマージする（overlay 優先）。``None`` は無視する。"""
    result = dict(base)
    for key, value in overlay.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"設定ファイルの JSON が不正です: {path} ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"設定ファイルを読めません: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"設定ファイルのトップレベルは辞書である必要があります: {path}"
        )
    return data


def strip_removed_keys(data: dict[str, Any]) -> dict[str, Any]:
    """受け付けないキーを取り除く。"""
    return {k: v for k, v in data.items() if k not in REMOVED_KEYS}


def load_settings_dict(config_path: Path | str | None = None) -> dict[str, Any]:
    """既定値 → userdata の設定 → --config の順にマージした辞書を返す。"""
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy

    user_settings = paths.settings_path()
    if user_settings.is_file():
        merged = _deep_merge(merged, strip_removed_keys(_read_json(user_settings)))

    if config_path is not None:
        extra = Path(config_path)
        if not extra.is_file():
            raise ConfigError(f"設定ファイルが見つかりません: {extra}")
        merged = _deep_merge(merged, strip_removed_keys(_read_json(extra)))

    return merged


def save_settings_dict(data: dict[str, Any]) -> Path:
    """ユーザー設定を保存する。**原子的に書く。**

    途中で失敗しても壊れた設定ファイルを残さない。
    """
    target = paths.settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(strip_removed_keys(data), ensure_ascii=False, indent=2)
    temp.write_text(payload + "\n", encoding="utf-8", newline="\n")
    temp.replace(target)
    return target


# --------------------------------------------------------------------------
# ffmpeg / ffprobe の解決
# --------------------------------------------------------------------------


def probe_tool_version(tool_path: Path, timeout: int = 30) -> str:
    """ffprobe / ffmpeg のバージョン文字列（1 行目）を返す。"""
    try:
        completed = process_utils.run(
            [tool_path, "-hide_banner", "-version"], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"バージョン取得に失敗しました: {tool_path} ({exc})") from exc

    text = completed.stdout.decode("utf-8", errors="replace")
    first_line = text.splitlines()[0].strip() if text.strip() else ""
    if not first_line:
        raise ConfigError(f"バージョン文字列を取得できません: {tool_path}")
    return first_line


def ffmpeg_has_whisper(ffmpeg_path: Path | None, timeout: int = 30) -> bool:
    """その ffmpeg が whisper フィルターを持つかを実際に問い合わせる。

    **判定はここ 1 箇所だけ。** 旧個人版では同じ判定が 2 箇所に別実装で
    存在し、片方は緩い部分一致だった。
    """
    if not ffmpeg_path or not Path(ffmpeg_path).is_file():
        return False
    try:
        completed = process_utils.run(
            [ffmpeg_path, "-hide_banner", "-filters"], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    text = completed.stdout.decode("utf-8", errors="replace")
    # フィルター一覧は "  T.. whisper  A->A  ..." の形。
    # 2 列目がフィルター名。説明文へ "whisper" が出ても誤判定しない。
    return any(
        line.split()[1:2] == ["whisper"]
        for line in text.splitlines()
        if line.strip()
    )


def resolve_ffprobe(settings_dict: dict[str, Any]) -> Path:
    """ffprobe を解決する。設定値を優先し、無ければ PATH を探す。"""
    configured = settings_dict.get("ffprobe_path")
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise ConfigError(
                f"設定された ffprobe が見つかりません: {path}\n"
                "「環境チェック」から ffprobe.exe の場所を指定し直してください。"
            )
        return path.resolve()

    found = shutil.which("ffprobe")
    if not found:
        raise ConfigError(
            "ffprobe が見つかりません。\n"
            "「環境チェック」から ffprobe.exe の場所を指定してください。"
        )
    return Path(found).resolve()


def resolve_ffmpeg(settings_dict: dict[str, Any]) -> Path | None:
    """ffmpeg を解決する。見つからなくても ``None`` を返して続行する。

    登録と ffprobe だけの実行では ffmpeg を使わないため。
    """
    configured = settings_dict.get("ffmpeg_path")
    if configured:
        path = Path(configured)
        return path.resolve() if path.is_file() else None

    found = shutil.which("ffmpeg")
    return Path(found).resolve() if found else None


# --------------------------------------------------------------------------
# 構築
# --------------------------------------------------------------------------


def _normalize_extension(ext: str) -> str:
    value = str(ext).strip().lower()
    if not value:
        return ""
    return value if value.startswith(".") else "." + value


def build_settings(
    settings_dict: dict[str, Any], *, require_ffprobe: bool = True
) -> Settings:
    """辞書を検証済みの Settings へ変換する。"""
    # 0 や未設定は「既定を使う」の意味として扱う。負の値だけを下限で丸める。
    workers = int(settings_dict.get("workers") or DEFAULT_SETTINGS["workers"])
    workers = max(workers, MIN_WORKERS)
    if workers > MAX_WORKERS:
        raise ConfigError(
            f"workers が上限を超えています: {workers}（上限 {MAX_WORKERS}）。\n"
            "ffprobe のプロセスを無制限に起動しないための制限です。"
        )

    # 空リストも「既定を使う」。ただし中身が空文字だけなど、指定した結果として
    # 対象が 0 件になる場合は、黙って全種類を対象にせずエラーにする。
    extensions = settings_dict.get("extensions") or DEFAULT_EXTENSIONS
    normalized = tuple(sorted({_normalize_extension(e) for e in extensions} - {""}))
    if not normalized:
        raise ConfigError(
            "対象拡張子が 1 つも残りませんでした。extensions を確認してください。"
        )

    fingerprint_cfg = settings_dict.get("fingerprint") or {}
    head_bytes = int(fingerprint_cfg.get("head_bytes", 1024 * 1024))
    tail_bytes = int(fingerprint_cfg.get("tail_bytes", 1024 * 1024))
    if head_bytes <= 0 or tail_bytes <= 0:
        raise ConfigError("fingerprint の head_bytes / tail_bytes は 1 以上にしてください。")

    cache_cfg = settings_dict.get("probe_cache") or {}
    source = settings_dict.get("source_path")

    if require_ffprobe:
        ffprobe_path = resolve_ffprobe(settings_dict)
    else:
        configured = settings_dict.get("ffprobe_path")
        ffprobe_path = Path(configured) if configured else Path("ffprobe")

    return Settings(
        source_path=Path(source) if source else None,
        ffmpeg_path=resolve_ffmpeg(settings_dict),
        ffprobe_path=ffprobe_path,
        recursive=bool(settings_dict.get("recursive", False)),
        workers=workers,
        extensions=normalized,
        exclude_patterns=tuple(settings_dict.get("exclude_patterns") or []),
        min_size_bytes=int(settings_dict.get("min_size_bytes") or 0),
        follow_symlinks=bool(settings_dict.get("follow_symlinks", False)),
        ffprobe_timeout_sec=int(settings_dict.get("ffprobe_timeout_sec") or 60),
        full_hash=bool(settings_dict.get("full_hash", False)),
        resume=bool(settings_dict.get("resume", True)),
        head_bytes=head_bytes,
        tail_bytes=tail_bytes,
        probe_cache_enabled=bool(cache_cfg.get("enabled", True)),
        probe_cache_gzip=bool(cache_cfg.get("gzip", True)),
        raw=settings_dict,
    )


# --------------------------------------------------------------------------
# 保存先の確認
# --------------------------------------------------------------------------


class UserDataError(Exception):
    """userdata を使用できない。**代替場所を勝手に決めずに停止する。**"""


def verify_userdata(*, create: bool = True) -> dict[str, Any]:
    """userdata の書き込み可否と空き容量を確認する。

    APP_ROOT は既に存在している場所なので、旧個人版のように
    「保存先が無ければ止まる」必要はない。不足しているフォルダーだけを作る。
    ただし**書けない場合は代替場所を選ばずに止まる**。
    """
    root = paths.app_root()
    info: dict[str, Any] = {
        "app_root": str(root),
        "userdata": str(paths.userdata_dir()),
    }

    if create:
        info["created_subdirectories"] = [
            str(p) for p in paths.ensure_userdata_tree()
        ]

    target = paths.userdata_dir()
    if not target.is_dir():
        raise UserDataError(f"userdata フォルダーを用意できません: {target}")

    try:
        usage = shutil.disk_usage(str(target))
        info["total_bytes"] = usage.total
        info["free_bytes"] = usage.free
    except OSError as exc:
        raise UserDataError(f"空き容量を取得できません: {target} ({exc})") from exc

    probe_file = target / ".write_check.tmp"
    try:
        probe_file.write_text("write-check\n", encoding="utf-8")
        probe_file.read_text(encoding="utf-8")
        info["writable"] = True
    except OSError as exc:
        raise UserDataError(
            f"userdata へ書き込めません: {target} ({exc})\n"
            "代替場所は自動で決めません。書き込み権限を確認してください。"
        ) from exc
    finally:
        try:
            probe_file.unlink(missing_ok=True)
        except OSError:
            pass

    return info
