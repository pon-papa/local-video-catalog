"""解析結果のまとめ（読み取り専用）.

実運用の試験後に「何がどこまで通ったか」を確認するためのもの。
台帳を開かずに、動画ごとの状態を 1 画面で読めるようにする。

**文字起こしの内訳を明示する。** 実運用で最も確認が要るのがここ。

    セグメント数 / 幻覚疑い件数 / 説明文へ使った件数 / 除外した件数

**何も変更しない。**
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from . import database as db_module
from . import paths
from .logging_utils import configure_stdio_utf8

EXIT_OK = 0
EXIT_ERROR = 1


@dataclass
class AssetSummary:
    catalog_id: str = ""
    file_name: str = ""
    stages: dict[str, str] = field(default_factory=dict)

    frame_count: int = 0
    visual_summary_present: bool = False
    visual_model: str = ""

    asr_status: str = ""
    asr_segment_count: int = 0
    asr_suspected_count: int = 0
    asr_chunk_total: int = 0
    asr_chunk_failed: int = 0

    description_present: bool = False
    description_generator: str = ""
    description_used_segments: int = 0
    description_excluded_segments: int = 0
    cache_cleanup_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id, "file_name": self.file_name,
            "stages": dict(self.stages),
            "frames": self.frame_count,
            "visual_summary": self.visual_summary_present,
            "visual_model": self.visual_model,
            "asr": {
                "status": self.asr_status,
                "segments": self.asr_segment_count,
                "suspected_hallucination": self.asr_suspected_count,
                "chunks_total": self.asr_chunk_total,
                "chunks_failed": self.asr_chunk_failed,
            },
            "description": {
                "present": self.description_present,
                "generator": self.description_generator,
                "segments_used": self.description_used_segments,
                "segments_excluded": self.description_excluded_segments,
            },
            "cache_cleanup": self.cache_cleanup_status,
        }


def collect(database: db_module.CatalogDatabase,
            *, source_root: str | None = None) -> list[AssetSummary]:
    """台帳から動画ごとのまとめを作る。**読み取りだけ。**"""
    if source_root:
        rows = database.list_assets_under(source_root)
    else:
        rows = list(database.connection.execute(
            "SELECT * FROM assets ORDER BY catalog_id").fetchall())

    found: list[AssetSummary] = []
    for row in rows:
        asset_id = row["asset_id"]
        summary = AssetSummary(catalog_id=row["catalog_id"],
                               file_name=row["file_name"])

        for stage, _label in db_module.PIPELINE_STAGES:
            status = database.get_stage_status(asset_id, stage)
            summary.stages[stage] = status["status"] if status else "-"

        summary.frame_count = int(database.connection.execute(
            "SELECT COUNT(*) AS c FROM extracted_frames "
            "WHERE asset_id = ? AND extraction_status IN (?, ?)",
            (asset_id, db_module.STATUS_OK,
             db_module.STATUS_REUSED)).fetchone()["c"])

        visual = database.get_latest_visual_summary(asset_id)
        if visual is not None:
            summary.visual_summary_present = bool(visual["visual_summary"])
            summary.visual_model = visual["model_id"] or ""

        transcripts = database.get_transcripts_for_asset(asset_id)
        if transcripts:
            transcript = transcripts[0]
            summary.asr_status = transcript["transcript_status"]
            segments = database.get_transcript_segments(
                transcript["transcript_id"])
            summary.asr_segment_count = len(segments)
            summary.asr_suspected_count = sum(
                1 for s in segments if s["is_suspected_hallucination"])

        chunk_row = database.connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN chunk_status = 'failed' THEN 1 ELSE 0 END) AS failed "
            "FROM asr_chunks WHERE asset_id = ?", (asset_id,)).fetchone()
        summary.asr_chunk_total = int(chunk_row["total"] or 0)
        summary.asr_chunk_failed = int(chunk_row["failed"] or 0)

        description = database.get_description(asset_id)
        if description is not None:
            summary.description_present = True
            summary.description_generator = description["generator"] or ""
            total = int(description["transcript_segment_count"] or 0)
            excluded = int(description["transcript_excluded_count"] or 0)
            summary.description_used_segments = max(0, total - excluded)
            summary.description_excluded_segments = excluded
            summary.cache_cleanup_status = (
                description["cache_cleanup_status"] or "")

        found.append(summary)
    return found


def format_lines(summaries: list[AssetSummary]) -> list[str]:
    """人が読める形にする。"""
    if not summaries:
        return ["登録された動画がありません。"]

    lines: list[str] = []
    lines.append(f"動画 {len(summaries)} 本")
    lines.append("")

    for summary in summaries:
        lines.append(f"■ {summary.catalog_id}  {summary.file_name}")
        stage_text = " / ".join(
            f"{label}={summary.stages.get(stage, '-')}"
            for stage, label in db_module.PIPELINE_STAGES)
        lines.append(f"    工程       : {stage_text}")
        lines.append(f"    代表画像   : {summary.frame_count} 枚")

        if summary.visual_summary_present:
            lines.append(f"    映像の概要 : あり"
                         + (f"（{summary.visual_model}）"
                            if summary.visual_model else ""))
        else:
            lines.append("    映像の概要 : なし")

        if summary.asr_status:
            suspected = (f" / 定型の疑い {summary.asr_suspected_count} 件"
                         if summary.asr_suspected_count else "")
            failed = (f" / 失敗チャンク {summary.asr_chunk_failed}"
                      if summary.asr_chunk_failed else "")
            lines.append(
                f"    文字起こし : {summary.asr_status}"
                f"（セグメント {summary.asr_segment_count} 件"
                f"{suspected}{failed}"
                f" / チャンク {summary.asr_chunk_total}）")
        else:
            lines.append("    文字起こし : まだありません")

        if summary.description_present:
            material = ""
            if summary.description_used_segments or \
                    summary.description_excluded_segments:
                material = (f" / 材料に使った発話 "
                            f"{summary.description_used_segments} 件"
                            f"・除外 {summary.description_excluded_segments} 件")
            lines.append(f"    説明文     : あり"
                         f"（{summary.description_generator}）{material}")
        else:
            lines.append("    説明文     : まだありません")

        if summary.cache_cleanup_status:
            lines.append(f"    中間ファイル: {summary.cache_cleanup_status}")
        lines.append("")

    total_segments = sum(s.asr_segment_count for s in summaries)
    total_suspected = sum(s.asr_suspected_count for s in summaries)
    total_excluded = sum(s.description_excluded_segments for s in summaries)
    lines.append("--- 文字起こしの合計 ---")
    lines.append(f"セグメント       : {total_segments} 件")
    lines.append(f"定型の疑い       : {total_suspected} 件")
    lines.append(f"説明文から除外   : {total_excluded} 件")
    lines.append("")
    lines.append("※ 疑いのある発話も記録は残しています。"
                 "説明文の材料から外しているだけです。")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_video_catalog.run_summary",
        description="解析結果のまとめを表示する（読み取り専用）")
    parser.add_argument("--source-folder")
    parser.add_argument("--json", action="store_true")
    return parser


def run(args: argparse.Namespace) -> int:
    configure_stdio_utf8()
    try:
        with db_module.CatalogDatabase() as database:
            summaries = collect(database, source_root=args.source_folder)
    except (paths.AppRootError, OSError) as exc:
        print(f"台帳を読めません: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps([s.to_dict() for s in summaries], ensure_ascii=False))
    else:
        for line in format_lines(summaries):
            print(line)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
