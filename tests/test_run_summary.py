"""解析結果のまとめ — 実運用の試験後に確認する数値が読めること.

**文字起こしの内訳**（セグメント数・疑い件数・使用/除外）が
台帳を開かずに読めることを固定する。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import SCHEMA_VERSION
from local_video_catalog import database as db_module
from local_video_catalog import paths, run_summary
from local_video_catalog.source_ref import SourceRef


class SummaryTestCase(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)
        self.asset_id = self._add_asset()

    def _add_asset(self, relative: str = "clip.mp4") -> str:
        asset_id = self.db.new_asset_id()
        self.db.insert_asset(
            asset_id=asset_id, catalog_id=self.db.next_catalog_id(),
            source=SourceRef(root=self.source_root, relative=relative),
            file_size=1, creation_time_fs=None, last_write_time_fs=None,
            file_fingerprint=None, quick_fingerprint="qfp1", full_sha256=None,
            now="t", registration_status=db_module.REG_NEW)
        return asset_id

    def _add_transcript(self, *, suspected: int, total: int) -> int:
        transcript_id = self.db.upsert_transcript({
            "asset_id": self.asset_id, "implementation_version": "v1",
            "engine_name": "e", "config_hash": "h",
            "source_quick_fingerprint": "qfp1",
            "primary_audio_stream_index": 1, "scope_type": "full",
            "scope_start_seconds": 0.0, "scope_duration_seconds": 10.0,
            "transcript_status": db_module.STATUS_COMPLETED,
            "full_text": "本文", "created_at": "t"})
        segments = []
        for index in range(total):
            segments.append({
                "asset_id": self.asset_id, "sequence_index": index,
                "start_seconds": float(index), "end_seconds": float(index + 1),
                "absolute_start_seconds": float(index),
                "absolute_end_seconds": float(index + 1),
                "text": f"発話{index}",
                "is_suspected_hallucination": index < suspected})
        self.db.replace_transcript_segments(transcript_id, segments, "t")
        return transcript_id

    def _add_description(self, *, total: int, excluded: int) -> None:
        self.db.upsert_description({
            "asset_id": self.asset_id, "catalog_id": "VID-000001",
            "source_root": str(self.source_root),
            "source_relative": "clip.mp4", "file_name": "clip.mp4",
            "description_file_path": paths.descriptions_dir() / "a.txt",
            "description_status": db_module.STATUS_COMPLETED,
            "generator": "local-llm", "used_transcription": 1,
            "transcript_segment_count": total,
            "transcript_excluded_count": excluded,
            "created_at": "t"})


class MigrationTests(SummaryTestCase):
    """schema_version 2 への移行。**既存データを壊さない。**"""

    def test_schema_version_is_recorded(self) -> None:
        self.assertEqual(self.db.get_meta("schema_version"), str(SCHEMA_VERSION))

    def test_new_columns_exist(self) -> None:
        columns = self.db.table_columns("asset_descriptions")
        self.assertIn("transcript_segment_count", columns)
        self.assertIn("transcript_excluded_count", columns)

    def test_migration_from_version_one_adds_the_columns(self) -> None:
        """古い台帳を開いても、列が足されて既存行は残ること。"""
        self._add_description(total=5, excluded=2)
        self.db.connection.execute(
            "ALTER TABLE asset_descriptions RENAME TO old_descriptions")
        self.db.connection.execute("""
            CREATE TABLE asset_descriptions (
                asset_id TEXT PRIMARY KEY
                    REFERENCES assets(asset_id) ON DELETE CASCADE,
                catalog_id TEXT, source_root TEXT NOT NULL,
                source_relative TEXT NOT NULL, file_name TEXT NOT NULL,
                description_file_path TEXT NOT NULL,
                description_status TEXT NOT NULL,
                recorded_from TEXT, recorded_to TEXT,
                recorded_precision TEXT, recorded_source TEXT,
                recorded_raw_text TEXT,
                used_visual_analysis INTEGER NOT NULL DEFAULT 0,
                used_transcription INTEGER NOT NULL DEFAULT 0,
                generator TEXT, model_id TEXT, implementation_version TEXT,
                cache_cleanup_status TEXT, cache_cleanup_at TEXT,
                cache_freed_bytes INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT)""")
        self.db.connection.execute(
            "INSERT INTO asset_descriptions SELECT asset_id, catalog_id, "
            "source_root, source_relative, file_name, description_file_path, "
            "description_status, recorded_from, recorded_to, "
            "recorded_precision, recorded_source, recorded_raw_text, "
            "used_visual_analysis, used_transcription, generator, model_id, "
            "implementation_version, cache_cleanup_status, cache_cleanup_at, "
            "cache_freed_bytes, created_at, updated_at FROM old_descriptions")
        self.db.connection.execute("DROP TABLE old_descriptions")
        self.db.set_meta("schema_version", "1")
        self.db.close()

        reopened = db_module.CatalogDatabase()
        self.addCleanup(reopened.close)
        columns = reopened.table_columns("asset_descriptions")
        self.assertIn("transcript_excluded_count", columns)
        row = reopened.get_description(self.asset_id)
        self.assertIsNotNone(row, "既存行が失われています。")
        self.assertEqual(row["generator"], "local-llm")
        self.assertEqual(row["transcript_excluded_count"], 0)
        self.assertEqual(reopened.get_meta("schema_version"),
                         str(SCHEMA_VERSION))


class TranscriptionVisibilityTests(SummaryTestCase):
    """**実 ASR 試験で確認したい数値**が読めること。"""

    def test_segment_and_suspected_counts(self) -> None:
        self._add_transcript(suspected=3, total=10)
        summary = run_summary.collect(self.db)[0]
        self.assertEqual(summary.asr_segment_count, 10)
        self.assertEqual(summary.asr_suspected_count, 3)
        self.assertEqual(summary.asr_status, db_module.STATUS_COMPLETED)

    def test_description_material_counts(self) -> None:
        self._add_transcript(suspected=3, total=10)
        self._add_description(total=10, excluded=3)
        summary = run_summary.collect(self.db)[0]
        self.assertEqual(summary.description_used_segments, 7)
        self.assertEqual(summary.description_excluded_segments, 3)

    def test_text_output_shows_the_breakdown(self) -> None:
        self._add_transcript(suspected=3, total=10)
        self._add_description(total=10, excluded=3)
        text = "\n".join(run_summary.format_lines(run_summary.collect(self.db)))
        self.assertIn("セグメント 10 件", text)
        self.assertIn("定型の疑い 3 件", text)
        self.assertIn("材料に使った発話 7 件", text)
        self.assertIn("除外 3 件", text)

    def test_output_states_that_records_are_kept(self) -> None:
        self._add_transcript(suspected=1, total=2)
        text = "\n".join(run_summary.format_lines(run_summary.collect(self.db)))
        self.assertIn("記録は残しています", text)

    def test_totals_are_reported(self) -> None:
        self._add_transcript(suspected=2, total=5)
        self._add_description(total=5, excluded=2)
        text = "\n".join(run_summary.format_lines(run_summary.collect(self.db)))
        self.assertIn("文字起こしの合計", text)

    def test_chunk_failures_are_visible(self) -> None:
        for index, status in enumerate(("completed", "failed")):
            self.db.upsert_asr_chunk({
                "asset_id": self.asset_id, "chunk_index": index,
                "absolute_start_seconds": float(index * 300),
                "duration_seconds": 300.0, "overlap_seconds": 0.0,
                "source_quick_fingerprint": "qfp1",
                "primary_audio_stream_index": 1, "engine_name": "e",
                "implementation_version": "v1", "model_sha256": "m",
                "config_hash": "h", "chunk_status": status,
                "created_at": "t"})
        summary = run_summary.collect(self.db)[0]
        self.assertEqual(summary.asr_chunk_total, 2)
        self.assertEqual(summary.asr_chunk_failed, 1)

    def test_missing_transcription_is_stated_plainly(self) -> None:
        text = "\n".join(run_summary.format_lines(run_summary.collect(self.db)))
        self.assertIn("文字起こし : まだありません", text)

    def test_json_output_is_machine_readable(self) -> None:
        self._add_transcript(suspected=1, total=4)
        self._add_description(total=4, excluded=1)
        payload = json.loads(json.dumps(
            [s.to_dict() for s in run_summary.collect(self.db)],
            ensure_ascii=False))
        self.assertEqual(payload[0]["asr"]["segments"], 4)
        self.assertEqual(payload[0]["asr"]["suspected_hallucination"], 1)
        self.assertEqual(payload[0]["description"]["segments_used"], 3)

    def test_collect_changes_nothing(self) -> None:
        self._add_transcript(suspected=1, total=3)
        before = self.db.connection.execute(
            "SELECT COUNT(*) AS c FROM transcript_segments").fetchone()["c"]
        run_summary.collect(self.db)
        after = self.db.connection.execute(
            "SELECT COUNT(*) AS c FROM transcript_segments").fetchone()["c"]
        self.assertEqual(before, after)


class StageVisibilityTests(SummaryTestCase):
    def test_stage_states_are_listed(self) -> None:
        self.db.set_stage_status(self.asset_id,
                                 db_module.STAGE_FRAME_EXTRACTION,
                                 db_module.STATUS_COMPLETED)
        summary = run_summary.collect(self.db)[0]
        self.assertEqual(summary.stages[db_module.STAGE_FRAME_EXTRACTION],
                         db_module.STATUS_COMPLETED)
        self.assertEqual(summary.stages[db_module.STAGE_DESCRIPTION], "-")

    def test_empty_catalog_is_stated(self) -> None:
        text = "\n".join(run_summary.format_lines([]))
        self.assertIn("登録された動画がありません", text)


if __name__ == "__main__":
    unittest.main()
