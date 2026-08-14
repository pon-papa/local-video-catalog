"""合成動画で pipeline を一周させる（A〜J）.

**実動画は使わない。** ffmpeg の testsrc / sine で作った動画だけ。
ローカル AI の部分は決定論的な偽物へ差し替える（LM Studio を起動しない）。
"""

from __future__ import annotations

import shutil
import time
import unittest
from pathlib import Path

from _support import (
    TempAppRootTestCase,
    file_state,
    find_ffmpeg,
    find_ffprobe,
    make_synthetic_video,
    quiet_logger,
    requires_ffmpeg,
    requires_ffprobe,
)

from local_video_catalog import config as config_module
from local_video_catalog import database as db_module
from local_video_catalog import html_catalog, paths, pipeline, register
from local_video_catalog import stage_report, vlm_client as vc
from local_video_catalog.logging_utils import new_run_id
from local_video_catalog.stages import description as description_stage
from local_video_catalog.stages import visual as visual_stage

FAKE_MODEL = "fake-vl-model"


class FakeClient:
    """決定論的な偽ローカル AI。**通信しない。**

    フレーム解析・視覚概要・説明文の 3 種類の要求を、内容で見分けて
    それらしい JSON を返す。壊れた応答や障害を再現する差し替えもできる。
    """

    def __init__(self, *, frame_error: Exception | None = None,
                 summary_error: Exception | None = None,
                 frame_reply: str | None = None,
                 summary_reply: str | None = None) -> None:
        self.frame_error = frame_error
        self.summary_error = summary_error
        self.frame_reply = frame_reply
        self.summary_reply = summary_reply
        self.calls: list[tuple[str, int | None]] = []

    def list_models(self) -> list[str]:
        return [FAKE_MODEL]

    def chat(self, *, model_id, messages, max_tokens=None,
             timeout_seconds=None):
        content = messages[0].get("content")
        has_image = isinstance(content, list)
        text = content if isinstance(content, str) else ""

        if has_image:
            self.calls.append(("frame", timeout_seconds))
            if self.frame_error:
                raise self.frame_error
            return (self.frame_reply
                    or '{"caption": "屋外で人が歩いている様子。",'
                       ' "setting": "屋外", "readable": true}',
                    {"request_duration_ms": 1})

        if "動画全体の概要" in text:
            self.calls.append(("summary", timeout_seconds))
            if self.summary_error:
                raise self.summary_error
            return (self.summary_reply
                    or '{"title_candidate": "屋外の記録",'
                       ' "visual_summary": "屋外で人が歩いている様子が続く。",'
                       ' "main_activity": "歩いている"}',
                    {"request_duration_ms": 1})

        self.calls.append(("description", timeout_seconds))
        return ('{"content": "屋外で人が歩いている記録です。",'
                ' "youtube": "屋外で撮影した記録です。"}',
                {"request_duration_ms": 1})


