# -*- coding: utf-8 -*-
"""動画カタログを画面なしで問い合わせる入口（ToolDock Connector v1 / JSON CLI）。

**読み取り専用。** 台帳（SQLite）は read-only で開き、説明文は読むだけ。
解析を始めたり、台帳・説明文・設定・元動画を変えたりする操作はここに無い。
長時間の解析（映像の解析・文字起こし）は画面から行う。

    python tooldock_cli.py capabilities      --input-json -
    python tooldock_cli.py environment_check --input-json -
    python tooldock_cli.py search            --input-json -
    python tooldock_cli.py get_video         --input-json -
    python tooldock_cli.py get_description   --input-json -
    python tooldock_cli.py list_recent       --input-json -

- 引数は標準入力の JSON オブジェクト1つ。標準出力には JSON を1行だけ出す
    成功: {"ok": true, "result": {...}}
    失敗: {"ok": false, "error": {"code": "...", "message": "..."}}
- 対象はこのフォルダー（APP_ROOT）の userdata にある台帳と説明文だけ
- 検索・一覧は HTML カタログと同じ読み方（html_catalog.collect_records）をする

終了コード: 0=成功 / 1=処理できなかった / 2=使い方の誤り
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from local_video_catalog import APPLICATION_VERSION, SCHEMA_VERSION   # noqa: E402
from local_video_catalog import description_builder as builder         # noqa: E402
from local_video_catalog import html_catalog, paths                    # noqa: E402

CATALOG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
STATUSES = tuple(key for key, _ in html_catalog.STATUS_FILTERS)
ORDERS = ("period", "period-desc", "name")
MAX_TEXT = 200_000


class CliError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- checks
def _keys(args: dict, allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise CliError("invalid_arguments", f"使えない項目があります: {sorted(unknown)}")


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CliError("invalid_arguments", f"{key} は {low}〜{high} の整数です。")
    return value


def _flag(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise CliError("invalid_arguments", f"{key} は true / false です。")
    return value


def _choice(args: dict, key: str, choices, default):
    value = args.get(key, default)
    if value not in choices:
        raise CliError("invalid_arguments", f"{key} は {list(choices)} のどれかです。")
    return value


def _catalog_id(args: dict) -> str:
    value = args.get("catalog_id")
    if not isinstance(value, str) or not CATALOG_ID.match(value):
        raise CliError("invalid_arguments", "catalog_id は台帳ID（例: VID-000001）です。")
    return value


# ---------------------------------------------------------------- read-only access
def _app_root() -> Path:
    try:
        return paths.app_root()
    except paths.AppRootError as exc:
        raise CliError("catalog_unavailable", str(exc)) from None


def _open_db() -> sqlite3.Connection | None:
    """台帳を読み取り専用で開く。無ければ None（作らない）。

    台帳は WAL 形式。read-only で開いても、SQLite は ``-wal`` / ``-shm`` が
    無ければ作ってしまう。**台帳のフォルダーに何も増やさない**ため:

      - ``-wal`` が無い（画面が台帳を開いていない）→ ``immutable=1``。
        未反映の書き込みが無いので、本体だけを読めば最新で、何も作らない
      - ``-wal`` がある（画面が処理中など）→ 通常の ``mode=ro``。
        既にある ``-wal`` / ``-shm`` を読むだけで、新しいファイルは作らない
    """
    path = paths.database_path()
    if not path.is_file():
        return None
    wal = path.with_name(path.name + "-wal")
    mode = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?{mode}", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error as exc:
        raise CliError("catalog_unavailable", f"台帳を読めません: {exc}") from None
    version = int(row["value"]) if row and str(row["value"]).isdigit() else None
    if version is not None and version > SCHEMA_VERSION:
        conn.close()
        raise CliError("catalog_too_new",
                       f"台帳がこのプログラムより新しい形式です（台帳={version} / プログラム={SCHEMA_VERSION}）。")
    return conn


def _records() -> list:
    return html_catalog.collect_records(paths.descriptions_dir())


def _record_dict(record) -> dict:
    data = record.to_dict()
    data["sort_period"] = record.sort_period
    return data


def _description_file(catalog_id: str) -> Path | None:
    """説明文 txt を探す。descriptions フォルダーの外は見ない。"""
    folder = paths.descriptions_dir()
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.txt")):
        if path.name.startswith(f"{catalog_id}_") or path.stem == catalog_id:
            record = html_catalog.read_record(path)
            if record is not None and record.catalog_id == catalog_id:
                return path
    return None


def _row(row: sqlite3.Row | None, columns: tuple[str, ...]) -> dict | None:
    if row is None:
        return None
    keys = row.keys()
    return {c: row[c] for c in columns if c in keys}


# ---------------------------------------------------------------- commands
def capabilities(args: dict) -> dict:
    _keys(args, set())
    root = _app_root()
    conn = _open_db()
    counts = {"videos": 0, "descriptions": len(_records())}
    try:
        if conn is not None:
            counts["videos"] = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    finally:
        if conn is not None:
            conn.close()
    return {
        "tool": "local-video-catalog",
        "version": APPLICATION_VERSION,
        "schema_version": SCHEMA_VERSION,
        "app_root": str(root),
        "catalog_exists": paths.database_path().is_file(),
        "counts": counts,
        "search_fields": list(html_catalog.CatalogRecord.SEARCHABLE),
        "statuses": dict(html_catalog.STATUS_FILTERS),
        "orders": list(ORDERS),
        "read_only": True,
        "notes": ["台帳と説明文は読むだけ。解析の開始・停止・再開はここではできない（画面で行う）",
                  "記録時期の「解釈保留」「不明」を日付へ読み替えない",
                  "説明文はローカル AI が作ったもので、人物・場所・行事は確認されていない"],
    }


def environment_check(args: dict) -> dict:
    _keys(args, {"quick", "skip_transcription"})
    quick = _flag(args, "quick", True)
    skip = _flag(args, "skip_transcription", False)
    _app_root()
    from local_video_catalog import config as config_module
    from local_video_catalog import environment_check as ec
    try:
        raw = config_module.load_settings_dict()
        settings = config_module.build_settings(raw, require_ffprobe=False)
    except config_module.ConfigError as exc:
        raise CliError("config_error", str(exc)) from None
    result = ec.check_environment(raw=raw, settings=settings, source_folder=raw.get("source_path"),
                                  skip_transcription=skip, quick=quick)
    readiness = result.readiness(skip_transcription=skip)
    return {"check": result.to_dict(), "can_start": bool(readiness.can_start),
            "readiness": list(readiness.detail_lines()), "quick": quick,
            "note": "確認だけで、解析は始めない。"}


def search(args: dict) -> dict:
    _keys(args, {"query", "status", "order", "limit", "offset", "include_transcripts"})
    query = args.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        raise CliError("invalid_arguments", "query は200文字までの文字列です。")
    status = _choice(args, "status", STATUSES, "all")
    order = _choice(args, "order", ORDERS, "period")
    limit = _int(args, "limit", 20, 1, 200)
    offset = _int(args, "offset", 0, 0, 1_000_000)
    with_transcripts = _flag(args, "include_transcripts", False)
    _app_root()

    terms = [t for t in query.lower().split() if t]
    transcript_text: dict[str, str] = {}
    if with_transcripts and terms:
        conn = _open_db()
        if conn is not None:
            try:
                for row in conn.execute(
                        "SELECT a.catalog_id, t.full_text FROM transcripts t "
                        "JOIN assets a ON a.asset_id = t.asset_id WHERE t.full_text IS NOT NULL"):
                    key = row["catalog_id"]
                    transcript_text[key] = transcript_text.get(key, "") + " " + str(row["full_text"]).lower()
            finally:
                conn.close()

    matched = []
    for record in _records():
        if status != "all" and status not in record.statuses:
            continue
        haystack = " ".join(str(getattr(record, k, "")) for k in record.SEARCHABLE).lower()
        where = []
        if all(t in haystack for t in terms):
            where.append("description")
        elif terms and with_transcripts and all(t in transcript_text.get(record.catalog_id, "") for t in terms):
            where.append("transcript")
        if terms and not where:
            continue
        matched.append((record, where))

    if order == "name":
        matched.sort(key=lambda m: m[0].file_name)
    else:
        matched.sort(key=lambda m: m[0].sort_period, reverse=(order == "period-desc"))
    page = matched[offset:offset + limit]
    return {"query": query, "status": status, "order": order, "total": len(matched),
            "offset": offset, "count": len(page),
            "results": [dict(_record_dict(r), matched_in=w) for r, w in page]}


def get_video(args: dict) -> dict:
    _keys(args, {"catalog_id"})
    catalog_id = _catalog_id(args)
    _app_root()
    conn = _open_db()
    if conn is None:
        raise CliError("not_found", "台帳がまだありません。")
    try:
        asset = conn.execute("SELECT * FROM assets WHERE catalog_id = ?", (catalog_id,)).fetchone()
        if asset is None:
            raise CliError("not_found", f"{catalog_id} は台帳にありません。")
        aid = asset["asset_id"]
        probe = conn.execute("SELECT * FROM probe_results WHERE asset_id = ?", (aid,)).fetchone()
        desc = conn.execute("SELECT * FROM asset_descriptions WHERE asset_id = ?", (aid,)).fetchone()
        stages = conn.execute("SELECT stage_name, status FROM stage_status WHERE asset_id = ? "
                              "ORDER BY stage_name", (aid,)).fetchall()
        dates = conn.execute("SELECT candidate_datetime, source_type, confidence, has_time, is_user_confirmed "
                             "FROM capture_time_candidates WHERE asset_id = ? "
                             "ORDER BY is_user_confirmed DESC, confidence DESC LIMIT 10", (aid,)).fetchall()
        visual = conn.execute("SELECT title_candidate, visual_summary, main_activity, model_id, created_at "
                              "FROM asset_visual_summaries WHERE asset_id = ? AND summary_status = 'ok' "
                              "ORDER BY created_at DESC LIMIT 1", (aid,)).fetchone()
    finally:
        conn.close()
    record_path = _description_file(catalog_id)
    record = html_catalog.read_record(record_path) if record_path else None
    return {
        "catalog_id": catalog_id,
        "asset": _row(asset, ("catalog_id", "file_name", "extension", "source_root", "source_relative",
                              "file_size", "creation_time_fs", "last_write_time_fs", "first_seen_at",
                              "last_seen_at", "is_available", "registration_status")),
        "source_path": str(Path(asset["source_root"]) / Path(asset["source_relative"])),
        "probe": _row(probe, ("probe_status", "duration_seconds", "format_name", "width", "height",
                              "video_codec", "frame_rate_decimal", "audio_codec", "sample_rate",
                              "channel_count", "creation_time_tag")),
        "description": _row(desc, ("description_status", "recorded_from", "recorded_to",
                                   "recorded_precision", "recorded_source", "used_visual_analysis",
                                   "used_transcription", "generator", "model_id", "created_at", "updated_at")),
        "description_record": _record_dict(record) if record else None,
        "visual_summary": _row(visual, ("title_candidate", "visual_summary", "main_activity",
                                        "model_id", "created_at")),
        "capture_time_candidates": [dict(r) for r in dates],
        "stages": {r["stage_name"]: r["status"] for r in stages},
    }


def get_description(args: dict) -> dict:
    _keys(args, {"catalog_id", "include_transcript"})
    catalog_id = _catalog_id(args)
    with_transcript = _flag(args, "include_transcript", False)
    _app_root()
    path = _description_file(catalog_id)
    if path is None:
        raise CliError("not_found", f"{catalog_id} の説明文はまだありません。")
    text = path.read_text(encoding="utf-8")[:MAX_TEXT]
    result = {"catalog_id": catalog_id, "file": path.name,
              "fields": builder.parse_description_text(text), "text": text}
    if with_transcript:
        transcript = None
        conn = _open_db()
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT t.full_text, t.language_detected, t.segment_count, t.transcript_status "
                    "FROM transcripts t JOIN assets a ON a.asset_id = t.asset_id "
                    "WHERE a.catalog_id = ? AND t.full_text IS NOT NULL "
                    "ORDER BY t.created_at DESC LIMIT 1", (catalog_id,)).fetchone()
            finally:
                conn.close()
            if row is not None:
                transcript = {"text": str(row["full_text"])[:MAX_TEXT], "language": row["language_detected"],
                              "segments": row["segment_count"], "status": row["transcript_status"]}
        result["transcript"] = transcript
        result["transcript_note"] = "文字起こしは AI の推定で、聞き間違いや幻覚を含むことがある。"
    return result


def list_recent(args: dict) -> dict:
    _keys(args, {"limit"})
    limit = _int(args, "limit", 20, 1, 200)
    _app_root()
    by_id = {r.catalog_id: r for r in _records()}
    conn = _open_db()
    rows = []
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT catalog_id, COALESCE(updated_at, created_at) AS described_at "
                "FROM asset_descriptions WHERE catalog_id IS NOT NULL "
                "ORDER BY described_at DESC, catalog_id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
    results = [dict(_record_dict(by_id[r["catalog_id"]]), described_at=r["described_at"])
               for r in rows if r["catalog_id"] in by_id]
    return {"count": len(results), "results": results}


COMMANDS = {"capabilities": capabilities, "environment_check": environment_check, "search": search,
            "get_video": get_video, "get_description": get_description, "list_recent": list_recent}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("usage_error", message)


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="tooldock_cli", description="local-video-catalog read-only JSON CLI")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--input-json", required=True, metavar="-",
                        help="引数の JSON。- で標準入力から読む")
    try:
        ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if ns.input_json != "-":
            raise CliError("usage_error", "--input-json には - だけを指定します。")
        try:
            # 標準入力は常に UTF-8（Windows の既定 cp932 に左右されない）
            args = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        except ValueError:
            raise CliError("usage_error", "標準入力が JSON ではありません。") from None
        if not isinstance(args, dict):
            raise CliError("usage_error", "引数は JSON オブジェクトです。")
        result = COMMANDS[ns.command](args)
    except CliError as e:
        emit({"ok": False, "error": {"code": e.code, "message": e.message}})
        return 2 if e.code == "usage_error" else 1
    except sqlite3.DatabaseError as e:
        emit({"ok": False, "error": {"code": "catalog_unavailable",
                                     "message": f"台帳を読めませんでした（画面が書き込み中なら、少し待って再度）: {e}"}})
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "error": {"code": "cancelled", "message": "処理を中止しました。"}})
        return 130
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"ok": False, "error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}})
        return 1
    emit({"ok": True, "result": result})
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    raise SystemExit(main())
