"""最終テキスト（工程 5/5）を台帳へ結びつける.

**既にある解析結果だけを材料にする。元動画をもう一度 AI へ渡さない。**

最重要:

    文字起こしの材料は ``transcript_segments`` から組み立て直し、
    ``is_suspected_hallucination=true`` のセグメントを**除外する**。

    元の ``full_text`` / segments / 印は**そのまま残す**。
    除外した結果 0 文字になっても、**幻覚疑いを材料へ戻さない**。
    その場合は映像の情報だけで安全に書く。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import DESCRIPTION_IMPL_VERSION
from .. import database as db_module
from .. import description_builder as builder
from .. import datetime_candidates as dtc
from .. import paths, pipeline as pipeline_module
from .. import vlm_client as vc
from ..visual_schemas import SchemaError, extract_json_object
from ..logging_utils import local_now_iso
from ..source_ref import SourceRef

GENERATOR_LOCAL_MODEL = "local-llm"
GENERATOR_FALLBACK = "template"

_ERROR_TO_FAILURE = {
    vc.ERROR_CONNECTION: pipeline_module.FAILURE_CONNECTION,
    vc.ERROR_TIMEOUT: pipeline_module.FAILURE_TIMEOUT,
    vc.ERROR_MODEL_NOT_FOUND: pipeline_module.FAILURE_MODEL,
    vc.ERROR_PRIVACY_CONFIG: pipeline_module.FAILURE_PRIVACY,
}


def usable_transcript(database, asset_id: str) -> tuple[str, int, str]:
    """説明文の材料に使ってよい本文を組み立てる。

    **幻覚の疑いがあるセグメントを AI へ渡さない。** 無音や BGM の区間で
    作り出された文は動画の内容ではない。

    **消すわけではない。** 台帳の本文もセグメントも印もそのまま残る。
    ここで作るのは「今回渡す材料」だけ。

    Returns:
        (材料に使う本文, 外したセグメント数, 文字起こしの状態)
    """
    transcripts = database.get_transcripts_for_asset(asset_id)
    if not transcripts:
        return ("", 0, "")

    row = transcripts[0]
    status = row["transcript_status"]
    segments = database.get_transcript_segments(row["transcript_id"])
    if not segments:
        return ("", 0, status)

    kept: list[str] = []
    excluded = 0
    for segment in segments:
        if segment["is_suspected_hallucination"]:
            excluded += 1
            continue
        kept.append(segment["text"] or "")
    return ("".join(kept).strip(), excluded, status)


def collect_material(database, asset_id: str) -> builder.DescriptionMaterial:
    """既存の解析結果から材料を集める。**動画そのものは開かない。**"""
    asset = database.find_assets_by_identifier(asset_id)[0]
    source = SourceRef.from_row(asset)

    probe = database.get_probe_result(asset_id)
    duration = (float(probe["duration_seconds"])
                if probe and probe["duration_seconds"] else None)

    candidates = [dict(row) for row in database.get_capture_candidates(asset_id)]
    embedded = None
    embedded_raw = ""
    for candidate in candidates:
        if str(candidate.get("source_type", "")).startswith("metadata"):
            try:
                embedded = datetime.fromisoformat(
                    candidate["candidate_datetime"]).date()
                embedded_raw = candidate["candidate_datetime"]
                break
            except (TypeError, ValueError):
                continue

    period = builder.resolve_recording_period(
        candidates=candidates, embedded=embedded, embedded_raw=embedded_raw)

    material = builder.DescriptionMaterial(
        catalog_id=asset["catalog_id"], file_name=source.file_name,
        source_path=str(source.absolute), duration_seconds=duration,
        period=period)

    summary = database.get_latest_visual_summary(asset_id)
    if summary is not None:
        material.visual_summary = (summary["visual_summary"] or "").strip()
        material.visual_title = (summary["title_candidate"] or "").strip()
        material.visual_activity = (summary["main_activity"] or "").strip()
        material.visual_model = (summary["model_id"] or "").strip()

    text, excluded, status = usable_transcript(database, asset_id)
    material.transcript_excerpt = text[:builder.TRANSCRIPT_EXCERPT_CHARS]
    material.transcript_excluded_count = excluded
    material.transcript_status = status

    transcripts = database.get_transcripts_for_asset(asset_id)
    if transcripts:
        material.transcript_segment_count = len(
            database.get_transcript_segments(transcripts[0]["transcript_id"]))
    return material


def _client_factory(settings: vc.VlmSettings):
    return vc.LocalVlmClient(settings)


def _write_atomically(path: Path, text: str) -> None:
    """**原子的に書く。** 途中で失敗しても壊れた本文を残さない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(path)