class EndToEndTestCase(TempAppRootTestCase):
    """合成動画 1 本を用意して pipeline を回す土台。"""

    def setUp(self) -> None:
        super().setUp()
        if find_ffmpeg() is None or find_ffprobe() is None:
            self.skipTest("ffmpeg / ffprobe が必要")
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.video = self.source_root / "clip.mp4"
        self.assertTrue(make_synthetic_video(
            find_ffmpeg(), self.video, duration=3.0, with_audio=True))
        self.video_state = file_state(self.video)

        # 文字起こしは合成動画に発話が無く、実モデルも要るため飛ばす。
        # ASR そのものの検証は test_asr_engine で行っている。
        self.skip_stages = frozenset({db_module.STAGE_AUDIO_TRANSCRIPTION})

        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)
        self.logger = quiet_logger(paths.log_dir(), new_run_id())
        self.addCleanup(self.logger.close)
        pipeline.clear_stop_request()

    def settings(self):
        raw = config_module.load_settings_dict()
        raw["ffmpeg_path"] = str(find_ffmpeg())
        raw["ffprobe_path"] = str(find_ffprobe())
        raw["vlm"] = {**raw["vlm"], "model_match": FAKE_MODEL}
        raw["frames"] = {"minimum_frame_count": 2, "maximum_frame_count": 3}
        return (raw, config_module.build_settings(raw))

    def context(self, *, budget_seconds: float | None = None):
        raw, settings = self.settings()
        return pipeline.RunContext(
            settings=settings, database=self.db, logger=self.logger,
            run_id="r1", raw=raw,
            deadline=(time.monotonic() + budget_seconds
                      if budget_seconds is not None else None))

    def runners(self, client: FakeClient | None = None) -> pipeline.StageRunners:
        fake = client or FakeClient()
        runners = pipeline.default_runners()
        runners.visual_analysis = (
            lambda asset_id, ctx: visual_stage.run_visual_analysis(
                asset_id, ctx, client_factory=lambda _s: fake))
        runners.description = (
            lambda asset_id, ctx: description_stage.run_description(
                asset_id, ctx, client_factory=lambda _s: fake))
        return runners

    def register_video(self) -> str:
        _raw, settings = self.settings()
        register.register_folder(self.source_root, settings, self.db,
                                 self.logger, run_id="reg")
        return self.db.list_assets_under(self.source_root)[0]["asset_id"]

    def targets(self):
        report = stage_report.collect(
            self.db, source_root=self.source_root,
            ignored_stages=self.skip_stages)
        return report.pending

    def run_once(self, *, client: FakeClient | None = None,
                 recycle_cache: bool = False, budget_seconds=None,
                 runners: pipeline.StageRunners | None = None):
        return pipeline.run_pipeline(
            self.context(budget_seconds=budget_seconds), self.targets(),
            runners or self.runners(client), skip_stages=self.skip_stages,
            recycle_cache=recycle_cache)


class AFullPassTests(EndToEndTestCase):
    """A. 登録 → frames → visual → description → catalog が通ること。"""

    def test_pipeline_completes(self) -> None:
        asset_id = self.register_video()
        result = self.run_once()

        self.assertEqual(result.stop_reason, pipeline.STOP_FINISHED)
        self.assertTrue(result.ok, f"失敗: {result.failures}")
        for stage in (db_module.STAGE_FRAME_EXTRACTION,
                      db_module.STAGE_VISUAL_ANALYSIS,
                      db_module.STAGE_DESCRIPTION):
            with self.subTest(stage=stage):
                self.assertTrue(self.db.is_stage_done(asset_id, stage))

    def test_artifacts_exist(self) -> None:
        asset_id = self.register_video()
        self.run_once()

        frames = list(paths.frames_cache_dir().rglob("*.jpg"))
        self.assertTrue(frames, "代表画像がありません。")

        summary = self.db.get_latest_visual_summary(asset_id)
        self.assertIsNotNone(summary)
        self.assertTrue(summary["visual_summary"])

        row = self.db.get_description(asset_id)
        self.assertIsNotNone(row)
        description = db_module.load_internal_path(row["description_file_path"])
        self.assertTrue(description.is_file())
        self.assertIn("屋外", description.read_text(encoding="utf-8"))

    def test_catalog_is_generated(self) -> None:
        self.register_video()
        self.run_once()
        target = html_catalog.write_catalog(html_catalog.collect_records())
        self.assertTrue(target.is_file())
        page = target.read_text(encoding="utf-8")
        self.assertIn("clip.mp4", page)

    def test_summary_uses_its_own_timeout(self) -> None:
        """**フレームの待ち時間を概要へ流用しない。**"""
        client = FakeClient()
        self.register_video()
        self.run_once(client=client)

        frame_timeouts = [t for kind, t in client.calls if kind == "frame"]
        summary_timeouts = [t for kind, t in client.calls if kind == "summary"]
        self.assertTrue(frame_timeouts and summary_timeouts)
        self.assertEqual(set(frame_timeouts), {300})
        self.assertEqual(set(summary_timeouts), {1200})


