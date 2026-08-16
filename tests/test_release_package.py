"""配布物の決まりを、テストとしても押さえる.

``tools/release_smoke_test.py`` は完成した ZIP を実際に展開して確かめる。
こちらは**その手前**、「配布物へ何を入れる／入れない」という決まりが
崩れていないことを、ZIP を作らずに確かめる。

なぜ両方あるか:

  - ZIP を作るには ``runtime\\`` が要る。手元に無い環境でも、
    決まりが壊れたことには気づけるようにしたい
  - 決まりを変えたときに、**気づかず配布物へ混ざる**のが一番こわい
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from _support import APP_ROOT

sys.path.insert(0, str(APP_ROOT / "tools"))

import make_release                                     # noqa: E402

from local_video_catalog import APPLICATION_VERSION     # noqa: E402


class VersionTests(unittest.TestCase):
    """版はひとつ。**どこを見ても同じ番号にする。**

    利用者が問い合わせるとき、README と画面と ZIP 名が違うと話が通じない。
    """

    def test_the_version_looks_like_a_release(self) -> None:
        self.assertRegex(APPLICATION_VERSION, r"^\d+\.\d+\.\d+(-\w+)?$")

    def test_the_package_name_carries_the_version(self) -> None:
        name = make_release.package_name()
        self.assertIn(APPLICATION_VERSION, name)
        self.assertTrue(name.endswith("-windows-x64"))

    def test_the_readme_shows_the_same_version(self) -> None:
        text = (APP_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"v{APPLICATION_VERSION}", text)

    def test_the_release_notes_exist_for_this_version(self) -> None:
        notes = APP_ROOT / f"RELEASE_NOTES_v{APPLICATION_VERSION}.md"
        self.assertTrue(notes.is_file(), f"{notes.name} がありません。")

    def test_the_window_title_shows_the_version(self) -> None:
        source = (APP_ROOT / "src" / "local_video_catalog" / "gui"
                  / "app.py").read_text(encoding="utf-8")
        self.assertIn("APPLICATION_VERSION", source.split("WINDOW_TITLE", 1)[1]
                      .split("\n", 1)[0] + "APPLICATION_VERSION")

    def test_the_version_is_not_part_of_any_reuse_key(self) -> None:
        """**版を上げただけで再処理させない。**

        再利用の鍵は各工程の IMPL_VERSION と config_hash 側にある。
        ここへ APPLICATION_VERSION が混ざると、公開のたびに
        利用者の解析結果が全部やり直しになる。
        """
        for name in ("frames", "visual", "transcription", "description"):
            source = (APP_ROOT / "src" / "local_video_catalog" / "stages"
                      / f"{name}.py").read_text(encoding="utf-8")
            with self.subTest(stage=name):
                self.assertNotIn("APPLICATION_VERSION", source)


class ContentRulesTests(unittest.TestCase):
    """**入れるものを名指しで決める。** 黙って増えないように。"""

    def test_developer_things_are_excluded(self) -> None:
        for name in (".git", ".github", "tests", "tools", "dist",
                     "userdata", "__pycache__"):
            with self.subTest(name=name):
                self.assertIn(name, make_release.EXCLUDED_NAMES)

    def test_real_data_suffixes_are_excluded(self) -> None:
        for suffix in (".sqlite3", ".log", ".jsonl", ".pyc"):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, make_release.EXCLUDED_SUFFIXES)

    def test_the_user_documents_are_included(self) -> None:
        self.assertEqual(set(make_release.INCLUDED_DOCS),
                         {"docs/QUICKSTART.md", "docs/TROUBLESHOOTING.md"})

    def test_internal_design_documents_are_not_shipped(self) -> None:
        """**読む相手が違う。** 設計文書はリポジトリに残す。"""
        shipped = " ".join(make_release.INCLUDED_DOCS)
        for internal in ("CURRENT_SYSTEM_AUDIT", "MIGRATION_PLAN",
                         "PORTABLE_V1_DESIGN", "GUI_FEATURE_PARITY",
                         "PYTHON_RUNTIME"):
            with self.subTest(name=internal):
                self.assertNotIn(internal, shipped)

    def test_the_required_files_are_named(self) -> None:
        for name in ("Start.cmd", "launch.py", "app-root.marker",
                     "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
            with self.subTest(name=name):
                self.assertIn(name, make_release.INCLUDED_FILES)

    def test_those_files_exist(self) -> None:
        for name in make_release.INCLUDED_FILES:
            with self.subTest(name=name):
                self.assertTrue((APP_ROOT / name).is_file(),
                                f"{name} がありません。")

    def test_the_user_documents_exist(self) -> None:
        for name in make_release.INCLUDED_DOCS:
            with self.subTest(name=name):
                self.assertTrue((APP_ROOT / name).is_file(),
                                f"{name} がありません。")

    def test_the_runtime_is_part_of_the_package(self) -> None:
        """**利用者に Python を入れさせない。**"""
        self.assertIn("runtime", make_release.INCLUDED_TREES)

    def test_userdata_ships_as_an_empty_skeleton(self) -> None:
        for folder in ("cache/frames", "models/whisper", "logs", "control"):
            with self.subTest(folder=folder):
                self.assertIn(folder, make_release.USERDATA_SKELETON)


class RuntimeSourcesTests(unittest.TestCase):
    """同梱 runtime の素性が追えること。"""

    def setUp(self) -> None:
        import json

        path = APP_ROOT / "tools" / "runtime_sources.json"
        self.assertTrue(path.is_file(), "runtime_sources.json がありません。")
        self.record = json.loads(path.read_text(encoding="utf-8"))

    def test_it_records_what_was_used(self) -> None:
        for key in ("embed", "tcltk"):
            with self.subTest(key=key):
                entry = self.record[key]
                self.assertTrue(entry["file_name"])
                self.assertTrue(entry["sha256"])
                self.assertEqual(len(entry["sha256"]), 64)

    def test_everything_comes_from_python_org(self) -> None:
        """**入手元を公式だけに保つ。**"""
        for key in ("embed", "tcltk"):
            with self.subTest(key=key):
                self.assertTrue(
                    self.record[key]["source_url"].startswith(
                        "https://www.python.org/ftp/python/"),
                    self.record[key]["source_url"])

    def test_the_fetcher_refuses_other_sources(self) -> None:
        source = (APP_ROOT / "tools"
                  / "fetch_runtime_sources.py").read_text(encoding="utf-8")
        self.assertIn("python.org 以外からは取得しません", source)

    def test_the_builder_does_not_reach_the_network(self) -> None:
        """**組み立てはオフラインで。** 毎回外から取ると再現しない。"""
        source = (APP_ROOT / "tools"
                  / "build_runtime.py").read_text(encoding="utf-8")
        for network in ("urllib", "requests", "http.client", "socket"):
            with self.subTest(name=network):
                self.assertNotIn(network, source)

    def test_the_builder_checks_the_checksum(self) -> None:
        source = (APP_ROOT / "tools"
                  / "build_runtime.py").read_text(encoding="utf-8")
        self.assertIn("sha256", source)
        self.assertIn("一致しません", source)


class LicenceTests(unittest.TestCase):
    """配るなら、必要な表示も一緒に配ること。"""

    def setUp(self) -> None:
        self.notices = (APP_ROOT / "THIRD_PARTY_NOTICES.md"
                        ).read_text(encoding="utf-8")

    def test_bundled_components_are_named(self) -> None:
        for name in ("Python", "Tcl/Tk", "PSF License", "license.terms"):
            with self.subTest(name=name):
                self.assertIn(name, self.notices)

    def test_external_components_are_named_as_not_bundled(self) -> None:
        for name in ("LM Studio", "ffmpeg", "Whisper"):
            with self.subTest(name=name):
                self.assertIn(name, self.notices)
        self.assertIn("同梱していないもの", self.notices)

    def test_ffmpeg_licence_is_not_asserted(self) -> None:
        """**ビルドで条件が変わる。** こちらから断定しない。"""
        self.assertIn("ビルドによってライセンス条件が異なります",
                      self.notices)

    def test_the_app_licence_is_stated(self) -> None:
        self.assertIn("MIT License", self.notices)
        self.assertTrue((APP_ROOT / "LICENSE").is_file())


def flat(path: Path) -> str:
    """改行を畳んだ本文。

    文書は読みやすさのために折り返す。**折り返しの位置で
    試験が落ちないように**、比べるときだけ 1 行にする。
    """
    return "".join((path.read_text(encoding="utf-8")).split())


class SupportExpectationTests(unittest.TestCase):
    """**約束しすぎない。** 期待を作らない書き方にする。"""

    def test_the_readme_sets_expectations(self) -> None:
        self.assertIn("お約束するものではありません",
                      flat(APP_ROOT / "README.md"))

    def test_the_release_notes_set_expectations(self) -> None:
        notes = APP_ROOT / f"RELEASE_NOTES_v{APPLICATION_VERSION}.md"
        self.assertIn("お約束するものではありません", flat(notes))
        self.assertIn("notguaranteed", flat(notes))

    def test_the_release_notes_list_the_limits(self) -> None:
        notes = flat(APP_ROOT / f"RELEASE_NOTES_v{APPLICATION_VERSION}.md")
        for limit in ("Windowsのみ", "LMStudio", "GPU", "自動ダウンロード"):
            with self.subTest(limit=limit):
                self.assertIn(limit, notes)


class UserDocumentTests(unittest.TestCase):
    """利用者向けの文書が、利用者の言葉で書かれていること。"""

    def test_quickstart_starts_from_the_zip(self) -> None:
        text = (APP_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
        self.assertIn("展開", text)
        self.assertIn("Start.cmd", text)
        self.assertIn("HTMLカタログを開く", text)

    def test_quickstart_recommends_one_video_first(self) -> None:
        """**いきなり大量に処理させない。**"""
        text = (APP_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
        self.assertIn("まず 1 本", text)

    def test_quickstart_does_not_ask_for_python(self) -> None:
        """runtime を同梱したので、利用者に入れさせない。"""
        text = (APP_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
        self.assertIn("Python のインストールは不要", text)

    def test_troubleshooting_covers_the_common_stops(self) -> None:
        text = (APP_ROOT / "docs" / "TROUBLESHOOTING.md"
                ).read_text(encoding="utf-8")
        for topic in ("Start.cmd", "ffmpeg", "whisper", "ローカルAIに接続",
                      "モデルが選択されていません", "画像入力",
                      "安全停止", "途中まで処理", "cleanup",
                      "動画が見つかりませんでした", "ログ"):
            with self.subTest(topic=topic):
                self.assertIn(topic, text)


if __name__ == "__main__":
    unittest.main()
