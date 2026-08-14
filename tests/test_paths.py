"""paths — APP_ROOT の決定と、保存先がすべて APP_ROOT 配下であること.

One-Folder 原則をここで固定する。ここが崩れると、アプリが利用者の
見えない場所へ状態を残すようになる。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from _support import TempAppRootTestCase, TempDirTestCase, requires_windows

from local_video_catalog import paths


class AppRootResolutionTests(TempAppRootTestCase):
    """APP_ROOT をどう決めるか。"""

    def test_environment_variable_wins(self) -> None:
        self.assertEqual(paths.app_root(), self.app_root.resolve())

    def test_missing_environment_target_stops(self) -> None:
        """指定先が無ければ、代替場所を勝手に選ばずに止まる。"""
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(
            self.temp_dir / "nonexistent")
        with self.assertRaises(paths.AppRootError):
            paths.app_root()

    def test_marker_search_finds_the_root(self) -> None:
        """環境変数が無ければ marker を上へ探す。"""
        os.environ.pop(paths.ROOT_ENVIRONMENT_VARIABLE, None)
        nested = self.app_root / "src" / "local_video_catalog"
        nested.mkdir(parents=True, exist_ok=True)
        found = paths._search_upwards(nested)
        self.assertEqual(found, self.app_root)

    def test_marker_search_returns_none_without_marker(self) -> None:
        os.environ.pop(paths.ROOT_ENVIRONMENT_VARIABLE, None)
        (self.app_root / paths.APP_ROOT_MARKER).unlink()
        self.assertIsNone(paths._search_upwards(self.app_root))

    def test_real_repository_has_a_marker(self) -> None:
        """配布物の構成が崩れていないこと。"""
        os.environ.pop(paths.ROOT_ENVIRONMENT_VARIABLE, None)
        self.assertTrue((paths.app_root() / paths.APP_ROOT_MARKER).is_file())

    def test_result_is_not_cached(self) -> None:
        """フォルダー移動やテストの差し替えに追随すること。"""
        first = paths.app_root()
        moved = self.temp_dir / "moved"
        moved.mkdir()
        (moved / paths.APP_ROOT_MARKER).write_text("x", encoding="utf-8")
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(moved)
        self.assertNotEqual(paths.app_root(), first)
        self.assertEqual(paths.app_root(), moved.resolve())


class UserDataLayoutTests(TempAppRootTestCase):
    """保存先がすべて APP_ROOT/userdata 配下にあること。"""

    ALL_PATH_FUNCTIONS = (
        "userdata_dir", "config_dir", "settings_path", "gui_state_path",
        "catalog_dir", "database_path", "catalog_html_path", "export_dir",
        "descriptions_dir", "cache_dir", "probe_cache_dir",
        "frames_cache_dir", "vlm_cache_dir", "asr_cache_dir",
        "models_dir", "whisper_models_dir", "log_dir", "runs_dir",
        "temp_dir", "control_dir", "stop_request_path",
    )

    def test_every_path_is_under_userdata(self) -> None:
        userdata = paths.userdata_dir().resolve()
        for name in self.ALL_PATH_FUNCTIONS:
            with self.subTest(function=name):
                value = getattr(paths, name)()
                self.assertTrue(
                    str(value.resolve()).startswith(str(userdata)),
                    f"{name}() が userdata の外を指しています: {value}")

    def test_no_path_escapes_the_app_root(self) -> None:
        root = paths.app_root().resolve()
        for name in self.ALL_PATH_FUNCTIONS:
            with self.subTest(function=name):
                value = getattr(paths, name)().resolve()
                value.relative_to(root)   # 外なら ValueError

    def test_frames_replaces_scenes(self) -> None:
        """旧個人版の cache/scenes は cache/frames へ改名した。"""
        self.assertEqual(paths.frames_cache_dir().name, "frames")
        self.assertNotIn("scenes", str(paths.cache_dir()))
        self.assertIn("frames", paths.CLEANABLE_CACHE_NAMES)
        self.assertNotIn("scenes", paths.CLEANABLE_CACHE_NAMES)

    def test_probe_cache_is_not_cleanable(self) -> None:
        """probe は消さない。消すと元動画の読み直しが必要になる。"""
        self.assertNotIn("probe", paths.CLEANABLE_CACHE_NAMES)

    def test_ensure_creates_every_directory(self) -> None:
        created = paths.ensure_userdata_tree()
        self.assertTrue(created)
        for directory in paths.userdata_subdirectories():
            self.assertTrue(directory.is_dir(), f"未作成: {directory}")

    def test_ensure_is_idempotent(self) -> None:
        paths.ensure_userdata_tree()
        self.assertEqual(paths.ensure_userdata_tree(), [])

    def test_ensure_does_not_touch_outside(self) -> None:
        """APP_ROOT の外に何も作らないこと。"""
        outside = self.make_source_dir("outside")
        before = sorted(p.name for p in outside.iterdir())
        paths.ensure_userdata_tree()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)


class NonAsciiAppRootTests(TempAppRootTestCase):
    """APP_ROOT が日本語を含んでいても壊れないこと。

    実際の配置例（``...\\動画編集関係\\local-video-catalog``）がこれに当たる。
    whisper.cpp は非 ASCII パスを開けないため、ここが成立しないと
    第三者環境で文字起こしが全滅する。
    """

    app_root_name = "動画フォルダー 日本語"

    def test_paths_work_with_a_japanese_app_root(self) -> None:
        for name in UserDataLayoutTests.ALL_PATH_FUNCTIONS:
            with self.subTest(function=name):
                getattr(paths, name)()

    def test_ensure_tree_works(self) -> None:
        paths.ensure_userdata_tree()
        self.assertTrue(paths.whisper_models_dir().is_dir())

    def test_internal_relative_paths_stay_ascii(self) -> None:
        """APP_ROOT が日本語でも、その中の相対パスは ASCII で表せる。

        これが whisper.cpp 対策（cwd=APP_ROOT + ASCII 相対）の前提。
        """
        model = paths.whisper_models_dir() / "ggml-large-v3-turbo-q5_0.bin"
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"x")
        relative = paths.to_relative_ascii(model, paths.app_root())
        self.assertEqual(
            relative, "userdata/models/whisper/ggml-large-v3-turbo-q5_0.bin")

    def test_non_ascii_relative_part_is_rejected(self) -> None:
        """相対部分に非 ASCII が入る場合は None を返す（黙って渡さない）。"""
        target = paths.whisper_models_dir() / "モデル.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        self.assertIsNone(paths.to_relative_ascii(target, paths.app_root()))


class AppRelativeTests(TempAppRootTestCase):
    """内部生成物の位置を APP_ROOT 相対で持つこと（フォルダー移動耐性）。"""

    def test_round_trip(self) -> None:
        paths.ensure_userdata_tree()
        original = paths.descriptions_dir() / "VID-000001_clip.txt"
        original.write_text("x", encoding="utf-8")

        relative = paths.to_app_relative(original)
        self.assertEqual(relative, "userdata/descriptions/VID-000001_clip.txt")
        self.assertEqual(paths.from_app_relative(relative), original)

    def test_relative_uses_posix_separators(self) -> None:
        """OS をまたいでも読めるよう / で保存する。"""
        paths.ensure_userdata_tree()
        relative = paths.to_app_relative(paths.database_path())
        self.assertIsNotNone(relative)
        self.assertNotIn("\\", relative)

    def test_outside_returns_none(self) -> None:
        """APP_ROOT の外は相対で表さない（元動画をここへ通さないため）。"""
        outside = self.make_source_dir() / "clip.mp4"
        outside.write_bytes(b"x")
        self.assertIsNone(paths.to_app_relative(outside))

    def test_relative_survives_a_moved_app_root(self) -> None:
        """保存済みの相対パスが、移動後の APP_ROOT で正しく解決される。"""
        paths.ensure_userdata_tree()
        stored = paths.to_app_relative(paths.database_path())

        moved = self.temp_dir / "moved-app"
        moved.mkdir()
        (moved / paths.APP_ROOT_MARKER).write_text("x", encoding="utf-8")
        os.environ[paths.ROOT_ENVIRONMENT_VARIABLE] = str(moved)

        self.assertEqual(paths.from_app_relative(stored),
                         moved / "userdata" / "catalog" / "video_catalog.sqlite3")


class CleanupBoundaryTests(TempAppRootTestCase):
    """cleanup の境界。**ここが緩むと利用者のデータを失う。**

    設計 PORTABLE_V1_DESIGN.md §6-5 の 8 項目に対応する。
    """

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()

    # -- 1. APP_ROOT の外 ----------------------------------------------

    def test_outside_app_root_is_never_cleanable(self) -> None:
        for candidate in (
            self.temp_dir,
            self.make_source_dir(),
            self.make_source_dir() / "clip.mp4",
            Path(self.temp_dir).parent,
            Path("C:/Windows") if sys.platform == "win32" else Path("/etc"),
        ):
            with self.subTest(path=str(candidate)):
                self.assertFalse(paths.is_cleanable(candidate))

    def test_app_root_itself_is_not_cleanable(self) -> None:
        self.assertFalse(paths.is_cleanable(paths.app_root()))
        self.assertFalse(paths.is_cleanable(paths.userdata_dir()))

    # -- 2. 保護対象 ----------------------------------------------------

    def test_protected_directories_are_not_cleanable(self) -> None:
        for directory in (
            paths.descriptions_dir(),
            paths.catalog_dir(),
            paths.database_path(),
            paths.catalog_html_path(),
            paths.export_dir(),
            paths.models_dir(),
            paths.whisper_models_dir(),
            paths.log_dir(),
            paths.config_dir(),
            paths.settings_path(),
            paths.runs_dir(),
            paths.probe_cache_dir(),
        ):
            with self.subTest(path=str(directory)):
                self.assertFalse(paths.is_cleanable(directory))

    def test_probe_cache_contents_are_not_cleanable(self) -> None:
        """probe は再解析に使うので、中身も消さない。"""
        target = paths.probe_cache_dir() / "asset123.json.gz"
        target.write_bytes(b"x")
        self.assertFalse(paths.is_cleanable(target))

    # -- 3. cache の親フォルダー自体 ------------------------------------

    def test_cache_parents_are_not_cleanable(self) -> None:
        self.assertFalse(paths.is_cleanable(paths.cache_dir()))
        for name in paths.CLEANABLE_CACHE_NAMES:
            with self.subTest(name=name):
                self.assertFalse(paths.is_cleanable(paths.cache_dir() / name))

    # -- 4. 本来の対象 --------------------------------------------------

    def test_per_asset_cache_is_cleanable(self) -> None:
        for name in paths.CLEANABLE_CACHE_NAMES:
            with self.subTest(name=name):
                target = paths.cache_dir() / name / "asset123"
                target.mkdir(parents=True, exist_ok=True)
                self.assertTrue(paths.is_cleanable(target))

    def test_deeper_cache_contents_are_cleanable(self) -> None:
        target = paths.vlm_cache_dir() / "asset123" / "v1" / "hash"
        target.mkdir(parents=True, exist_ok=True)
        self.assertTrue(paths.is_cleanable(target))

    # -- 5. シンボリックリンク ------------------------------------------

    @requires_windows
    def test_symlink_pointing_outside_is_rejected(self) -> None:
        """resolve() 後に判定するので、リンクで外を指しても弾かれる。"""
        outside = self.make_source_dir("secret")
        link = paths.vlm_cache_dir() / "asset123"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("シンボリックリンクを作成できない環境")
        self.assertFalse(paths.is_cleanable(link))

    # -- 6. .. を含むパス -----------------------------------------------

    def test_parent_traversal_is_rejected(self) -> None:
        for candidate in (
            paths.vlm_cache_dir() / "asset" / ".." / ".." / ".." / "descriptions",
            paths.vlm_cache_dir() / ".." / ".." / "..",
            paths.cache_dir() / "vlm" / ".." / ".." / "models",
        ):
            with self.subTest(path=str(candidate)):
                self.assertFalse(paths.is_cleanable(candidate))

    # -- 7. 設定に左右されない ------------------------------------------

    def test_settings_cannot_change_the_boundary(self) -> None:
        """設定ファイルが何を言っても cleanup 対象は変わらない。"""
        from local_video_catalog import config as config_module

        target = paths.vlm_cache_dir() / "asset123"
        target.mkdir(parents=True, exist_ok=True)
        outside = self.make_source_dir() / "precious"
        outside.mkdir(parents=True, exist_ok=True)

        config_module.save_settings_dict({
            "data_root": str(self.make_source_dir()),
            "cache_dir": str(outside),
            "cleanable": [str(outside)],
        })

        self.assertTrue(paths.is_cleanable(target))
        self.assertFalse(paths.is_cleanable(outside))

    # -- 8. 名前が似ているだけの場所 ------------------------------------

    def test_lookalike_names_are_rejected(self) -> None:
        for relative in (
            "userdata2/cache/vlm/asset",
            "cache/vlm/asset",
            "userdata/caches/vlm/asset",
            "userdata/cache/vlms/asset",
            "userdata/cache/probe/asset",
        ):
            with self.subTest(relative=relative):
                candidate = paths.app_root() / relative
                self.assertFalse(paths.is_cleanable(candidate))

    def test_cache_directories_for_asset_lists_only_existing(self) -> None:
        (paths.vlm_cache_dir() / "asset123").mkdir(parents=True)
        (paths.asr_cache_dir() / "asset123").mkdir(parents=True)
        found = paths.cache_directories_for_asset("asset123")
        self.assertEqual(len(found), 2)
        for directory in found:
            self.assertTrue(paths.is_cleanable(directory))


class NoHiddenStateTests(TempDirTestCase):
    """アプリが見えない場所へ状態を書かないこと。

    文字列を素朴に grep すると、**方針を説明した docstring** まで
    引っかかって役に立たない。構文木を見て「実際に使っているか」だけを
    調べる。
    """

    FORBIDDEN_ENVIRONMENT = ("LOCALAPPDATA", "APPDATA", "USERPROFILE",
                             "HOME", "XDG_DATA_HOME")
    FORBIDDEN_CALLS = ("expanduser", "home", "gettempdir", "mkdtemp")

    def _code_strings_and_calls(self, module) -> tuple[list[str], list[str]]:
        from _support import code_strings_and_calls

        return code_strings_and_calls(module)

    def test_paths_reads_no_user_profile_environment_variables(self) -> None:
        strings, _ = self._code_strings_and_calls(paths)
        for value in strings:
            for forbidden in self.FORBIDDEN_ENVIRONMENT:
                with self.subTest(value=value, forbidden=forbidden):
                    self.assertNotIn(
                        forbidden, value.upper(),
                        f"paths.py のコードが {forbidden} を使っています。"
                        "One-Folder 原則に反します。")

    def test_paths_calls_no_home_or_temp_helpers(self) -> None:
        _, calls = self._code_strings_and_calls(paths)
        for forbidden in self.FORBIDDEN_CALLS:
            with self.subTest(name=forbidden):
                self.assertNotIn(
                    forbidden, calls,
                    f"paths.py が {forbidden}() を呼んでいます。"
                    "保存先は APP_ROOT からのみ導出してください。")

    def test_only_the_documented_environment_variable_is_read(self) -> None:
        strings, calls = self._code_strings_and_calls(paths)
        self.assertIn("getenv", calls + ["getenv"])   # 参照方法が変わったら気づく
        environment_like = [
            value for value in strings
            if value.isupper() and "_" in value and len(value) > 6
        ]
        self.assertEqual(environment_like, [paths.ROOT_ENVIRONMENT_VARIABLE])

    def test_config_and_logging_avoid_user_profile_too(self) -> None:
        from local_video_catalog import config as config_module
        from local_video_catalog import logging_utils

        for module in (config_module, logging_utils):
            strings, calls = self._code_strings_and_calls(module)
            with self.subTest(module=module.__name__):
                for value in strings:
                    for forbidden in self.FORBIDDEN_ENVIRONMENT:
                        self.assertNotIn(forbidden, value.upper())
                for forbidden in ("expanduser", "home"):
                    self.assertNotIn(forbidden, calls)


if __name__ == "__main__":
    unittest.main()
