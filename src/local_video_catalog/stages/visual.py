"""映像解析（工程 3/5）を台帳へ結びつける.

**接続先は localhost の LM Studio だけ。** ``vlm_client`` が
画像を組み立てる前に検証する。

待ち時間の扱いが要:

  - フレーム 1 枚 …… ``timeout_seconds``（実測 20〜40 秒）
  - 視覚概要 ……… ``summary_timeout_seconds``（**枚数に比例して伸びる**）

概要へフレームと同じ 300 秒を適用したせいで、22〜24 枚の動画が
まとめて失敗した実績がある。**同じ失敗を繰り返さない。**

**待ち時間は config_hash に含めない。** 生成内容を変えないので、
値を変えても保存済みのフレーム解析はそのまま再利用できる。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path

from .. import FRAME_EXTRACTION_IMPL_VERSION, VISUAL_ANALYSIS_IMPL_VERSION
from .. import database as db_module
from .. import frame_extractor as fx
from .. import pipeline as pipeline_module
from .. import visual_schemas as schemas
from .. import vlm_client as vc
from ..logging_utils import local_now_iso

_ERROR_TO_FAILURE = {
    vc.ERROR_CONNECTION: pipeline_module.FAILURE_CONNECTION,
    vc.ERROR_TIMEOUT: pipeline_module.FAILURE_TIMEOUT,
    vc.ERROR_MODEL_NOT_FOUND: pipeline_module.FAILURE_MODEL,
    vc.ERROR_PRIVACY_CONFIG: pipeline_module.FAILURE_PRIVACY,
}


def _config_hash(settings: vc.VlmSettings, model_id: str) -> str:
    """再利用キー。**生成内容に影響するものだけ**から作る。"""
    material = json.dumps({
        "impl": VISUAL_ANALYSIS_IMPL_VERSION,
        "frame_prompt": schemas.FRAME_PROMPT_VERSION,
        "summary_prompt": schemas.SUMMARY_PROMPT_VERSION,
        "model_id": model_id,
        **settings.generation_identity(),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _client_factory(settings: vc.VlmSettings):
    return vc.LocalVlmClient(settings)


def run_visual_analysis(asset_id: str, context,
                        *, client_factory=_client_factory
                        ) -> "pipeline_module.StageOutcome":
    """フレームごとの解析と、全体の視覚概要を作る。

    ``client_factory`` を差し替えられるのは、LM Studio 無しで
    接続の扱いを試験するため。
    """
    outcome = pipeline_module.StageOutcome
    database = context.database
    logger = context.logger

    settings = vc.VlmSettings.from_settings(context.raw)
    try:
        client = client_factory(settings)
    except vc.PrivacyConfigurationError as exc:
        return outcome.failed(pipeline_module.FAILURE_PRIVACY, str(exc))
    except vc.VlmError as exc:
        return outcome.failed(
            _ERROR_TO_FAILURE.get(exc.error_type, pipeline_module.FAILURE_OTHER),
            str(exc))

    try:
        model_id = vc.select_model(client.list_models(), settings.model_match)
    except vc.ModelSelectionError as exc:
        return outcome.failed(pipeline_module.FAILURE_MODEL, str(exc))
    except vc.VlmError as exc:
        return outcome.failed(
            _ERROR_TO_FAILURE.get(exc.error_type, pipeline_module.FAILURE_OTHER),
            str(exc))

    # 解析対象は「台帳に記録された代表画像」。
    # **ファイルシステムを検索しない。**
    frame_config = fx.ExtractionConfig.from_settings(context.raw)
    row = database.find_assets_by_identifier(asset_id)[0]
    frames = database.get_frames_by_extraction_set(
        asset_id=asset_id,
        implementation_version=FRAME_EXTRACTION_IMPL_VERSION,
        config_hash=frame_config.config_hash,
        source_quick_fingerprint=row["quick_fingerprint"])
    if not frames:
        logger.info("    代表画像がないため、映像解析は行いません。")
        return outcome.ok(db_module.STATUS_SKIPPED_NO_FRAMES)

    config_hash = _config_hash(settings, model_id)
    run_id = uuid.uuid4().hex
    started = local_now_iso()

    with database.transaction():
        database.start_visual_run({
            "visual_run_id": run_id, "asset_id": asset_id,
            "catalog_id": row["catalog_id"], "started_at": started,
            "status": db_module.STATUS_RUNNING,
            "implementation_version": VISUAL_ANALYSIS_IMPL_VERSION,
            "frame_prompt_version": schemas.FRAME_PROMPT_VERSION,
            "summary_prompt_version": schemas.SUMMARY_PROMPT_VERSION,
            "model_id": model_id,
            "model_api_base": vc.safe_api_base_for_record(settings.base_url),
            "config_hash": config_hash,
            "source_quick_fingerprint": row["quick_fingerprint"],
            "frame_extraction_config_hash": frame_config.config_hash,
            "planned_frame_count": len(frames),
        })

    captions: list[str] = []
    succeeded = reused = failed = 0
    blocking: pipeline_module.StageOutcome | None = None
    interrupted = False

    for frame in frames:
        if pipeline_module.stop_requested() or context.out_of_time():
            # **途中のフレームで視覚概要を作らない。**
            # 作ってしまうと「完了」になり、次回は少ない枚数のまま
            # 二度と作り直されない（黙って質が落ちる）。
            interrupted = True
            break

        sha = frame["sha256"] or ""
        existing = database.find_frame_analysis(
            asset_id=asset_id, frame_sha256=sha, model_id=model_id,
            prompt_version=schemas.FRAME_PROMPT_VERSION,
            implementation_version=VISUAL_ANALYSIS_IMPL_VERSION,
            config_hash=config_hash)
        if existing is not None and existing["analysis_status"] == db_module.STATUS_OK:
            captions.append(existing["caption"] or "")
            reused += 1
            continue

        image = db_module.load_internal_path(frame["file_path"])
        if image is None or not image.is_file():
            failed += 1
            continue

        message = vc.build_image_message(
            text=schemas.FRAME_PROMPT,
            image_base64=base64.b64encode(image.read_bytes()).decode("ascii"))

        values = {
            "visual_run_id": run_id, "asset_id": asset_id,
            "frame_id": frame["frame_id"],
            "sequence_index": frame["sequence_index"], "frame_sha256": sha,
            "target_time_milliseconds": frame["target_time_milliseconds"],
            "model_id": model_id,
            "prompt_version": schemas.FRAME_PROMPT_VERSION,
            "implementation_version": VISUAL_ANALYSIS_IMPL_VERSION,
            "config_hash": config_hash, "attempt_count": 1,
            "created_at": local_now_iso(),
            "last_verified_at": local_now_iso(),
            "last_verified_run_id": run_id,
        }

        try:
            text, usage = client.chat(
                model_id=model_id, messages=[message],
                max_tokens=settings.max_tokens_per_frame,
                timeout_seconds=settings.timeout_seconds)
            analysis = schemas.parse_frame_analysis(text)
        except vc.VlmError as exc:
            values.update({"analysis_status": db_module.STATUS_FAILED,
                           "error_type": exc.error_type,
                           "error_message": str(exc)})
            failed += 1
            kind = _ERROR_TO_FAILURE.get(exc.error_type)
            if kind is not None:
                blocking = outcome.failed(kind, str(exc))
        except schemas.SchemaError as exc:
            # 応答が壊れている。**成功として扱わない。**
            values.update({"analysis_status": db_module.STATUS_FAILED,
                           "error_type": exc.error_type,
                           "error_message": str(exc)})
            failed += 1
        else:
            values.update({
                "analysis_status": db_module.STATUS_OK,
                "caption": analysis.caption,
                "structured_analysis_json": json.dumps(analysis.to_dict(),
                                                       ensure_ascii=False),
                "request_duration_ms": usage.get("request_duration_ms"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            })
            captions.append(analysis.caption)
            succeeded += 1

        with database.transaction():
            database.upsert_frame_analysis(values)

        if blocking is not None:
            break

    if blocking is not None:
        with database.transaction():
            database.finish_visual_run(run_id, {
                "finished_at": local_now_iso(),
                "status": db_module.STATUS_FAILED,
                "successful_frame_count": succeeded,
                "failed_frame_count": failed, "reused_frame_count": reused,
                "error_message": blocking.message})
        return blocking

    if interrupted:
        # 済んだフレームの結果は保存済み。次回はそれを再利用して残りを行う。
        # **失敗ではない**ので、そう分かる形で返す。
        done = succeeded + reused
        with database.transaction():
            database.finish_visual_run(run_id, {
                "finished_at": local_now_iso(),
                "status": db_module.STATUS_INTERRUPTED,
                "successful_frame_count": succeeded,
                "failed_frame_count": failed, "reused_frame_count": reused,
                "error_message": "中断されました。"})
        return pipeline_module.StageOutcome.stopped(
            db_module.STATUS_PARTIAL,
            f"止めたため映像の解析を途中で終了しました。"
            f"{len(frames)} 枚中 {done} 枚完了。次回は残りから再開します。")

    if not captions:
        with database.transaction():
            database.finish_visual_run(run_id, {
                "finished_at": local_now_iso(),
                "status": db_module.STATUS_FAILED,
                "successful_frame_count": succeeded,
                "failed_frame_count": failed, "reused_frame_count": reused,
                "error_message": "解析できたフレームがありません。"})
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "解析できたフレームがありません。")

    # --- 視覚概要 -------------------------------------------------------
    #
    # **フレームとは別の待ち時間**を使う。枚数に比例して伸びるため。
    source_hash = hashlib.sha256(
        "|".join(captions).encode("utf-8")).hexdigest()
    existing_summary = database.find_visual_summary(
        asset_id=asset_id, source_frame_analysis_hash=source_hash,
        model_id=model_id, prompt_version=schemas.SUMMARY_PROMPT_VERSION,
        implementation_version=VISUAL_ANALYSIS_IMPL_VERSION,
        config_hash=config_hash)

    summary_values = {
        "visual_run_id": run_id, "asset_id": asset_id,
        "catalog_id": row["catalog_id"], "model_id": model_id,
        "prompt_version": schemas.SUMMARY_PROMPT_VERSION,
        "implementation_version": VISUAL_ANALYSIS_IMPL_VERSION,
        "config_hash": config_hash,
        "source_frame_analysis_hash": source_hash,
        "created_at": local_now_iso(), "last_verified_at": local_now_iso(),
        "last_verified_run_id": run_id,
    }

    if existing_summary is not None and \
            existing_summary["summary_status"] == db_module.STATUS_OK:
        logger.info("    視覚概要は前回の結果を再利用します。")
        summary_status = db_module.STATUS_REUSED
        result_outcome = outcome.ok()
    else:
        logger.info(f"    視覚概要を作成します（{len(captions)} 枚ぶん・"
                    f"最大 {settings.summary_timeout_seconds} 秒）")
        try:
            text, usage = client.chat(
                model_id=model_id,
                messages=[vc.build_text_message(
                    schemas.build_summary_material(captions))],
                max_tokens=settings.max_tokens_summary,
                # **ここが要。フレームの待ち時間を使わない。**
                timeout_seconds=settings.summary_timeout_seconds)
            summary = schemas.parse_visual_summary(
                text, frame_count=len(captions))
        except vc.VlmError as exc:
            summary_values.update({
                "summary_status": db_module.STATUS_FAILED,
                "error_type": exc.error_type, "error_message": str(exc)})
            summary_status = db_module.STATUS_FAILED
            result_outcome = outcome.failed(
                _ERROR_TO_FAILURE.get(exc.error_type,
                                      pipeline_module.FAILURE_OTHER), str(exc))
        except schemas.SchemaError as exc:
            summary_values.update({
                "summary_status": db_module.STATUS_FAILED,
                "error_type": exc.error_type, "error_message": str(exc)})
            summary_status = db_module.STATUS_FAILED
            result_outcome = outcome.failed(pipeline_module.FAILURE_OTHER,
                                            str(exc))
        else:
            summary_values.update({
                "summary_status": db_module.STATUS_OK,
                "title_candidate": summary.title_candidate,
                "visual_summary": summary.visual_summary,
                "main_activity": summary.main_activity,
                "structured_summary_json": json.dumps(summary.to_dict(),
                                                      ensure_ascii=False),
                "request_duration_ms": usage.get("request_duration_ms"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            })
            summary_status = db_module.STATUS_OK
            result_outcome = outcome.ok()

        with database.transaction():
            database.upsert_visual_summary(summary_values)

    with database.transaction():
        database.finish_visual_run(run_id, {
            "finished_at": local_now_iso(),
            "status": (db_module.STATUS_COMPLETED if result_outcome.done
                       else db_module.STATUS_FAILED),
            "successful_frame_count": succeeded, "failed_frame_count": failed,
            "reused_frame_count": reused, "summary_status": summary_status,
            "error_message": result_outcome.message or None})

    logger.info(f"    フレーム解析 {succeeded + reused} 枚"
                f"（新規 {succeeded} / 再利用 {reused} / 失敗 {failed}）")
    return result_outcome
