"""代表画像の抽出（工程 2/5）を台帳へ結びつける.

**元動画は ffmpeg の入力としてのみ渡す。** 出力は
``userdata/cache/frames/`` の下だけ。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from .. import FRAME_EXTRACTION_IMPL_VERSION
from .. import database as db_module
from .. import frame_extractor as fx
from .. import pipeline as pipeline_module
from ..logging_utils import local_now_iso
from ..source_ref import SourceRef


def run_frame_extraction(asset_id: str, context) -> "pipeline_module.StageOutcome":
    """1 本ぶんの代表画像を用意する。

    既に同じ条件で成功している画像は**作り直さない**。ファイルが実在
    することまで確かめてから再利用する（台帳にあってもファイルが消えて
    いれば作り直す）。
    """
    outcome = pipeline_module.StageOutcome
    database = context.database
    logger = context.logger

    row = database.find_assets_by_identifier(asset_id)
    if not row:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "台帳にこの動画がありません。")
    source = SourceRef.from_row(row[0])

    probe = database.get_probe_result(asset_id)
    if probe is None:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "先に動画の基本情報を取得してください。")
    if not probe["playable_video_stream_count"]:
        # カバーアートしか無い、または映像が無い。**異常ではない。**
        logger.info("    再生できる映像がないため、代表画像は作りません。")
        return outcome.ok(db_module.STATUS_SKIPPED_NO_PLAYABLE_VIDEO)

    duration = probe["duration_seconds"]
    if not duration or duration <= 0:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "再生時間が分からないため抽出できません。")

    if not source.absolute.is_file():
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              f"元動画が見つかりません: {source.absolute}")
    if context.settings.ffmpeg_path is None:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "ffmpeg が見つかりません。設定を確認してください。")

    config = fx.ExtractionConfig.from_settings(context.raw)
    planned = fx.plan_frames(float(duration), config)
    if not planned:
        return outcome.failed(pipeline_module.FAILURE_NO_FRAMES,
                              "抽出する時刻を決められませんでした。")

    quick_fp = row[0]["quick_fingerprint"]
    directory = fx.output_directory(asset_id, config)
    run_id = uuid.uuid4().hex
    started = local_now_iso()

    with database.transaction():
        database.start_extraction_run({
            "extraction_run_id": run_id, "asset_id": asset_id,
            "started_at": started, "status": db_module.STATUS_RUNNING,
            "implementation_version": FRAME_EXTRACTION_IMPL_VERSION,
            "config_hash": config.config_hash,
            "config_json": str(config.to_dict()),
            "source_quick_fingerprint": quick_fp,
            "primary_video_stream_index": probe["primary_video_stream_index"],
            "duration_seconds": duration,
            "planned_frame_count": len(planned),
            "output_directory": directory,
        })

    succeeded = reused = failed = 0
    interrupted = False

    for frame in planned:
        if pipeline_module.stop_requested() or context.out_of_time():
            interrupted = True
            break

        target = directory / fx.frame_file_name(frame)
        existing = database.find_existing_frame(
            asset_id=asset_id,
            implementation_version=FRAME_EXTRACTION_IMPL_VERSION,
            config_hash=config.config_hash,
            source_quick_fingerprint=quick_fp,
            target_time_milliseconds=frame.target_time_milliseconds)

        # 台帳にあっても、ファイルが消えていれば作り直す。
        if existing is not None and existing["extraction_status"] in (
                db_module.STATUS_OK, db_module.STATUS_REUSED):
            saved = db_module.load_internal_path(existing["file_path"])
            if saved is not None and saved.is_file():
                with database.transaction():
                    database.mark_frame_reused(existing["frame_id"],
                                               run_id=run_id,
                                               updated_at=local_now_iso())
                reused += 1
                continue

        ok, exit_code, message = fx.extract_one(
            context.settings.ffmpeg_path, source.absolute, frame, target,
            config, stream_index=probe["primary_video_stream_index"])

        values = {
            "extraction_run_id": run_id, "asset_id": asset_id,
            "implementation_version": FRAME_EXTRACTION_IMPL_VERSION,
            "config_hash": config.config_hash,
            "source_quick_fingerprint": quick_fp,
            "sequence_index": frame.sequence_index,
            "target_time_seconds": frame.target_time_seconds,
            "target_time_milliseconds": frame.target_time_milliseconds,
            "relative_position": frame.relative_position,
            "ffmpeg_exit_code": exit_code,
            "created_at": local_now_iso(), "updated_at": local_now_iso(),
        }
        if ok:
            values.update({
                "file_path": target, "file_size": target.stat().st_size,
                "image_format": fx.IMAGE_FORMAT,
                "sha256": fx.sha256_of(target),
                "extraction_status": db_module.STATUS_OK,
            })
            succeeded += 1
        else:
            values.update({"extraction_status": db_module.STATUS_FAILED,
                           "error_message": message})
            failed += 1

        with database.transaction():
            database.upsert_frame(values)

    usable = succeeded + reused
    if usable == 0:
        status = db_module.STATUS_FAILED
    elif failed or usable < len(planned):
        status = db_module.STATUS_PARTIAL
    else:
        status = db_module.STATUS_COMPLETED

    with database.transaction():
        database.finish_extraction_run(
            run_id, finished_at=local_now_iso(), status=status,
            successful_frame_count=succeeded, failed_frame_count=failed,
            reused_frame_count=reused)

    logger.info(f"    代表画像 {usable} 枚（新規 {succeeded} / 再利用 {reused}"
                f" / 失敗 {failed}）")

    if usable == 0:
        return outcome.failed(pipeline_module.FAILURE_NO_FRAMES,
                              "代表画像を 1 枚も作れませんでした。")

    if interrupted:
        # 途中で止めた。**completed にしない。** 次回に残りを行う。
        # **失敗ではない**ので、そう分かる形で返す。
        return pipeline_module.StageOutcome.stopped(
            db_module.STATUS_PARTIAL,
            f"止めたため代表画像の抽出を途中で終了しました。"
            f"{usable} 枚まで作成済みです。次回は残りから再開します。")

    if failed:
        # 全部試したうえで一部が取れなかった。動画の末尾が壊れている等、
        # **やり直しても同じ結果になる**ことが多い。使える画像がある以上、
        # ここで完了とみなす。毎回やり直して先へ進めなくなる方が困る。
        logger.warning(f"    {failed} 枚は取得できませんでした。"
                       "残りの画像で解析を続けます。")
    return outcome.ok()
