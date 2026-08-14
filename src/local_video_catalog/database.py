"""SQLite 台帳（正本）.

CSV / JSON / JSONL と HTML カタログは、ここからのエクスポートであり正本ではない。

設計の要:

  1. **AI の推定と、利用者が確認した事実を同じ列で上書きしない。**
     ``capture_time_candidates.is_user_confirmed`` と
     ``asset_relations.confirmed_by_user`` がその境界。
     再解析が人手の確認を消さない。
  2. 複数の元動画 → 1 本の変換済み動画（N:1）を表現できる。
  3. 処理状態は単一の総合状態ではなく、**段階ごと**に持つ（``stage_status``）。
     これが Resume の正本になる。
  4. 1 本処理するごとに確定（コミット）する。全件終了までメモリへ溜めない。
     中断しても、それまでの成果は失われない。

パスの持ち方（旧個人版からの重要な変更）:

  - **内部生成物**（cache・descriptions・catalog・logs）は
    **APP_ROOT からの相対パス**で保存する。アプリのフォルダーごと
    移動しても、過去の成果物を見失わない。
  - **元動画**は APP_ROOT の外にある外部入力なので、
    ``source_root`` + ``source_relative`` の 2 列で持つ。
    **相対パスの意味がまったく違うものを、同じ形で持たない。**
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import SCHEMA_VERSION
from . import paths as paths_module
from .source_ref import SourceRef

# --------------------------------------------------------------------------
# stage_name / status
# --------------------------------------------------------------------------

STAGE_DISCOVERY = "discovery"
STAGE_FINGERPRINT = "fingerprint"
STAGE_FFPROBE = "ffprobe"
STAGE_EXPORT = "export"
STAGE_FRAME_EXTRACTION = "frame_extraction"
STAGE_VISUAL_ANALYSIS = "visual_analysis"
STAGE_AUDIO_TRANSCRIPTION = "audio_transcription"
STAGE_DESCRIPTION = "description"

KNOWN_STAGES = (
    STAGE_DISCOVERY,
    STAGE_FINGERPRINT,
    STAGE_FFPROBE,
    STAGE_EXPORT,
    STAGE_FRAME_EXTRACTION,
    STAGE_VISUAL_ANALYSIS,
    STAGE_AUDIO_TRANSCRIPTION,
    STAGE_DESCRIPTION,
)

PIPELINE_STAGES = (
    (STAGE_FRAME_EXTRACTION, "代表画像の抽出"),
    (STAGE_VISUAL_ANALYSIS, "映像の解析"),
    (STAGE_AUDIO_TRANSCRIPTION, "文字起こし"),
    (STAGE_DESCRIPTION, "説明文の作成"),
)
"""動画 1 本に対して順に行う工程。Resume の判定単位。"""

STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_REUSED = "reused"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_SOURCE_CHANGED = "source_changed"

# 工程ごとの固有状態。中断や一部失敗を単純な completed として扱わない。
STATUS_SKIPPED_NO_PLAYABLE_VIDEO = "skipped_no_playable_video"
STATUS_SKIPPED_NO_FRAMES = "skipped_no_frames"
STATUS_MODEL_UNAVAILABLE = "model_unavailable"
STATUS_INVALID_FRAME_SET = "invalid_frame_set"
STATUS_NO_SPEECH = "no_speech"
STATUS_SKIPPED_NO_AUDIO = "skipped_no_audio"
STATUS_INVALID_OUTPUT = "invalid_output"
STATUS_INTERRUPTED = "interrupted"

DONE_STATUSES = frozenset({
    STATUS_OK,
    STATUS_COMPLETED,
    STATUS_REUSED,
    STATUS_SKIPPED,
    STATUS_NO_SPEECH,
    STATUS_SKIPPED_NO_AUDIO,
    STATUS_SKIPPED_NO_PLAYABLE_VIDEO,
})
"""「もう一度やらなくてよい」状態。

