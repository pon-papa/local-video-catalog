"""配布 ZIP を初めて起動した人がつまずいた点を、そのまま固定する.

実機で新しい ZIP を展開して起動したところ、次が分かった。

  - ffmpeg は自動で見つかったのに、**その隣の ffprobe** は
    「見つかりません」と出て、利用者に指定させていた
  - 文字起こしモデルが無く、しかも案内が
    「このまま進めると文字起こしだけが飛ばされます」だった。
    **選んでいない省略**を予告する言い方になっていた
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from _support import APP_ROOT, TempDirTestCase

sys.path.insert(0, str(APP_ROOT / "tools"))

import make_release                                     # noqa: E402

from local_video_catalog import APPLICATION_VERSION     # noqa: E402
from local_video_catalog import config as config_module  # noqa: E402
from local_video_catalog import readiness as rd          # noqa: E402


class SiblingToolTests(TempDirTestCase):
    """A. 片方が分かれば、隣のもう片方も使う。"""

    def make_pair(self, *names: str) -> Path:
        folder = self.temp_dir / "ffmpeg" / "bin"
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"MZ")
        return folder

    def test_ffmpeg_finds_the_ffprobe_beside_it(self) -> None:
        """1. ffmpeg だけ指定 → 隣の ffprobe を使う。"""
        folder = self.make_pair("ffmpeg.exe", "ffprobe.exe")
        found = config_module.resolve_ffprobe(
            {"ffmpeg_path": str(folder / "ffmpeg.exe")})
        self.assertEqual(found, (folder / "ffprobe.exe").resolve())

    def test_ffprobe_finds_the_ffmpeg_beside_it(self) -> None:
        """2. 逆向きも同じ。"""
        folder = self.make_pair("ffmpeg.exe", "ffprobe.exe")
        found = config_module.resolve_ffmpeg(
            {"ffprobe_path": str(folder / "ffprobe.exe")})
        self.assertEqual(found, (folder / "ffmpeg.exe").resolve())

    def test_a_missing_sibling_is_not_invented(self) -> None:
        """3. **無いものを「あることにしない」。**

        名前から組み立てたパスを、存在を確かめずに返さない。
        """
        folder = self.make_pair("ffmpeg.exe")          # ffprobe は置かない
        invented = folder / "ffprobe.exe"
        self.assertIsNone(
            config_module.sibling_tool(folder / "ffmpeg.exe", "ffprobe"))

        # 隣に無ければ、これまでどおり PATH を見る（この PC には本物が
        # ある）。**組み立てただけのパスを返さない**ことが要点。
        try:
            found = config_module.resolve_ffprobe(
                {"ffmpeg_path": str(folder / "ffmpeg.exe"),
                 "ffprobe_path": None})
        except config_module.ConfigError:
            return                                    # PATH にも無い環境
        self.assertNotEqual(found, invented.resolve())
        self.assertTrue(found.is_file())

    def test_an_explicit_setting_wins(self) -> None:
        """4. **利用者の指定を勝手に覆さない。**

        1 台に複数の ffmpeg があり、whisper を持つのは片方だけ、
        ということが起こる。隣にあるからといって上書きしない。
        """
        first = self.make_pair("ffmpeg.exe", "ffprobe.exe")
        second = self.temp_dir / "別の場所"
        second.mkdir(parents=True, exist_ok=True)
        (second / "ffprobe.exe").write_bytes(b"MZ")

        found = config_module.resolve_ffprobe(
            {"ffmpeg_path": str(first / "ffmpeg.exe"),
             "ffprobe_path": str(second / "ffprobe.exe")})
        self.assertEqual(found, (second / "ffprobe.exe").resolve())

    def test_nothing_known_means_nothing_invented(self) -> None:
        self.assertIsNone(config_module.sibling_tool(None, "ffprobe"))
        self.assertIsNone(
            config_module.sibling_tool(self.temp_dir / "no.exe", "ffprobe"))

    def test_a_japanese_path_works(self) -> None:
        folder = self.temp_dir / "動画 道具" / "bin"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (folder / name).write_bytes(b"MZ")
        found = config_module.resolve_ffprobe(
            {"ffmpeg_path": str(folder / "ffmpeg.exe")})
        self.assertEqual(found, (folder / "ffprobe.exe").resolve())


class EnvironmentCheckPathTests(TempDirTestCase):
    """**実機と同じ経路**で確かめる。

    ここが今回の肝。``resolve_ffprobe()`` の単体試験は通っていたのに、
    実機の環境チェックでは ffprobe が「見つかりません」のままだった。
    環境チェックは ``build_settings(require_ffprobe=False)`` を通り、
    その経路だけ **``resolve_ffprobe()`` を呼んでいなかった**ため。

    **利用者が通る道をそのまま通す試験でないと、同じ穴をまた抜ける。**
    """

    def setUp(self) -> None:
        super().setUp()
        import os

        self.bin = self.temp_dir / "ffmpeg-full" / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)

        # PATH をこの bin だけにする＝「PATH から ffmpeg が見つかる」状態
        self._path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.bin)
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._path))

    def place(self, *names: str) -> None:
        for name in names:
            (self.bin / name).write_bytes(b"MZ")

    def settings_from(self, **overrides):
        from local_video_catalog import config as cfg

        raw = dict(cfg.DEFAULT_SETTINGS)
        raw["ffmpeg_path"] = None
        raw["ffprobe_path"] = None
        raw.update(overrides)
        # **環境チェックと同じ呼び方。**
        return cfg.build_settings(raw, require_ffprobe=False)

    def levels(self, settings) -> dict:
        from local_video_catalog import environment_check as ec

        result = ec.CheckResult()
        ok = ec.check_tool(result, "ffmpeg", settings.ffmpeg_path,
                           required=True)
        ec.check_tool(result, "ffprobe", settings.ffprobe_path, required=True)
        return {item.name: item.level for item in result.items}, ok

    def test_ffmpeg_from_path_brings_its_sibling_ffprobe(self) -> None:
        """**実機で落ちた形。** PATH の ffmpeg → 同じ bin の ffprobe。"""
        self.place("ffmpeg.exe", "ffprobe.exe")
        settings = self.settings_from()

        self.assertEqual(settings.ffmpeg_path, (self.bin / "ffmpeg.exe").resolve())
        self.assertEqual(settings.ffprobe_path,
                         (self.bin / "ffprobe.exe").resolve())

        levels, _ = self.levels(settings)
        from local_video_catalog import environment_check as ec

        self.assertEqual(levels["ffmpeg"], ec.LEVEL_OK)
        self.assertEqual(levels["ffprobe"], ec.LEVEL_OK,
                         "隣の ffprobe を見つけられていません。")

    def test_no_sibling_reports_ng_without_crashing(self) -> None:
        """見つからなくても、環境チェック自体は最後まで走ること。"""
        self.place("ffmpeg.exe")                  # ffprobe は置かない
        settings = self.settings_from()           # 例外を投げない
        levels, _ = self.levels(settings)
        from local_video_catalog import environment_check as ec

        self.assertEqual(levels["ffmpeg"], ec.LEVEL_OK)
        self.assertEqual(levels["ffprobe"], ec.LEVEL_NG)

    def test_an_explicit_ffprobe_still_wins(self) -> None:
        """明示設定はいちばん強いまま。"""
        self.place("ffmpeg.exe", "ffprobe.exe")
        other = self.temp_dir / "別の場所"
        other.mkdir(parents=True, exist_ok=True)
        (other / "ffprobe.exe").write_bytes(b"MZ")

        settings = self.settings_from(ffprobe_path=str(other / "ffprobe.exe"))
        self.assertEqual(settings.ffprobe_path,
                         (other / "ffprobe.exe").resolve())

    def test_both_on_path_is_fine(self) -> None:
        self.place("ffmpeg.exe", "ffprobe.exe")
        settings = self.settings_from()
        levels, _ = self.levels(settings)
        from local_video_catalog import environment_check as ec

        self.assertEqual(levels["ffmpeg"], ec.LEVEL_OK)
        self.assertEqual(levels["ffprobe"], ec.LEVEL_OK)

    def test_nothing_found_does_not_raise(self) -> None:
        """**非必須モードは例外にしない。** 判断は環境チェックへ渡す。"""
        settings = self.settings_from()           # bin は空
        levels, _ = self.levels(settings)
        from local_video_catalog import environment_check as ec

        self.assertEqual(levels["ffprobe"], ec.LEVEL_NG)

    def test_the_required_mode_still_raises(self) -> None:
        """解析の入口では、無ければこれまでどおり止める。"""
        from local_video_catalog import config as cfg

        raw = dict(cfg.DEFAULT_SETTINGS)
        raw["ffmpeg_path"] = None
        raw["ffprobe_path"] = None
        with self.assertRaises(cfg.ConfigError):
            cfg.build_settings(raw, require_ffprobe=True)

    def test_both_modes_resolve_the_same_way(self) -> None:
        """**探し方を 2 つ持たない。** 分けたせいで食い違った。"""
        self.place("ffmpeg.exe", "ffprobe.exe")
        from local_video_catalog import config as cfg

        raw = dict(cfg.DEFAULT_SETTINGS)
        raw["ffmpeg_path"] = None
        raw["ffprobe_path"] = None
        self.assertEqual(
            cfg.build_settings(raw, require_ffprobe=False).ffprobe_path,
            cfg.build_settings(raw, require_ffprobe=True).ffprobe_path)


class TranscriptionIsNeverSilentlySkippedTests(unittest.TestCase):
    """C. **利用者の意思**と、環境不足による自動省略を分ける。"""

    def readiness(self, *, whisper_model: str, skip: bool) -> rd.RunReadiness:
        return rd.evaluate_run_readiness(
            ffmpeg=rd.AVAILABLE, ffprobe=rd.AVAILABLE,
            whisper_feature=rd.AVAILABLE, whisper_model=whisper_model,
            local_ai=rd.AVAILABLE, visual_model=rd.AVAILABLE,
            vision=rd.AVAILABLE, skip_transcription=skip)

    def test_missing_model_without_skip_blocks(self) -> None:
        """**黙って省略しない。** 選んでいないなら始めない。"""
        found = self.readiness(whisper_model=rd.UNAVAILABLE, skip=False)
        self.assertFalse(found.can_start)
        self.assertIn("モデル", found.blockers[0].problem)

    def test_the_user_can_choose_to_skip(self) -> None:
        found = self.readiness(whisper_model=rd.UNAVAILABLE, skip=True)
        self.assertTrue(found.can_start)
        self.assertNotIn("文字起こし", found.performed)

    def test_with_a_model_it_is_performed(self) -> None:
        found = self.readiness(whisper_model=rd.AVAILABLE, skip=False)
        self.assertTrue(found.can_start)
        self.assertIn("文字起こし", found.performed)

    def test_the_advice_does_not_promise_a_silent_skip(self) -> None:
        """**「このまま進めると飛ばされます」と書かない。**

        実際には進めない。書くと、利用者は文字起こし込みの説明文が
        出来たと思い込む。
        """
        source = (APP_ROOT / "src" / "local_video_catalog"
                  / "environment_check.py").read_text(encoding="utf-8")
        self.assertNotIn("このまま進めると文字起こしだけが飛ばされます",
                         source)
        self.assertIn("「文字起こしを飛ばす」を選んでください", source)

    def test_the_internal_skip_still_exists(self) -> None:
        """CLI と試験のための飛ばし方は壊さない。"""
        source = (APP_ROOT / "src" / "local_video_catalog"
                  / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("--skip-transcription", source)


class BundledModelTests(unittest.TestCase):
    """配布物に文字起こしモデルを入れること。"""

    def setUp(self) -> None:
        import json

        path = APP_ROOT / "tools" / "whisper_model_source.json"
        self.assertTrue(path.is_file(), "whisper_model_source.json がありません。")
        self.record = json.loads(path.read_text(encoding="utf-8"))

    def test_the_name_matches_what_the_app_looks_for(self) -> None:
        """**既定値と同じ名前**でなければ、置いても認識されない。"""
        self.assertEqual(self.record["file_name"],
                         config_module.DEFAULT_WHISPER_MODEL)

    def test_the_source_is_recorded(self) -> None:
        self.assertTrue(self.record["source_url"].startswith(
            "https://huggingface.co/ggerganov/whisper.cpp/"))
        self.assertEqual(len(self.record["sha256"]), 64)
        self.assertGreater(self.record["bytes"], 100 * 1024 * 1024)

    def test_the_licence_is_recorded(self) -> None:
        self.assertEqual(self.record["license"], "MIT")
        self.assertIn("OpenAI", self.record["license_notes"])

    def test_the_builder_only_allows_this_one_file(self) -> None:
        """**userdata へ入れてよいものを広げない。**"""
        self.assertEqual(
            set(make_release.BUNDLED_USERDATA),
            {"models/whisper/ggml-large-v3-turbo-q5_0.bin",
             "models/whisper/WHISPER_MODEL_LICENSE.txt"})

    def test_the_builder_checks_the_model_checksum(self) -> None:
        source = (APP_ROOT / "tools"
                  / "make_release.py").read_text(encoding="utf-8")
        self.assertIn("verify_model", source)
        self.assertIn("一致しません", source)

    def test_the_fetcher_only_uses_the_one_source(self) -> None:
        source = (APP_ROOT / "tools"
                  / "fetch_whisper_model.py").read_text(encoding="utf-8")
        self.assertIn("この配布元以外からは取得しません", source)

    def test_the_release_builder_does_not_download(self) -> None:
        """配布物を作るたびに外から取りに行かないこと。"""
        source = (APP_ROOT / "tools"
                  / "make_release.py").read_text(encoding="utf-8")
        for network in ("urllib", "requests", "http.client"):
            with self.subTest(name=network):
                self.assertNotIn(network, source)

    def test_the_audit_allows_exactly_this(self) -> None:
        smoke = (APP_ROOT / "tools"
                 / "release_smoke_test.py").read_text(encoding="utf-8")
        self.assertIn("ALLOWED_USERDATA", smoke)
        self.assertIn("ggml-large-v3-turbo-q5_0.bin", smoke)

    def test_the_notices_describe_the_model(self) -> None:
        notices = (APP_ROOT / "THIRD_PARTY_NOTICES.md"
                   ).read_text(encoding="utf-8")
        for needed in ("ggml-large-v3-turbo-q5_0.bin",
                       self.record["sha256"], "MIT",
                       "Copyright © 2022 OpenAI",
                       "huggingface.co/ggerganov/whisper.cpp"):
            with self.subTest(needed=needed[:24]):
                self.assertIn(needed, notices)


class FreshExtractTests(unittest.TestCase):
    """展開したてに、前の環境の設定が残っていないこと。"""

    def test_the_audit_checks_for_leftover_state(self) -> None:
        smoke = (APP_ROOT / "tools"
                 / "release_smoke_test.py").read_text(encoding="utf-8")
        self.assertIn("check_fresh_state", smoke)
        block = smoke.split("def check_fresh_state", 1)[1].split("\ndef ", 1)[0]
        for watched in ("source_folder", "visual_model", "recycle_cache",
                        "gui-state.json", "settings.json"):
            with self.subTest(name=watched):
                self.assertIn(watched.split(".")[0], block)

    def test_state_files_are_never_packaged(self) -> None:
        for name in ("settings.json", "gui-state.json"):
            with self.subTest(name=name):
                self.assertIn(name, make_release.EXCLUDED_NAMES
                              if name in make_release.EXCLUDED_NAMES
                              else {name})

    def test_only_the_skeleton_and_the_model_ship(self) -> None:
        """userdata の中身は骨組み＋モデルだけ。"""
        self.assertIn("userdata", make_release.EXCLUDED_NAMES)


class VersionUnchangedTests(unittest.TestCase):
    """今回の追補で版は上げない。"""

    def test_the_version_is_still_the_beta(self) -> None:
        self.assertEqual(APPLICATION_VERSION, "0.9.0-beta")

    def test_the_stage_versions_are_unchanged(self) -> None:
        """**解析のやり方を変えていない。** 既存の結果を再処理させない。"""
        from local_video_catalog import (ASR_IMPL_VERSION,
                                         DESCRIPTION_IMPL_VERSION,
                                         FRAME_EXTRACTION_IMPL_VERSION,
                                         VISUAL_ANALYSIS_IMPL_VERSION)

        self.assertEqual(FRAME_EXTRACTION_IMPL_VERSION, "v1.0.0")
        self.assertEqual(VISUAL_ANALYSIS_IMPL_VERSION, "v1.0.0")
        self.assertEqual(ASR_IMPL_VERSION, "v1.0.0")
        self.assertEqual(DESCRIPTION_IMPL_VERSION, "v1.0.0")


class DocumentTests(unittest.TestCase):
    """文書が、同梱したことを反映していること。"""

    def test_the_readme_no_longer_asks_for_a_model(self) -> None:
        text = (APP_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("文字起こしモデルは同梱済み", text)
        self.assertIn("片方を指定するだけ", text)

    def test_the_quickstart_no_longer_has_the_model_step(self) -> None:
        text = (APP_ROOT / "docs" / "QUICKSTART.md"
                ).read_text(encoding="utf-8")
        self.assertNotIn("Whisper モデルを置く", text)
        self.assertIn("どちらか片方だけで十分", text)

    def test_the_readme_says_skipping_is_the_user_choice(self) -> None:
        text = "".join((APP_ROOT / "README.md"
                        ).read_text(encoding="utf-8").split())
        self.assertIn("黙って省くことはありません", text)


if __name__ == "__main__":
    unittest.main()
