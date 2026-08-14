"""source_ref — 元動画（外部入力）の位置表現.

**内部生成物の APP_ROOT 相対パスと取り違えないこと**をここで固定する。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import paths
from local_video_catalog.source_ref import SourceRef, SourceRefError, is_inside


class ConstructionTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_root = self.make_source_dir()

    def test_from_absolute(self) -> None:
        clip = self.source_root / "2009" / "clip.mp4"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"x")

        ref = SourceRef.from_absolute(clip, self.source_root)
        self.assertEqual(ref.relative, "2009/clip.mp4")
        self.assertEqual(ref.absolute, clip)
        self.assertEqual(ref.file_name, "clip.mp4")
        self.assertEqual(ref.extension, ".mp4")

    def test_outside_the_source_root_is_refused(self) -> None:
        """指定されていないフォルダーの動画を黙って取り込まない。"""
        other = self.make_source_dir("other")
        clip = other / "clip.mp4"
        clip.write_bytes(b"x")
        with self.assertRaises(SourceRefError):
            SourceRef.from_absolute(clip, self.source_root)

    def test_absolute_relative_is_refused(self) -> None:
        with self.assertRaises(SourceRefError):
            SourceRef(root=self.source_root, relative=str(self.source_root))

    def test_parent_traversal_is_refused(self) -> None:
        with self.assertRaises(SourceRefError):
            SourceRef(root=self.source_root, relative="../escape.mp4")

    def test_empty_relative_is_refused(self) -> None:
        with self.assertRaises(SourceRefError):
            SourceRef(root=self.source_root, relative="   ")

    def test_parent_names_stay_inside_the_source_root(self) -> None:
        """利用者の領域の外側のフォルダー名を持ち出さない。"""
        ref = SourceRef(root=self.source_root, relative="2009/summer/clip.mp4")
        self.assertEqual(ref.parent_names(), ["summer", "2009"])
        self.assertNotIn(self.source_root.name, ref.parent_names())

    def test_extension_is_lowercased(self) -> None:
        ref = SourceRef(root=self.source_root, relative="a/B.M2TS")
        self.assertEqual(ref.extension, ".m2ts")


class RowRoundTripTests(TempAppRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_root = self.make_source_dir()

    def test_round_trip(self) -> None:
        ref = SourceRef(root=self.source_root, relative="2009/clip.mp4")
        restored = SourceRef.from_row(ref.to_row())
        self.assertEqual(restored, ref)

    def test_row_keeps_the_root_absolute(self) -> None:
        """元動画は外部入力。APP_ROOT 相対にしない。"""
        ref = SourceRef(root=self.source_root, relative="clip.mp4")
        row = ref.to_row()
        self.assertTrue(Path(row["source_root"]).is_absolute())
        self.assertFalse(Path(row["source_relative"]).is_absolute())

    def test_missing_columns_raise(self) -> None:
        with self.assertRaises(SourceRefError):
            SourceRef.from_row({"source_root": str(self.source_root)})

    def test_empty_columns_raise(self) -> None:
        with self.assertRaises(SourceRefError):
            SourceRef.from_row({"source_root": "", "source_relative": "a.mp4"})


class SeparationFromInternalPathsTests(TempAppRootTestCase):
    """外部入力と内部生成物を混同しないこと。"""

    def test_source_videos_are_never_app_relative(self) -> None:
        source_root = self.make_source_dir()
        clip = source_root / "clip.mp4"
        clip.write_bytes(b"x")
        self.assertIsNone(paths.to_app_relative(clip))

    def test_app_root_is_not_a_valid_source_root_for_generated_files(self) -> None:
        """内部生成物を SourceRef で表そうとしても外部扱いにならない。"""
        paths.ensure_userdata_tree()
        generated = paths.descriptions_dir() / "VID-000001.txt"
        generated.write_text("x", encoding="utf-8")
        source_root = self.make_source_dir()
        with self.assertRaises(SourceRefError):
            SourceRef.from_absolute(generated, source_root)

    def test_moving_the_app_root_does_not_move_source_videos(self) -> None:
        """アプリを移動しても元動画は元の場所にあり続ける。"""
        import os

        source_root = self.make_source_dir()
        clip = source_root / "clip.mp4"
        clip.write_bytes(b"x")
        ref = SourceRef.from_absolute(clip, source_root)

        moved = self.temp_dir / "moved-app"
        moved.mkdir()
        (moved / paths.APP_ROOT_MARKER).write_text("x", encoding="utf-8")
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(moved)

        restored = SourceRef.from_row(ref.to_row())
        self.assertEqual(restored.absolute, clip)
        self.assertTrue(restored.absolute.is_file())


class IsInsideTests(TempAppRootTestCase):
    def test_inside_and_outside(self) -> None:
        root = self.make_source_dir()
        inside = root / "a" / "b.mp4"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(b"x")
        self.assertTrue(is_inside(inside, root))
        self.assertFalse(is_inside(self.make_source_dir("other"), root))

    def test_traversal_is_rejected(self) -> None:
        root = self.make_source_dir()
        self.assertFalse(is_inside(root / ".." / "escape", root))


if __name__ == "__main__":
    unittest.main()
