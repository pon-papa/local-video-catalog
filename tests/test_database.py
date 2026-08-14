"""database — 台帳の骨格、パスの持ち方、AI 推定と利用者確認の分離."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import SCHEMA_VERSION
from local_video_catalog import database as db_module
from local_video_catalog import paths
from local_video_catalog.source_ref import SourceRef


class DatabaseTestCase(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)

    def add_asset(self, relative: str = "clip.mp4", **overrides: object) -> str:
        source = SourceRef(root=self.source_root, relative=relative)
        asset_id = self.db.new_asset_id()
        values = dict(
            asset_id=asset_id,
            catalog_id=self.db.next_catalog_id(),
            source=source,
            file_size=1234,
            creation_time_fs=None,
            last_write_time_fs=None,
            file_fingerprint="fp1",
            quick_fingerprint="qfp1",
            full_sha256=None,
            now="2026-08-14T00:00:00+09:00",
            registration_status=db_module.REG_NEW,
        )
        values.update(overrides)
        self.db.insert_asset(**values)
        return asset_id


class SchemaTests(DatabaseTestCase):
    def test_schema_version_is_recorded(self) -> None:
        self.assertEqual(self.db.get_meta("schema_version"), str(SCHEMA_VERSION))

    def test_expected_tables_exist(self) -> None:
        expected = {
            "schema_meta", "assets", "probe_results",
            "capture_time_candidates", "asset_relations", "processing_runs",
            "stage_status", "frame_extraction_runs", "extracted_frames",
            "visual_analysis_runs", "frame_visual_analyses",
            "asset_visual_summaries", "asr_runs", "asr_chunks",
            "transcripts", "transcript_segments", "asset_descriptions",
        }
        self.assertTrue(expected.issubset(self.db._table_names()))

    def test_foreign_keys_are_on(self) -> None:
        value = self.db.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(value, 1)

    def test_write_ahead_logging(self) -> None:
        mode = self.db.connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")

    def test_migrate_is_idempotent(self) -> None:
        self.assertEqual(self.db.migrate(from_version=0), [])
        self.assertEqual(self.db.migrate(from_version=0), [])

    def test_newer_schema_refuses_to_open(self) -> None:
        """台帳がプログラムより新しければ、変更せずに止まる。"""
        self.db.set_meta("schema_version", str(SCHEMA_VERSION + 1))
        self.db.close()
        with self.assertRaises(db_module.SchemaTooNewError):
            db_module.CatalogDatabase()

    def test_database_lives_in_userdata(self) -> None:
        self.assertEqual(self.db.path, paths.database_path())
        self.assertTrue(str(self.db.path).startswith(str(paths.userdata_dir())))


class CatalogIdTests(DatabaseTestCase):
    def test_first_id(self) -> None:
        self.assertEqual(self.db.next_catalog_id(), "VID-000001")

    def test_sequence_continues(self) -> None:
        self.add_asset("a.mp4")
        self.assertEqual(self.db.next_catalog_id(), "VID-000002")

    def test_ids_are_stable_after_a_move(self) -> None:
        asset_id = self.add_asset("a.mp4")
        before = self.db.find_assets_by_identifier(asset_id)[0]["catalog_id"]
        moved = SourceRef(root=self.source_root, relative="moved/a.mp4")
        self.db.update_asset_seen(
            asset_id, source=moved, file_size=1, creation_time_fs=None,
            last_write_time_fs=None, file_fingerprint="fp1",
            quick_fingerprint=None, full_sha256=None,
            now="2026-08-14T01:00:00+09:00",
            registration_status=db_module.REG_MOVED)
        after = self.db.find_assets_by_identifier(asset_id)[0]["catalog_id"]
        self.assertEqual(before, after)


class SourcePathTests(DatabaseTestCase):
    """元動画は外部入力として持つこと。**APP_ROOT 相対にしない。**"""

    def test_source_is_stored_as_root_plus_relative(self) -> None:
        asset_id = self.add_asset("2009/clip.mp4")
        row = self.db.find_assets_by_identifier(asset_id)[0]
        self.assertEqual(row["source_root"], str(self.source_root))
        self.assertEqual(row["source_relative"], "2009/clip.mp4")

    def test_source_ref_round_trip(self) -> None:
        asset_id = self.add_asset("2009/clip.mp4")
        row = self.db.find_assets_by_identifier(asset_id)[0]
        restored = SourceRef.from_row(row)
        self.assertEqual(restored.absolute,
                         self.source_root / "2009" / "clip.mp4")

    def test_lookup_by_source(self) -> None:
        self.add_asset("2009/clip.mp4")
        source = SourceRef(root=self.source_root, relative="2009/clip.mp4")
        self.assertIsNotNone(self.db.find_asset_by_source(source))

    def test_original_location_never_changes(self) -> None:
        asset_id = self.add_asset("a.mp4")
        moved = SourceRef(root=self.source_root, relative="sub/a.mp4")
        self.db.update_asset_seen(
            asset_id, source=moved, file_size=1, creation_time_fs=None,
            last_write_time_fs=None, file_fingerprint="fp1",
            quick_fingerprint=None, full_sha256=None,
            now="2026-08-14T01:00:00+09:00",
            registration_status=db_module.REG_MOVED)
        row = self.db.find_assets_by_identifier(asset_id)[0]
        self.assertEqual(row["original_source_relative"], "a.mp4")
        self.assertEqual(row["source_relative"], "sub/a.mp4")

    def test_source_paths_survive_an_app_root_move(self) -> None:
        """アプリを移動しても元動画は元の場所を指し続ける。"""
        asset_id = self.add_asset("2009/clip.mp4")
        expected = self.source_root / "2009" / "clip.mp4"
        self.db.close()

        moved_app = self.temp_dir / "moved-app"
        moved_app.mkdir()
        (moved_app / paths.APP_ROOT_MARKER).write_text("x", encoding="utf-8")
        # 台帳ごとアプリを移した状況を作る
        (moved_app / "userdata" / "catalog").mkdir(parents=True)
        Path(paths.database_path()).replace(
            moved_app / "userdata" / "catalog" / "video_catalog.sqlite3")
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(moved_app)

        reopened = db_module.CatalogDatabase()
        self.addCleanup(reopened.close)
        row = reopened.find_assets_by_identifier(asset_id)[0]
        self.assertEqual(SourceRef.from_row(row).absolute, expected)

    def test_no_column_holds_a_raw_absolute_internal_path(self) -> None:
        """内部生成物の列に絶対パスが入っていないこと。"""
        asset_id = self.add_asset()
        self.db.upsert_probe_result(asset_id, {
            "probe_status": db_module.STATUS_OK,
            "raw_probe_cache_path": paths.probe_cache_dir() / "x.json.gz",
        })
        row = self.db.get_probe_result(asset_id)
        self.assertEqual(row["raw_probe_cache_path"], "userdata/cache/probe/x.json.gz")
        self.assertFalse(Path(row["raw_probe_cache_path"]).is_absolute())

    def test_partial_probe_update_keeps_counter_defaults(self) -> None:
        """渡していない件数列は既定の 0 のままになる。"""
        asset_id = self.add_asset()
        self.db.upsert_probe_result(asset_id,
                                    {"probe_status": db_module.STATUS_OK})
        row = self.db.get_probe_result(asset_id)
        self.assertEqual(row["video_stream_count"], 0)
        self.assertEqual(row["audio_stream_count"], 0)

    def test_partial_probe_update_does_not_clear_other_columns(self) -> None:
        asset_id = self.add_asset()
        self.db.upsert_probe_result(asset_id, {
            "probe_status": db_module.STATUS_OK, "duration_seconds": 12.5})
        self.db.upsert_probe_result(asset_id, {
            "probe_status": db_module.STATUS_OK, "width": 640})
        row = self.db.get_probe_result(asset_id)
        self.assertEqual(row["duration_seconds"], 12.5)
        self.assertEqual(row["width"], 640)

    def test_unknown_probe_column_is_refused(self) -> None:
        """打ち間違えた列名が黙って捨てられないこと。"""
        asset_id = self.add_asset()
        with self.assertRaises(ValueError):
            self.db.upsert_probe_result(
                asset_id, {"probe_status": db_module.STATUS_OK,
                           "durationn_seconds": 1.0})

    def test_probe_status_is_required(self) -> None:
        """SQLite の NOT NULL 検査で分かりにくく落ちる前に止める。"""
        asset_id = self.add_asset()
        with self.assertRaises(ValueError):
            self.db.upsert_probe_result(asset_id, {"width": 640})


class InternalPathTests(DatabaseTestCase):
    """内部生成物は APP_ROOT 相対で保存すること。"""

    def test_store_and_load(self) -> None:
        target = paths.descriptions_dir() / "VID-000001_clip.txt"
        stored = db_module.store_internal_path(target)
        self.assertEqual(stored, "userdata/descriptions/VID-000001_clip.txt")
        self.assertEqual(db_module.load_internal_path(stored), target)

    def test_none_passes_through(self) -> None:
        self.assertIsNone(db_module.store_internal_path(None))
        self.assertIsNone(db_module.load_internal_path(None))
        self.assertIsNone(db_module.load_internal_path(""))

    def test_external_path_is_refused(self) -> None:
        """元動画を内部生成物として保存しようとしたら止める。"""
        outside = self.source_root / "clip.mp4"
        outside.write_bytes(b"x")
        with self.assertRaises(ValueError) as ctx:
            db_module.store_internal_path(outside)
        self.assertIn("source_root", str(ctx.exception))

    def test_frame_paths_are_relative(self) -> None:
        asset_id = self.add_asset()
        self.db.start_extraction_run({
            "extraction_run_id": "run1", "asset_id": asset_id,
            "started_at": "t", "status": db_module.STATUS_RUNNING,
            "implementation_version": "v1", "config_hash": "h",
            "config_json": "{}", "planned_frame_count": 1,
            "output_directory": paths.frames_cache_dir() / asset_id,
        })
        run = self.db.get_extraction_run("run1")
        self.assertEqual(run["output_directory"],
                         f"userdata/cache/frames/{asset_id}")

        self.db.upsert_frame({
            "extraction_run_id": "run1", "asset_id": asset_id,
            "implementation_version": "v1", "config_hash": "h",
            "source_quick_fingerprint": "qfp1", "sequence_index": 0,
            "target_time_seconds": 1.0, "target_time_milliseconds": 1000,
            "file_path": paths.frames_cache_dir() / asset_id / "frame_0001.jpg",
            "extraction_status": db_module.STATUS_OK, "created_at": "t",
        })
        frames = self.db.get_frames_by_extraction_set(
            asset_id=asset_id, implementation_version="v1", config_hash="h",
            source_quick_fingerprint="qfp1")
        self.assertEqual(len(frames), 1)
        self.assertFalse(Path(frames[0]["file_path"]).is_absolute())
        self.assertEqual(
            db_module.load_internal_path(frames[0]["file_path"]),
            paths.frames_cache_dir() / asset_id / "frame_0001.jpg")


class ReuseKeyTests(DatabaseTestCase):
    """再利用キーが効くこと（Resume の土台）。"""

    def setUp(self) -> None:
        super().setUp()
        self.asset_id = self.add_asset()
        self.db.start_extraction_run({
            "extraction_run_id": "run1", "asset_id": self.asset_id,
            "started_at": "t", "status": db_module.STATUS_RUNNING,
            "implementation_version": "v1", "config_hash": "h",
            "config_json": "{}", "planned_frame_count": 1,
            "output_directory": paths.frames_cache_dir() / self.asset_id,
        })

    def _frame(self, **overrides: object) -> dict:
        values = {
            "extraction_run_id": "run1", "asset_id": self.asset_id,
            "implementation_version": "v1", "config_hash": "h",
            "source_quick_fingerprint": "qfp1", "sequence_index": 0,
            "target_time_seconds": 1.0, "target_time_milliseconds": 1000,
            "file_path": paths.frames_cache_dir() / self.asset_id / "f.jpg",
            "extraction_status": db_module.STATUS_OK, "created_at": "t",
        }
        values.update(overrides)
        return values

    def test_same_key_updates_instead_of_duplicating(self) -> None:
        self.db.upsert_frame(self._frame())
        self.db.upsert_frame(self._frame(width=640))
        count = self.db.connection.execute(
            "SELECT COUNT(*) AS c FROM extracted_frames").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_a_different_config_hash_makes_a_new_row(self) -> None:
        self.db.upsert_frame(self._frame())
        self.db.upsert_frame(self._frame(config_hash="other"))
        count = self.db.connection.execute(
            "SELECT COUNT(*) AS c FROM extracted_frames").fetchone()["c"]
        self.assertEqual(count, 2)

    def test_a_changed_source_fingerprint_makes_a_new_row(self) -> None:
        """元ファイルが差し替わったら、過去の結果を再利用しない。"""
        self.db.upsert_frame(self._frame())
        self.db.upsert_frame(self._frame(source_quick_fingerprint="qfp2"))
        count = self.db.connection.execute(
            "SELECT COUNT(*) AS c FROM extracted_frames").fetchone()["c"]
        self.assertEqual(count, 2)

    def test_find_existing_frame(self) -> None:
        self.db.upsert_frame(self._frame())
        found = self.db.find_existing_frame(
            asset_id=self.asset_id, implementation_version="v1",
            config_hash="h", source_quick_fingerprint="qfp1",
            target_time_milliseconds=1000)
        self.assertIsNotNone(found)

    def test_reuse_keeps_the_creating_run(self) -> None:
        """再利用しても、画像を作った run の記録は残す。"""
        self.db.upsert_frame(self._frame())
        frame = self.db.find_existing_frame(
            asset_id=self.asset_id, implementation_version="v1",
            config_hash="h", source_quick_fingerprint="qfp1",
            target_time_milliseconds=1000)
        self.db.mark_frame_reused(frame["frame_id"], run_id="run2",
                                  updated_at="t2")
        after = self.db.find_existing_frame(
            asset_id=self.asset_id, implementation_version="v1",
            config_hash="h", source_quick_fingerprint="qfp1",
            target_time_milliseconds=1000)
        self.assertEqual(after["extraction_run_id"], "run1")
        self.assertEqual(after["last_verified_run_id"], "run2")


class StageStatusTests(DatabaseTestCase):
    """Resume の正本。"""

    def setUp(self) -> None:
        super().setUp()
        self.asset_id = self.add_asset()

    def test_unknown_stage_is_not_done(self) -> None:
        self.assertFalse(
            self.db.is_stage_done(self.asset_id, db_module.STAGE_VISUAL_ANALYSIS))

    def test_completed_is_done(self) -> None:
        self.db.set_stage_status(self.asset_id,
                                 db_module.STAGE_VISUAL_ANALYSIS,
                                 db_module.STATUS_COMPLETED)
        self.assertTrue(
            self.db.is_stage_done(self.asset_id, db_module.STAGE_VISUAL_ANALYSIS))

    def test_failed_is_not_done(self) -> None:
        self.db.set_stage_status(self.asset_id,
                                 db_module.STAGE_VISUAL_ANALYSIS,
                                 db_module.STATUS_FAILED)
        self.assertFalse(
            self.db.is_stage_done(self.asset_id, db_module.STAGE_VISUAL_ANALYSIS))

    def test_no_audio_counts_as_done(self) -> None:
        """音声の無い動画を毎回やり直さない。**失敗ではない。**"""
        for status in (db_module.STATUS_NO_SPEECH,
                       db_module.STATUS_SKIPPED_NO_AUDIO):
            with self.subTest(status=status):
                self.db.set_stage_status(
                    self.asset_id, db_module.STAGE_AUDIO_TRANSCRIPTION, status)
                self.assertTrue(self.db.is_stage_done(
                    self.asset_id, db_module.STAGE_AUDIO_TRANSCRIPTION))

    def test_partial_is_not_done(self) -> None:
        self.db.set_stage_status(self.asset_id,
                                 db_module.STAGE_AUDIO_TRANSCRIPTION,
                                 db_module.STATUS_PARTIAL)
        self.assertFalse(self.db.is_stage_done(
            self.asset_id, db_module.STAGE_AUDIO_TRANSCRIPTION))

    def test_attempts_accumulate(self) -> None:
        for _ in range(3):
            self.db.set_stage_status(self.asset_id,
                                     db_module.STAGE_VISUAL_ANALYSIS,
                                     db_module.STATUS_FAILED)
        row = self.db.get_stage_status(self.asset_id,
                                       db_module.STAGE_VISUAL_ANALYSIS)
        self.assertEqual(row["attempt_count"], 3)

    def test_started_at_is_kept_when_finishing(self) -> None:
        self.db.set_stage_status(self.asset_id, db_module.STAGE_FFPROBE,
                                 db_module.STATUS_RUNNING, started_at="t1")
        self.db.set_stage_status(self.asset_id, db_module.STAGE_FFPROBE,
                                 db_module.STATUS_OK, finished_at="t2",
                                 increment_attempt=False)
        row = self.db.get_stage_status(self.asset_id, db_module.STAGE_FFPROBE)
        self.assertEqual(row["last_started_at"], "t1")
        self.assertEqual(row["last_finished_at"], "t2")
        self.assertEqual(row["attempt_count"], 1)


class UserConfirmationTests(DatabaseTestCase):
    """**AI の推定が、利用者の確認を上書きしないこと。**"""

    def setUp(self) -> None:
        super().setUp()
        self.asset_id = self.add_asset()

    def _candidate(self, when: str, rule: str = "filename") -> dict:
        return {"candidate_datetime": when, "source_type": "filename",
                "source_value": "x", "parser_rule": rule, "confidence": 0.5,
                "has_time": False}

    def test_reanalysis_replaces_unconfirmed_candidates(self) -> None:
        self.db.replace_capture_candidates(
            self.asset_id, [self._candidate("2009-08-15")], "t")
        self.db.replace_capture_candidates(
            self.asset_id, [self._candidate("2010-01-01")], "t")
        found = self.db.get_capture_candidates(self.asset_id)
        self.assertEqual([r["candidate_datetime"] for r in found],
                         ["2010-01-01"])

    def test_reanalysis_never_deletes_a_confirmed_candidate(self) -> None:
        self.db.replace_capture_candidates(
            self.asset_id, [self._candidate("2009-08-15")], "t")
        confirmed = self.db.get_capture_candidates(self.asset_id)[0]
        self.db.confirm_capture_candidate(confirmed["candidate_id"])

        self.db.replace_capture_candidates(
            self.asset_id, [self._candidate("2010-01-01")], "t")

        remaining = {r["candidate_datetime"]
                     for r in self.db.get_capture_candidates(self.asset_id)}
        self.assertIn("2009-08-15", remaining)

    def test_confirmed_candidates_sort_first(self) -> None:
        self.db.replace_capture_candidates(
            self.asset_id,
            [self._candidate("2009-08-15", "a"),
             self._candidate("2010-01-01", "b")], "t")
        rows = self.db.get_capture_candidates(self.asset_id)
        self.db.confirm_capture_candidate(rows[-1]["candidate_id"])
        self.assertEqual(
            self.db.get_capture_candidates(self.asset_id)[0]["is_user_confirmed"], 1)

    def test_relation_confirmation_is_a_separate_column(self) -> None:
        other = self.add_asset("converted.mp4")
        self.db.add_relation(
            source_asset_id=self.asset_id, target_asset_id=other,
            relation_type=db_module.RELATION_CONVERTED_TO,
            created_at="t", confidence=0.4)
        row = self.db.get_relations_for_target(other)[0]
        self.assertEqual(row["confirmed_by_user"], 0)
        self.assertEqual(row["confidence"], 0.4)

    def test_unknown_relation_type_is_refused(self) -> None:
        other = self.add_asset("b.mp4")
        with self.assertRaises(ValueError):
            self.db.add_relation(
                source_asset_id=self.asset_id, target_asset_id=other,
                relation_type="invented", created_at="t")


class TranscriptTests(DatabaseTestCase):
    """幻覚の疑いは **保存する**。消すのはここではない。"""

    def setUp(self) -> None:
        super().setUp()
        self.asset_id = self.add_asset()

    def _transcript(self) -> int:
        return self.db.upsert_transcript({
            "asset_id": self.asset_id, "implementation_version": "v1",
            "engine_name": "e", "config_hash": "h",
            "source_quick_fingerprint": "qfp1",
            "primary_audio_stream_index": 1, "scope_type": "full",
            "scope_start_seconds": 0.0, "scope_duration_seconds": 10.0,
            "transcript_status": db_module.STATUS_COMPLETED,
            "full_text": "こんにちは。ご視聴ありがとうございました",
            "created_at": "t",
        })

    def test_segments_keep_the_suspicion_flag(self) -> None:
        transcript_id = self._transcript()
        self.db.replace_transcript_segments(transcript_id, [
            {"asset_id": self.asset_id, "sequence_index": 0,
             "start_seconds": 0.0, "end_seconds": 1.0,
             "absolute_start_seconds": 0.0, "absolute_end_seconds": 1.0,
             "text": "こんにちは", "is_suspected_hallucination": False},
            {"asset_id": self.asset_id, "sequence_index": 1,
             "start_seconds": 1.0, "end_seconds": 2.0,
             "absolute_start_seconds": 1.0, "absolute_end_seconds": 2.0,
             "text": "ご視聴ありがとうございました",
             "is_suspected_hallucination": True},
        ], "t")
        segments = self.db.get_transcript_segments(transcript_id)
        self.assertEqual([s["is_suspected_hallucination"] for s in segments],
                         [0, 1])

    def test_full_text_is_not_stripped(self) -> None:
        """印を付けても本文は残る。判断材料は後段で選ぶ。"""
        transcript_id = self._transcript()
        row = self.db.connection.execute(
            "SELECT full_text FROM transcripts WHERE transcript_id = ?",
            (transcript_id,)).fetchone()
        self.assertIn("ご視聴ありがとうございました", row["full_text"])

    def test_upsert_returns_a_stable_id(self) -> None:
        first = self._transcript()
        second = self._transcript()
        self.assertEqual(first, second)


class RunTests(DatabaseTestCase):
    def test_snapshot_has_no_app_root(self) -> None:
        self.db.start_run(
            run_id="r1", source_root=str(self.source_root),
            started_at="t", worker_count=4,
            config_snapshot={"workers": 4}, application_version="0.1.0")
        row = self.db.connection.execute(
            "SELECT * FROM processing_runs WHERE run_id = 'r1'").fetchone()
        self.assertNotIn(str(paths.app_root()), row["config_snapshot"])
        self.assertEqual(json.loads(row["config_snapshot"])["workers"], 4)

    def test_finish_records_the_stop_reason(self) -> None:
        self.db.start_run(run_id="r1", source_root=None, started_at="t",
                          worker_count=1, config_snapshot={},
                          application_version="0.1.0")
        self.db.finish_run("r1", finished_at="t2",
                           status=db_module.STATUS_COMPLETED,
                           stop_reason="time_budget")
        row = self.db.connection.execute(
            "SELECT * FROM processing_runs WHERE run_id = 'r1'").fetchone()
        self.assertEqual(row["stop_reason"], "time_budget")


class MissingAssetTests(DatabaseTestCase):
    def test_missing_assets_are_flagged_not_deleted(self) -> None:
        """外付けドライブの切断と本当の削除を区別できないため消さない。"""
        asset_id = self.add_asset()
        self.db.mark_assets_unavailable([asset_id], "t")
        row = self.db.find_assets_by_identifier(asset_id)[0]
        self.assertEqual(row["is_available"], 0)
        self.assertEqual(row["registration_status"], db_module.REG_MISSING)

    def test_ambiguous_identifiers_return_every_match(self) -> None:
        """勝手に 1 件へ絞らない。"""
        self.add_asset("a.mp4")
        self.add_asset("b.mp4")
        self.assertEqual(len(self.db.find_assets_by_identifier("VID-000001")), 1)
        self.assertEqual(len(self.db.find_assets_by_identifier("nope")), 0)


class TransactionTests(DatabaseTestCase):
    def test_rollback_on_error(self) -> None:
        asset_id = self.add_asset()
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.set_stage_status(asset_id, db_module.STAGE_FFPROBE,
                                         db_module.STATUS_OK)
                raise RuntimeError("boom")
        self.assertIsNone(
            self.db.get_stage_status(asset_id, db_module.STAGE_FFPROBE))

    def test_commit_persists(self) -> None:
        asset_id = self.add_asset()
        with self.db.transaction():
            self.db.set_stage_status(asset_id, db_module.STAGE_FFPROBE,
                                     db_module.STATUS_OK)
        self.assertTrue(self.db.is_stage_done(asset_id, db_module.STAGE_FFPROBE))

    def test_cascade_delete_removes_children(self) -> None:
        asset_id = self.add_asset()
        self.db.set_stage_status(asset_id, db_module.STAGE_FFPROBE,
                                 db_module.STATUS_OK)
        self.db.connection.execute("DELETE FROM assets WHERE asset_id = ?",
                                   (asset_id,))
        self.assertIsNone(
            self.db.get_stage_status(asset_id, db_module.STAGE_FFPROBE))


if __name__ == "__main__":
    unittest.main()
