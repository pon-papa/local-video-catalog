"""privacy_guard 自身のテスト.

**検査する側が壊れていないこと**を確かめる。
既知の「入れてはいけないもの」を検出できること、
正当なコード・文書を誤検出しないことの両方を固定する。

実データは一切使わない。すべて一時フォルダー内の合成値。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_APP_ROOT = _TESTS_DIR.parent
_TOOLS = _APP_ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import privacy_guard  # noqa: E402


class CheckPathTests(unittest.TestCase):
    """パスに対する検査。"""

    def assert_blocked(self, path: str) -> None:
        self.assertIsNotNone(
            privacy_guard.check_path(path),
            f"検出されるべきパスが素通りしました: {path}")

    def assert_allowed(self, path: str) -> None:
        violation = privacy_guard.check_path(path)
        self.assertIsNone(
            violation,
            f"正当なパスが誤検出されました: {path} "
            f"({violation.reason if violation else ''})")

    # -- 遮断されるべきもの --------------------------------------------

    def test_userdata_is_blocked(self) -> None:
        for path in (
            "userdata/catalog/video_catalog.sqlite3",
            "userdata/descriptions/VID-000001_clip.txt",
            "userdata/config/settings.json",
            "userdata/logs/run_abc.log",
            "userdata/cache/asr/x/y/transcript_full.json",
        ):
            with self.subTest(path=path):
                self.assert_blocked(path)

    def test_media_is_blocked(self) -> None:
        for path in ("a.mp4", "docs/b.MOV", "x/y/c.m2ts", "d.wav", "e.jpg",
                     "f.png", "g.webp"):
            with self.subTest(path=path):
                self.assert_blocked(path)

    def test_database_and_models_are_blocked(self) -> None:
        for path in ("catalog.sqlite3", "x.db", "models/whisper/ggml.bin",
                     "m.gguf", "s.safetensors"):
            with self.subTest(path=path):
                self.assert_blocked(path)

    def test_analysis_outputs_are_blocked(self) -> None:
        for path in (
            "descriptions/VID-000001_clip.txt",
            "VID-000123_something.txt",
            "catalog.html",
            "catalog.html.tmp",
            "cache/vlm/asset/frame_0001.analysis.json",
            "cache/frames/asset/manifest.json",
            "asset_visual_summary.json",
            "transcript_full.json",
            "transcript.txt",
            "segments_full.jsonl",
            "chunk_000012.json",
            "raw_engine_output.txt",
            "run_manifest_full.json",
        ):
            with self.subTest(path=path):
                self.assert_blocked(path)

    def test_source_namespace_is_blocked(self) -> None:
        self.assert_blocked("cache/asr/a/b/src_" + "0" * 64 + "/x.json")
        self.assert_blocked("src_deadbeefcafe/anything")

    def test_user_settings_are_blocked(self) -> None:
        for path in ("settings.json", "config/settings.json",
                     "config/settings.local.json", "gui-state.json",
                     ".env", ".env.production"):
            with self.subTest(path=path):
                self.assert_blocked(path)

    # -- 通されるべきもの ----------------------------------------------

    def test_source_code_is_allowed(self) -> None:
        for path in (
            "src/local_video_catalog/paths.py",
            "src/local_video_catalog/vlm_client.py",
            "src/local_video_catalog/gui/app.py",
            "tools/privacy_guard.py",
            "tests/test_privacy_guard.py",
            "config/settings.example.json",
            "docs/PORTABLE_V1_DESIGN.md",
            "README.md",
            ".gitignore",
            "userdata/.gitignore",
            "Start.cmd",
            ".github/workflows/ci.yml",
            "app-root.marker",
        ):
            with self.subTest(path=path):
                self.assert_allowed(path)

    def test_userdata_gitignore_itself_is_allowed(self) -> None:
        """userdata/.gitignore だけは追跡してよい（フォルダー構造を残すため）。"""
        self.assert_allowed("userdata/.gitignore")

    def test_allowlist_is_exact_match_only(self) -> None:
        """許可リストが前方一致に緩まないこと。

        "userdata/.gitignore" の許可が "userdata/.gitignore.bak" や
        "userdata/config/..." まで通してしまうと防御が崩れる。
        """
        for path in (
            "userdata/.gitignore.bak",
            "userdata/.gitignore/x.txt",
            "other/userdata/.gitignore",
        ):
            with self.subTest(path=path):
                self.assert_blocked(path)


class CheckContentTests(unittest.TestCase):
    """追跡ファイルの中身に対する検査。"""

    def test_user_profile_path_is_detected(self) -> None:
        pattern = privacy_guard.USER_PROFILE_PATTERN
        for line in (
            r'ffmpeg = "C:\Users\Taro\AppData\Local\ffmpeg.exe"',
            r'path = "C:/Users/hanako/Documents/videos/"',
            r'D:\Users\someone\data\x',
        ):
            with self.subTest(line=line):
                self.assertIsNotNone(pattern.search(line))

    def test_placeholders_are_not_detected(self) -> None:
        pattern = privacy_guard.USER_PROFILE_PATTERN
        for line in (
            r'"ffmpeg_path": "<FFMPEG_PATH>"',
            r'例: C:\Users\<ユーザー名>\... は書かない',
            r'path = "C:/Users/{user}/videos"',
        ):
            with self.subTest(line=line):
                self.assertIsNone(
                    pattern.search(line),
                    f"プレースホルダを誤検出しました: {line}")

    def test_non_ascii_drive_path_is_detected(self) -> None:
        pattern = privacy_guard.NON_ASCII_DRIVE_PATH_PATTERN
        for line in (
            r'data_root = "D:\動画データ"',
            r'source = "F:\ホームビデオ\2009"',
        ):
            with self.subTest(line=line):
                self.assertIsNotNone(pattern.search(line))

    def test_ascii_drive_path_is_allowed(self) -> None:
        """ASCII の例示パスは設計文書で使う。誤検出しないこと。"""
        pattern = privacy_guard.NON_ASCII_DRIVE_PATH_PATTERN
        for line in (
            r'例: D:\Tools\local-video-catalog へ移動しても起動する',
            r'X:\videos\20090815_trip.m4v',
        ):
            with self.subTest(line=line):
                self.assertIsNone(pattern.search(line))

    def test_guard_and_its_tests_are_skipped(self) -> None:
        """規則そのものと、その試験値を持つファイルは中身検査から外れている。"""
        self.assertIn("tools/privacy_guard.py", privacy_guard.CONTENT_SCAN_SKIP)
        self.assertIn("tests/test_privacy_guard.py",
                      privacy_guard.CONTENT_SCAN_SKIP)

    def test_skip_list_stays_small(self) -> None:
        """除外リストが膨らんで防御が形骸化していないこと。"""
        self.assertLessEqual(
            len(privacy_guard.CONTENT_SCAN_SKIP), 6,
            "内容検査の除外リストが増えています。本当に必要か確認してください。")
        self.assertLessEqual(
            len(privacy_guard.CONTENT_ALLOWLIST), 3,
            "内容検査の許可リストが増えています。本当に必要か確認してください。")


class RepositoryTests(unittest.TestCase):
    """このリポジトリ自体が clean であること。"""

    def test_this_repository_passes(self) -> None:
        result = privacy_guard.run_guard(_APP_ROOT)
        if result.error:
            self.skipTest(f"git を実行できないためスキップ: {result.error}")
        details = "\n".join(
            f"  {v.path}: {v.reason} {v.detail}" for v in result.violations)
        self.assertEqual(
            result.violations, [],
            f"個人データの混入を検出しました:\n{details}")

    def test_app_root_marker_exists(self) -> None:
        self.assertTrue((_APP_ROOT / "app-root.marker").is_file())

    def test_userdata_has_its_own_gitignore(self) -> None:
        """二重防御の内側が存在すること。"""
        inner = _APP_ROOT / "userdata" / ".gitignore"
        self.assertTrue(inner.is_file())
        text = inner.read_text(encoding="utf-8")
        self.assertIn("*", text)
        self.assertIn("!.gitignore", text)

    def test_root_gitignore_blocks_userdata_contents(self) -> None:
        """/userdata/ ではなく /userdata/* で書かれていること。

        ディレクトリ自体を除外すると git が中へ降りず、
        !/userdata/.gitignore の再包含が効かなくなる。
        """
        text = (_APP_ROOT / ".gitignore").read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines()]
        self.assertIn("/userdata/*", lines)
        self.assertIn("!/userdata/.gitignore", lines)
        self.assertNotIn("/userdata/", lines)


class StandardLibraryOnlyTests(unittest.TestCase):
    """サードパーティ依存がゼロであること。"""

    def test_no_requirements_file(self) -> None:
        for name in ("requirements.txt", "Pipfile", "poetry.lock",
                     "pyproject.toml"):
            with self.subTest(name=name):
                self.assertFalse(
                    (_APP_ROOT / name).exists(),
                    f"{name} があります。標準ライブラリのみの方針を確認してください。")


if __name__ == "__main__":
    unittest.main()
