"""コンソール窓の数を、**窓を持たない親の下で**数える補助.

これは ``pythonw`` から実行されることを前提にしている。
コンソールを持つ親（``python.exe``）から実行すると、子はその
コンソールに相乗りするので新しい窓が作られず、**何を測っても 0 になる**。
テストを空振りさせないために、必ず窓なしの親で走らせること。

``sys.argv``:
    1. 何を測るか（``ffprobe`` / ``ffmpeg`` / ``pipeline`` / ``register``）
    2. 結果を書き出すファイル
    3. src フォルダー
    4. 以降は測る対象ごとの引数
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

MODE = sys.argv[1]
OUT = Path(sys.argv[2])
SRC = Path(sys.argv[3])
REST = sys.argv[4:]

sys.path.insert(0, str(SRC))

user32 = ctypes.windll.user32
CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")


def console_windows() -> int:
    total = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        nonlocal total
        if user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buffer, 64)
            if buffer.value in CONSOLE_CLASSES:
                total += 1
        return True

    user32.EnumWindows(visit, 0)
    return total


def peak_during(work) -> int:
    """処理中に増えた窓の最大数。"""
    baseline = console_windows()
    peak = 0
    stop = threading.Event()

    def watch() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, console_windows() - baseline)
            time.sleep(0.02)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        work()
    finally:
        stop.set()
        watcher.join(timeout=5)
    return peak


def has_console() -> bool:
    """このプロセスがコンソールを持っているか。

    ``sys.stdout is None`` では判定できない。出力をパイプへつないだ場合、
    コンソールが無くても stdout は None にならないため。
    **窓が作られるかどうかを決めるのはコンソールの有無**なので、
    Windows へ直接尋ねる。
    """
    return ctypes.windll.kernel32.GetConsoleWindow() != 0


result: dict[str, object] = {
    "mode": MODE,
    "parent": sys.executable,
    "parent_has_console": has_console(),
}

try:
    if MODE == "control":
        # **この測り方で窓を検出できることの確認。**
        # 修正前の書き方を再現して、窓が増えることを見る。
        ffprobe, video = REST

        def old_way() -> None:
            for _ in range(6):
                subprocess.run(
                    [ffprobe, "-hide_banner", "-loglevel", "error",
                     "-print_format", "json", "-show_format", video],
                    capture_output=True, timeout=30, check=False)

        result["peak"] = peak_during(old_way)

    elif MODE == "ffprobe":
        from local_video_catalog import probe

        ffprobe, video = REST

        def work() -> None:
            for _ in range(6):
                probe.probe(Path(ffprobe), Path(video), timeout=30)

        result["peak"] = peak_during(work)

    elif MODE == "ffmpeg":
        from local_video_catalog import frame_extractor as fx

        ffmpeg, video, target_dir = REST
        config = fx.ExtractionConfig()
        frames = fx.plan_frames(2.0, config)

        def work() -> None:
            for frame in frames[:4]:
                fx.extract_one(Path(ffmpeg), Path(video), frame,
                               Path(target_dir) / fx.frame_file_name(frame),
                               config)

        result["peak"] = peak_during(work)

    elif MODE == "whisper_check":
        from local_video_catalog import config as config_module

        (ffmpeg,) = REST

        def work() -> None:
            for _ in range(3):
                config_module.ffmpeg_has_whisper(Path(ffmpeg), timeout=60)

        result["peak"] = peak_during(work)

    elif MODE == "pipeline_child":
        from local_video_catalog.gui import runner as gui_runner

        def work() -> None:
            task = gui_runner.BackgroundTask("x", [])
            task.command = [sys.executable, "-X", "utf8", "-c",
                            "import time; time.sleep(1.5); print('done')"]
            task.start()
            task.wait(timeout=60)

        result["peak"] = peak_during(work)

    elif MODE == "register":
        import os

        from local_video_catalog import config as config_module
        from local_video_catalog import database as db_module
        from local_video_catalog import paths, register
        from local_video_catalog.logging_utils import RunLogger, new_run_id

        app_root, source_root, ffprobe = REST
        os.environ["LOCAL_VIDEO_CATALOG_ROOT"] = app_root
        paths.ensure_userdata_tree()

        raw = config_module.load_settings_dict()
        raw["ffprobe_path"] = ffprobe
        settings = config_module.build_settings(raw, require_ffprobe=False)
        database = db_module.CatalogDatabase()
        logger = RunLogger(paths.log_dir(), new_run_id(),
                           console_level=100)

        def work() -> None:
            register.register_folder(Path(source_root), settings, database,
                                     logger, run_id="probe")

        result["peak"] = peak_during(work)
        logger.close()
        database.close()

    else:
        raise SystemExit(f"unknown mode: {MODE}")

    result["ok"] = True
except BaseException as error:                    # noqa: BLE001
    import traceback

    result["ok"] = False
    result["error"] = traceback.format_exc()

OUT.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
