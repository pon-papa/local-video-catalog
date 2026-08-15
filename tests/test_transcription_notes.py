"""文字起こしの診断メッセージの見え方.

実運用のログに、こう出た:

    参考: 参考: 同一の文が 10 回連続しています。実際に繰り返されている
    可能性もあるため、除外はしていません。

本文の側が既に「参考:」で始まっていて、表示の側でも付けていた。
**表示だけの問題**なので、判定には触れずに表示層で直す。

あわせて、**「同一文の連続」は診断であって除外条件ではない**という
決まりが崩れていないことも、ここで固定する。
"""

from __future__ import annotations

import unittest

from _support import APP_ROOT

from local_video_catalog import transcript_schemas as ts
from local_video_catalog.stages import transcription


class NoteLabelTests(unittest.TestCase):
    """I / J. 「参考:」を二重にしない。"""

    def test_a_plain_message_gets_one_prefix(self) -> None:
        self.assertEqual(transcription.label_note("何かありました。"),
                         "参考: 何かありました。")

    def test_a_message_that_already_has_it_is_not_doubled(self) -> None:
        """J. **本文に既に入っていても、表示は 1 つ。**"""
        self.assertEqual(transcription.label_note("参考: 何かありました。"),
                         "参考: 何かありました。")

    def test_even_a_triple_is_collapsed(self) -> None:
        self.assertEqual(
            transcription.label_note("参考: 参考: 参考: 何か"),
            "参考: 何か")

    def test_the_result_never_contains_the_doubled_form(self) -> None:
        for message in ("参考: 同一の文が 10 回連続しています。",
                        "同一の文が 10 回連続しています。",
                        "参考:同一の文が 10 回連続しています。",
                        "  参考: 余分な空白付き  "):
            with self.subTest(message=message):
                self.assertNotIn("参考: 参考:",
                                 transcription.label_note(message))

    def test_the_real_repetition_warning_shows_once(self) -> None:
        """実際に出た文言で確かめる。"""
        chunk = ts.NormalizedChunk()
        for index in range(ts.REPETITION_THRESHOLD + 2):
            chunk.segments.append(ts.Segment(
                sequence_index=index, start_seconds=index,
                end_seconds=index + 1, text="ご視聴ありがとうございました"))
        warning = next(
            (w for w in _repetition_warnings(chunk) if "連続" in w), "")
        self.assertTrue(warning, "連続の警告が作られていません。")
        shown = transcription.label_note(warning)
        self.assertEqual(shown.count("参考:"), 1, shown)


def _repetition_warnings(chunk) -> list[str]:
    """本文側が作る「連続」の警告を取り出す。"""
    repetition = ts.detect_repetition([s.text for s in chunk.segments])
    if repetition < ts.REPETITION_THRESHOLD:
        return []
    return [f"参考: 同一の文が {repetition} 回連続しています。"
            "実際に繰り返されている可能性もあるため、除外はしていません。"]


class DisplayOnlyTests(unittest.TestCase):
    """表示層だけを直したこと。"""

    def test_the_stage_uses_the_helper(self) -> None:
        source = (APP_ROOT / "src" / "local_video_catalog" / "stages"
                  / "transcription.py").read_text(encoding="utf-8")
        block = source.split("for warning in merged.warnings", 1)[1]
        block = block.split("\n\n", 1)[0]
        self.assertIn("label_note", block)

    def test_no_raw_prefix_is_concatenated_any_more(self) -> None:
        """表示側で直接くっつける書き方が残っていないこと。"""
        source = (APP_ROOT / "src" / "local_video_catalog" / "stages"
                  / "transcription.py").read_text(encoding="utf-8")
        self.assertNotIn('f"    参考: {warning}"', source)


class RepetitionStaysDiagnosticTests(unittest.TestCase):
    """K. **連続は診断のまま。** 除外条件へ変えていないこと。"""

    REPEATS = ts.REPETITION_THRESHOLD + 5

    def _normalized(self) -> "ts.NormalizedChunk":
        """**本物の正規化を通す。** 同じ文がずっと続く音声を再現する。"""
        items = [{"start": index * 1000, "end": (index + 1) * 1000,
                  "text": "まったく同じ文です"}
                 for index in range(self.REPEATS)]
        return ts.normalize_engine_items(items)

    def test_repeated_segments_are_kept(self) -> None:
        chunk = self._normalized()
        self.assertEqual(len(chunk.segments), self.REPEATS,
                         "連続を理由に消してはいけません。")

    def test_the_repetition_is_only_reported(self) -> None:
        chunk = self._normalized()
        self.assertTrue(any("連続" in w for w in chunk.warnings),
                        "連続していることは伝えること。")
        self.assertNotEqual(chunk.status, ts.STATUS_NO_SPEECH,
                            "繰り返しを「発話なし」にしてはいけません。")

    def test_repeated_text_still_feeds_the_description(self) -> None:
        """**説明文の材料からも外さない。** 外すのは既知の定型だけ。"""
        chunk = self._normalized()
        text, excluded = ts.usable_text(chunk.segments)
        self.assertEqual(excluded, 0)
        self.assertIn("まったく同じ文です", text)

    def test_repetition_does_not_mark_hallucination(self) -> None:
        """**繰り返し自体は幻覚の印にしない。** 既知の定型とは別の話。"""
        self.assertFalse(ts.looks_like_hallucination("まったく同じ文です"))

    def test_detection_threshold_is_unchanged(self) -> None:
        self.assertEqual(ts.REPETITION_THRESHOLD, 3)

    def test_repetition_is_not_used_as_a_removal_reason(self) -> None:
        """除外の判断に ``detect_repetition`` を使っていないこと。"""
        source = (APP_ROOT / "src" / "local_video_catalog"
                  / "transcript_schemas.py").read_text(encoding="utf-8")
        block = source.split("repetition = detect_repetition", 1)[1]
        block = block.split("suspected =", 1)[0]
        for removal in ("STATUS_NO_SPEECH", "remove", "del ", "pop(",
                        "is_suspected_hallucination = True"):
            with self.subTest(name=removal):
                self.assertNotIn(removal, block)


if __name__ == "__main__":
    unittest.main()
