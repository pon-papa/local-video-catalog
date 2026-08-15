"""選んだモデルが**本当に画像を受け取れるか**の確認.

ここで固定したいこと:

  - **名前で判断しない。** ``vl`` の有無ではなく実際の応答で決める
  - **利用者のデータを一切送らない**（画像はその場で作る）
  - **接続先は 127.0.0.1 だけ**
  - 「画像非対応」と「確かめられなかった」を混ぜない
  - **時間切れを「たぶん使える」にしない**
"""

from __future__ import annotations

import ast
import base64
import struct
import unittest

from _fake_lm_studio import (EMPTY, SERVER_ERROR, SLOW, TEXT_ONLY,
                             FakeLmStudio)
from _support import APP_ROOT

from local_video_catalog import vision_probe, vlm_client


def settings_for(server: FakeLmStudio, *, timeout: int = 10
                 ) -> vlm_client.VlmSettings:
    return vlm_client.VlmSettings(base_url=server.base_url,
                                  timeout_seconds=timeout)


class TestImageTests(unittest.TestCase):
    """**送るのは、その場で作った 8×8 の PNG だけ。**"""

    def test_the_image_is_a_valid_png(self) -> None:
        data = vision_probe.test_image_png()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (8, 8))

    def test_the_image_is_tiny(self) -> None:
        self.assertLess(len(vision_probe.test_image_png()), 1024)

    def test_the_image_does_not_depend_on_anything(self) -> None:
        """毎回同じ。**利用者のデータや環境が混ざる余地がない。**"""
        self.assertEqual(vision_probe.test_image_png(),
                         vision_probe.test_image_png())

    def test_base64_round_trip(self) -> None:
        self.assertEqual(base64.b64decode(vision_probe.test_image_base64()),
                         vision_probe.test_image_png())


class OutcomeTests(unittest.TestCase):
    """A〜E の区別。"""

    def test_success(self) -> None:
        with FakeLmStudio(["some-model"]) as server:
            found = vision_probe.probe(settings_for(server), "some-model")
        self.assertEqual(found.outcome, vision_probe.OK)
        self.assertTrue(found.usable)

    def test_text_only_model_is_rejected(self) -> None:
        """**名前ではなく応答で判定する。** 画像を拒めば非対応。"""
        with FakeLmStudio(["plain-llm"], behaviour=TEXT_ONLY) as server:
            found = vision_probe.probe(settings_for(server), "plain-llm")
        self.assertEqual(found.outcome, vision_probe.NO_IMAGE)
        self.assertFalse(found.usable)
        self.assertIn("別のモデル", found.advice)

    def test_a_vision_sounding_name_does_not_pass_on_its_own(self) -> None:
        """名前に vl が入っていても、拒まれたら非対応。"""
        with FakeLmStudio(["fancy-vl-vision-image"],
                          behaviour=TEXT_ONLY) as server:
            found = vision_probe.probe(settings_for(server),
                                       "fancy-vl-vision-image")
        self.assertEqual(found.outcome, vision_probe.NO_IMAGE)

    def test_a_plain_name_can_still_pass(self) -> None:
        """逆に、名前に手がかりが無くても通れば使える。"""
        with FakeLmStudio(["model-a"]) as server:
            found = vision_probe.probe(settings_for(server), "model-a")
        self.assertEqual(found.outcome, vision_probe.OK)

    def test_timeout_is_unknown_not_usable(self) -> None:
        """**「たぶん使える」にしない。**"""
        with FakeLmStudio(["slow-model"], behaviour=SLOW,
                          delay_seconds=3) as server:
            found = vision_probe.probe(settings_for(server), "slow-model",
                                       timeout_seconds=1)
        self.assertEqual(found.outcome, vision_probe.UNKNOWN)
        self.assertFalse(found.usable)
        self.assertIn("確認できませんでした", found.detail)

    def test_server_error_is_unknown_not_no_image(self) -> None:
        """5xx は一時的かもしれない。**非対応と決めつけない。**"""
        with FakeLmStudio(["m"], behaviour=SERVER_ERROR) as server:
            found = vision_probe.probe(settings_for(server), "m")
        self.assertEqual(found.outcome, vision_probe.UNKNOWN)

    def test_empty_answer_counts_as_no_image(self) -> None:
        with FakeLmStudio(["m"], behaviour=EMPTY) as server:
            found = vision_probe.probe(settings_for(server), "m")
        self.assertEqual(found.outcome, vision_probe.NO_IMAGE)

    def test_no_connection(self) -> None:
        with FakeLmStudio(["m"]) as server:
            dead = server.base_url
        found = vision_probe.probe(
            vlm_client.VlmSettings(base_url=dead, timeout_seconds=3), "m")
        self.assertEqual(found.outcome, vision_probe.NOT_CONNECTED)

    def test_no_model_selected(self) -> None:
        with FakeLmStudio(["m"]) as server:
            found = vision_probe.probe(settings_for(server), "")
        self.assertEqual(found.outcome, vision_probe.MODEL_MISSING)

    def test_nothing_is_sent_when_no_model_is_selected(self) -> None:
        with FakeLmStudio(["m"]) as server:
            vision_probe.probe(settings_for(server), "   ")
            self.assertEqual(server.requests, [])


