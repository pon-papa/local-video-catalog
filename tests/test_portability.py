"""One-Folder の受け入れ条件 — 移動耐性と残骸ゼロ.

設計 PORTABLE_V1_DESIGN.md §12 の 13〜14 番と、§12-1 の安全性条件に対応する。
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

from _support import TempAppRootTestCase, code_strings_and_calls

from local_video_catalog import config as config_module
from local_video_catalog import database as db_module
from local_video_catalog import description_builder as builder
from local_video_catalog import html_catalog, paths
from local_video_catalog.source_ref import SourceRef


class FolderMoveTests(TempAppRootTestCase):
    """**フォルダーごと移動しても、続きから使えること。**

    移動して再解析が起きると、何時間もかけた成果が無駄になる。
    """

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.source_root = self.make_source_dir()
        self.original_root = self.app_root

    def _populate(self) -> tuple[str, Path]:
        """1 本ぶんの成果物を作る。"""
        with db_module.CatalogDatabase() as database:
            asset_id = database.new_asset_id()
            database.insert_asset(
                asset_id=asset_id, catalog_id=database.next_catalog_id(),
                source=SourceRef(root=self.source_root, relative="clip.mp4"),
                file_size=1, creation_time_fs=None, last_write_time_fs=None,
                file_fingerprint="fp1", quick_fingerprint="qfp1",
                full_sha256=None, now="t",
                registration_status=db_module.REG_NEW)
            for stage, _label in db_module.PIPELINE_STAGES:
                database.set_stage_status(asset_id, stage,
                                          db_module.STATUS_COMPLETED)

            description = paths.descriptions_dir() / "VID-000001_clip.txt"
            description.write_text("ファイル名：clip.mp4\n", encoding="utf-8")
            database.upsert_description({
                "asset_id": asset_id, "catalog_id": "VID-000001",
                "source_root": str(self.source_root),
                "source_relative": "clip.mp4", "file_name": "clip.mp4",
                "description_file_path": description,
                "description_status": db_module.STATUS_COMPLETED,
                "created_at": "t"})

            frame = paths.frames_cache_dir() / asset_id / "frame_0001.jpg"
            frame.parent.mkdir(parents=True, exist_ok=True)
            frame.write_bytes(b"x")
            database.start_extraction_run({
                "extraction_run_id": "run1", "asset_id": asset_id,
                "started_at": "t", "status": db_module.STATUS_COMPLETED,
                "implementation_version": "v1", "config_hash": "h",
                "config_json": "{}", "planned_frame_count": 1,
                "output_directory": frame.parent})
            database.upsert_frame({
                "extraction_run_id": "run1", "asset_id": asset_id,
                "implementation_version": "v1", "config_hash": "h",
                "source_quick_fingerprint": "qfp1", "sequence_index": 0,
                "target_time_seconds": 1.0, "target_time_milliseconds": 1000,
                "file_path": frame,
                "extraction_status": db_module.STATUS_OK, "created_at": "t"})
        return (asset_id, description)

    def _move_app(self, name: str = "moved-app") -> Path:
        destination = self.temp_dir / name
        shutil.copytree(self.original_root, destination)
        shutil.rmtree(self.original_root)
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(destination)
        return destination

    def test_stage_status_survives_a_move(self) -> None:
        """**移動後に再解析が発生しないこと。**"""
        asset_id, _ = self._populate()
        self._move_app()
        with db_module.CatalogDatabase() as database:
            for stage, _label in db_module.PIPELINE_STAGES:
                with self.subTest(stage=stage):
                    self.assertTrue(database.is_stage_done(asset_id, stage))

    def test_internal_paths_follow_the_move(self) -> None:
        asset_id, _ = self._populate()
        moved = self._move_app()
        with db_module.CatalogDatabase() as database:
            frames = database.get_frames_by_extraction_set(
                asset_id=asset_id, implementation_version="v1",
                config_hash="h", source_quick_fingerprint="qfp1")
            resolved = db_module.load_internal_path(frames[0]["file_path"])
            self.assertTrue(str(resolved).startswith(str(moved)))
            self.assertTrue(resolved.is_file())

            row = database.get_description(asset_id)
            description = db_module.load_internal_path(
                row["description_file_path"])
            self.assertTrue(description.is_file())

    def test_source_videos_do_not_follow_the_move(self) -> None:
        """**元動画は外部入力。** アプリを移しても元の場所にあり続ける。"""
        asset_id, _ = self._populate()
        self._move_app()
        with db_module.CatalogDatabase() as database:
            row = database.find_assets_by_identifier(asset_id)[0]
            self.assertEqual(SourceRef.from_row(row).absolute,
                             self.source_root / "clip.mp4")

    def test_move_to_a_japanese_path_works(self) -> None:
        asset_id, _ = self._populate()
        self._move_app("移動先フォルダー")
        with db_module.CatalogDatabase() as database:
            self.assertTrue(database.is_stage_done(
                asset_id, db_module.STAGE_DESCRIPTION))

    def test_catalog_can_be_rebuilt_after_a_move(self) -> None:
        material = builder.DescriptionMaterial(
            catalog_id="VID-000001", file_name="clip.mp4",
            source_path="X:/videos/clip.mp4", duration_seconds=95.0)
        (paths.descriptions_dir() / "VID-000001_clip.txt").write_text(
            builder.build_description_text(
                material, content="内容", youtube="概要",
                generator="local-llm"), encoding="utf-8")
        self._move_app()
        target = html_catalog.write_catalog(html_catalog.collect_records())
        self.assertTrue(target.is_file())
        self.assertTrue(str(target).startswith(str(paths.userdata_dir())))


class NoHiddenResidueTests(TempAppRootTestCase):
    """**フォルダーを消せば、アプリの生成物は片付く。**"""

    MODULES_THAT_MUST_STAY_INSIDE = (
        "paths", "config", "database", "recycle", "register", "pipeline",
        "stage_report", "html_catalog", "description_builder",
        "frame_extractor", "asr_engine", "logging_utils",
    )

    def test_no_module_reads_a_user_profile_variable(self) -> None:
        import importlib

        for name in self.MODULES_THAT_MUST_STAY_INSIDE:
            module = importlib.import_module(f"local_video_catalog.{name}")
            strings, calls = code_strings_and_calls(module)
            with self.subTest(module=name):
                for text in strings:
                    upper = text.upper()
                    for forbidden in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
                        self.assertNotIn(
                            forbidden, upper,
                            f"{name} が {forbidden} を使っています。")
                for forbidden in ("expanduser",):
                    self.assertNotIn(forbidden, calls,
                                     f"{name} が {forbidden}() を呼んでいます。")

    def test_gui_state_module_stays_inside_too(self) -> None:
        from local_video_catalog.gui import state as gui_state

        strings, calls = code_strings_and_calls(gui_state)
        for text in strings:
            self.assertNotIn("APPDATA", text.upper())
        self.assertNotIn("expanduser", calls)

    def test_full_run_writes_only_inside_userdata(self) -> None:
        """一通り動かしたあと、userdata の外に生成物が無いこと。"""
        paths.ensure_userdata_tree()
        config_module.verify_userdata()

        with db_module.CatalogDatabase() as database:
            asset_id = database.new_asset_id()
            database.insert_asset(
                asset_id=asset_id, catalog_id=database.next_catalog_id(),
                source=SourceRef(root=self.make_source_dir(),
                                 relative="clip.mp4"),
                file_size=1, creation_time_fs=None, last_write_time_fs=None,
                file_fingerprint=None, quick_fingerprint=None,
                full_sha256=None, now="t",
                registration_status=db_module.REG_NEW)
        html_catalog.write_catalog(html_catalog.collect_records())

        produced = [p for p in paths.app_root().rglob("*") if p.is_file()]
        outside = [p for p in produced
                   if not str(p).startswith(str(paths.userdata_dir()))
                   and p.name != paths.APP_ROOT_MARKER]
        self.assertEqual(outside, [], f"userdata の外に生成物: {outside}")

    def test_deleting_userdata_removes_every_generated_file(self) -> None:
        paths.ensure_userdata_tree()
        with db_module.CatalogDatabase() as database:
            database.set_meta("x", "y")
        shutil.rmtree(paths.userdata_dir())
        remaining = [p for p in paths.app_root().rglob("*") if p.is_file()]
        self.assertEqual([p.name for p in remaining], [paths.APP_ROOT_MARKER])


class ReleasePackagingTests(TempAppRootTestCase):
    """配布物に開発用ファイルと実データが入らないこと。"""

    def test_packager_excludes_development_files(self) -> None:
        from _support import APP_ROOT
        import sys

        tools = APP_ROOT / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import make_release

        for name in (".git", ".github", "tests", "userdata"):
            with self.subTest(name=name):
                self.assertTrue(make_release.is_excluded(Path(name)),
                                f"{name} が配布物へ入ります。")

    def test_packager_keeps_what_the_app_needs(self) -> None:
        from _support import APP_ROOT
        import sys

        tools = APP_ROOT / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import make_release

        for name in ("Start.cmd", "app-root.marker", "README.md", "LICENSE",
                     Path("src") / "local_video_catalog" / "paths.py",
                     Path("config") / "settings.example.json"):
            with self.subTest(name=str(name)):
                self.assertFalse(make_release.is_excluded(Path(name)),
                                 f"{name} が配布物から抜けます。")


if __name__ == "__main__":
    unittest.main()
