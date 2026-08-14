"""recycle — ゴミ箱送りと、完全削除へ落ちないこと.

境界そのものの検証は test_paths.CleanupBoundaryTests にある。
ここでは「境界を通った後の振る舞い」を見る。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from _support import TempAppRootTestCase

from local_video_catalog import paths, recycle


class CleanupPlanTests(TempAppRootTestCase):
    """dry_run で、何を対象にするかだけを確かめる（何も消さない）。"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.asset_id = "asset123"

    def _make_cache(self, name: str, size: int = 100) -> Path:
        directory = paths.cache_dir() / name / self.asset_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "data.bin").write_bytes(b"x" * size)
        return directory

    def test_nothing_to_clean(self) -> None:
        result = recycle.cleanup_intermediate_cache(self.asset_id, dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.moved_paths, [])
        self.assertEqual(result.status, recycle.CLEANUP_NOTHING)

    def test_every_cleanable_cache_is_planned(self) -> None:
        expected = {self._make_cache(name)
                    for name in paths.CLEANABLE_CACHE_NAMES}
        result = recycle.cleanup_intermediate_cache(self.asset_id, dry_run=True)
        self.assertEqual(set(result.moved_paths), expected)

    def test_freed_bytes_are_measured(self) -> None:
        self._make_cache("vlm", size=500)
        result = recycle.cleanup_intermediate_cache(self.asset_id, dry_run=True)
        self.assertEqual(result.freed_bytes, 500)

    def test_dry_run_changes_nothing(self) -> None:
        directory = self._make_cache("vlm")
        recycle.cleanup_intermediate_cache(self.asset_id, dry_run=True)
        self.assertTrue(directory.is_dir())
        self.assertTrue((directory / "data.bin").is_file())

    def test_probe_cache_is_never_planned(self) -> None:
        """probe は再解析に使うので消さない。"""
        probe = paths.probe_cache_dir() / self.asset_id
        probe.mkdir(parents=True)
        (probe / "x.json.gz").write_bytes(b"x")
        result = recycle.cleanup_intermediate_cache(self.asset_id, dry_run=True)
        self.assertEqual(result.moved_paths, [])
        self.assertTrue(probe.is_dir())

    def test_protected_outputs_are_never_planned(self) -> None:
        for directory in (paths.descriptions_dir(), paths.catalog_dir(),
                          paths.whisper_models_dir(), paths.log_dir(),
                          paths.config_dir()):
            (directory / self.asset_id).mkdir(parents=True, exist_ok=True)
        self._make_cache("vlm")

        result = recycle.cleanup_intermediate_cache(self.asset_id, dry_run=True)
        for planned in result.moved_paths:
            self.assertTrue(paths.is_cleanable(planned))
        self.assertEqual(len(result.moved_paths), 1)

    def test_every_planned_path_passes_the_boundary(self) -> None:
        for name in paths.CLEANABLE_CACHE_NAMES:
            self._make_cache(name)
        result = recycle.cleanup_intermediate_cache(self.asset_id, dry_run=True)
        self.assertTrue(result.moved_paths)
        for planned in result.moved_paths:
            self.assertTrue(paths.is_cleanable(planned), planned)


class NoDeleteFallbackTests(TempAppRootTestCase):
    """**ゴミ箱へ送れなかったらファイルを残す。削除へ落ちない。**"""

    def setUp(self) -> None:
        super().setUp()
        paths.ensure_userdata_tree()
        self.asset_id = "asset123"
        self.directory = paths.vlm_cache_dir() / self.asset_id
        self.directory.mkdir(parents=True)
        (self.directory / "data.bin").write_bytes(b"x" * 10)

    def test_failure_keeps_the_files(self) -> None:
        def always_fails(_targets: list[Path]) -> None:
            raise recycle.RecycleError("テスト用の失敗")

        original = recycle.send_to_recycle_bin
        recycle.send_to_recycle_bin = always_fails
        self.addCleanup(setattr, recycle, "send_to_recycle_bin", original)

        result = recycle.cleanup_intermediate_cache(self.asset_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, recycle.CLEANUP_FAILED)
        self.assertEqual(result.freed_bytes, 0)
        self.assertTrue(self.directory.is_dir(),
                        "失敗したのにフォルダーが消えています。")
        self.assertTrue((self.directory / "data.bin").is_file())

    def test_module_never_calls_a_destructive_helper(self) -> None:
        """rmtree / unlink / remove を呼んでいないこと。"""
        import ast

        tree = ast.parse(Path(recycle.__file__).read_text(encoding="utf-8"))
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for forbidden in ("rmtree", "unlink", "remove", "rmdir", "removedirs"):
            with self.subTest(name=forbidden):
                self.assertNotIn(
                    forbidden, called,
                    f"recycle が {forbidden}() を呼んでいます。"
                    "完全削除へフォールバックしない方針に反します。")

    def test_allow_undo_flag_is_present(self) -> None:
        """FOF_ALLOWUNDO を落とすと、ゴミ箱ではなく完全削除になる。"""
        source = Path(recycle.__file__).read_text(encoding="utf-8")
        self.assertIn("FOF_ALLOWUNDO", source)
        self.assertIn("0x0040", source)

    @unittest.skipIf(sys.platform == "win32", "非 Windows の挙動の確認")
    def test_non_windows_refuses(self) -> None:
        with self.assertRaises(recycle.RecycleError):
            recycle.send_to_recycle_bin([self.directory])


class RecycleOnWindowsTests(TempAppRootTestCase):
    """実際にゴミ箱へ送る（Windows のみ）。"""

    def setUp(self) -> None:
        super().setUp()
        if sys.platform != "win32":
            self.skipTest("Windows 専用")
        paths.ensure_userdata_tree()
        self.asset_id = "asset_recycle_test"

    def test_cache_is_moved_and_recorded(self) -> None:
        directory = paths.vlm_cache_dir() / self.asset_id
        directory.mkdir(parents=True)
        (directory / "data.bin").write_bytes(b"x" * 64)

        result = recycle.cleanup_intermediate_cache(self.asset_id)
        if not result.ok:
            self.skipTest(f"ゴミ箱を使えない環境: {result.error}")

        self.assertFalse(directory.exists())
        self.assertEqual(result.freed_bytes, 64)
        self.assertEqual(result.status, recycle.CLEANUP_OK)

    def test_empty_target_list_is_a_no_op(self) -> None:
        recycle.send_to_recycle_bin([])
        recycle.send_to_recycle_bin([paths.vlm_cache_dir() / "does-not-exist"])


if __name__ == "__main__":
    unittest.main()