def run_description(asset_id: str, context,
                    *, client_factory=_client_factory
                    ) -> "pipeline_module.StageOutcome":
    """1 本ぶんの最終テキストを作る。"""
    outcome = pipeline_module.StageOutcome
    database = context.database
    logger = context.logger

    if not database.find_assets_by_identifier(asset_id):
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              "台帳にこの動画がありません。")

    material = collect_material(database, asset_id)
    if material.transcript_segment_count:
        logger.info(
            f"    文字起こしの材料: セグメント {material.transcript_segment_count} 件中 "
            f"{material.transcript_used_count} 件を使用 / "
            f"{material.transcript_excluded_count} 件を除外（定型の疑い）")
        logger.event("description_material",
                     asset_id=asset_id,
                     transcript_segments=material.transcript_segment_count,
                     transcript_used=material.transcript_used_count,
                     transcript_excluded=material.transcript_excluded_count,
                     used_visual=material.has_visual)
        if material.transcript_excluded_count and not material.has_speech:
            logger.info("    使える発話が残らなかったため、"
                        "映像の情報だけで説明文を作ります。")

    settings = vc.VlmSettings.from_settings(context.raw)
    description_model = str(
        dict(context.raw.get("description") or {}).get("model_match", "")
    ).strip() or settings.model_match

    content = youtube = ""
    generator = GENERATOR_FALLBACK
    model_id = ""

    try:
        client = client_factory(settings)
        model_id = vc.select_model(client.list_models(), description_model)
        text, _usage = client.chat(
            model_id=model_id,
            messages=[vc.build_text_message(
                builder.build_material_prompt(material))],
            max_tokens=settings.max_tokens_summary,
            timeout_seconds=settings.summary_timeout_seconds)
        try:
            payload = extract_json_object(text)
            content = str(payload.get("content") or "").strip()
            youtube = str(payload.get("youtube") or "").strip()
        except SchemaError as exc:
            logger.warning(f"    返答を解釈できませんでした（{exc}）。定型文で作成します。")
        if content and youtube:
            generator = GENERATOR_LOCAL_MODEL
        else:
            content = youtube = ""
    except (vc.PrivacyConfigurationError, vc.ModelSelectionError,
            vc.VlmError) as exc:
        # ローカル AI を使えない。**定型文で作る。内容は断定しない。**
        logger.warning(f"    ローカル AI を使えませんでした（{exc}）。"
                       "定型文で作成します。")

    if not content:
        content = builder.fallback_content_text(material)
        youtube = builder.fallback_youtube_text(material)
        generator = GENERATOR_FALLBACK
        model_id = ""

    text = builder.build_description_text(
        material, content=content, youtube=youtube, generator=generator,
        model_id=model_id)

    safe_name = "".join(
        ch for ch in Path(material.file_name).stem
        if ch not in '<>:"/\\|?*').strip() or "video"
    target = paths.descriptions_dir() / f"{material.catalog_id}_{safe_name}.txt"

    try:
        _write_atomically(target, text)
    except OSError as exc:
        return outcome.failed(pipeline_module.FAILURE_OTHER,
                              f"説明文を保存できません: {exc}")

    asset = database.find_assets_by_identifier(asset_id)[0]
    with database.transaction():
        database.upsert_description({
            "asset_id": asset_id, "catalog_id": asset["catalog_id"],
            "source_root": asset["source_root"],
            "source_relative": asset["source_relative"],
            "file_name": material.file_name,
            "description_file_path": target,
            "description_status": db_module.STATUS_COMPLETED,
            "recorded_from": material.period.text,
            "recorded_precision": ("ambiguous" if material.period.is_ambiguous
                                   else "date"),
            "recorded_source": material.period.basis,
            "recorded_raw_text": material.period.note,
            "used_visual_analysis": 1 if material.has_visual else 0,
            "used_transcription": 1 if material.has_speech else 0,
            "transcript_segment_count": material.transcript_segment_count,
            "transcript_excluded_count": material.transcript_excluded_count,
            "generator": generator, "model_id": model_id or None,
            "implementation_version": DESCRIPTION_IMPL_VERSION,
            "created_at": local_now_iso(), "updated_at": local_now_iso(),
        })

    logger.info(f"    説明文を作成しました（{generator}）: {target.name}")
    return outcome.ok()
