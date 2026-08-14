"""説明文と HTML カタログ — **AI の推定を事実へ昇格させないこと**."""

from __future__ import annotations

import re
import unittest
from datetime import date
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import description_builder as builder
from local_video_catalog import html_catalog, paths


class PromptTests(unittest.TestCase):
    """材料に無いことを書かせない指示が残っていること。"""

    def test_prompt_forbids_inventing_relationships(self) -> None:
        for phrase in ("material に無いことを書かない",
                       "推測で補わない",
                       "人物名", "人間関係", "地名", "行事名"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, builder.PROMPT)

    def test_prompt_gives_a_concrete_substitution(self) -> None:
        self.assertIn("屋外での行事のような様子", builder.PROMPT)

    def test_prompt_forbids_asserting_people_counts(self) -> None:
        self.assertIn("人数・性別・年齢を断定しない", builder.PROMPT)


class RecordingPeriodTests(unittest.TestCase):
    """**根拠が食い違うなら確定させない。**"""

    def test_embedded_date_is_preferred(self) -> None:
        period = builder.resolve_recording_period(
            candidates=[], embedded=date(2009, 8, 15))
        self.assertEqual(period.text, "2009年8月15日")
        self.assertFalse(period.is_ambiguous)

    def test_conflict_with_the_file_name_is_kept_ambiguous(self) -> None:
        period = builder.resolve_recording_period(
            candidates=[{"candidate_datetime": "2010-01-01",
                         "source_type": "filename_8digit", "confidence": 0.6}],
            embedded=date(2009, 8, 15))
        self.assertTrue(period.is_ambiguous)
        self.assertIn(builder.AMBIGUOUS_MARK, period.describe())

    def test_multiple_name_candidates_stay_ambiguous(self) -> None:
        period = builder.resolve_recording_period(candidates=[
            {"candidate_datetime": "2009-08-15",
             "source_type": "filename_8digit", "confidence": 0.6},
            {"candidate_datetime": "2010-01-01",
             "source_type": "folder_name", "confidence": 0.5},
        ])
        self.assertTrue(period.is_ambiguous)

    def test_no_evidence_is_unknown(self) -> None:
        period = builder.resolve_recording_period(candidates=[])
        self.assertEqual(period.text, builder.UNKNOWN_PERIOD)

    def test_file_timestamps_are_not_used_as_evidence(self) -> None:
        """ファイル日時はコピーで書き換わる。撮影日時にしない。"""
        period = builder.resolve_recording_period(candidates=[
            {"candidate_datetime": "2024-01-01",
             "source_type": "filesystem_creation_time", "confidence": 0.15},
        ])
        self.assertEqual(period.text, builder.UNKNOWN_PERIOD)


class SortKeyTests(unittest.TestCase):
    """**「解釈保留」を日付へ読み替えない。**"""

    def test_ambiguous_sorts_to_the_end(self) -> None:
        ambiguous = builder.sort_key_for_period(
            f"2009年8月15日（{builder.AMBIGUOUS_MARK}）")
        normal = builder.sort_key_for_period("2020年1月1日")
        self.assertGreater(ambiguous, normal)

    def test_unknown_sorts_last(self) -> None:
        self.assertGreater(builder.sort_key_for_period("不明"),
                           builder.sort_key_for_period("2099年12月31日"))

    def test_ordinary_dates_sort_chronologically(self) -> None:
        self.assertLess(builder.sort_key_for_period("2009年8月15日"),
                        builder.sort_key_for_period("2010年1月1日"))

    def test_month_only_is_accepted(self) -> None:
        self.assertEqual(builder.sort_key_for_period("2009年8月"), "2009-08-00")


class FallbackTests(unittest.TestCase):
    """AI を使えないときに**内容を断定しない**こと。"""

    def setUp(self) -> None:
        self.material = builder.DescriptionMaterial(
            catalog_id="VID-000001", file_name="clip.mp4",
            source_path="X:/videos/clip.mp4", duration_seconds=95.0)

    def test_fallback_does_not_assert_content(self) -> None:
        text = builder.fallback_content_text(self.material)
        self.assertTrue(any(mark in text for mark in builder.FALLBACK_MARKS))

    def test_fallback_is_marked_in_the_saved_text(self) -> None:
        text = builder.build_description_text(
            self.material,
            content=builder.fallback_content_text(self.material),
            youtube=builder.fallback_youtube_text(self.material),
            generator="template")
        self.assertIn("生成=template", text)

    def test_duration_formatting(self) -> None:
        self.assertEqual(builder.format_duration(None), "不明")
        self.assertEqual(builder.format_duration(45), "45秒")
        self.assertEqual(builder.format_duration(95), "1分35秒")
        self.assertEqual(builder.format_duration(3725), "1時間02分05秒")


class SavedTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.material = builder.DescriptionMaterial(
            catalog_id="VID-000001", file_name="clip.mp4",
            source_path="X:/videos/clip.mp4", duration_seconds=95.0,
            visual_summary="屋外で人が歩いている様子。",
            transcript_excerpt="こっち向いて")

    def test_round_trip(self) -> None:
        text = builder.build_description_text(
            self.material, content="内容の説明です。",
            youtube="概要欄の文章です。", generator="local-llm",
            model_id="qwen3-vl-8b-instruct")
        parsed = builder.parse_description_text(text)
        self.assertEqual(parsed["catalog_id"], "VID-000001")
        self.assertEqual(parsed["file_name"], "clip.mp4")
        self.assertEqual(parsed["content"], "内容の説明です。")
        self.assertEqual(parsed["youtube"], "概要欄の文章です。")

    def test_generating_model_is_recorded(self) -> None:
        """あとから「これは AI が書いた」と分かるようにする。"""
        text = builder.build_description_text(
            self.material, content="x", youtube="y", generator="local-llm",
            model_id="qwen3-vl-8b-instruct")
        self.assertIn("qwen3-vl-8b-instruct", text)
        self.assertIn("ローカル AI が解析結果から作成した", text)

    def test_footer_states_nothing_is_confirmed(self) -> None:
        text = builder.build_description_text(
            self.material, content="x", youtube="y", generator="local-llm")
        self.assertIn("確認されていません", text)


class CatalogRenderingTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()

    def _write(self, name: str, **fields: object) -> None:
        material = builder.DescriptionMaterial(
            catalog_id=fields.get("catalog_id", "VID-000001"),
            file_name=fields.get("file_name", name),
            source_path=fields.get("source_path", f"X:/videos/{name}"),
            duration_seconds=95.0)
        if "period" in fields:
            material.period = fields["period"]
        text = builder.build_description_text(
            material, content=fields.get("content", "内容の説明です。"),
            youtube=fields.get("youtube", "概要欄の文章です。"),
            generator=fields.get("generator", "local-llm"))
        (paths.descriptions_dir() / f"{name}.txt").write_text(
            text, encoding="utf-8")

    def test_records_are_collected(self) -> None:
        self._write("VID-000001_clip")
        self._write("VID-000002_other", catalog_id="VID-000002")
        self.assertEqual(len(html_catalog.collect_records()), 2)

    def test_catalog_lands_in_userdata(self) -> None:
        self._write("VID-000001_clip")
        target = html_catalog.write_catalog(html_catalog.collect_records())
        self.assertEqual(target, paths.catalog_html_path())
        self.assertTrue(str(target).startswith(str(paths.userdata_dir())))

    def test_no_external_resources(self) -> None:
        """**外部 CDN・フォント・API を使わない。通信しない。**"""
        self._write("VID-000001_clip")
        page = html_catalog.render_html(html_catalog.collect_records())
        for pattern in (r"https?://(?!127\.0\.0\.1|localhost)",
                        r"<link[^>]+href", r"src\s*=\s*[\"']http",
                        r"@import", r"fetch\s*\(", r"XMLHttpRequest",
                        r"WebSocket"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, page),
                                  f"外部参照らしき記述があります: {pattern}")

    def test_html_special_characters_do_not_break_the_page(self) -> None:
        self._write("VID-000001_clip",
                    file_name='<script>alert("x")</script>.mp4',
                    content='内容に < と & と " が入っています')
        page = html_catalog.render_html(html_catalog.collect_records())
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)

    def test_searchable_text_is_escaped_too(self) -> None:
        """検索用の文字列にも生のタグを残さない。"""
        self._write("VID-000001_clip", content='<b>太字</b> と "引用"')
        page = html_catalog.render_html(html_catalog.collect_records())
        payload = page.split("window.__RECORDS__ = ", 1)[1].split("\n", 1)[0]
        self.assertNotIn("<b>", payload)
        self.assertIn("&lt;b&gt;", payload)

    def test_script_terminator_is_neutralised(self) -> None:
        """本文の </script> でページが壊れないこと。"""
        self._write("VID-000001_clip", content="ここで </script> と書く")
        page = html_catalog.render_html(html_catalog.collect_records())
        payload = page.split("window.__RECORDS__ = ", 1)[1].split("\n", 1)[0]
        self.assertNotIn("</script>", payload)

    def test_ambiguous_dates_are_not_reinterpreted(self) -> None:
        period = builder.RecordingPeriod(text="2009年8月15日", is_ambiguous=True)
        self._write("VID-000001_clip", period=period)
        records = html_catalog.collect_records()
        self.assertIn("date-ambiguous", records[0].statuses)
        self.assertGreater(records[0].sort_period, "2100-00-00")

    def test_template_descriptions_are_flagged(self) -> None:
        material = builder.DescriptionMaterial(
            catalog_id="VID-000001", file_name="clip.mp4",
            source_path="X:/videos/clip.mp4", duration_seconds=95.0)
        text = builder.build_description_text(
            material, content=builder.fallback_content_text(material),
            youtube=builder.fallback_youtube_text(material),
            generator="template")
        (paths.descriptions_dir() / "VID-000001_clip.txt").write_text(
            text, encoding="utf-8")
        records = html_catalog.collect_records()
        self.assertIn("template", records[0].statuses)

    def test_empty_catalog_still_renders(self) -> None:
        page = html_catalog.render_html([])
        self.assertIn("動画カタログ", page)

    def test_write_is_atomic(self) -> None:
        self._write("VID-000001_clip")
        target = html_catalog.write_catalog(html_catalog.collect_records())
        self.assertFalse(target.with_suffix(target.suffix + ".tmp").exists())

    def test_unreadable_files_are_skipped_not_fatal(self) -> None:
        self._write("VID-000001_clip")
        (paths.descriptions_dir() / "broken.txt").write_text(
            "これは説明文ではありません", encoding="utf-8")
        self.assertEqual(len(html_catalog.collect_records()), 1)


if __name__ == "__main__":
    unittest.main()