class PrivacyTests(unittest.TestCase):
    """**利用者のデータを送らない。外へも出さない。**"""

    def test_only_the_generated_image_is_sent(self) -> None:
        with FakeLmStudio(["m"]) as server:
            vision_probe.probe(settings_for(server), "m")
            images = server.images_sent()
        self.assertEqual(len(images), 1)
        expected = f"data:image/png;base64,{vision_probe.test_image_base64()}"
        self.assertEqual(images[0], expected)

    def test_the_request_is_small(self) -> None:
        with FakeLmStudio(["m"]) as server:
            vision_probe.probe(settings_for(server), "m")
            payload = server.requests[0]
        self.assertLessEqual(payload.get("max_tokens", 9999), 32)

    def test_external_hosts_are_refused_before_sending(self) -> None:
        for url in ("http://example.com/v1", "https://api.openai.com/v1",
                    "http://192.168.1.10:1234/v1"):
            with self.subTest(url=url):
                found = vision_probe.probe(
                    vlm_client.VlmSettings(base_url=url), "m")
                self.assertEqual(found.outcome, vision_probe.NOT_CONNECTED)

    def test_the_traffic_goes_to_loopback(self) -> None:
        with FakeLmStudio(["m"]) as server:
            self.assertIn("127.0.0.1", server.base_url)
            vision_probe.probe(settings_for(server), "m")
            self.assertTrue(server.requests)

    def test_the_module_never_reads_user_data(self) -> None:
        """**画像は作る。読まない。**

        ファイルを開く手段そのものを持たせない。持たせた瞬間、いつか
        代表画像を使う実装に書き換わる。

        「使わない」と書いた docstring まで証拠として拾わないよう、
        構文木で実際の使用だけを見る。
        """
        from _support import code_strings_and_calls

        strings, calls = code_strings_and_calls(vision_probe)
        for forbidden in ("open", "read_bytes", "read_text", "glob",
                          "listdir", "iterdir"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, calls)
        for text in strings:
            with self.subTest(text=text[:20]):
                self.assertNotIn("userdata", text)
                self.assertNotIn("frames", text)

    def test_the_module_creates_no_files(self) -> None:
        from _support import code_strings_and_calls

        _strings, calls = code_strings_and_calls(vision_probe)
        for forbidden in ("write_bytes", "write_text", "mkdir",
                          "NamedTemporaryFile", "TemporaryDirectory"):
            self.assertNotIn(forbidden, calls)


if __name__ == "__main__":
    unittest.main()
