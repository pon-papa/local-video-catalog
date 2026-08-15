"""今回どの動画を選ぶか — **決定論的で、事前に分かり、理由が言えること**.

実運用で「329 本あるのに、なぜこの 3 本？」が分からなかった。
規則自体は決まっていたので、**規則は変えず、見えるようにした**。

実装から確認した規則:
    登録済みで手が残っている動画を、台帳ID順に並べ、上限だけ先頭から取る。
"""

from __future__ import annotations

import json
import unittest

from _support import TempAppRootTestCase, quiet_logger

from local_video_catalog import database as db_module
from local_video_catalog import paths, selection, stage_report
from local_video_catalog.logging_utils import new_run_id
from local_video_catalog.source_ref import SourceRef


class SelectionTestCase(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.db = db_module.CatalogDatabase()
        self.addCleanup(self.db.close)

    def add(self, relative: str) -> str:
        asset_id = self.db.new_asset_id()
        self.db.insert_asset(
            asset_id=asset_id, catalog_id=self.db.next_catalog_id(),
            source=SourceRef(root=self.source_root, relative=relative),
            file_size=1, creation_time_fs=None, last_write_time_fs=None,
            file_fingerprint=None, quick_fingerprint=None, full_sha256=None,
            now="t", registration_status=db_module.REG_NEW)
        return asset_id

    def add_many(self, count: int) -> list[str]:
        return [self.add(f"clip{index:03d}.mp4") for index in range(count)]

    def complete(self, asset_id: str, *stages: str) -> None:
        for stage in stages:
            self.db.set_stage_status(asset_id, stage,
                                     db_module.STATUS_COMPLETED)

    def plan(self, **kwargs) -> selection.SelectionPlan:
        options = {"source_root": str(self.source_root), "max_videos": 0}
        options.update(kwargs)
        return selection.build_plan(self.db, **options)


class DeterminismTests(SelectionTestCase):
    """A. 同じ状態なら毎回同じ結果になること。"""

    def test_same_state_gives_the_same_selection(self) -> None:
        self.add_many(10)
        first = [v.catalog_id for v in self.plan(max_videos=3).videos]
        for _ in range(5):
            again = [v.catalog_id for v in self.plan(max_videos=3).videos]
            self.assertEqual(first, again)

    def test_selection_follows_catalog_id_order(self) -> None:
        """**規則は台帳ID順。** これが実装されている規則そのもの。"""
        self.add_many(10)
        chosen = [v.catalog_id for v in self.plan(max_videos=3).videos]
        self.assertEqual(chosen, ["VID-000001", "VID-000002", "VID-000003"])

    def test_completed_videos_drop_out_and_the_rest_shift_up(self) -> None:
        assets = self.add_many(10)
        for asset_id in assets[:3]:
            self.complete(asset_id, *[s for s, _ in db_module.PIPELINE_STAGES])
        chosen = [v.catalog_id for v in self.plan(max_videos=3).videos]
        self.assertEqual(chosen, ["VID-000004", "VID-000005", "VID-000006"])

    def test_registration_order_is_alphabetical(self) -> None:
        """台帳IDは列挙順に振られ、列挙は名前順。**偶然ではない。**"""
        from local_video_catalog import discovery

        for name in ("zebra.mp4", "alpha.mp4", "middle.mp4"):
            (self.source_root / name).write_bytes(b"x")
        found = [f.source.relative for f in discovery.discover(
            self.source_root, extensions=(".mp4",))]
        self.assertEqual(found, ["alpha.mp4", "middle.mp4", "zebra.mp4"])


class LimitTests(SelectionTestCase):
    """B / C. 上限は解析本数にだけ効き、ライブラリ総数を切らない。"""

    def test_limit_selects_exactly_that_many(self) -> None:
        self.add_many(10)
        self.assertEqual(len(self.plan(max_videos=3).videos), 3)

    def test_library_total_is_not_truncated(self) -> None:
        """**「3本と指定したのに329本」の誤解を生まないため。**"""
        self.add_many(10)
        plan = self.plan(max_videos=3)
        self.assertEqual(plan.library_total, 10)
        self.assertEqual(plan.outstanding_total, 10)
        self.assertEqual(len(plan.videos), 3)

    def test_no_limit_takes_everything(self) -> None:
        self.add_many(10)
        self.assertEqual(len(self.plan(max_videos=0).videos), 10)

    def test_limit_larger_than_library(self) -> None:
        self.add_many(3)
        self.assertEqual(len(self.plan(max_videos=99).videos), 3)


class MatchesWhatRunsTests(SelectionTestCase):
    """D / G. 予定として見せた一覧と、実際に処理する一覧が一致すること。"""

    def test_plan_matches_select_pending(self) -> None:
        self.add_many(10)
        report = stage_report.collect(self.db, source_root=self.source_root)
        targets = stage_report.select_pending(report, max_videos=4)
        plan = self.plan(max_videos=4)
        self.assertEqual([item.asset_id for item in targets],
                         [video.asset_id for video in plan.videos])

    def test_plan_does_not_reorder_for_display(self) -> None:
        """表示のために並べ替えない。食い違いの元になる。"""
        self.add_many(5)
        plan = self.plan(max_videos=5)
        self.assertEqual([v.catalog_id for v in plan.videos],
                         sorted(v.catalog_id for v in plan.videos))

    def test_html_sort_does_not_affect_selection(self) -> None:
        """H. HTML の並び替えは見せ方の都合で、選択とは無関係。"""
        from local_video_catalog import description_builder as builder

        self.add_many(5)
        before = [v.catalog_id for v in self.plan(max_videos=3).videos]
        # 日付での並び替えキーを触っても選択は変わらない
        builder.sort_key_for_period("2020年1月1日")
        after = [v.catalog_id for v in self.plan(max_videos=3).videos]
        self.assertEqual(before, after)


class ReasonTests(SelectionTestCase):
    """E. 表示した理由が、実際の工程の状態と一致すること。"""

    def test_new_video_says_untouched(self) -> None:
        self.add_many(1)
        video = self.plan(max_videos=1).videos[0]
        self.assertEqual(video.reason, selection.REASON_NEW)
        self.assertIn("まだ手をつけていない", video.describe_reason())

    def test_partly_done_video_says_resume(self) -> None:
        asset_id = self.add("clip.mp4")
        self.complete(asset_id, db_module.STAGE_FRAME_EXTRACTION,
                      db_module.STAGE_VISUAL_ANALYSIS)
        video = self.plan(max_videos=1).videos[0]
        self.assertEqual(video.reason, selection.REASON_RESUME)
        self.assertEqual(video.done_stages,
                         [db_module.STAGE_FRAME_EXTRACTION,
                          db_module.STAGE_VISUAL_ANALYSIS])
        self.assertEqual(video.next_stage,
                         db_module.STAGE_AUDIO_TRANSCRIPTION)
        text = video.describe_reason()
        self.assertIn("代表画像の抽出", text)
        self.assertIn("文字起こし", text)

    def test_reason_matches_the_recorded_stage_state(self) -> None:
        assets = self.add_many(3)
        self.complete(assets[1], db_module.STAGE_FRAME_EXTRACTION)
        for video in self.plan(max_videos=3).videos:
            with self.subTest(catalog_id=video.catalog_id):
                for stage in video.done_stages:
                    self.assertTrue(
                        self.db.is_stage_done(video.asset_id, stage))

    def test_skipped_stages_are_not_called_done(self) -> None:
        asset_id = self.add("clip.mp4")
        self.complete(asset_id, db_module.STAGE_FRAME_EXTRACTION)
        plan = self.plan(
            max_videos=1,
            ignored_stages=frozenset({db_module.STAGE_VISUAL_ANALYSIS}))
        video = plan.videos[0]
        self.assertNotIn(db_module.STAGE_VISUAL_ANALYSIS, video.done_stages)


class RetryTests(SelectionTestCase):
    """F. 失敗のみ再試行では、対象と説明が変わること。"""

    def test_only_the_named_video_is_selected(self) -> None:
        assets = self.add_many(5)
        self.db.set_stage_status(assets[2], db_module.STAGE_VISUAL_ANALYSIS,
                                 db_module.STATUS_FAILED)
        plan = self.plan(only_catalog_ids=("VID-000003",))
        self.assertEqual([v.catalog_id for v in plan.videos], ["VID-000003"])

    def test_rule_text_differs_from_normal(self) -> None:
        self.add_many(3)
        normal = self.plan(max_videos=3)
        retry = self.plan(only_catalog_ids=("VID-000002",))
        self.assertEqual(normal.rule, selection.RULE_NORMAL)
        self.assertEqual(retry.rule, selection.RULE_RETRY)
        self.assertNotEqual(normal.rule_text, retry.rule_text)
        self.assertIn("うまくいかなかった", retry.rule_text)

    def test_failed_stage_is_named_in_the_reason(self) -> None:
        assets = self.add_many(3)
        self.db.set_stage_status(assets[1], db_module.STAGE_VISUAL_ANALYSIS,
                                 db_module.STATUS_FAILED)
        video = self.plan(only_catalog_ids=("VID-000002",)).videos[0]
        self.assertEqual(video.reason, selection.REASON_RETRY)
        self.assertIn("映像の解析", video.describe_reason())

    def test_reason_holds_no_traceback(self) -> None:
        """**長い内部エラーを状況説明へ出さない。**"""
        assets = self.add_many(1)
        self.db.set_stage_status(
            assets[0], db_module.STAGE_VISUAL_ANALYSIS,
            db_module.STATUS_FAILED,
            error_message="Traceback (most recent call last):\n  File ...")
        video = self.plan(only_catalog_ids=("VID-000001",)).videos[0]
        self.assertNotIn("Traceback", video.describe_reason())


class DisplayTests(SelectionTestCase):
    """利用者に見せる文面。"""

    def test_summary_states_library_limit_and_rule(self) -> None:
        self.add_many(329)
        text = "\n".join(self.plan(max_videos=3).summary_lines())
        self.assertIn("329 本", text)
        self.assertIn("3 本", text)
        self.assertIn("台帳ID順", text)

    def test_details_list_every_selected_video_with_a_reason(self) -> None:
        self.add_many(5)
        plan = self.plan(max_videos=3)
        text = "\n".join(plan.detail_lines())
        for video in plan.videos:
            self.assertIn(video.catalog_id, text)
            self.assertIn(video.file_name, text)
        self.assertEqual(text.count("理由:"), 3)

    def test_progress_line_names_the_current_video(self) -> None:
        self.add_many(3)
        plan = self.plan(max_videos=3)
        lines = plan.progress_line(2)
        self.assertIn("2 / 3 本目", lines[0])
        self.assertIn(plan.videos[1].catalog_id, lines[1])

    def test_japanese_file_names_survive(self) -> None:
        """J. 日本語ファイル名でも表示が壊れないこと。"""
        self.add("2013-12-28_宮崎_結合済み.mp4")
        plan = self.plan(max_videos=1)
        text = "\n".join(plan.detail_lines())
        self.assertIn("2013-12-28_宮崎_結合済み.mp4", text)

    def test_nothing_to_do_is_stated(self) -> None:
        asset_id = self.add("clip.mp4")
        self.complete(asset_id, *[s for s, _ in db_module.PIPELINE_STAGES])
        self.assertIn("ありません",
                      "\n".join(self.plan(max_videos=3).detail_lines()))


class LoggingTests(SelectionTestCase):
    """I. 選択規則・対象・理由がログへ残ること。"""

    def test_plan_serialises_for_the_log(self) -> None:
        self.add_many(5)
        payload = json.loads(json.dumps(self.plan(max_videos=2).to_dict(),
                                        ensure_ascii=False))
        self.assertEqual(payload["library_total"], 5)
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(payload["rule"], selection.RULE_NORMAL)
        self.assertEqual(len(payload["videos"]), 2)
        for video in payload["videos"]:
            self.assertIn("catalog_id", video)
            self.assertIn("reason", video)

    def test_structured_event_reaches_the_log_file(self) -> None:
        self.add_many(3)
        logger = quiet_logger(paths.log_dir(), new_run_id())
        self.addCleanup(logger.close)
        logger.event("selection_plan", **self.plan(max_videos=2).to_dict())
        logger.close()

        written = logger.jsonl_log_path.read_text(encoding="utf-8")
        record = json.loads(written.strip().splitlines()[-1])
        self.assertEqual(record["event"], "selection_plan")
        self.assertEqual(record["library_total"], 3)
        self.assertEqual(len(record["videos"]), 2)


class UnavailableTests(SelectionTestCase):
    def test_missing_videos_are_counted_but_not_selected(self) -> None:
        assets = self.add_many(3)
        self.db.mark_assets_unavailable([assets[0]], "t")
        plan = self.plan(max_videos=3)
        self.assertEqual(plan.unavailable_total, 1)
        self.assertNotIn("VID-000001", [v.catalog_id for v in plan.videos])


if __name__ == "__main__":
    unittest.main()