class BResumeTests(EndToEndTestCase):
    """B. 停止後の Resume で、完了済み工程を再実行しないこと。"""

    def test_completed_stages_are_not_repeated(self) -> None:
        asset_id = self.register_video()

        runners = self.runners()
        original = runners.frame_extraction

        def stop_after_frames(asset_id, ctx):
            """代表画像だけ作って停止要求を出す。

            次の工程へ入る前に判定されるので、visual と description は
            次回へ回る。
            """
            outcome = original(asset_id, ctx)
            pipeline.request_stop()
            return outcome

        runners.frame_extraction = stop_after_frames
        first = self.run_once(runners=runners)
        self.assertEqual(first.stop_reason, pipeline.STOP_REQUESTED)
        self.assertTrue(self.db.is_stage_done(
            asset_id, db_module.STAGE_FRAME_EXTRACTION))
        self.assertFalse(self.db.is_stage_done(
            asset_id, db_module.STAGE_VISUAL_ANALYSIS))

        second = self.run_once()
        self.assertTrue(second.ok, f"失敗: {second.failures}")
        # frames は再実行されず、visual/description だけが動く
        self.assertTrue(self.db.is_stage_done(
            asset_id, db_module.STAGE_DESCRIPTION))
        frame_attempts = self.db.get_stage_status(
            asset_id, db_module.STAGE_FRAME_EXTRACTION)["attempt_count"]
        self.assertEqual(frame_attempts, 1,
                         "代表画像の工程が再実行されています。")

    def test_frames_are_reused_not_reextracted(self) -> None:
        self.register_video()
        self.run_once()
        before = {p: p.stat().st_mtime_ns
                  for p in paths.frames_cache_dir().rglob("*.jpg")}

        # 説明文だけをやり直させる
        asset_id = self.db.list_assets_under(self.source_root)[0]["asset_id"]
        self.db.set_stage_status(asset_id, db_module.STAGE_DESCRIPTION,
                                 db_module.STATUS_FAILED)
        self.run_once()

        after = {p: p.stat().st_mtime_ns
                 for p in paths.frames_cache_dir().rglob("*.jpg")}
        self.assertEqual(before, after, "代表画像が作り直されています。")


class CFailurePropagationTests(EndToEndTestCase):
    """C. 一工程が失敗したとき、後続を不正に completed にしないこと。"""

    def test_visual_failure_stops_description(self) -> None:
        asset_id = self.register_video()
        client = FakeClient(
            summary_error=vc.VlmError(vc.ERROR_TIMEOUT, "時間切れ"))
        result = self.run_once(client=client)

        self.assertFalse(result.ok)
        self.assertFalse(self.db.is_stage_done(
            asset_id, db_module.STAGE_VISUAL_ANALYSIS))
        self.assertFalse(self.db.is_stage_done(
            asset_id, db_module.STAGE_DESCRIPTION))
        self.assertIsNone(self.db.get_description(asset_id))

    def test_broken_model_reply_is_not_success(self) -> None:
        """**壊れた応答を「解析できた」ことにしない。**"""
        asset_id = self.register_video()
        client = FakeClient(summary_reply='{"visual_summary": ')  # 途中で切れる
        result = self.run_once(client=client)
        self.assertFalse(result.ok)
        self.assertFalse(self.db.is_stage_done(
            asset_id, db_module.STAGE_VISUAL_ANALYSIS))

    def test_phantom_frame_reference_is_rejected(self) -> None:
        asset_id = self.register_video()
        client = FakeClient(
            summary_reply='{"title_candidate": "x",'
                          ' "visual_summary": "フレーム99では屋内だった。",'
                          ' "main_activity": "y"}')
        result = self.run_once(client=client)
        self.assertFalse(result.ok)
        self.assertFalse(self.db.is_stage_done(
            asset_id, db_module.STAGE_VISUAL_ANALYSIS))

    def test_unreadable_frame_reply_is_a_failure(self) -> None:
        """画像を渡しているのに「読めない」と言われたら成功にしない。"""
        asset_id = self.register_video()
        client = FakeClient(
            frame_reply='{"caption": "", "setting": "不明", "readable": false}')
        result = self.run_once(client=client)
        self.assertFalse(result.ok)
        self.assertFalse(self.db.is_stage_done(
            asset_id, db_module.STAGE_VISUAL_ANALYSIS))


