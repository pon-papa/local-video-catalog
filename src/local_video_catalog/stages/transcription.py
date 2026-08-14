"""文字起こし（工程 4/5）を台帳へ結びつける.

**チャンク単位で保存する。** 中断したときの損失は最大 1 チャンク。

**生データを「きれいにする」ために消さない。** 幻覚の疑いがある
セグメントにも印を付けて保存する。材料から外すのは説明文の工程であって、
ここではない。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from .. import ASR_IMPL_VERSION
from .. import asr_engine as ae
from .. import database as db_module
from .. import pipeline as pipeline_module
from .. import transcript_schemas as ts
from ..logging_utils import local_now_iso
from ..source_ref import SourceRef

SCOPE_FULL = "full"


def _model_sha256(path: Path) -> str:
    """モデルの同一性。**中身が変われば再処理させる。**

    大きいファイルなので先頭・末尾とサイズで代表させる。
    """
    size = path.stat().st_size
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            handle.seek(-1024 * 1024, 2)
            digest.update(handle.read(1024 * 1024))
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def run_transcription(asset_id: str, context) -> "pipeline_module.StageOutcome":
    """1 本ぶんの文字起こしを作る。"""
    outcome = pipeline_module.StageOutcome
    database = context.database
    logger = context.logger

    row = database.find_assets_by_identifier(asset_id)
    if not row:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "台帳にこの動画がありません。")
    asset = row[0]
    source = SourceRef.from_row(asset)

    probe = database.get_probe_result(asset_id)
    if probe is None:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "先に動画の基本情報を取得してください。")
    if not probe["audio_stream_count"]:
        # 音声が無い動画。**異常ではない。** 毎回やり直さない。
        logger.info("    音声がないため、文字起こしは行いません。")
        return outcome.ok(db_module.STATUS_SKIPPED_NO_AUDIO)

    duration = probe["duration_seconds"]
    if not duration or duration <= 0:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "再生時間が分からないため処理できません。")
    if context.settings.ffmpeg_path is None:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "ffmpeg が見つかりません。設定を確認してください。")
    if not source.absolute.is_file():
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              f"元動画が見つかりません: {source.absolute}")

    config = ae.AsrConfig.from_settings(context.raw)
    usable, reason = ae.check_model(config)
    if not usable:
        return outcome.failed(pipeline_module.FAILURE_MODEL, reason)

    model_sha = _model_sha256(ae.model_path(config))
    quick_fp = asset["quick_fingerprint"]
    audio_index = probe["primary_audio_stream_index"]
    planned = ae.plan_chunks(float(duration), config)
    if not planned:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "チャンクを決められませんでした。")

    directory = ae.chunk_output_directory(asset_id, config, quick_fp)
    run_id = uuid.uuid4().hex
    started = local_now_iso()

    with database.transaction():
        database.start_asr_run({
            "asr_run_id": run_id, "asset_id": asset_id,
            "catalog_id": asset["catalog_id"], "started_at": started,
            "status": db_module.STATUS_RUNNING,
            "implementation_version": ASR_IMPL_VERSION,
            "engine_name": ae.ENGINE_NAME,
            "model_name": config.model_name, "model_sha256": model_sha,
            "config_hash": config.config_hash,
            "config_json": json.dumps(config.to_dict(), ensure_ascii=False),
            "source_quick_fingerprint": quick_fp,
            "primary_audio_stream_index": audio_index,
            "scope_type": SCOPE_FULL, "scope_start_seconds": 0.0,
            "scope_duration_seconds": float(duration),
            "language_requested": config.language,
            "vad_enabled": 1 if config.vad_enabled else 0,
            "planned_chunk_count": len(planned),
            "planned_duration_seconds": float(duration),
        })

    collected: list[tuple[float, ts.NormalizedChunk]] = []
    processed = reused = failed = 0
    interrupted = False

    for chunk in planned:
        if pipeline_module.stop_requested() or context.out_of_time():
            interrupted = True
            break

        key = dict(
            asset_id=asset_id, source_quick_fingerprint=quick_fp,
            primary_audio_stream_index=audio_index,
            chunk_index=chunk.chunk_index,
            absolute_start_seconds=chunk.absolute_start_seconds,
            duration_seconds=chunk.duration_seconds,
            engine_name=ae.ENGINE_NAME,
            implementation_version=ASR_IMPL_VERSION,
            model_sha256=model_sha, config_hash=config.config_hash)

        existing = database.find_asr_chunk(**key)
        if existing is not None and existing["chunk_status"] in (
                ae.CHUNK_COMPLETED, ae.CHUNK_NO_SPEECH):
            stored = existing["normalized_chunk_json"]
            if stored:
                payload = json.loads(stored)
                restored = ts.NormalizedChunk(
                    segments=[ts.Segment(
                        sequence_index=s["sequence_index"],
                        start_seconds=s["start_seconds"],
                        end_seconds=s["end_seconds"], text=s["text"],
                        confidence=s.get("confidence"),
                        is_suspected_hallucination=bool(
                            s.get("is_suspected_hallucination")))
                        for s in payload.get("segments", [])],
                    warnings=list(payload.get("warnings", [])),
                    status=payload.get("status", ts.STATUS_COMPLETED))
                collected.append((chunk.absolute_start_seconds, restored))
                reused += 1
                continue

        result = ae.run_chunk(
            context.settings.ffmpeg_path, source.absolute, chunk, directory,
            config, audio_stream_index=audio_index)

        values = {
            **key, "asr_run_id": run_id,
            "overlap_seconds": chunk.overlap_seconds,
            "attempt_count": 1,
            "processing_duration_ms": result.processing_duration_ms,
            "created_at": local_now_iso(), "updated_at": local_now_iso(),
            "last_verified_at": local_now_iso(),
        }

        if not result.ok:
            values.update({"chunk_status": ae.CHUNK_FAILED,
                           "error_type": result.error_type,
                           "error_message": result.error_message})
            failed += 1
            with database.transaction():
                database.upsert_asr_chunk(values)
            continue

        normalized = ts.normalize_engine_items(
            result.items, chunk_duration_seconds=chunk.duration_seconds)
        values.update({
            "chunk_status": (ae.CHUNK_NO_SPEECH
                             if normalized.status == ts.STATUS_NO_SPEECH
                             else ae.CHUNK_COMPLETED),
            "transcript_text": normalized.text,
            "normalized_chunk_json": json.dumps(normalized.to_dict(),
                                                ensure_ascii=False),
            "segment_count": len(normalized.segments),
        })
        with database.transaction():
            database.upsert_asr_chunk(values)
        collected.append((chunk.absolute_start_seconds, normalized))
        processed += 1

    if not collected:
        with database.transaction():
            database.finish_asr_run(run_id, {
                "finished_at": local_now_iso(),
                "status": db_module.STATUS_FAILED,
                "processed_chunk_count": processed, "reused_chunk_count": reused,
                "failed_chunk_count": failed,
                "error_message": "処理できたチャンクがありません。"})
        if interrupted:
            return pipeline_module.StageOutcome(
                done=False, status=db_module.STATUS_INTERRUPTED,
                failure_kind=pipeline_module.FAILURE_OTHER,
                message="中断されました。次回は続きから処理します。")
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "処理できたチャンクがありません。")

    merged = ts.merge_chunks(collected)

    # 統合結果を保存する。**幻覚の疑いも印つきで残す。**
    transcript_id = None
    with database.transaction():
        transcript_id = database.upsert_transcript({
            "asr_run_id": run_id, "asset_id": asset_id,
            "catalog_id": asset["catalog_id"],
            "implementation_version": ASR_IMPL_VERSION,
            "engine_name": ae.ENGINE_NAME, "model_sha256": model_sha,
            "config_hash": config.config_hash,
            "source_quick_fingerprint": quick_fp,
            "primary_audio_stream_index": audio_index,
            "scope_type": SCOPE_FULL, "scope_start_seconds": 0.0,
            "scope_duration_seconds": float(duration),
            "language_requested": config.language,
            "transcript_status": (db_module.STATUS_NO_SPEECH
                                  if merged.status == ts.STATUS_NO_SPEECH
                                  else db_module.STATUS_COMPLETED),
            "full_text": merged.text,
            "normalized_transcript_json": json.dumps(merged.to_dict(),
                                                     ensure_ascii=False),
            "segment_count": len(merged.segments),
            "created_at": local_now_iso(),
            "last_verified_at": local_now_iso(),
            "last_verified_run_id": run_id,
        })
        database.replace_transcript_segments(
            transcript_id,
            [{**segment.to_dict(), "asset_id": asset_id,
              "absolute_start_seconds": segment.start_seconds,
              "absolute_end_seconds": segment.end_seconds}
             for segment in merged.segments],
            local_now_iso())

    incomplete = failed or interrupted or (processed + reused) < len(planned)
    with database.transaction():
        database.finish_asr_run(run_id, {
            "finished_at": local_now_iso(),
            "status": (db_module.STATUS_PARTIAL if incomplete
                       else db_module.STATUS_COMPLETED),
            "processed_chunk_count": processed, "reused_chunk_count": reused,
            "failed_chunk_count": failed,
            "segment_count": len(merged.segments),
            "stop_reason": "interrupted" if interrupted else None})

    suspected = merged.suspected_count
    logger.info(f"    チャンク {processed + reused}/{len(planned)}"
                f"（新規 {processed} / 再利用 {reused} / 失敗 {failed}）"
                f"・セグメント {len(merged.segments)} 件"
                + (f"・うち定型の疑い {suspected} 件" if suspected else ""))

    if incomplete:
        # **completed にしない。** 次回に残りをやり直す。
        return pipeline_module.StageOutcome(
            done=False, status=db_module.STATUS_PARTIAL,
            failure_kind=pipeline_module.FAILURE_OTHER,
            message="一部のチャンクが未完了です。次回は続きから処理します。")
    if merged.status == ts.STATUS_NO_SPEECH:
        logger.info("    内容として使える発話は確認できませんでした。")
        return outcome.ok(db_module.STATUS_NO_SPEECH)
    return outcome.ok()
