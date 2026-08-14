"""ログ出力.

二本立てにする。

  1. 人間可読ログ ``run_<run_id>.log``
  2. 構造化ログ   ``run_<run_id>.jsonl``  1 行 1 イベント。集計・障害調査用。

プライバシー方針:

  - ffprobe の生 JSON 全文をログへ出さない。
  - 文字起こしの全文・AI の出力もログへ出さない（本モジュールに口を作らない）。
  - コンソールへは件数と進捗のみ。ファイル名は人間可読ログにのみ出す。

伏せ字は「うっかり渡された場合の最後の砦」であり、
**呼び出し側が最初から渡さないことが前提**である。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "local_video_catalog"

_REDACT_KEYS = frozenset({
    "raw_json",
    "raw_probe_json",
    "raw_response",
    "raw_engine_output",
    "stdout",
    "transcript",
    "transcript_text",
    "full_text",
    "summary",
    "visual_summary",
    "caption",
    "description",
    "token",
    "password",
    "api_key",
})
"""ログへ出してはいけないキー。"""

_MAX_FIELD_LEN = 500


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    """実行 ID。ファイル名にも使えるよう記号を含めない。

    秒単位のタイムスタンプだけでは、同じ秒に 2 回起動したときに衝突する。
    短いランダム接尾辞で一意性を保証する。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:6]}"


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """ログへ書けない値を伏せ、長すぎる文字列を切り詰める。"""
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _REDACT_KEYS:
            clean[key] = "<redacted>"
            continue
        if isinstance(value, Path):
            clean[key] = str(value)
            continue
        if isinstance(value, str) and len(value) > _MAX_FIELD_LEN:
            clean[key] = value[:_MAX_FIELD_LEN] + f"...<truncated {len(value)} chars>"
            continue
        clean[key] = value
    return clean


class RunLogger:
    """人間可読ログと JSONL ログの両方へ書く。

    ワーカースレッドから呼ばれても壊れないようロックで保護する。
    """

    def __init__(
        self,
        log_dir: Path,
        run_id: str,
        *,
        console_level: int = logging.INFO,
        file_level: int = logging.DEBUG,
    ) -> None:
        self.run_id = run_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.text_log_path = self.log_dir / f"run_{run_id}.log"
        self.jsonl_log_path = self.log_dir / f"run_{run_id}.jsonl"

        self._lock = threading.Lock()
        self._jsonl = open(self.jsonl_log_path, "a", encoding="utf-8", newline="\n")

        self._logger = logging.getLogger(f"{LOGGER_NAME}.{run_id}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        # 同一 run_id で再構築された場合に重複ハンドラーを作らない
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()

        file_handler = logging.FileHandler(self.text_log_path, encoding="utf-8")
        file_handler.setLevel(file_level)
        file_handler.setFormatter(logging.Formatter(
            fmt="[%(asctime)s] %(levelname)-5s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        self._logger.addHandler(file_handler)

        stream = getattr(sys, "stdout", None)
        if stream is not None:
            console_handler = logging.StreamHandler(stream)
            console_handler.setLevel(console_level)
            console_handler.setFormatter(logging.Formatter(fmt="%(message)s"))
            self._logger.addHandler(console_handler)

    # -- 基本 -------------------------------------------------------------

    def info(self, message: str) -> None:
        self._logger.info(message)

    def warning(self, message: str) -> None:
        self._logger.warning(message)

    def error(self, message: str) -> None:
        self._logger.error(message)

    def debug(self, message: str) -> None:
        """人間可読ログのみへ出す（コンソールには出ない）。"""
        self._logger.debug(message)

    # -- 構造化イベント ---------------------------------------------------

    def event(self, event: str, **fields: Any) -> None:
        """JSONL へ 1 イベント書く。コンソールには出さない。"""
        record: dict[str, Any] = {
            "ts": local_now_iso(),
            "run_id": self.run_id,
            "event": event,
        }
        record.update(_sanitize_fields(fields))
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._jsonl.write(line + "\n")
            self._jsonl.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._jsonl.flush()
                self._jsonl.close()
            except (OSError, ValueError):
                pass
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except (OSError, ValueError):
                pass

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def configure_stdio_utf8() -> None:
    """コンソールを UTF-8 にする。

    日本語 Windows のシステム ANSI コードページは 932 のため、
    明示しないと日本語が化ける。**画面（GUI）が出力をリダイレクトして
    受け取るときに顕在化する。**
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def child_process_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """子プロセスへ渡す環境変数。

    **UTF-8 を明示しないと、日本語 Windows で出力が CP932 として
    復号され文字化けする。** 画面が子プロセスの出力を取り込む構成では
    必ず必要になる。
    """
    import os

    env = dict(base if base is not None else os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env