class DHallucinationTests(EndToEndTestCase):
    """D. 幻覚疑いのセグメントが説明文の材料へ入らないこと。"""

    def _store_transcript(self, asset_id: str) -> int:
        transcript_id = self.db.upsert_transcript({
            "asset_id": asset_id, "implementation_version": "v1",
            "engine_name": "e", "config_hash": "h",
            "source_quick_fingerprint": "qfp1",
            "primary_audio_stream_index": 1, "scope_type": "full",
            "scope_start_seconds": 0.0, "scope_duration_seconds": 3.0,
            "transcript_status": db_module.STATUS_COMPLETED,
            "full_text": "こっち向いてご視聴ありがとうございました",
            "created_at": "t"})
        self.db.replace_transcript_segments(transcript_id, [
            {"asset_id": asset_id, "sequence_index": 0,
             "start_seconds": 0.0, "end_seconds": 1.0,
             "absolute_start_seconds": 0.0, "absolute_end_seconds": 1.0,
             "text": "こっち向いて", "is_suspected_hallucination": False},
            {"asset_id": asset_id, "sequence_index": 1,
             "start_seconds": 1.0, "end_seconds": 2.0,
             "absolute_start_seconds": 1.0, "absolute_end_seconds": 2.0,
             "text": "ご視聴ありがとうございました",
             "is_suspected_hallucination": True},
        ], "t")
        return transcript_id

    def test_suspected_segments_are_excluded_from_material(self) -> None:
        asset_id = self.register_video()
        self._store_transcript(asset_id)

        material = description_stage.collect_material(self.db, asset_id)
        self.assertIn("こっち向いて", material.transcript_excerpt)
        self.assertNotIn("ご視聴ありがとうございました",
                         material.transcript_excerpt)
        self.assertEqual(material.transcript_excluded_count, 1)

    def test_original_records_are_kept(self) -> None:
        """**材料から外すだけ。記録は消さない。**"""
        asset_id = self.register_video()
        transcript_id = self._store_transcript(asset_id)

        description_stage.collect_material(self.db, asset_id)

        segments = self.db.get_transcript_segments(transcript_id)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1]["is_suspected_hallucination"], 1)
        row = self.db.get_transcripts_for_asset(asset_id)[0]
        self.assertIn("ご視聴ありがとうございました", row["full_text"])

    def test_all_hallucination_yields_no_material_not_a_fallback(self) -> None:
        """**幻覚だけなら材料は空。fallback で戻さない。**"""
        asset_id = self.register_video()
        transcript_id = self.db.upsert_transcript({
            "asset_id": asset_id, "implementation_version": "v1",
            "engine_name": "e", "config_hash": "h",
            "source_quick_fingerprint": "qfp1",
            "primary_audio_stream_index": 1, "scope_type": "full",
            "scope_start_seconds": 0.0, "scope_duration_seconds": 3.0,
            "transcript_status": db_module.STATUS_NO_SPEECH,
            "full_text": "ご視聴ありがとうございました", "created_at": "t"})
        self.db.replace_transcript_segments(transcript_id, [
            {"asset_id": asset_id, "sequence_index": 0,
             "start_seconds": 0.0, "end_seconds": 1.0,
             "absolute_start_seconds": 0.0, "absolute_end_seconds": 1.0,
             "text": "ご視聴ありがとうございました",
             "is_suspected_hallucination": True}], "t")

        material = description_stage.collect_material(self.db, asset_id)
        self.assertEqual(material.transcript_excerpt, "")

        prompt = description_stage.builder.build_material_prompt(material)
        self.assertNotIn("ご視聴ありがとうございました", prompt)
        self.assertIn("使える発話は確認できていません", prompt)

    def test_description_still_succeeds_with_visual_only(self) -> None:
        """発話が使えなくても、映像情報だけで安全に書けること。"""
        self.register_video()
        result = self.run_once()
        self.assertTrue(result.ok, f"失敗: {result.failures}")


