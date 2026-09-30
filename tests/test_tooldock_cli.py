"""tooldock_cli — 台帳を読み取り専用で問い合わせる入口（ToolDock Connector v1）.

方針:
  - **一時フォルダーの APP_ROOT に合成した台帳だけを使う。** 実データへ触れない。
  - 台帳・説明文・設定は**一切変わらない**こと（中身・更新時刻・ファイル一覧）を確かめる。
  - 入口は単体の JSON CLI として別プロセスで動かす（ToolDock・MCP は使わない）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from _support import APP_ROOT, TempAppRootTestCase

from local_video_catalog import database as db_module
from local_video_catalog import description_builder as builder
from local_video_catalog import paths
from local_video_catalog.source_ref import SourceRef

CLI = APP_ROOT / "tooldock_cli.py"
MANIFEST = APP_ROOT / "tooldock.tool.json"
NOW = "2026-09-30T10:00:00+09:00"

SAMPLES = [
    # relative, period, content, transcript, status
    ("2019 運動会/徒競走 — 1位 & ゴール (前).mp4", "2019年10月12日",
     "校庭で子どもたちが徒競走をしている。ゴール付近で拍手が起きる。", "よーいどん がんばれ", "ok"),
    ("旅行/海辺の夕日.mov", "不明",
     "海辺で夕日が沈む様子を固定カメラで撮影している。", None, "ok"),
    ("誕生日/ケーキ.mp4", "2021年3月頃（解釈保留）",
     "テーブルの上のケーキにろうそくが立っている。", "お誕生日おめでとう", "ok"),
]


def description_text(file_name: str, source: str, catalog_id: str, period: str,
                     content: str, has_speech: bool) -> str:
    speech = "文字起こしあり" if has_speech else "文字起こしなし"
    return "\n".join([
        f"ファイル名：{file_name}", f"元ファイル：{source}", f"台帳ID：{catalog_id}",
        f"記録時期：{period}", "再生時間：0分12秒", "",
        f"内容：{content}", "", "概要欄用：試験用の説明文です。", "",
        f"解析情報：映像解析あり / {speech} / 生成=test", builder.FOOTER_MARKER,
        "この説明文は試験用です。", ""])


def build_sample_catalog(source_root: Path) -> list[str]:
    """APP_ROOT（環境変数で差し替え済み）に、合成の台帳と説明文を作る。"""
    paths.ensure_userdata_tree()
    ids = []
    with db_module.CatalogDatabase() as db:
        for index, (relative, period, content, transcript, _) in enumerate(SAMPLES):
            target = source_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"not a real video")
            asset_id = db.new_asset_id()
            catalog_id = db.next_catalog_id()
            db.insert_asset(asset_id=asset_id, catalog_id=catalog_id,
                            source=SourceRef(root=source_root, relative=relative),
                            file_size=16, creation_time_fs=None, last_write_time_fs=None,
                            file_fingerprint=f"fp{index}", quick_fingerprint=f"q{index}",
                            full_sha256=None, now=NOW, registration_status=db_module.REG_NEW)
            db.upsert_probe_result(asset_id, {"probe_status": db_module.STATUS_OK,
                                              "duration_seconds": 12.0, "width": 1920, "height": 1080,
                                              "video_codec": "h264"})
            db.set_stage_status(asset_id, "probe", db_module.STATUS_COMPLETED)
            if transcript:
                db.upsert_transcript({
                    "asset_id": asset_id, "catalog_id": catalog_id, "implementation_version": "t",
                    "engine_name": "test", "config_hash": "c", "source_quick_fingerprint": f"q{index}",
                    "primary_audio_stream_index": 1, "scope_type": "full", "scope_start_seconds": 0.0,
                    "scope_duration_seconds": 12.0, "transcript_status": db_module.STATUS_OK,
                    "full_text": transcript, "segment_count": 1, "created_at": NOW})
            file_name = Path(relative).name
            desc = paths.descriptions_dir() / f"{catalog_id}_{Path(relative).stem}.txt"
            desc.write_text(description_text(file_name, str(target), catalog_id, period, content,
                                             bool(transcript)), encoding="utf-8")
            db.upsert_description({
                "asset_id": asset_id, "catalog_id": catalog_id, "source_root": str(source_root),
                "source_relative": relative, "file_name": file_name, "description_file_path": desc,
                "description_status": db_module.STATUS_COMPLETED, "created_at": NOW,
                "updated_at": f"2026-09-30T1{index}:00:00+09:00"})
            ids.append(catalog_id)
    return ids


def run(command: str, payload, raw: str | None = None):
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    # 単体で呼ばれたときと同じ条件（PYTHONUTF8 なし）で試す
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-B", str(CLI), command, "--input-json", "-"],
                          input=data.encode("utf-8"), capture_output=True, env=env, timeout=300)
    lines = proc.stdout.decode("utf-8").strip().splitlines()
    assert len(lines) == 1, (proc.stdout, proc.stderr.decode("utf-8", "replace")[-2000:])
    return proc.returncode, json.loads(lines[0])


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


class ManifestTests(unittest.TestCase):
    def test_manifest_matches_the_cli(self) -> None:
        sys.path.insert(0, str(APP_ROOT))
        import tooldock_cli
        spec = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(spec["connector_version"], 2)
        self.assertEqual(spec["interface"]["entry"], CLI.name)
        self.assertEqual({a["command"] for a in spec["actions"]}, set(tooldock_cli.COMMANDS))
        # 読み取りの操作は今までどおり読み取りだけ。書くのは長時間の job（analyze）だけ
        for action in spec["actions"]:
            if action["name"] == "analyze":
                self.assertEqual(action["access"], "write")
                self.assertTrue(action["job"]["confirmation_required"])
                self.assertTrue(action["job"]["resources"]["gpu_exclusive"])
                self.assertFalse(action["job"]["resumable"])
            else:
                self.assertEqual(action["access"], "read", action["name"])
                self.assertNotIn("job", action)
        search = next(a for a in spec["actions"] if a["name"] == "search")
        props = search["input_schema"]["properties"]
        self.assertEqual(tuple(props["status"]["enum"]), tooldock_cli.STATUSES)
        self.assertEqual(tuple(props["order"]["enum"]), tooldock_cli.ORDERS)
        for action in spec["actions"]:
            self.assertFalse(action["input_schema"]["additionalProperties"], action["name"])

    def test_the_app_does_not_depend_on_tooldock(self) -> None:
        for path in list((APP_ROOT / "src").rglob("*.py")) + [APP_ROOT / "launch.py"]:
            text = path.read_text(encoding="utf-8")
            for word in ("tooldock", "import mcp", "from mcp"):
                self.assertNotIn(word, text, f"{path}: {word}")


class QueryTests(TempAppRootTestCase):
    app_root_name = "動画 カタログ — 試験 & 台帳 (合成)"

    def setUp(self) -> None:
        super().setUp()
        self.source_root = self.make_source_dir("元動画 & 素材 (試験)")
        self.ids = build_sample_catalog(self.source_root)
        self.before = snapshot(self.app_root)
        self.source_before = snapshot(self.source_root)

    def tearDown(self) -> None:
        # どの試験の後でも、台帳・説明文・設定・元動画は変わらない
        self.assertEqual(snapshot(self.app_root), self.before)
        self.assertEqual(snapshot(self.source_root), self.source_before)
        super().tearDown()

    def test_capabilities(self) -> None:
        code, reply = run("capabilities", {})
        self.assertEqual(code, 0, reply)
        res = reply["result"]
        self.assertEqual(res["counts"], {"videos": 3, "descriptions": 3})
        self.assertTrue(res["read_only"])
        self.assertEqual(Path(res["app_root"]), self.app_root.resolve())

    def test_search_matches_the_html_catalog_reading(self) -> None:
        code, reply = run("search", {})
        self.assertEqual(reply["result"]["total"], 3)
        # 記録時期の順。「解釈保留」「不明」は末尾（日付へ読み替えない）
        self.assertEqual([r["catalog_id"] for r in reply["result"]["results"]],
                         [self.ids[0], self.ids[2], self.ids[1]])
        code, reply = run("search", {"query": "徒競走 ゴール"})
        self.assertEqual([r["catalog_id"] for r in reply["result"]["results"]], [self.ids[0]])
        self.assertEqual(reply["result"]["results"][0]["matched_in"], ["description"])
        code, reply = run("search", {"query": "ケーキ", "status": "date-ambiguous"})
        self.assertEqual(reply["result"]["total"], 1)
        code, reply = run("search", {"status": "no-speech"})
        self.assertEqual([r["catalog_id"] for r in reply["result"]["results"]], [self.ids[1]])
        code, reply = run("search", {"order": "name", "limit": 1, "offset": 1})
        self.assertEqual((reply["result"]["total"], reply["result"]["count"]), (3, 1))

    def test_search_in_transcripts_only_when_asked(self) -> None:
        code, reply = run("search", {"query": "がんばれ"})
        self.assertEqual(reply["result"]["total"], 0)
        code, reply = run("search", {"query": "がんばれ", "include_transcripts": True})
        self.assertEqual([r["catalog_id"] for r in reply["result"]["results"]], [self.ids[0]])
        self.assertEqual(reply["result"]["results"][0]["matched_in"], ["transcript"])

    def test_get_video(self) -> None:
        code, reply = run("get_video", {"catalog_id": self.ids[0]})
        self.assertEqual(code, 0, reply)
        res = reply["result"]
        self.assertEqual(res["asset"]["file_name"], "徒競走 — 1位 & ゴール (前).mp4")
        self.assertEqual(Path(res["source_path"]), self.source_root / SAMPLES[0][0])
        self.assertEqual(res["probe"]["width"], 1920)
        self.assertEqual(res["stages"], {"probe": "completed"})
        self.assertEqual(res["description_record"]["period"], "2019年10月12日")

    def test_get_description_and_transcript(self) -> None:
        code, reply = run("get_description", {"catalog_id": self.ids[2]})
        self.assertEqual(reply["result"]["fields"]["content"], SAMPLES[2][2])
        self.assertNotIn("transcript", reply["result"])
        code, reply = run("get_description", {"catalog_id": self.ids[2], "include_transcript": True})
        self.assertEqual(reply["result"]["transcript"]["text"], "お誕生日おめでとう")
        code, reply = run("get_description", {"catalog_id": self.ids[1], "include_transcript": True})
        self.assertIsNone(reply["result"]["transcript"])

    def test_list_recent(self) -> None:
        code, reply = run("list_recent", {"limit": 2})
        self.assertEqual([r["catalog_id"] for r in reply["result"]["results"]], [self.ids[2], self.ids[1]])

    def test_environment_check_is_quick_and_changes_nothing(self) -> None:
        code, reply = run("environment_check", {})
        self.assertEqual(code, 0, reply)
        self.assertIn("can_start", reply["result"])
        self.assertTrue(reply["result"]["quick"])

    def test_not_found_and_invalid_arguments(self) -> None:
        self.assertEqual(run("get_video", {"catalog_id": "VID-999999"})[1]["error"]["code"], "not_found")
        self.assertEqual(run("get_description", {"catalog_id": "VID-999999"})[1]["error"]["code"], "not_found")
        for bad in ({"catalog_id": "../x"}, {"catalog_id": "VID 1"}, {"catalog_id": 1}, {}):
            self.assertEqual(run("get_video", bad)[1]["error"]["code"], "invalid_arguments", bad)
        for bad in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"status": "x"}, {"order": "size"},
                    {"query": "x" * 201}, {"sql": "DROP TABLE assets"}):
            self.assertEqual(run("search", bad)[1]["error"]["code"], "invalid_arguments", bad)
        self.assertEqual(run("capabilities", {"x": 1})[1]["error"]["code"], "invalid_arguments")

    def test_malformed_input(self) -> None:
        self.assertEqual(run("search", None, raw="{not json")[1]["error"]["code"], "usage_error")
        self.assertEqual(run("search", None, raw="[]")[1]["error"]["code"], "usage_error")
        proc = subprocess.run([sys.executable, "-B", str(CLI), "start_analysis", "--input-json", "-"],
                              input=b"{}", capture_output=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "usage_error")

    def test_reading_while_the_gui_has_the_catalog_open(self) -> None:
        """画面が台帳を開いている（-wal / -shm がある）間も読め、ファイルを増やさない。"""
        with db_module.CatalogDatabase() as db:
            db.get_meta("schema_version")
            listing = sorted(p.name for p in paths.catalog_dir().iterdir())
            self.assertIn("video_catalog.sqlite3-wal", listing)
            code, reply = run("search", {"query": "がんばれ", "include_transcripts": True})
            self.assertEqual((code, reply["result"]["total"]), (0, 1))
            code, reply = run("get_video", {"catalog_id": self.ids[1]})
            self.assertEqual(code, 0, reply)
            self.assertEqual(sorted(p.name for p in paths.catalog_dir().iterdir()), listing)
        # ここで台帳に触れたのは「画面」役の CatalogDatabase（開くときにスキーマを確かめる）で、
        # 入口ではない。入口が何も変えないことは上の一覧比較で確かめたので、
        # tearDown の比較は画面役が閉じた後の状態を基準にする。
        self.assertFalse(paths.catalog_dir().joinpath("video_catalog.sqlite3-wal").exists())
        self.before = snapshot(self.app_root)

    def test_query_text_is_never_sql(self) -> None:
        code, reply = run("search", {"query": "'; DROP TABLE assets; --", "include_transcripts": True})
        self.assertEqual((code, reply["result"]["total"]), (0, 0))
        self.assertEqual(run("capabilities", {})[1]["result"]["counts"]["videos"], 3)


class EmptyCatalogTests(TempAppRootTestCase):
    def test_no_catalog_is_not_created(self) -> None:
        code, reply = run("capabilities", {})
        self.assertEqual(code, 0, reply)
        self.assertFalse(reply["result"]["catalog_exists"])
        self.assertEqual(run("search", {})[1]["result"]["total"], 0)
        self.assertEqual(run("list_recent", {})[1]["result"]["count"], 0)
        self.assertEqual(run("get_video", {"catalog_id": "VID-000001"})[1]["error"]["code"], "not_found")
        self.assertFalse(paths.database_path().exists())
        self.assertFalse(paths.userdata_dir().exists())


if __name__ == "__main__":
    unittest.main()