``no_speech`` と ``skipped_no_audio`` を完了に含めるのは、音声の無い
動画を毎回やり直さないため。**失敗ではない。**
"""

# registration_status
REG_NEW = "new"
REG_EXISTING = "existing"
REG_MOVED = "moved"
REG_AMBIGUOUS = "ambiguous"
REG_MISSING = "missing"

# relation_type
RELATION_CONVERTED_TO = "converted_to"
RELATION_CONCATENATED_INTO = "concatenated_into"
RELATION_SEGMENT_OF = "segment_of"
RELATION_DERIVED_FROM = "derived_from"
RELATION_DUPLICATE_OF = "duplicate_of"
RELATION_RELATED_TO = "related_to"

KNOWN_RELATION_TYPES = (
    RELATION_CONVERTED_TO,
    RELATION_CONCATENATED_INTO,
    RELATION_SEGMENT_OF,
    RELATION_DERIVED_FROM,
    RELATION_DUPLICATE_OF,
    RELATION_RELATED_TO,
)


# --------------------------------------------------------------------------
# 内部生成物のパス（APP_ROOT 相対で保存する）
# --------------------------------------------------------------------------


def store_internal_path(path: Path | str | None) -> str | None:
    """内部生成物の位置を、台帳へ保存できる形（APP_ROOT 相対）へ変換する。

    **APP_ROOT の外を渡してはいけない。** 元動画は外部入力であり、
    ``SourceRef`` が扱う。渡された場合は ``ValueError`` で止める。
    黙って絶対パスを保存すると、フォルダー移動で解決できなくなる。
    """
    if path is None:
        return None
    relative = paths_module.to_app_relative(path)
    if relative is None:
        raise ValueError(
            f"内部生成物ではないパスを台帳へ保存しようとしました: {path}\n"
            "元動画などの外部パスは source_root / source_relative で持ちます。"
        )
    return relative


def load_internal_path(relative: str | None) -> Path | None:
    """``store_internal_path`` の逆。現在の APP_ROOT を基準に解決する。"""
    if not relative:
        return None
    return paths_module.from_app_relative(relative)


# --------------------------------------------------------------------------
# スキーマ
# --------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- A. assets : 元動画 1 件につき 1 行
--
--    source_root     解析対象フォルダーの絶対パス（APP_ROOT の外）
--    source_relative その中での相対パス（POSIX）
--
--    original_* は最初に見つけたときの位置。**決して変更しない。**
--    source_* は現在の位置。動画が移動されたら更新する。
--    どちらも「外部入力」であって、APP_ROOT 相対ではない。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    asset_id                 TEXT PRIMARY KEY,
    catalog_id               TEXT NOT NULL UNIQUE,
    original_source_root     TEXT NOT NULL,
    original_source_relative TEXT NOT NULL,
    source_root              TEXT NOT NULL,
    source_relative          TEXT NOT NULL,
    file_name                TEXT NOT NULL,
    extension                TEXT NOT NULL,
    file_size                INTEGER,
    creation_time_fs         TEXT,
    last_write_time_fs       TEXT,
    file_fingerprint         TEXT,
    quick_fingerprint        TEXT,
    full_sha256              TEXT,
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL,
    is_available             INTEGER NOT NULL DEFAULT 1,
    registration_status      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_location
    ON assets(source_root, source_relative);
CREATE INDEX IF NOT EXISTS idx_assets_file_fp  ON assets(file_fingerprint);
CREATE INDEX IF NOT EXISTS idx_assets_quick_fp ON assets(quick_fingerprint);
CREATE INDEX IF NOT EXISTS idx_assets_catalog  ON assets(catalog_id);

-- ---------------------------------------------------------------------
-- B. probe_results : ffprobe の結果（asset ごとに最新 1 行）
--
--    raw_probe_cache_path は APP_ROOT 相対。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS probe_results (
    asset_id             TEXT PRIMARY KEY
                         REFERENCES assets(asset_id) ON DELETE CASCADE,
    probe_status         TEXT NOT NULL,
    probe_started_at     TEXT,
    probe_finished_at    TEXT,
    probe_duration_ms    INTEGER,
    duration_seconds     REAL,
    format_name          TEXT,
    format_long_name     TEXT,
    bit_rate             INTEGER,
    video_stream_count   INTEGER NOT NULL DEFAULT 0,
    playable_video_stream_count   INTEGER NOT NULL DEFAULT 0,
    attached_picture_stream_count INTEGER NOT NULL DEFAULT 0,
    primary_video_stream_index    INTEGER,
    primary_video_selection_rule  TEXT,
    audio_stream_count   INTEGER NOT NULL DEFAULT 0,
    primary_audio_stream_index    INTEGER,
    subtitle_stream_count INTEGER NOT NULL DEFAULT 0,
    chapter_count        INTEGER NOT NULL DEFAULT 0,
    width                INTEGER,
    height               INTEGER,
    video_codec          TEXT,
    pixel_format         TEXT,
    frame_rate_num       INTEGER,
    frame_rate_den       INTEGER,
    frame_rate_decimal   REAL,
    audio_codec          TEXT,
    sample_rate          INTEGER,
    channel_count        INTEGER,
    creation_time_tag    TEXT,
    location_tag_present INTEGER NOT NULL DEFAULT 0,
    raw_probe_cache_path TEXT,
    error_type           TEXT,
    error_message        TEXT,
    exit_code            INTEGER,
    ffprobe_version      TEXT,
    ffprobe_impl_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_probe_status ON probe_results(probe_status);

-- ---------------------------------------------------------------------
-- C. capture_time_candidates : 撮影日時の候補と根拠
--
--    **候補は削除しない。確定は利用者の確認でのみ行う。**
--    is_user_confirmed = 1 の行は再解析でも消さない。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS capture_time_candidates (
    candidate_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id            TEXT NOT NULL
                        REFERENCES assets(asset_id) ON DELETE CASCADE,
    candidate_datetime  TEXT NOT NULL,
    source_type         TEXT NOT NULL,
    source_value        TEXT,
    parser_rule         TEXT,
    confidence          REAL NOT NULL DEFAULT 0.0,
    has_time            INTEGER NOT NULL DEFAULT 0,
    is_user_confirmed   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    UNIQUE(asset_id, candidate_datetime, source_type, parser_rule)
);
CREATE INDEX IF NOT EXISTS idx_capture_asset
    ON capture_time_candidates(asset_id);

-- ---------------------------------------------------------------------
-- D. asset_relations : 複数元動画 対 1 変換済み動画
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_relations (
    relation_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_asset_id    TEXT NOT NULL
                       REFERENCES assets(asset_id) ON DELETE CASCADE,
    target_asset_id    TEXT NOT NULL
                       REFERENCES assets(asset_id) ON DELETE CASCADE,
    relation_type      TEXT NOT NULL,
    sequence_index     INTEGER,
    confidence         REAL NOT NULL DEFAULT 0.0,
    evidence           TEXT,
    created_at         TEXT NOT NULL,
    confirmed_by_user  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_asset_id, target_asset_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON asset_relations(source_asset_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON asset_relations(target_asset_id);

-- ---------------------------------------------------------------------
-- E. processing_runs : 実行 1 回につき 1 行
--
--    config_snapshot には APP_ROOT の絶対パスを含めない（移動耐性）。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processing_runs (
    run_id              TEXT PRIMARY KEY,
    source_root         TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL,
    worker_count        INTEGER,
    files_discovered    INTEGER NOT NULL DEFAULT 0,
    files_processed     INTEGER NOT NULL DEFAULT 0,
    files_reused        INTEGER NOT NULL DEFAULT 0,
    files_failed        INTEGER NOT NULL DEFAULT 0,
    stop_reason         TEXT,
    config_snapshot     TEXT,
    application_version TEXT
);

-- ---------------------------------------------------------------------
-- F. stage_status : 処理段階ごとの状態。**Resume の正本。**
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stage_status (
    asset_id               TEXT NOT NULL
                           REFERENCES assets(asset_id) ON DELETE CASCADE,
    stage_name             TEXT NOT NULL,
    status                 TEXT NOT NULL,
    attempt_count          INTEGER NOT NULL DEFAULT 0,
    last_started_at        TEXT,
    last_finished_at       TEXT,
    error_message          TEXT,
    implementation_version TEXT,
    PRIMARY KEY (asset_id, stage_name)
);
CREATE INDEX IF NOT EXISTS idx_stage_lookup ON stage_status(stage_name, status);

-- ---------------------------------------------------------------------
-- G. frame_extraction_runs : 代表画像抽出の実行 1 回につき 1 行
--
--    output_directory は APP_ROOT 相対。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS frame_extraction_runs (
    extraction_run_id        TEXT PRIMARY KEY,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    started_at               TEXT NOT NULL,
    finished_at              TEXT,
    status                   TEXT NOT NULL,
    implementation_version   TEXT NOT NULL,
    config_hash              TEXT NOT NULL,
    config_json              TEXT,
    source_quick_fingerprint TEXT,
    primary_video_stream_index INTEGER,
    duration_seconds         REAL,
    planned_frame_count      INTEGER NOT NULL DEFAULT 0,
    successful_frame_count   INTEGER NOT NULL DEFAULT 0,
    failed_frame_count       INTEGER NOT NULL DEFAULT 0,
    reused_frame_count       INTEGER NOT NULL DEFAULT 0,
    output_directory         TEXT,
    error_message            TEXT
);
CREATE INDEX IF NOT EXISTS idx_frame_runs_asset
    ON frame_extraction_runs(asset_id, implementation_version, config_hash);

-- ---------------------------------------------------------------------
-- H. extracted_frames : 抽出した静止画 1 枚につき 1 行
--
--    同一の (asset, 実装, 設定, 元動画の fingerprint, 抽出時刻) に対して
--    重複行を作らない。再抽出は同じ行を更新する。
--
--    extraction_run_id    その画像を実際に作った run（履歴として保持）
--    last_verified_run_id 最後に検証・再利用した run
--
--    file_path は APP_ROOT 相対。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extracted_frames (
    frame_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_run_id        TEXT NOT NULL
                             REFERENCES frame_extraction_runs(extraction_run_id)
                             ON DELETE CASCADE,
    last_verified_run_id     TEXT,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    implementation_version   TEXT NOT NULL,
    config_hash              TEXT NOT NULL,
    source_quick_fingerprint TEXT,
    sequence_index           INTEGER NOT NULL,
    target_time_seconds      REAL NOT NULL,
    target_time_milliseconds INTEGER NOT NULL,
    relative_position        REAL,
    file_path                TEXT,
    file_size                INTEGER,
    width                    INTEGER,
    height                   INTEGER,
    image_format             TEXT,
    sha256                   TEXT,
    extraction_status        TEXT NOT NULL,
    ffmpeg_exit_code         INTEGER,
    ffmpeg_duration_ms       INTEGER,
    error_message            TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT,
    UNIQUE(asset_id, implementation_version, config_hash,
           source_quick_fingerprint, target_time_milliseconds)
);
CREATE INDEX IF NOT EXISTS idx_frames_run ON extracted_frames(extraction_run_id);
CREATE INDEX IF NOT EXISTS idx_frames_asset ON extracted_frames(asset_id);

-- ---------------------------------------------------------------------
-- I. visual_analysis_runs : ローカル VLM 解析の実行 1 回につき 1 行
--
--    model_api_base には localhost であることが分かる最小限だけを保存する。
--    **認証情報・トークン・base64 画像は保存しない。**
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS visual_analysis_runs (
    visual_run_id            TEXT PRIMARY KEY,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    catalog_id               TEXT,
    started_at               TEXT NOT NULL,
    finished_at              TEXT,
    status                   TEXT NOT NULL,
    implementation_version   TEXT NOT NULL,
    frame_prompt_version     TEXT NOT NULL,
    summary_prompt_version   TEXT NOT NULL,
    model_id                 TEXT,
    model_api_base           TEXT,
    config_hash              TEXT NOT NULL,
    config_json              TEXT,
    source_quick_fingerprint TEXT,
    frame_extraction_implementation_version TEXT,
    frame_extraction_config_hash            TEXT,
    planned_frame_count      INTEGER NOT NULL DEFAULT 0,
    successful_frame_count   INTEGER NOT NULL DEFAULT 0,
    failed_frame_count       INTEGER NOT NULL DEFAULT 0,
    reused_frame_count       INTEGER NOT NULL DEFAULT 0,
    repair_attempt_count     INTEGER NOT NULL DEFAULT 0,
    summary_status           TEXT,
    frame_total_duration_ms  INTEGER,
    summary_duration_ms      INTEGER,
    error_message            TEXT
);
CREATE INDEX IF NOT EXISTS idx_visual_runs_asset
    ON visual_analysis_runs(asset_id, implementation_version, config_hash);

-- ---------------------------------------------------------------------
-- J. frame_visual_analyses : 静止画 1 枚の解析結果
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS frame_visual_analyses (
    frame_visual_analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    visual_run_id            TEXT NOT NULL
                             REFERENCES visual_analysis_runs(visual_run_id)
                             ON DELETE CASCADE,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    frame_id                 INTEGER,
    sequence_index           INTEGER NOT NULL,
    frame_sha256             TEXT NOT NULL,
    target_time_milliseconds INTEGER,
    model_id                 TEXT NOT NULL,
    prompt_version           TEXT NOT NULL,
    implementation_version   TEXT NOT NULL,
    config_hash              TEXT NOT NULL,
    analysis_status          TEXT NOT NULL,
    attempt_count            INTEGER NOT NULL DEFAULT 0,
    caption                  TEXT,
    structured_analysis_json TEXT,
    raw_response_json        TEXT,
    result_file_path         TEXT,
    request_duration_ms      INTEGER,
    prompt_tokens            INTEGER,
    completion_tokens        INTEGER,
    total_tokens             INTEGER,
    error_type               TEXT,
    error_message            TEXT,
    created_at               TEXT NOT NULL,
    last_verified_at         TEXT,
    last_verified_run_id     TEXT,
    UNIQUE(asset_id, frame_sha256, model_id, prompt_version,
           implementation_version, config_hash)
);
CREATE INDEX IF NOT EXISTS idx_frame_visual_run
    ON frame_visual_analyses(visual_run_id);
CREATE INDEX IF NOT EXISTS idx_frame_visual_asset
    ON frame_visual_analyses(asset_id);

-- ---------------------------------------------------------------------
-- K. asset_visual_summaries : 映像全体の視覚概要
--
--    source_frame_analysis_hash は、元になったフレーム解析結果の集合から
--    決まるハッシュ。フレーム解析が変わらなければ概要も再生成しない。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_visual_summaries (
    asset_visual_summary_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    visual_run_id            TEXT NOT NULL
                             REFERENCES visual_analysis_runs(visual_run_id)
                             ON DELETE CASCADE,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    catalog_id               TEXT,
    model_id                 TEXT NOT NULL,
    prompt_version           TEXT NOT NULL,
    implementation_version   TEXT NOT NULL,
    config_hash              TEXT NOT NULL,
    source_frame_analysis_hash TEXT NOT NULL,
    summary_status           TEXT NOT NULL,
    title_candidate          TEXT,
    visual_summary           TEXT,
    main_activity            TEXT,
    structured_summary_json  TEXT,
    raw_response_json        TEXT,
    result_file_path         TEXT,
    request_duration_ms      INTEGER,
    prompt_tokens            INTEGER,
    completion_tokens        INTEGER,
    total_tokens             INTEGER,
    error_type               TEXT,
    error_message            TEXT,
    created_at               TEXT NOT NULL,
    last_verified_at         TEXT,
    last_verified_run_id     TEXT,
    UNIQUE(asset_id, source_frame_analysis_hash, model_id, prompt_version,
           implementation_version, config_hash)
);
CREATE INDEX IF NOT EXISTS idx_visual_summary_asset
    ON asset_visual_summaries(asset_id);

-- ---------------------------------------------------------------------
-- L. asr_runs : 文字起こし実行 1 回につき 1 行
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asr_runs (
    asr_run_id               TEXT PRIMARY KEY,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    catalog_id               TEXT,
    started_at               TEXT NOT NULL,
    finished_at              TEXT,
    status                   TEXT NOT NULL,
    implementation_version   TEXT NOT NULL,
    engine_name              TEXT NOT NULL,
    ffmpeg_version           TEXT,
    model_name               TEXT,
    model_sha256             TEXT,
    config_hash              TEXT NOT NULL,
    config_json              TEXT,
    source_quick_fingerprint TEXT,
    primary_audio_stream_index INTEGER,
    scope_type               TEXT,
    scope_start_seconds      REAL,
    scope_duration_seconds   REAL,
    language_requested       TEXT,
    vad_enabled              INTEGER NOT NULL DEFAULT 0,
    planned_chunk_count      INTEGER NOT NULL DEFAULT 0,
    processed_chunk_count    INTEGER NOT NULL DEFAULT 0,
    reused_chunk_count       INTEGER NOT NULL DEFAULT 0,
    failed_chunk_count       INTEGER NOT NULL DEFAULT 0,
    planned_duration_seconds REAL,
    processed_duration_seconds REAL,
    processing_duration_ms   INTEGER,
    real_time_factor         REAL,
    segment_count            INTEGER,
    stop_reason              TEXT,
    error_type               TEXT,
    error_message            TEXT
);
CREATE INDEX IF NOT EXISTS idx_asr_runs_asset
    ON asr_runs(asset_id, implementation_version, config_hash);

-- ---------------------------------------------------------------------
-- M. asr_chunks : チャンク 1 個につき 1 行
--
--    長時間動画を 1 回の ffmpeg プロセスで処理しないための単位。
--    **中断時の損失は最大 1 チャンクに限定される。**
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asr_chunks (
    asr_chunk_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asr_run_id               TEXT,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    chunk_index              INTEGER NOT NULL,
    absolute_start_seconds   REAL NOT NULL,
    duration_seconds         REAL NOT NULL,
    overlap_seconds          REAL NOT NULL DEFAULT 0,
    source_quick_fingerprint TEXT,
    primary_audio_stream_index INTEGER,
    engine_name              TEXT NOT NULL,
    implementation_version   TEXT NOT NULL,
    model_sha256             TEXT,
    config_hash              TEXT NOT NULL,
    chunk_status             TEXT NOT NULL,
    transcript_text          TEXT,
    normalized_chunk_json    TEXT,
    raw_engine_output        TEXT,
    result_file_path         TEXT,
    segment_count            INTEGER,
    processing_duration_ms   INTEGER,
    real_time_factor         REAL,
    attempt_count            INTEGER NOT NULL DEFAULT 0,
    error_type               TEXT,
    error_message            TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT,
    last_verified_at         TEXT,
    UNIQUE(asset_id, source_quick_fingerprint, primary_audio_stream_index,
           chunk_index, absolute_start_seconds, duration_seconds,
           engine_name, implementation_version, model_sha256, config_hash)
);
CREATE INDEX IF NOT EXISTS idx_asr_chunks_asset ON asr_chunks(asset_id);
CREATE INDEX IF NOT EXISTS idx_asr_chunks_run ON asr_chunks(asr_run_id);

-- ---------------------------------------------------------------------
-- N. transcripts : 全チャンクを統合した文字起こし
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asr_run_id               TEXT,
    asset_id                 TEXT NOT NULL
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    catalog_id               TEXT,
    implementation_version   TEXT NOT NULL,
    engine_name              TEXT NOT NULL,
    model_sha256             TEXT,
    config_hash              TEXT NOT NULL,
    source_quick_fingerprint TEXT,
    primary_audio_stream_index INTEGER,
    scope_type               TEXT NOT NULL,
    scope_start_seconds      REAL NOT NULL,
    scope_duration_seconds   REAL NOT NULL,
    language_requested       TEXT,
    language_detected        TEXT,
    transcript_status        TEXT NOT NULL,
    full_text                TEXT,
    normalized_transcript_json TEXT,
    result_file_path         TEXT,
    segment_count            INTEGER,
    processing_duration_ms   INTEGER,
    real_time_factor         REAL,
    created_at               TEXT NOT NULL,
    last_verified_at         TEXT,
    last_verified_run_id     TEXT,
    UNIQUE(asset_id, source_quick_fingerprint, primary_audio_stream_index,
           scope_type, scope_start_seconds, scope_duration_seconds,
           engine_name, implementation_version, model_sha256, config_hash)
);
CREATE INDEX IF NOT EXISTS idx_transcripts_asset ON transcripts(asset_id);

-- ---------------------------------------------------------------------
-- O. transcript_segments : タイムスタンプ付きセグメント
--
--    is_suspected_hallucination は **AI 推定の「疑い」であって確定ではない。**
--    印を付けても本文は消さない。説明文の材料から外すだけ。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transcript_segments (
    transcript_segment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id            INTEGER NOT NULL
                             REFERENCES transcripts(transcript_id)
                             ON DELETE CASCADE,
    asset_id                 TEXT NOT NULL,
    sequence_index           INTEGER NOT NULL,
    start_seconds            REAL NOT NULL,
    end_seconds              REAL NOT NULL,
    absolute_start_seconds   REAL NOT NULL,
    absolute_end_seconds     REAL NOT NULL,
    text                     TEXT NOT NULL,
    confidence               REAL,
    is_suspected_hallucination INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL,
    UNIQUE(transcript_id, sequence_index)
);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_tid
    ON transcript_segments(transcript_id);

-- ---------------------------------------------------------------------
-- P. asset_descriptions : 動画 1 本の最終テキスト（利用者が読む成果物）
--
--    **これが出来た動画だけ、中間キャッシュを片付けてよい。**
--    最終テキストは派生物ではなく、残し続ける成果物である。
--
--    description_file_path は APP_ROOT 相対。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_descriptions (
    asset_id                 TEXT PRIMARY KEY
                             REFERENCES assets(asset_id) ON DELETE CASCADE,
    catalog_id               TEXT,
    source_root              TEXT NOT NULL,
    source_relative          TEXT NOT NULL,
    file_name                TEXT NOT NULL,
    description_file_path    TEXT NOT NULL,
    description_status       TEXT NOT NULL,
    recorded_from            TEXT,
    recorded_to              TEXT,
    recorded_precision       TEXT,
    recorded_source          TEXT,
    recorded_raw_text        TEXT,
    used_visual_analysis     INTEGER NOT NULL DEFAULT 0,
    used_transcription       INTEGER NOT NULL DEFAULT 0,
    generator                TEXT,
    model_id                 TEXT,
    implementation_version   TEXT,
    cache_cleanup_status     TEXT,
    cache_cleanup_at         TEXT,
    cache_freed_bytes        INTEGER,
    created_at               TEXT NOT NULL,
    updated_at               TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_descriptions_catalog
    ON asset_descriptions(catalog_id);
"""