class ECleanupOrderTests(EndToEndTestCase):
    """E / F. 正常完了した動画だけ cleanup 対象になること。"""

    def test_no_cleanup_when_description_fails(self) -> None:
        asset_id = self.register_video()
        runners = self.runners()
        runners.description = lambda a, c: pipeline.StageOutcome.failed(
            pipeline.FAILURE_OTHER, "テスト用の失敗")
        self.run_once(runners=runners, recycle_cache=True)

        self.assertTrue((paths.vlm_cache_dir() / asset_id).is_dir()
                        or list(paths.frames_cache_dir().rglob("*.jpg")),
                        "中間ファイルが残っていません。")
        self.assertIsNone(self.db.get_description(asset_id))

    def test_cleanup_after_successful_completion(self) -> None:
        asset_id = self.register_video()
        result = self.run_once(recycle_cache=True)
        self.assertTrue(result.ok)

        row = self.db.get_description(asset_id)
        self.assertIsNotNone(row, "説明文が保存されていません。")
        description = db_module.load_internal_path(row["description_file_path"])
        self.assertTrue(description.is_file(),
                        "説明文が cleanup で消えています。")
        self.assertTrue(paths.database_path().is_file())

    def test_cleanup_never_touches_the_source(self) -> None:
        self.register_video()
        self.run_once(recycle_cache=True)
        self.assertTrue(self.video.is_file())
        self.assertEqual(file_state(self.video), self.video_state)


class HSourceIntegrityTests(EndToEndTestCase):
    """H / I. 元動画が不変で、userdata の外へ書かないこと。"""

    def test_source_video_is_unchanged(self) -> None:
        self.register_video()
        self.run_once()
        self.assertEqual(file_state(self.video), self.video_state,
                         "元動画のサイズ・更新時刻・内容が変わっています。")

    def test_source_folder_gains_no_files(self) -> None:
        before = sorted(p.name for p in self.source_root.rglob("*"))
        self.register_video()
        self.run_once()
        self.assertEqual(sorted(p.name for p in self.source_root.rglob("*")),
                         before, "元動画フォルダーにファイルが作られています。")

    def test_nothing_is_written_outside_userdata(self) -> None:
        self.register_video()
        self.run_once()
        html_catalog.write_catalog(html_catalog.collect_records())

        outside = [
            p for p in paths.app_root().rglob("*")
            if p.is_file()
            and not str(p).startswith(str(paths.userdata_dir()))
            and p.name != paths.APP_ROOT_MARKER
        ]
        self.assertEqual(outside, [], f"userdata の外に生成物: {outside}")


class GPortabilityTests(EndToEndTestCase):
    """G. APP_ROOT をコピーしたあとも Resume できること。"""

    def test_resume_after_a_folder_move(self) -> None:
        import os

        asset_id = self.register_video()
        self.run_once()
        self.db.close()
        self.logger.close()

        destination = self.temp_dir / "moved-app"
        shutil.copytree(self.app_root, destination)
        shutil.rmtree(self.app_root)
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(destination)

        with db_module.CatalogDatabase() as moved:
            for stage in (db_module.STAGE_FRAME_EXTRACTION,
                          db_module.STAGE_VISUAL_ANALYSIS,
                          db_module.STAGE_DESCRIPTION):
                with self.subTest(stage=stage):
                    self.assertTrue(moved.is_stage_done(asset_id, stage))
            row = moved.get_description(asset_id)
            description = db_module.load_internal_path(
                row["description_file_path"])
            self.assertTrue(description.is_file())
            self.assertTrue(str(description).startswith(str(destination)))


class JRepeatedFailureTests(EndToEndTestCase):
    """J. 同種の設備障害が 3 本続いたら安全停止すること。"""

    def setUp(self) -> None:
        super().setUp()
        for index in range(4):
            extra = self.source_root / f"clip{index}.mp4"
            self.assertTrue(make_synthetic_video(
                find_ffmpeg(), extra, duration=2.0))

    def test_three_connection_failures_stop_the_run(self) -> None:
        self.register_video()
        client = FakeClient(
            frame_error=vc.VlmError(vc.ERROR_CONNECTION, "接続できません"))
        result = self.run_once(client=client)

        self.assertEqual(result.stop_reason, pipeline.STOP_REPEATED_FAILURE)
        self.assertEqual(result.processed, 3)
        self.assertGreater(len(self.targets()), 0,
                           "未処理の動画が残っているはずです。")


if __name__ == "__main__":
    unittest.main()
