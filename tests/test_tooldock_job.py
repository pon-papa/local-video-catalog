"""tooldock_cli analyze — 長時間の解析を ToolDock の job として動かす入口（Connector v2）.

方針:
  - **一時フォルダーの APP_ROOT と合成動画だけを使う。** 実データへ触れない。
  - LM Studio の代わりに ``_fake_lm_studio``（127.0.0.1、応答は test_end_to_end の FakeClient と同じ）。
  - 入口は単体の JSON CLI として別プロセスで動かす（ToolDock・MCP は使わない）。
    ToolDock から渡されるのは、標準入力の JSON と ``TOOLDOCK_CANCEL_FILE`` だけ。
  - 元動画は変わらないこと、止めても台帳と中間成果（再開の材料）が残ることを確かめる。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from _fake_lm_studio import CATALOG, FakeLmStudio
from _support import (APP_ROOT, TempAppRootTestCase, find_ffmpeg, find_ffprobe,
                      make_synthetic_video)

from local_video_catalog import database as db_module
from local_video_catalog import paths

CLI = APP_ROOT / "tooldock_cli.py"
FAKE_MODEL = "fake-vl-model"


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


def child_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("TOOLDOCK_CANCEL_FILE", None)
    env.update(extra)
    return env


def progress_events(stderr: str) -> list[dict]:
    events = []
    for line in stderr.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("event") == "progress":
            events.append(item)
    return events


def analyze(payload: dict, **env: str):
    proc = subprocess.run([sys.executable, "-B", str(CLI), "analyze", "--input-json", "-"],
                          input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          capture_output=True, env=child_env(**env), timeout=600)
    lines = proc.stdout.decode("utf-8").strip().splitlines()
    stderr = proc.stderr.decode("utf-8", "replace")
    assert len(lines) == 1, (proc.stdout, stderr[-3000:])
    return proc.returncode, json.loads(lines[0]), progress_events(stderr)


class AnalyzeJobTestCase(TempAppRootTestCase):
    app_root_name = "動画 カタログ — job 試験 & (合成) #1"
    videos = 2
    delay_seconds = 0.0

    def setUp(self) -> None:
        super().setUp()
        self.ffmpeg, self.ffprobe = find_ffmpeg(), find_ffprobe()
        if self.ffmpeg is None or self.ffprobe is None:
            self.skipTest("ffmpeg / ffprobe が必要")
        paths.ensure_userdata_tree()
        self.source = self.make_source_dir("元動画 & 素材 # (job 試験)")
        for index in range(self.videos):
            self.assertTrue(make_synthetic_video(
                self.ffmpeg, self.source / f"合成 {index + 1} — 試験 & (#{index + 1}).mp4",
                duration=3.0, with_audio=True))
        self.server = FakeLmStudio([FAKE_MODEL], behaviour=CATALOG, delay_seconds=self.delay_seconds)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.write_settings(visual_model=FAKE_MODEL)
        self.source_before = snapshot(self.source)

    def tearDown(self) -> None:
        # どの試験の後でも、元動画は変わらず、元動画のフォルダーに何も増えない
        self.assertEqual(snapshot(self.source), self.source_before)
        super().tearDown()

    def write_settings(self, *, visual_model: str) -> None:
        paths.settings_path().write_text(json.dumps({
            "ffmpeg_path": str(self.ffmpeg), "ffprobe_path": str(self.ffprobe),
            "vlm": {"base_url": self.server.base_url, "model_match": FAKE_MODEL},
            "frames": {"minimum_frame_count": 2, "maximum_frame_count": 3},
        }, ensure_ascii=False), encoding="utf-8")
        # 人が画面で選んだモデル。job の引数ではモデルを指定できない
        paths.gui_state_path().write_text(json.dumps({"visual_model": visual_model}), encoding="utf-8")

    def job(self, **options) -> dict:
        return {"source_folder": str(self.source), "skip_transcription": True, **options}


class AnalyzeTests(AnalyzeJobTestCase):
    def test_runs_the_same_analysis_as_the_gui_and_returns_a_summary(self) -> None:
        code, reply, events = analyze(self.job())
        self.assertEqual(code, 0, reply)
        result = reply["result"]
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual((result["planned"], result["processed"], result["completed"], result["failures"]),
                         (2, 2, 2, 0))
        self.assertEqual(result["library"], {"videos": 2, "done": 2, "pending": 0, "unavailable": 0})
        self.assertEqual(result["descriptions"], 2)
        self.assertEqual(result["catalog_html"], "userdata/catalog/catalog.html")
        self.assertEqual(result["visual_model"], FAKE_MODEL)
        # 絶対パスは返さない（台帳 ID と相対の名前だけ）
        text = json.dumps(reply, ensure_ascii=False)
        for private in (str(self.source), str(self.app_root)):
            self.assertNotIn(private, text)
        phases = [e["phase"] for e in events]
        self.assertEqual(phases[0], "環境の確認")
        self.assertIn("対象 2 本", phases)
        for label in ("代表画像の抽出", "映像の解析", "説明文の作成"):
            self.assertIn(f"1/2 VID-000001 {label}", phases)
            self.assertIn(f"2/2 VID-000002 {label}", phases)
        self.assertFalse(any("文字起こし" in p for p in phases))          # 飛ばした工程は始まらない
        self.assertTrue(any(paths.log_dir().glob("run_*.log")))            # このアプリのログはそのまま残る
        # 何もすることが無ければ、そう答える
        code, reply, _ = analyze(self.job())
        self.assertEqual((code, reply["result"]["outcome"], reply["result"]["processed"]),
                         (0, "nothing_to_do", 0))

    def test_the_same_job_again_continues_where_it_stopped(self) -> None:
        code, reply, _ = analyze(self.job(max_videos=1))
        self.assertEqual((code, reply["result"]["outcome"]), (0, "stopped_max_videos"))
        self.assertEqual(reply["result"]["library"]["pending"], 1)
        frames_before = len(self.server.images_sent())
        code, reply, events = analyze(self.job())
        self.assertEqual((code, reply["result"]["completed"]), (0, 1))
        self.assertEqual(reply["result"]["library"], {"videos": 2, "done": 2, "pending": 0, "unavailable": 0})
        self.assertFalse(any("VID-000001" in e["phase"] for e in events))  # 済んだ動画はやり直さない
        self.assertGreater(len(self.server.images_sent()), frames_before)

    def test_not_ready_environment_writes_nothing(self) -> None:
        for model in ("", "other-model"):
            with self.subTest(model=model):
                self.write_settings(visual_model=model)
                code, reply, _ = analyze(self.job())
                self.assertEqual((code, reply["ok"], reply["error"]["code"]),
                                 (1, False, "environment_not_ready"))
                self.assertFalse(paths.database_path().exists())
        # 文字起こしを飛ばさないなら、モデルが要る（ここには無い）
        self.write_settings(visual_model=FAKE_MODEL)
        code, reply, _ = analyze(self.job(skip_transcription=False))
        self.assertEqual(reply["error"]["code"], "environment_not_ready")
        self.assertFalse(paths.database_path().exists())

    def test_only_declared_options_are_accepted(self) -> None:
        cases = [{"source_folder": str(self.source), "visual_model": "x"},
                 {"source_folder": str(self.source), "ffmpeg_args": "-y"},
                 {"source_folder": "relative/folder"},
                 {"source_folder": str(self.source / "無い")},
                 {"source_folder": str(self.source), "time_budget_minutes": 0},
                 {"source_folder": str(self.source), "time_budget_minutes": 1321},
                 {"source_folder": str(self.source), "max_videos": -1},
                 {"source_folder": str(self.source), "recursive": "yes"},
                 {}]
        for payload in cases:
            with self.subTest(payload=payload):
                code, reply, _ = analyze(payload)
                self.assertEqual((code, reply["error"]["code"]), (1, "invalid_arguments"))
        self.assertFalse(paths.database_path().exists())


class CancelTests(AnalyzeJobTestCase):
    delay_seconds = 0.6

    def test_the_cancel_file_becomes_the_apps_stop_request(self) -> None:
        cancel = self.temp_dir / "合図 # (cancel)"
        proc = subprocess.Popen([sys.executable, "-B", str(CLI), "analyze", "--input-json", "-"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=child_env(TOOLDOCK_CANCEL_FILE=str(cancel)))
        proc.stdin.write(json.dumps(self.job()).encode("utf-8"))
        proc.stdin.close()
        deadline = time.monotonic() + 120
        seen = []
        while time.monotonic() < deadline:
            line = proc.stderr.readline().decode("utf-8", "replace")
            if not line:
                break
            seen.extend(progress_events(line))
            if seen and seen[-1]["phase"] == "1/2 VID-000001 映像の解析":
                cancel.write_text("cancel\n", encoding="utf-8")         # ToolDock が合図を置く
                break
        out, err = proc.communicate(timeout=180)
        reply = json.loads(out.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual((proc.returncode, reply["error"]["code"]), (1, "cancelled"), err[-2000:])
        self.assertFalse(paths.stop_request_path().exists())             # 停止要求は残さない
        with db_module.CatalogDatabase() as db:
            first = db.list_assets_under(self.source)[0]["asset_id"]
            self.assertTrue(db.is_stage_done(first, db_module.STAGE_FRAME_EXTRACTION))
            self.assertFalse(db.is_stage_done(first, db_module.STAGE_DESCRIPTION))
        # 再開の材料は残っていて、同じ job で続きから終わる
        code, reply, events = analyze(self.job())
        self.assertEqual((code, reply["result"]["outcome"]), (0, "completed"))
        self.assertEqual(reply["result"]["library"]["done"], 2)
        self.assertNotIn("1/2 VID-000001 代表画像の抽出", [e["phase"] for e in events])


if __name__ == "__main__":
    import unittest
    unittest.main()
