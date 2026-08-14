"""文字起こしエンジン — 非 ASCII パス対策・VAD 既定・チャンク分割."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import ASR_IMPL_VERSION
from local_video_catalog import asr_engine as ae
from local_video_catalog import paths


class ChunkPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ae.AsrConfig()

    def test_zero_duration(self) -> None:
        self.assertEqual(ae.plan_chunks(0, self.config), [])

    def test_short_video_is_one_chunk(self) -> None:
        chunks = ae.plan_chunks(30.0, self.config)
        self.assertEqual(len(chunks), 1)
        self.assertAlmostEqual(chunks[0].duration_seconds, 30.0)

    def test_long_video_is_split(self) -> None:
        chunks = ae.plan_chunks(1000.0, self.config)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(chunk.duration_seconds,
                                 self.config.chunk_duration_seconds)

    def test_chunks_overlap_after_the_first(self) -> None:
        chunks = ae.plan_chunks(1000.0, self.config)
        self.assertEqual(chunks[0].overlap_seconds, 0.0)
        self.assertGreater(chunks[1].overlap_seconds, 0.0)

    def test_chunks_cover_the_whole_video(self) -> None:
        duration = 1000.0
        chunks = ae.plan_chunks(duration, self.config)
        last = chunks[-1]
        self.assertGreaterEqual(
            last.absolute_start_seconds + last.duration_seconds, duration - 0.01)

    def test_planning_is_deterministic(self) -> None:
        first = ae.plan_chunks(1000.0, self.config)
        second = ae.plan_chunks(1000.0, self.config)
        self.assertEqual([c.absolute_start_seconds for c in first],
                         [c.absolute_start_seconds for c in second])

    def test_loss_on_interruption_is_one_chunk(self) -> None:
        """チャンク長が中断時の損失の上限になる。"""
        chunks = ae.plan_chunks(3600.0, self.config)
        self.assertLessEqual(max(c.duration_seconds for c in chunks),
                             self.config.chunk_duration_seconds)

    def test_invalid_configs_are_refused(self) -> None:
        for bad in (
            ae.AsrConfig(model_name=""),
            ae.AsrConfig(model_name="sub/model.bin"),
            ae.AsrConfig(queue_seconds=0),
            ae.AsrConfig(chunk_duration_seconds=0),
            ae.AsrConfig(chunk_overlap_seconds=-1),
            ae.AsrConfig(chunk_duration_seconds=10, chunk_overlap_seconds=10),
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    bad.validate()


class ProvenDefaultTests(unittest.TestCase):
    """実測で決まった既定値を、うっかり変えないための固定。"""

    def test_vad_is_off(self) -> None:
        """有効化で無音 60 秒が 3 秒→598 秒、日本語 CER が 0.000→0.737。"""
        self.assertFalse(ae.AsrConfig().vad_enabled)
        self.assertFalse(ae.AsrConfig.from_settings({}).vad_enabled)

    def test_queue_is_thirty_seconds(self) -> None:
        """既定 3 秒だと窓が重なり同じ内容が繰り返し出る。"""
        self.assertEqual(ae.AsrConfig().queue_seconds, 30)

    def test_chunk_is_five_minutes(self) -> None:
        self.assertEqual(ae.AsrConfig().chunk_duration_seconds, 300.0)

    def test_vad_changes_the_reuse_key(self) -> None:
        """VAD を切り替えたら認識結果が変わるので、再処理させる。"""
        self.assertNotEqual(ae.AsrConfig().config_hash,
                            ae.AsrConfig(vad_enabled=True).config_hash)

    def test_gpu_choice_does_not_change_the_reuse_key(self) -> None:
        """GPU の使用可否は認識結果の互換性を変えない。"""
        self.assertEqual(ae.AsrConfig().config_hash,
                         ae.AsrConfig(use_gpu=False).config_hash)

    def test_language_changes_the_reuse_key(self) -> None:
        self.assertNotEqual(ae.AsrConfig().config_hash,
                            ae.AsrConfig(language="en").config_hash)


class FilterArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ae.AsrConfig()

    def _filter(self, **kwargs: object) -> str:
        options = {"model_arg": "userdata/models/whisper/m.bin",
                   "destination_arg": "userdata/cache/asr/out.jsonl",
                   "config": self.config}
        options.update(kwargs)
        return ae.build_whisper_filter(**options)  # type: ignore[arg-type]

    def test_required_options_are_present(self) -> None:
        text = self._filter()
        for key in ("model=", "language=", "format=json", "queue=",
                    "destination="):
            self.assertIn(key, text)

    def test_nonexistent_options_are_not_used(self) -> None:
        """translate / temperature は whisper フィルターに存在しない。"""
        text = self._filter()
        self.assertNotIn("translate", text)
        self.assertNotIn("temperature", text)

    def test_vad_is_absent_by_default(self) -> None:
        self.assertNotIn("vad", self._filter())

    def test_gpu_is_disabled_explicitly_when_requested(self) -> None:
        text = self._filter(config=ae.AsrConfig(use_gpu=False))
        self.assertIn("use_gpu=0", text)

    def test_drive_letters_are_escaped(self) -> None:
        self.assertEqual(ae.escape_filter_value(r"D:\models\m.bin"),
                         r"D\:/models/m.bin")


class NonAsciiAppRootTests(TempAppRootTestCase):
    """**APP_ROOT が日本語でも whisper へ ASCII 相対で渡せること。**

    これが崩れると第三者環境で文字起こしが全滅する。
    """

    app_root_name = "動画カタログ 日本語"

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.config = ae.AsrConfig()
        model = ae.model_path(self.config)
        model.write_bytes(b"x" * (2 * 1024 * 1024))

    def test_app_root_really_is_non_ascii(self) -> None:
        with self.assertRaises(UnicodeEncodeError):
            str(paths.app_root()).encode("ascii")

    def test_model_check_passes(self) -> None:
        usable, reason = ae.check_model(self.config)
        self.assertTrue(usable, reason)

    def test_model_argument_is_ascii(self) -> None:
        relative = paths.to_relative_ascii(
            ae.model_path(self.config), paths.app_root())
        self.assertIsNotNone(relative)
        relative.encode("ascii")
        self.assertEqual(
            relative, f"userdata/models/whisper/{self.config.model_name}")

    def test_output_directory_is_ascii_relative(self) -> None:
        directory = ae.chunk_output_directory("asset123", self.config, "qfp1:abc")
        relative = paths.to_relative_ascii(directory, paths.app_root())
        self.assertIsNotNone(relative)
        relative.encode("ascii")

    def test_output_directory_is_cleanable(self) -> None:
        directory = ae.chunk_output_directory("asset123", self.config, "qfp1:abc")
        directory.mkdir(parents=True)
        self.assertTrue(paths.is_cleanable(directory))

    def test_source_namespaces_keep_versions_apart(self) -> None:
        """元ファイルが差し替わっても新旧が同じ場所を奪い合わない。"""
        first = ae.chunk_output_directory("a", self.config, "qfp1:aaa")
        second = ae.chunk_output_directory("a", self.config, "qfp1:bbb")
        self.assertNotEqual(first, second)

    def test_implementation_version_is_in_the_path(self) -> None:
        directory = ae.chunk_output_directory("a", self.config, "qfp1:aaa")
        self.assertIn(ASR_IMPL_VERSION, directory.parts)


class ModelCheckTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()

    def test_missing_model(self) -> None:
        usable, reason = ae.check_model(ae.AsrConfig())
        self.assertFalse(usable)
        self.assertIn("ありません", reason)

    def test_tiny_model_is_refused(self) -> None:
        config = ae.AsrConfig()
        ae.model_path(config).write_bytes(b"x")
        usable, reason = ae.check_model(config)
        self.assertFalse(usable)
        self.assertIn("小さすぎます", reason)

    def test_path_in_the_name_is_refused(self) -> None:
        usable, _ = ae.check_model(ae.AsrConfig(model_name="../evil.bin"))
        self.assertFalse(usable)

    def test_non_ascii_model_name_is_refused(self) -> None:
        """whisper は非 ASCII のパスを開けない。"""
        config = ae.AsrConfig(model_name="モデル.bin")
        ae.model_path(config).write_bytes(b"x" * (2 * 1024 * 1024))
        usable, reason = ae.check_model(config)
        self.assertFalse(usable)
        self.assertIn("ASCII", reason)


class CommandTests(TempAppRootTestCase):
    def test_source_is_only_an_input(self) -> None:
        source = self.make_source_dir() / "clip.mp4"
        chunk = ae.PlannedChunk(0, 0.0, 300.0, 0.0)
        command = ae.build_ffmpeg_command(
            Path("ffmpeg"), source, chunk,
            model_arg="userdata/models/whisper/m.bin",
            destination_arg="userdata/cache/asr/out.jsonl",
            config=ae.AsrConfig())
        self.assertEqual(command[command.index("-i") + 1], str(source))
        # 出力は null muxer。元動画が出力先として現れないこと。
        self.assertEqual(command[-3:], ["-f", "null", "-"])
        self.assertNotIn(str(source), command[command.index("-i") + 2:])

    def test_video_is_dropped(self) -> None:
        chunk = ae.PlannedChunk(0, 0.0, 300.0, 0.0)
        command = ae.build_ffmpeg_command(
            Path("ffmpeg"), Path("in.mp4"), chunk, model_arg="m",
            destination_arg="d", config=ae.AsrConfig())
        for flag in ("-vn", "-sn", "-dn"):
            self.assertIn(flag, command)

    def test_chunk_window_is_passed(self) -> None:
        chunk = ae.PlannedChunk(2, 600.0, 300.0, 1.0)
        command = ae.build_ffmpeg_command(
            Path("ffmpeg"), Path("in.mp4"), chunk, model_arg="m",
            destination_arg="d", config=ae.AsrConfig())
        self.assertEqual(command[command.index("-ss") + 1], "600.000")
        self.assertEqual(command[command.index("-t") + 1], "300.000")


class JsonLinesTests(TempAppRootTestCase):
    """出力は JSON Lines。``format=json`` でも配列にはならない。"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.target = paths.temp_dir() / "out.jsonl"

    def test_reads_line_delimited_objects(self) -> None:
        self.target.write_text(
            '{"start":0,"end":1000,"text":"a"}\n'
            '{"start":1000,"end":2000,"text":"b"}\n', encoding="utf-8")
        items = ae.read_json_lines(self.target)
        self.assertEqual([i["text"] for i in items], ["a", "b"])

    def test_broken_lines_are_skipped(self) -> None:
        self.target.write_text(
            '{"start":0,"end":1000,"text":"a"}\n'
            'not json\n'
            '{"start":1000,"end":2000,"text":"b"}\n', encoding="utf-8")
        self.assertEqual(len(ae.read_json_lines(self.target)), 2)

    def test_array_wrapping_is_tolerated(self) -> None:
        self.target.write_text(
            '[\n{"start":0,"end":1000,"text":"a"},\n'
            '{"start":1000,"end":2000,"text":"b"}\n]\n', encoding="utf-8")
        self.assertEqual(len(ae.read_json_lines(self.target)), 2)

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(ae.read_json_lines(paths.temp_dir() / "nope"), [])


if __name__ == "__main__":
    unittest.main()
