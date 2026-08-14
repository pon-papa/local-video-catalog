"""ローカル AI まわり — privacy guard と、文字起こしの幻覚の扱い.

**外部へ 1 バイトも出さないこと**をここで固定する。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import transcript_schemas as ts
from local_video_catalog import vlm_client as vc


class LocalOnlyTests(unittest.TestCase):
    """**接続先はループバックだけ。**"""

    def test_loopback_hosts_are_allowed(self) -> None:
        for host in ("localhost", "127.0.0.1", "127.0.0.5", "::1", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(vc.is_local_host(host))

    def test_external_hosts_are_rejected(self) -> None:
        for host in ("example.com", "192.168.1.10", "10.0.0.1", "8.8.8.8",
                     "0.0.0.0", "api.openai.com", "", None):
            with self.subTest(host=host):
                self.assertFalse(vc.is_local_host(host))

    def test_hostnames_are_not_resolved(self) -> None:
        """名前解決に頼らない。hosts を書き換えても外へ向かない。"""
        for host in ("localhost.example.com", "my-localhost", "localhost.evil"):
            with self.subTest(host=host):
                self.assertFalse(vc.is_local_host(host))

    def test_external_base_url_raises(self) -> None:
        for url in ("http://example.com/v1", "https://api.openai.com/v1",
                    "http://192.168.1.10:1234/v1"):
            with self.subTest(url=url):
                with self.assertRaises(vc.PrivacyConfigurationError):
                    vc.assert_local_base_url(url)

    def test_unsupported_scheme_raises(self) -> None:
        for url in ("ftp://127.0.0.1/v1", "file:///etc/passwd", "ws://127.0.0.1"):
            with self.subTest(url=url):
                with self.assertRaises(vc.PrivacyConfigurationError):
                    vc.assert_local_base_url(url)

    def test_empty_base_url_raises(self) -> None:
        for url in ("", "   ", None):
            with self.subTest(url=url):
                with self.assertRaises(vc.PrivacyConfigurationError):
                    vc.assert_local_base_url(url)

    def test_trailing_slash_is_trimmed(self) -> None:
        self.assertEqual(vc.assert_local_base_url("http://127.0.0.1:1234/v1/"),
                         "http://127.0.0.1:1234/v1")

    def test_client_refuses_to_construct_for_external_hosts(self) -> None:
        """**画像を組み立てる前に**止まること。"""
        settings = vc.VlmSettings(base_url="http://example.com/v1")
        with self.assertRaises(vc.PrivacyConfigurationError):
            vc.LocalVlmClient(settings)

    def test_record_form_drops_the_path(self) -> None:
        self.assertEqual(
            vc.safe_api_base_for_record("http://127.0.0.1:1234/v1/chat"),
            "http://127.0.0.1:1234")


class NoEscapeHatchTests(unittest.TestCase):
    """**外部を許可する逃げ道が存在しないこと。**"""

    def setUp(self) -> None:
        self.source = Path(vc.__file__).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_allow_external_option(self) -> None:
        for forbidden in ("allow_external", "allow_remote", "insecure",
                          "skip_verify", "disable_privacy", "force_host"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_redirects_are_refused(self) -> None:
        self.assertIn("_NoRedirectHandler", self.source)
        self.assertIn("redirect_request", self.source)

    def test_environment_proxies_are_disabled(self) -> None:
        self.assertIn("ProxyHandler({})", self.source)

    def test_only_urllib_is_used(self) -> None:
        """HTTP クライアントを追加しない。"""
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("requests", "httpx", "aiohttp", "socket", "http"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, imported)

    def test_every_request_revalidates_the_url(self) -> None:
        """base_url を後から書き換えられても守る。"""
        request_body = self.source.split("def _request(", 1)[1]
        self.assertIn("assert_local_base_url(url)", request_body)


class ModelSelectionTests(unittest.TestCase):
    """**曖昧なら勝手に選ばない。**"""

    def test_exact_match(self) -> None:
        self.assertEqual(
            vc.select_model(["a-model", "b-model"], "a-model"), "a-model")

    def test_single_partial_match(self) -> None:
        self.assertEqual(
            vc.select_model(["qwen3-vl-8b-instruct-q4"], "qwen3-vl-8b"),
            "qwen3-vl-8b-instruct-q4")

    def test_ambiguous_match_raises(self) -> None:
        with self.assertRaises(vc.ModelSelectionError):
            vc.select_model(["qwen3-vl-8b-q4", "qwen3-vl-8b-q6"], "qwen3-vl-8b")

    def test_no_match_raises(self) -> None:
        with self.assertRaises(vc.ModelSelectionError):
            vc.select_model(["a"], "b")

    def test_empty_request_raises(self) -> None:
        with self.assertRaises(vc.ModelSelectionError):
            vc.select_model(["a"], "")


class TimeoutSeparationTests(TempAppRootTestCase):
    """**視覚概要の待ち時間をフレームと分ける。**

    2026-08-12 に、概要へ 300 秒を適用したせいで 22〜24 枚の動画が
    まとめて失敗した。同じ失敗を繰り返さない。
    """

    def test_summary_timeout_defaults_longer(self) -> None:
        settings = vc.VlmSettings()
        self.assertGreater(settings.summary_timeout_seconds,
                           settings.timeout_seconds)

    def test_summary_timeout_never_falls_below_the_frame_timeout(self) -> None:
        settings = vc.VlmSettings.from_settings(
            {"vlm": {"timeout_seconds": 2000}})
        self.assertGreaterEqual(settings.summary_timeout_seconds, 2000)

    def test_explicit_summary_timeout_is_respected(self) -> None:
        settings = vc.VlmSettings.from_settings(
            {"vlm": {"timeout_seconds": 300, "summary_timeout_seconds": 3600}})
        self.assertEqual(settings.summary_timeout_seconds, 3600)

    def test_timeouts_do_not_affect_the_reuse_key(self) -> None:
        """待ち時間を変えても、保存済みの解析を作り直さない。"""
        base = vc.VlmSettings()
        changed = vc.VlmSettings(timeout_seconds=999,
                                 summary_timeout_seconds=9999)
        self.assertEqual(base.generation_identity(),
                         changed.generation_identity())

    def test_generation_settings_do_affect_the_reuse_key(self) -> None:
        self.assertNotEqual(
            vc.VlmSettings().generation_identity(),
            vc.VlmSettings(temperature=0.9).generation_identity())

    def test_concurrency_above_one_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            vc.VlmSettings(maximum_concurrent_requests=2).validate()


class HallucinationTests(unittest.TestCase):
    """**印は付ける。本文は消さない。**"""

    def test_known_phrases_are_flagged(self) -> None:
        for text in ("ご視聴ありがとうございました",
                     "ご視聴ありがとうございます。",
                     "チャンネル登録お願いします",
                     "Thanks for watching!",
                     "作詞・作曲 初音ミク",
                     "作曲・編曲 誰か",
                     "Subtitles by the community",
                     "ご覧いただきましてありがとうございます",
                     "♪♪♪"):
            with self.subTest(text=text):
                self.assertTrue(ts.looks_like_hallucination(text))

    def test_ordinary_speech_is_not_flagged(self) -> None:
        for text in ("おはよう", "こっち向いて", "はい、チーズ",
                     "ありがとう", "今日はいい天気ですね",
                     "作品を見に行こう", "登録は済んだ？"):
            with self.subTest(text=text):
                self.assertFalse(ts.looks_like_hallucination(text))

    def test_empty_text_is_not_flagged(self) -> None:
        for text in ("", "   ", "。", None):
            with self.subTest(text=text):
                self.assertFalse(ts.looks_like_hallucination(text))

    def test_repetition_is_measured_but_not_a_removal_rule(self) -> None:
        """短い語が本当に繰り返されることがある。**削除条件にしない。**"""
        texts = ["わーい"] * 5
        self.assertEqual(ts.detect_repetition(texts), 5)
        for text in texts:
            self.assertFalse(ts.looks_like_hallucination(text))

    def test_repeated_ordinary_speech_survives_normalisation(self) -> None:
        items = [{"start": i * 1000, "end": (i + 1) * 1000, "text": "わーい"}
                 for i in range(5)]
        chunk = ts.normalize_engine_items(items)
        self.assertEqual(len(chunk.segments), 5)
        self.assertEqual(chunk.suspected_count, 0)
        self.assertEqual(chunk.status, ts.STATUS_COMPLETED)
        self.assertTrue(any("参考" in w for w in chunk.warnings))

    def test_mixed_content_keeps_everything(self) -> None:
        items = [
            {"start": 0, "end": 1000, "text": "こっち向いて"},
            {"start": 1000, "end": 2000, "text": "ご視聴ありがとうございました"},
        ]
        chunk = ts.normalize_engine_items(items)
        self.assertEqual(len(chunk.segments), 2)
        self.assertEqual(chunk.suspected_count, 1)
        self.assertEqual(chunk.status, ts.STATUS_COMPLETED)
        self.assertIn("こっち向いて", chunk.text)
        self.assertIn("ご視聴ありがとうございました", chunk.text)

    def test_all_hallucination_becomes_no_speech(self) -> None:
        items = [{"start": 0, "end": 1000,
                  "text": "ご視聴ありがとうございました"}]
        chunk = ts.normalize_engine_items(items)
        self.assertEqual(chunk.status, ts.STATUS_NO_SPEECH)
        self.assertEqual(len(chunk.segments), 1, "本文が消えています。")

    def test_material_excludes_only_suspected_segments(self) -> None:
        segments = [
            ts.Segment(0, 0.0, 1.0, "こっち向いて"),
            ts.Segment(1, 1.0, 2.0, "ご視聴ありがとうございました",
                       is_suspected_hallucination=True),
            ts.Segment(2, 2.0, 3.0, "楽しかったね"),
        ]
        material, excluded = ts.usable_text(segments)
        self.assertEqual(material, "こっち向いて楽しかったね")
        self.assertEqual(excluded, 1)
        self.assertEqual(len(segments), 3, "元のセグメントが減っています。")

    def test_material_can_end_up_empty(self) -> None:
        segments = [ts.Segment(0, 0.0, 1.0, "ご視聴ありがとうございました",
                               is_suspected_hallucination=True)]
        material, excluded = ts.usable_text(segments)
        self.assertEqual(material, "")
        self.assertEqual(excluded, 1)


class NormalizationTests(unittest.TestCase):
    def test_empty_input_is_no_speech(self) -> None:
        self.assertEqual(ts.normalize_engine_items([]).status,
                         ts.STATUS_NO_SPEECH)

    def test_milliseconds_become_seconds(self) -> None:
        chunk = ts.normalize_engine_items(
            [{"start": 1500, "end": 2500, "text": "はい"}])
        self.assertAlmostEqual(chunk.segments[0].start_seconds, 1.5)
        self.assertAlmostEqual(chunk.segments[0].end_seconds, 2.5)

    def test_broken_rows_become_warnings_not_silent_drops(self) -> None:
        chunk = ts.normalize_engine_items([
            {"start": "x", "end": 1000, "text": "a"},
            "not a dict",
            {"start": 0, "end": 1000, "text": "ちゃんとした発話"},
        ])
        self.assertEqual(len(chunk.segments), 1)
        self.assertEqual(len(chunk.warnings), 2)

    def test_reversed_times_are_swapped_with_a_warning(self) -> None:
        chunk = ts.normalize_engine_items(
            [{"start": 2000, "end": 1000, "text": "はい"}])
        self.assertLess(chunk.segments[0].start_seconds,
                        chunk.segments[0].end_seconds)
        self.assertTrue(chunk.warnings)

    def test_merge_shifts_to_absolute_time(self) -> None:
        first = ts.normalize_engine_items(
            [{"start": 0, "end": 1000, "text": "あ"}])
        second = ts.normalize_engine_items(
            [{"start": 0, "end": 1000, "text": "い"}])
        merged = ts.merge_chunks([(0.0, first), (300.0, second)])
        self.assertEqual(len(merged.segments), 2)
        self.assertAlmostEqual(merged.segments[1].start_seconds, 300.0)
        self.assertEqual([s.sequence_index for s in merged.segments], [0, 1])

    def test_merge_of_only_hallucination_is_no_speech(self) -> None:
        chunk = ts.normalize_engine_items(
            [{"start": 0, "end": 1000, "text": "ご視聴ありがとうございました"}])
        merged = ts.merge_chunks([(0.0, chunk)])
        self.assertEqual(merged.status, ts.STATUS_NO_SPEECH)


if __name__ == "__main__":
    unittest.main()