@dataclass
class AssetRow:
    """assets の 1 行（よく使う列のみ）。"""

    asset_id: str
    catalog_id: str
    source: SourceRef
    file_size: int | None
    file_fingerprint: str | None
    quick_fingerprint: str | None
    full_sha256: str | None
    registration_status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AssetRow":
        return cls(
            asset_id=row["asset_id"],
            catalog_id=row["catalog_id"],
            source=SourceRef.from_row(row),
            file_size=row["file_size"],
            file_fingerprint=row["file_fingerprint"],
            quick_fingerprint=row["quick_fingerprint"],
            full_sha256=row["full_sha256"],
            registration_status=row["registration_status"],
        )


class SchemaTooNewError(RuntimeError):
    """台帳がコードより新しい。**DB を変更しない。**"""


class CatalogDatabase:
    """SQLite 台帳への読み書き。

    書き込みは呼び出し側（主スレッド）だけが行う前提。
    ワーカースレッドは ffprobe と fingerprint のみを担当する。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else paths_module.database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._initialize_schema()
        except Exception:
            # スキーマ確認・移行に失敗したら接続を確実に閉じる。
            # 開いたままだと DB ファイルがロックされ続ける。
            self.close()
            raise

    # -- 初期化 -----------------------------------------------------------

    def _initialize_schema(self) -> None:
        # CREATE TABLE IF NOT EXISTS のため、新規 DB はここで最新になる。
        self.connection.executescript(SCHEMA_SQL)

        current = self.get_meta("schema_version")
        if current is None:
            self.migrate(from_version=0)
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return

        current_version = int(current)
        if current_version > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"台帳のスキーマがプログラムより新しいです"
                f"（台帳={current_version} / プログラム={SCHEMA_VERSION}）。\n"
                "新しいバージョンのプログラムを使ってください。台帳は変更しません。"
            )
        if current_version < SCHEMA_VERSION:
            self.migrate(from_version=current_version)
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -- マイグレーション -------------------------------------------------

    def migrate(self, *, from_version: int) -> list[str]:
        """既存の台帳を最新スキーマへ更新する。

        方針:
          - **既存ファイルを削除して作り直さない。**
          - 不足している列だけを ``ALTER TABLE ADD COLUMN`` で追加する。
          - 何度実行しても失敗しない（列の有無を毎回確認する）。
          - ``asset_id`` / ``catalog_id`` を変更しない。
          - **利用者が確認した情報**（``is_user_confirmed`` /
            ``confirmed_by_user``）を失わない。既存行には触れない。

        Returns:
            実際に追加した列の一覧（``テーブル.列`` 形式）。
        """
        added: list[str] = []
        # 一般配布版は SCHEMA_VERSION 1 から始まる。将来の版で
        # 列を足すときは、ここへ _ensure_column を並べる。
        return added

    def table_columns(self, table: str) -> set[str]:
        rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def _table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row["name"] for row in rows}

    def _ensure_column(self, table: str, column: str, ddl: str) -> list[str]:
        """列が無ければ追加する。既にあれば何もしない（冪等）。"""
        if table not in self._table_names():
            return []
        if column in self.table_columns(table):
            return []
        self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return [f"{table}.{column}"]

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))

    # -- トランザクション -------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """1 本分の書き込みを原子的に確定する。"""
        self.connection.execute("BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        self.connection.execute("COMMIT")

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "CatalogDatabase":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 識別子 ------------------------------------------------------------

    def next_catalog_id(self) -> str:
        """``VID-000001`` 形式の連番を発行する。一度発行したら変更しない。"""
        row = self.connection.execute(
            "SELECT catalog_id FROM assets "
            "WHERE catalog_id LIKE 'VID-%' ORDER BY catalog_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return "VID-000001"
        try:
            number = int(str(row["catalog_id"]).split("-", 1)[1])
        except (IndexError, ValueError):
            number = self.connection.execute(
                "SELECT COUNT(*) AS c FROM assets").fetchone()["c"]
        return f"VID-{number + 1:06d}"

    @staticmethod
    def new_asset_id() -> str:
        return uuid.uuid4().hex

    # -- assets ------------------------------------------------------------

    def find_asset_by_source(self, source: SourceRef) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM assets WHERE source_root = ? AND source_relative = ?",
            (str(source.root), source.relative),
        ).fetchone()

    def find_assets_by_file_fingerprint(self, fingerprint: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM assets WHERE file_fingerprint = ?",
            (fingerprint,)).fetchall())

    def find_assets_by_quick_fingerprint(self, fingerprint: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM assets WHERE quick_fingerprint = ?",
            (fingerprint,)).fetchall())

    def find_assets_by_identifier(self, identifier: str) -> list[sqlite3.Row]:
        """asset_id または catalog_id で探す。

        **曖昧な指定で勝手に 1 件へ絞らない。** 一致したものをすべて返し、
        判断は呼び出し側へ委ねる。
        """
        return list(self.connection.execute(
            "SELECT * FROM assets WHERE asset_id = ? OR catalog_id = ?",
            (identifier, identifier)).fetchall())

    def list_assets_under(self, source_root: Path | str) -> list[sqlite3.Row]:
        """ある解析対象フォルダーに属する動画を返す。"""
        return list(self.connection.execute(
            "SELECT * FROM assets WHERE source_root = ? ORDER BY catalog_id",
            (str(Path(source_root).resolve()),)).fetchall())

    def insert_asset(
        self,
        *,
        asset_id: str,
        catalog_id: str,
        source: SourceRef,
        file_size: int | None,
        creation_time_fs: str | None,
        last_write_time_fs: str | None,
        file_fingerprint: str | None,
        quick_fingerprint: str | None,
        full_sha256: str | None,
        now: str,
        registration_status: str,
        is_available: bool = True,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO assets (
                asset_id, catalog_id,
                original_source_root, original_source_relative,
                source_root, source_relative,
                file_name, extension, file_size,
                creation_time_fs, last_write_time_fs,
                file_fingerprint, quick_fingerprint, full_sha256,
                first_seen_at, last_seen_at, is_available, registration_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                asset_id, catalog_id,
                str(source.root), source.relative,
                str(source.root), source.relative,
                source.file_name, source.extension, file_size,
                creation_time_fs, last_write_time_fs,
                file_fingerprint, quick_fingerprint, full_sha256,
                now, now, 1 if is_available else 0, registration_status,
            ),
        )

    def update_asset_seen(
        self,
        asset_id: str,
        *,
        source: SourceRef,
        file_size: int | None,
        creation_time_fs: str | None,
        last_write_time_fs: str | None,
        file_fingerprint: str | None,
        quick_fingerprint: str | None,
        full_sha256: str | None,
        now: str,
        registration_status: str,
    ) -> None:
        """既存 asset の現況を更新する。

        **original_source_* は決して変更しない。** 最初にどこで見つけたかは
        履歴として残す。
        """
        self.connection.execute(
            """
            UPDATE assets SET
                source_root = ?,
                source_relative = ?,
                file_name = ?,
                extension = ?,
                file_size = ?,
                creation_time_fs = ?,
                last_write_time_fs = ?,
                file_fingerprint = ?,
                quick_fingerprint = COALESCE(?, quick_fingerprint),
                full_sha256 = COALESCE(?, full_sha256),
                last_seen_at = ?,
                is_available = 1,
                registration_status = ?
            WHERE asset_id = ?
            """,
            (
                str(source.root), source.relative,
                source.file_name, source.extension, file_size,
                creation_time_fs, last_write_time_fs,
                file_fingerprint, quick_fingerprint, full_sha256,
                now, registration_status, asset_id,
            ),
        )

    def mark_assets_unavailable(self, asset_ids: Sequence[str], now: str) -> int:
        """見つからなかった asset を unavailable にする。**行は削除しない。**

        外付けドライブの切断と本当の削除を区別できないため、決して消さない。
        """
        if not asset_ids:
            return 0
        self.connection.executemany(
            "UPDATE assets SET is_available = 0, registration_status = ?, "
            "last_seen_at = ? WHERE asset_id = ?",
            [(REG_MISSING, now, aid) for aid in asset_ids])
        return len(asset_ids)

    # -- probe_results -----------------------------------------------------

    PROBE_COLUMNS = (
        "probe_status", "probe_started_at", "probe_finished_at",
        "probe_duration_ms", "duration_seconds", "format_name",
        "format_long_name", "bit_rate", "video_stream_count",
        "playable_video_stream_count", "attached_picture_stream_count",
        "primary_video_stream_index", "primary_video_selection_rule",
        "audio_stream_count", "primary_audio_stream_index",
        "subtitle_stream_count", "chapter_count", "width", "height",
        "video_codec", "pixel_format", "frame_rate_num", "frame_rate_den",
        "frame_rate_decimal", "audio_codec", "sample_rate", "channel_count",
        "creation_time_tag", "location_tag_present", "raw_probe_cache_path",
        "error_type", "error_message", "exit_code", "ffprobe_version",
        "ffprobe_impl_version",
    )

    def upsert_probe_result(self, asset_id: str, values: dict[str, Any]) -> None:
        """ffprobe の結果を登録または更新する。

        **渡された列だけを書く。** 未指定の列へ明示的に NULL を入れると、
        件数系の NOT NULL 列（video_stream_count など）の既定値 0 が
        効かなくなる。

        ``probe_status`` は必須。SQLite は ON CONFLICT で更新へ回る前に
        挿入行の NOT NULL を検査するため、既存行の部分更新であっても
        省略できない。省略されたら分かる言葉で止める。
        """
        payload = dict(values)
        unknown = set(payload) - set(self.PROBE_COLUMNS)
        if unknown:
            raise ValueError(f"probe_results に無い列です: {sorted(unknown)}")
        if not payload.get("probe_status"):
            raise ValueError("probe_status は必須です。")

        if "raw_probe_cache_path" in payload:
            payload["raw_probe_cache_path"] = store_internal_path(
                payload["raw_probe_cache_path"])

        columns = [c for c in self.PROBE_COLUMNS if c in payload]

        placeholders = ",".join("?" for _ in range(len(columns) + 1))
        updates = ",".join(f"{c} = excluded.{c}" for c in columns)
        self.connection.execute(
            f"INSERT INTO probe_results (asset_id, {','.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(asset_id) DO UPDATE SET {updates}",
            [asset_id] + [payload[c] for c in columns])

    def get_probe_result(self, asset_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM probe_results WHERE asset_id = ?",
            (asset_id,)).fetchone()

    # -- capture_time_candidates -------------------------------------------

    def replace_capture_candidates(
        self, asset_id: str, candidates: Sequence[dict[str, Any]], now: str
    ) -> int:
        """自動抽出した候補を入れ替える。

        **利用者が確認済み（``is_user_confirmed = 1``）の行は削除しない。**
        再解析が人手の確認を上書きしないための保護。
        """
        self.connection.execute(
            "DELETE FROM capture_time_candidates "
            "WHERE asset_id = ? AND is_user_confirmed = 0", (asset_id,))
        inserted = 0
        for candidate in candidates:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO capture_time_candidates (
                    asset_id, candidate_datetime, source_type, source_value,
                    parser_rule, confidence, has_time, is_user_confirmed,
                    created_at
                ) VALUES (?,?,?,?,?,?,?,0,?)
                """,
                (
                    asset_id,
                    candidate["candidate_datetime"],
                    candidate["source_type"],
                    candidate.get("source_value"),
                    candidate.get("parser_rule"),
                    float(candidate.get("confidence") or 0.0),
                    1 if candidate.get("has_time") else 0,
                    now,
                ),
            )
            inserted += 1
        return inserted

    def get_capture_candidates(self, asset_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM capture_time_candidates WHERE asset_id = ? "
            "ORDER BY is_user_confirmed DESC, confidence DESC, has_time DESC",
            (asset_id,)).fetchall())

    def confirm_capture_candidate(self, candidate_id: int) -> None:
        """利用者が確定した候補に印を付ける。**AI 推定とは区別される。**"""
        self.connection.execute(
            "UPDATE capture_time_candidates SET is_user_confirmed = 1 "
            "WHERE candidate_id = ?", (candidate_id,))

    # -- asset_relations ---------------------------------------------------

    def add_relation(
        self,
        *,
        source_asset_id: str,
        target_asset_id: str,
        relation_type: str,
        sequence_index: int | None = None,
        confidence: float = 0.0,
        evidence: str | None = None,
        created_at: str,
        confirmed_by_user: bool = False,
    ) -> None:
        if relation_type not in KNOWN_RELATION_TYPES:
            raise ValueError(f"未知の relation_type です: {relation_type}")
        self.connection.execute(
            """
            INSERT OR IGNORE INTO asset_relations (
                source_asset_id, target_asset_id, relation_type, sequence_index,
                confidence, evidence, created_at, confirmed_by_user
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (source_asset_id, target_asset_id, relation_type, sequence_index,
             confidence, evidence, created_at, 1 if confirmed_by_user else 0),
        )

    def get_relations_for_target(self, target_asset_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM asset_relations WHERE target_asset_id = ? "
            "ORDER BY sequence_index IS NULL, sequence_index",
            (target_asset_id,)).fetchall())

    # -- processing_runs ---------------------------------------------------

    def start_run(
        self,
        *,
        run_id: str,
        source_root: str | None,
        started_at: str,
        worker_count: int,
        config_snapshot: dict[str, Any],
        application_version: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO processing_runs (
                run_id, source_root, started_at, status, worker_count,
                config_snapshot, application_version
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (run_id, source_root, started_at, STATUS_RUNNING, worker_count,
             json.dumps(config_snapshot, ensure_ascii=False),
             application_version),
        )

    def finish_run(
        self,
        run_id: str,
        *,
        finished_at: str,
        status: str,
        files_discovered: int = 0,
        files_processed: int = 0,
        files_reused: int = 0,
        files_failed: int = 0,
        stop_reason: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE processing_runs SET
                finished_at = ?, status = ?, files_discovered = ?,
                files_processed = ?, files_reused = ?, files_failed = ?,
                stop_reason = ?
            WHERE run_id = ?
            """,
            (finished_at, status, files_discovered, files_processed,
             files_reused, files_failed, stop_reason, run_id),
        )

    # -- stage_status ------------------------------------------------------

    def set_stage_status(
        self,
        asset_id: str,
        stage_name: str,
        status: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_message: str | None = None,
        implementation_version: str | None = None,
        increment_attempt: bool = True,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO stage_status (
                asset_id, stage_name, status, attempt_count,
                last_started_at, last_finished_at, error_message,
                implementation_version
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_id, stage_name) DO UPDATE SET
                status = excluded.status,
                attempt_count = stage_status.attempt_count + ?,
                last_started_at = COALESCE(excluded.last_started_at,
                                           stage_status.last_started_at),
                last_finished_at = COALESCE(excluded.last_finished_at,
                                            stage_status.last_finished_at),
                error_message = excluded.error_message,
                implementation_version = excluded.implementation_version
            """,
            (asset_id, stage_name, status, 1 if increment_attempt else 0,
             started_at, finished_at, error_message, implementation_version,
             1 if increment_attempt else 0),
        )

    def get_stage_status(self, asset_id: str, stage_name: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stage_status WHERE asset_id = ? AND stage_name = ?",
            (asset_id, stage_name)).fetchone()

    def is_stage_done(self, asset_id: str, stage_name: str) -> bool:
        """その工程をもう一度やらなくてよいか。**Resume の判定。**"""
        row = self.get_stage_status(asset_id, stage_name)
        return bool(row and row["status"] in DONE_STATUSES)

    # -- 代表画像 ----------------------------------------------------------

    def start_extraction_run(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        payload["output_directory"] = store_internal_path(
            payload.get("output_directory"))
        columns = [
            "extraction_run_id", "asset_id", "started_at", "status",
            "implementation_version", "config_hash", "config_json",
            "source_quick_fingerprint", "primary_video_stream_index",
            "duration_seconds", "planned_frame_count", "output_directory",
        ]
        self.connection.execute(
            f"INSERT INTO frame_extraction_runs ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [payload.get(c) for c in columns])

    def finish_extraction_run(
        self,
        extraction_run_id: str,
        *,
        finished_at: str,
        status: str,
        successful_frame_count: int = 0,
        failed_frame_count: int = 0,
        reused_frame_count: int = 0,
        error_message: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE frame_extraction_runs SET
                finished_at = ?, status = ?, successful_frame_count = ?,
                failed_frame_count = ?, reused_frame_count = ?, error_message = ?
            WHERE extraction_run_id = ?
            """,
            (finished_at, status, successful_frame_count, failed_frame_count,
             reused_frame_count, error_message, extraction_run_id))

    def get_extraction_run(self, extraction_run_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM frame_extraction_runs WHERE extraction_run_id = ?",
            (extraction_run_id,)).fetchone()

    def get_latest_completed_extraction_runs(
        self, asset_id: str
    ) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM frame_extraction_runs WHERE asset_id = ? "
            "AND status IN (?, ?) ORDER BY started_at DESC",
            (asset_id, STATUS_COMPLETED, STATUS_PARTIAL)).fetchall())

    def find_existing_frame(
        self,
        *,
        asset_id: str,
        implementation_version: str,
        config_hash: str,
        source_quick_fingerprint: str | None,
        target_time_milliseconds: int,
    ) -> sqlite3.Row | None:
        """再利用できる既存フレームを探す。"""
        return self.connection.execute(
            """
            SELECT * FROM extracted_frames
            WHERE asset_id = ? AND implementation_version = ? AND config_hash = ?
              AND source_quick_fingerprint IS ? AND target_time_milliseconds = ?
            """,
            (asset_id, implementation_version, config_hash,
             source_quick_fingerprint, target_time_milliseconds)).fetchone()

    def upsert_frame(self, values: dict[str, Any]) -> None:
        """フレームを登録または更新する（重複行を作らない）。"""
        payload = dict(values)
        payload["file_path"] = store_internal_path(payload.get("file_path"))
        self.connection.execute(
            """
            INSERT INTO extracted_frames (
                extraction_run_id, last_verified_run_id, asset_id,
                implementation_version, config_hash, source_quick_fingerprint,
                sequence_index, target_time_seconds, target_time_milliseconds,
                relative_position, file_path, file_size, width, height,
                image_format, sha256, extraction_status, ffmpeg_exit_code,
                ffmpeg_duration_ms, error_message, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_id, implementation_version, config_hash,
                        source_quick_fingerprint, target_time_milliseconds)
            DO UPDATE SET
                extraction_run_id = excluded.extraction_run_id,
                last_verified_run_id = excluded.last_verified_run_id,
                sequence_index = excluded.sequence_index,
                relative_position = excluded.relative_position,
                file_path = excluded.file_path,
                file_size = excluded.file_size,
                width = excluded.width,
                height = excluded.height,
                image_format = excluded.image_format,
                sha256 = excluded.sha256,
                extraction_status = excluded.extraction_status,
                ffmpeg_exit_code = excluded.ffmpeg_exit_code,
                ffmpeg_duration_ms = excluded.ffmpeg_duration_ms,
                error_message = excluded.error_message,
                updated_at = excluded.updated_at
            """,
            (
                payload["extraction_run_id"], payload.get("last_verified_run_id"),
                payload["asset_id"], payload["implementation_version"],
                payload["config_hash"], payload.get("source_quick_fingerprint"),
                payload["sequence_index"], payload["target_time_seconds"],
                payload["target_time_milliseconds"],
                payload.get("relative_position"), payload.get("file_path"),
                payload.get("file_size"), payload.get("width"),
                payload.get("height"), payload.get("image_format"),
                payload.get("sha256"), payload["extraction_status"],
                payload.get("ffmpeg_exit_code"), payload.get("ffmpeg_duration_ms"),
                payload.get("error_message"), payload["created_at"],
                payload.get("updated_at"),
            ),
        )

    def mark_frame_reused(self, frame_id: int, *, run_id: str,
                          updated_at: str) -> None:
        """既存フレームを再利用したことを記録する。

        **画像を作った run は変更しない。** 履歴を残すため。
        """
        self.connection.execute(
            "UPDATE extracted_frames SET last_verified_run_id = ?, updated_at = ? "
            "WHERE frame_id = ?", (run_id, updated_at, frame_id))

    def get_frames_by_extraction_set(
        self,
        *,
        asset_id: str,
        implementation_version: str,
        config_hash: str,
        source_quick_fingerprint: str | None,
    ) -> list[sqlite3.Row]:
        """1 つの抽出セットに属する成功フレームを順番に返す。

        **ファイルシステムを検索せず、台帳の記録を情報源にする。**
        """
        return list(self.connection.execute(
            """
            SELECT * FROM extracted_frames
            WHERE asset_id = ? AND implementation_version = ?
              AND config_hash = ? AND source_quick_fingerprint IS ?
              AND extraction_status IN (?, ?)
            ORDER BY sequence_index
            """,
            (asset_id, implementation_version, config_hash,
             source_quick_fingerprint, STATUS_OK, STATUS_REUSED)).fetchall())

    def count_successful_frames(
        self,
        *,
        asset_id: str,
        implementation_version: str,
        config_hash: str,
        source_quick_fingerprint: str | None,
    ) -> int:
        return int(self.connection.execute(
            """
            SELECT COUNT(*) AS c FROM extracted_frames
            WHERE asset_id = ? AND implementation_version = ?
              AND config_hash = ? AND source_quick_fingerprint IS ?
              AND extraction_status IN (?, ?)
            """,
            (asset_id, implementation_version, config_hash,
             source_quick_fingerprint, STATUS_OK, STATUS_REUSED),
        ).fetchone()["c"])

    # -- 映像解析 ----------------------------------------------------------

    VISUAL_RUN_COLUMNS = (
        "visual_run_id", "asset_id", "catalog_id", "started_at", "status",
        "implementation_version", "frame_prompt_version",
        "summary_prompt_version", "model_id", "model_api_base",
        "config_hash", "config_json", "source_quick_fingerprint",
        "frame_extraction_implementation_version",
        "frame_extraction_config_hash", "planned_frame_count",
    )

    def start_visual_run(self, values: dict[str, Any]) -> None:
        columns = list(self.VISUAL_RUN_COLUMNS)
        self.connection.execute(
            f"INSERT INTO visual_analysis_runs ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [values.get(c) for c in columns])

    def finish_visual_run(self, visual_run_id: str, values: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE visual_analysis_runs SET
                finished_at = ?, status = ?, successful_frame_count = ?,
                failed_frame_count = ?, reused_frame_count = ?,
                repair_attempt_count = ?, summary_status = ?,
                frame_total_duration_ms = ?, summary_duration_ms = ?,
                error_message = ?, model_id = COALESCE(?, model_id)
            WHERE visual_run_id = ?
            """,
            (values.get("finished_at"), values.get("status"),
             values.get("successful_frame_count", 0),
             values.get("failed_frame_count", 0),
             values.get("reused_frame_count", 0),
             values.get("repair_attempt_count", 0),
             values.get("summary_status"),
             values.get("frame_total_duration_ms"),
             values.get("summary_duration_ms"),
             values.get("error_message"), values.get("model_id"),
             visual_run_id))

    def get_visual_run(self, visual_run_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM visual_analysis_runs WHERE visual_run_id = ?",
            (visual_run_id,)).fetchone()

    def find_frame_analysis(
        self,
        *,
        asset_id: str,
        frame_sha256: str,
        model_id: str,
        prompt_version: str,
        implementation_version: str,
        config_hash: str,
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM frame_visual_analyses
            WHERE asset_id = ? AND frame_sha256 = ? AND model_id = ?
              AND prompt_version = ? AND implementation_version = ?
              AND config_hash = ?
            """,
            (asset_id, frame_sha256, model_id, prompt_version,
             implementation_version, config_hash)).fetchone()

    FRAME_ANALYSIS_COLUMNS = (
        "visual_run_id", "asset_id", "frame_id", "sequence_index",
        "frame_sha256", "target_time_milliseconds", "model_id",
        "prompt_version", "implementation_version", "config_hash",
        "analysis_status", "attempt_count", "caption",
        "structured_analysis_json", "raw_response_json", "result_file_path",
        "request_duration_ms", "prompt_tokens", "completion_tokens",
        "total_tokens", "error_type", "error_message", "created_at",
        "last_verified_at", "last_verified_run_id",
    )

    def upsert_frame_analysis(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        payload["result_file_path"] = store_internal_path(
            payload.get("result_file_path"))
        columns = list(self.FRAME_ANALYSIS_COLUMNS)
        updatable = [c for c in columns if c != "created_at"]
        updates = ",".join(f"{c} = excluded.{c}" for c in updatable)
        self.connection.execute(
            f"INSERT INTO frame_visual_analyses ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(asset_id, frame_sha256, model_id, prompt_version, "
            f"implementation_version, config_hash) DO UPDATE SET {updates}",
            [payload.get(c) for c in columns])

    def get_frame_analyses_for_run(self, visual_run_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM frame_visual_analyses "
            "WHERE visual_run_id = ? OR last_verified_run_id = ? "
            "ORDER BY sequence_index", (visual_run_id, visual_run_id)).fetchall())

    def find_visual_summary(
        self,
        *,
        asset_id: str,
        source_frame_analysis_hash: str,
        model_id: str,
        prompt_version: str,
        implementation_version: str,
        config_hash: str,
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM asset_visual_summaries
            WHERE asset_id = ? AND source_frame_analysis_hash = ?
              AND model_id = ? AND prompt_version = ?
              AND implementation_version = ? AND config_hash = ?
            """,
            (asset_id, source_frame_analysis_hash, model_id, prompt_version,
             implementation_version, config_hash)).fetchone()

    VISUAL_SUMMARY_COLUMNS = (
        "visual_run_id", "asset_id", "catalog_id", "model_id",
        "prompt_version", "implementation_version", "config_hash",
        "source_frame_analysis_hash", "summary_status", "title_candidate",
        "visual_summary", "main_activity", "structured_summary_json",
        "raw_response_json", "result_file_path", "request_duration_ms",
        "prompt_tokens", "completion_tokens", "total_tokens", "error_type",
        "error_message", "created_at", "last_verified_at",
        "last_verified_run_id",
    )

    def upsert_visual_summary(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        payload["result_file_path"] = store_internal_path(
            payload.get("result_file_path"))
        columns = list(self.VISUAL_SUMMARY_COLUMNS)
        updatable = [c for c in columns if c != "created_at"]
        updates = ",".join(f"{c} = excluded.{c}" for c in updatable)
        self.connection.execute(
            f"INSERT INTO asset_visual_summaries ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(asset_id, source_frame_analysis_hash, model_id, "
            f"prompt_version, implementation_version, config_hash) "
            f"DO UPDATE SET {updates}",
            [payload.get(c) for c in columns])

    def get_latest_visual_summary(self, asset_id: str) -> sqlite3.Row | None:
        """最後に成功した視覚概要。設定ハッシュは問わない。"""
        return self.connection.execute(
            "SELECT * FROM asset_visual_summaries WHERE asset_id = ? "
            "AND summary_status IN (?, ?) "
            "ORDER BY asset_visual_summary_id DESC LIMIT 1",
            (asset_id, STATUS_OK, STATUS_REUSED)).fetchone()

    # -- 文字起こし --------------------------------------------------------

    ASR_RUN_COLUMNS = (
        "asr_run_id", "asset_id", "catalog_id", "started_at", "status",
        "implementation_version", "engine_name", "ffmpeg_version",
        "model_name", "model_sha256", "config_hash", "config_json",
        "source_quick_fingerprint", "primary_audio_stream_index",
        "scope_type", "scope_start_seconds", "scope_duration_seconds",
        "language_requested", "vad_enabled", "planned_chunk_count",
        "planned_duration_seconds",
    )

    def start_asr_run(self, values: dict[str, Any]) -> None:
        columns = list(self.ASR_RUN_COLUMNS)
        self.connection.execute(
            f"INSERT INTO asr_runs ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [values.get(c) for c in columns])

    def finish_asr_run(self, asr_run_id: str, values: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE asr_runs SET
                finished_at = ?, status = ?, processed_chunk_count = ?,
                reused_chunk_count = ?, failed_chunk_count = ?,
                processed_duration_seconds = ?, processing_duration_ms = ?,
                real_time_factor = ?, segment_count = ?, stop_reason = ?,
                error_type = ?, error_message = ?
            WHERE asr_run_id = ?
            """,
            (values.get("finished_at"), values.get("status"),
             values.get("processed_chunk_count", 0),
             values.get("reused_chunk_count", 0),
             values.get("failed_chunk_count", 0),
             values.get("processed_duration_seconds"),
             values.get("processing_duration_ms"),
             values.get("real_time_factor"), values.get("segment_count"),
             values.get("stop_reason"), values.get("error_type"),
             values.get("error_message"), asr_run_id))

    def find_asr_chunk(
        self,
        *,
        asset_id: str,
        source_quick_fingerprint: str | None,
        primary_audio_stream_index: int | None,
        chunk_index: int,
        absolute_start_seconds: float,
        duration_seconds: float,
        engine_name: str,
        implementation_version: str,
        model_sha256: str | None,
        config_hash: str,
    ) -> sqlite3.Row | None:
        """再利用できるチャンクを探す。**Resume の最小単位。**"""
        return self.connection.execute(
            """
            SELECT * FROM asr_chunks
            WHERE asset_id = ? AND source_quick_fingerprint IS ?
              AND primary_audio_stream_index IS ? AND chunk_index = ?
              AND absolute_start_seconds = ? AND duration_seconds = ?
              AND engine_name = ? AND implementation_version = ?
              AND model_sha256 IS ? AND config_hash = ?
            """,
            (asset_id, source_quick_fingerprint, primary_audio_stream_index,
             chunk_index, absolute_start_seconds, duration_seconds,
             engine_name, implementation_version, model_sha256,
             config_hash)).fetchone()

    ASR_CHUNK_COLUMNS = (
        "asr_run_id", "asset_id", "chunk_index", "absolute_start_seconds",
        "duration_seconds", "overlap_seconds", "source_quick_fingerprint",
        "primary_audio_stream_index", "engine_name", "implementation_version",
        "model_sha256", "config_hash", "chunk_status", "transcript_text",
        "normalized_chunk_json", "raw_engine_output", "result_file_path",
        "segment_count", "processing_duration_ms", "real_time_factor",
        "attempt_count", "error_type", "error_message", "created_at",
        "updated_at", "last_verified_at",
    )

    def upsert_asr_chunk(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        payload["result_file_path"] = store_internal_path(
            payload.get("result_file_path"))
        columns = list(self.ASR_CHUNK_COLUMNS)
        updatable = [c for c in columns if c != "created_at"]
        updates = ",".join(f"{c} = excluded.{c}" for c in updatable)
        self.connection.execute(
            f"INSERT INTO asr_chunks ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(asset_id, source_quick_fingerprint, "
            f"primary_audio_stream_index, chunk_index, absolute_start_seconds, "
            f"duration_seconds, engine_name, implementation_version, "
            f"model_sha256, config_hash) DO UPDATE SET {updates}",
            [payload.get(c) for c in columns])

    TRANSCRIPT_COLUMNS = (
        "asr_run_id", "asset_id", "catalog_id", "implementation_version",
        "engine_name", "model_sha256", "config_hash",
        "source_quick_fingerprint", "primary_audio_stream_index",
        "scope_type", "scope_start_seconds", "scope_duration_seconds",
        "language_requested", "language_detected", "transcript_status",
        "full_text", "normalized_transcript_json", "result_file_path",
        "segment_count", "processing_duration_ms", "real_time_factor",
        "created_at", "last_verified_at", "last_verified_run_id",
    )

    def upsert_transcript(self, values: dict[str, Any]) -> int:
        """統合済み文字起こしを登録または更新し、transcript_id を返す。"""
        payload = dict(values)
        payload["result_file_path"] = store_internal_path(
            payload.get("result_file_path"))
        columns = list(self.TRANSCRIPT_COLUMNS)
        updatable = [c for c in columns if c != "created_at"]
        updates = ",".join(f"{c} = excluded.{c}" for c in updatable)
        self.connection.execute(
            f"INSERT INTO transcripts ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(asset_id, source_quick_fingerprint, "
            f"primary_audio_stream_index, scope_type, scope_start_seconds, "
            f"scope_duration_seconds, engine_name, implementation_version, "
            f"model_sha256, config_hash) DO UPDATE SET {updates}",
            [payload.get(c) for c in columns])
        row = self.connection.execute(
            """
            SELECT transcript_id FROM transcripts
            WHERE asset_id = ? AND source_quick_fingerprint IS ?
              AND primary_audio_stream_index IS ? AND scope_type = ?
              AND scope_start_seconds = ? AND scope_duration_seconds = ?
              AND engine_name = ? AND implementation_version = ?
              AND model_sha256 IS ? AND config_hash = ?
            """,
            (payload["asset_id"], payload.get("source_quick_fingerprint"),
             payload.get("primary_audio_stream_index"), payload["scope_type"],
             payload["scope_start_seconds"], payload["scope_duration_seconds"],
             payload["engine_name"], payload["implementation_version"],
             payload.get("model_sha256"), payload["config_hash"])).fetchone()
        return int(row["transcript_id"])

    def replace_transcript_segments(
        self, transcript_id: int, segments: Sequence[dict[str, Any]], now: str
    ) -> int:
        """セグメントを入れ替える。

        **幻覚の疑い（``is_suspected_hallucination``）は保存する。**
        印を付けるだけで、本文は消さない。説明文の材料から外すのは
        ``description`` 工程の役目であり、ここではない。
        """
        self.connection.execute(
            "DELETE FROM transcript_segments WHERE transcript_id = ?",
            (transcript_id,))
        for segment in segments:
            self.connection.execute(
                """
                INSERT INTO transcript_segments (
                    transcript_id, asset_id, sequence_index,
                    start_seconds, end_seconds,
                    absolute_start_seconds, absolute_end_seconds,
                    text, confidence, is_suspected_hallucination, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (transcript_id, segment["asset_id"], segment["sequence_index"],
                 segment["start_seconds"], segment["end_seconds"],
                 segment["absolute_start_seconds"],
                 segment["absolute_end_seconds"], segment["text"],
                 segment.get("confidence"),
                 1 if segment.get("is_suspected_hallucination") else 0, now))
        return len(segments)

    def get_transcript_segments(self, transcript_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM transcript_segments WHERE transcript_id = ? "
            "ORDER BY sequence_index", (transcript_id,)).fetchall())

    def get_transcripts_for_asset(self, asset_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM transcripts WHERE asset_id = ? "
            "ORDER BY transcript_id DESC", (asset_id,)).fetchall())

    # -- 最終テキスト ------------------------------------------------------

    DESCRIPTION_COLUMNS = (
        "asset_id", "catalog_id", "source_root", "source_relative",
        "file_name", "description_file_path", "description_status",
        "recorded_from", "recorded_to", "recorded_precision",
        "recorded_source", "recorded_raw_text", "used_visual_analysis",
        "used_transcription", "generator", "model_id",
        "implementation_version", "created_at", "updated_at",
    )

    def upsert_description(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        payload["description_file_path"] = store_internal_path(
            payload.get("description_file_path"))
        columns = list(self.DESCRIPTION_COLUMNS)
        updatable = [c for c in columns if c not in ("asset_id", "created_at")]
        updates = ",".join(f"{c} = excluded.{c}" for c in updatable)
        self.connection.execute(
            f"INSERT INTO asset_descriptions ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(asset_id) DO UPDATE SET {updates}",
            [payload.get(c) for c in columns])

    def get_description(self, asset_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM asset_descriptions WHERE asset_id = ?",
            (asset_id,)).fetchone()

    def record_cache_cleanup(
        self, asset_id: str, *, status: str, at: str, freed_bytes: int
    ) -> None:
        self.connection.execute(
            "UPDATE asset_descriptions SET cache_cleanup_status = ?, "
            "cache_cleanup_at = ?, cache_freed_bytes = ? WHERE asset_id = ?",
            (status, at, freed_bytes, asset_id))
