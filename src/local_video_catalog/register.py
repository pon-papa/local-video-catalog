"""動画の登録と基本情報の取得（工程 1/5）.

解析対象フォルダーを列挙し、fingerprint を計算し、ffprobe を実行して
台帳へ記録する。**元動画は読み取り専用でのみ開く。**

この工程だけはフォルダー単位で 1 回動く。以降の工程は動画ごと。

ワーカースレッドは ffprobe と fingerprint（読み取りのみ）を担当し、
**台帳への書き込みは主スレッドだけ**が行う。
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import APPLICATION_VERSION
from . import config as config_module
from . import database as db_module
from . import datetime_candidates, discovery, fingerprint, paths, probe
from .logging_utils import RunLogger, configure_stdio_utf8, local_now_iso, new_run_id
from .source_ref import SourceRef

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_NO_SOURCE = 3


@dataclass
class RegisterSummary:
    discovered: int = 0
    registered: int = 0
    reused: int = 0
    moved: int = 0
    failed: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered, "registered": self.registered,
            "reused": self.reused, "moved": self.moved,
            "failed": self.failed, "missing": self.missing,
        }


@dataclass
class _Prepared:
    """ワーカースレッドが作る、書き込み前の材料。"""

    found: discovery.DiscoveredFile
    file_fingerprint: fingerprint.FileFingerprint | None = None
    full_sha256: str | None = None
    error: str = ""


def _prepare(found: discovery.DiscoveredFile, settings: config_module.Settings
             ) -> _Prepared:
    """読み取りだけを行う（スレッドから呼ばれる）。"""
    prepared = _Prepared(found=found)
    try:
        prepared.file_fingerprint = fingerprint.compute_file_fingerprint(
            found.path, head_bytes=settings.head_bytes,
            tail_bytes=settings.tail_bytes, size=found.size)
        if settings.full_hash:
            prepared.full_sha256 = fingerprint.compute_full_sha256(found.path)
    except OSError as exc:
        prepared.error = f"読み取りに失敗しました: {exc}"
    return prepared


def _probe_cache_path(asset_id: str, gzip_enabled: bool) -> Path:
    suffix = ".json.gz" if gzip_enabled else ".json"
    return paths.probe_cache_dir() / f"{asset_id}{suffix}"


def register_folder(
    source_root: Path,
    settings: config_module.Settings,
    database: db_module.CatalogDatabase,
    logger: RunLogger,
    *,
    run_id: str,
    dry_run: bool = False,
) -> RegisterSummary:
    """1 つの解析対象フォルダーを登録する。"""
    summary = RegisterSummary()
    now = local_now_iso()

    found_files = list(discovery.discover(
        source_root,
        extensions=settings.extensions,
        exclude_patterns=settings.exclude_patterns,
        recursive=settings.recursive,
        min_size_bytes=settings.min_size_bytes,
        follow_symlinks=settings.follow_symlinks))
    summary.discovered = len(found_files)
    logger.info(f"動画ライブラリの確認: {summary.discovered} 本が見つかりました")
    logger.event("discovery_finished", count=summary.discovered)

    if dry_run:
        return summary

    seen_asset_ids: set[str] = set()

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        prepared_items = pool.map(lambda f: _prepare(f, settings), found_files)

        for index, prepared in enumerate(prepared_items, start=1):
            found = prepared.found
            if prepared.error:
                summary.failed += 1
                summary.errors.append(f"{found.source.relative}: {prepared.error}")
                logger.warning(f"{found.source.relative}: {prepared.error}")
                continue

            asset_id, status = _upsert_asset(
                database, found, prepared, now=now)
            seen_asset_ids.add(asset_id)

            if status == db_module.REG_NEW:
                summary.registered += 1
            elif status == db_module.REG_MOVED:
                summary.moved += 1
            else:
                summary.reused += 1

            ok = _probe_and_record(
                database, asset_id, found, prepared, settings, logger, now=now)
            if not ok:
                summary.failed += 1

            if index % 25 == 0 or index == summary.discovered:
                # **「解析」ではなく「ライブラリの確認」だと分かる言い方にする。**
                # 上限 3 本を指定した利用者が「329 本処理している」と
                # 誤解しないため。
                logger.info(f"  動画ライブラリを確認しています: "
                            f"{index} / {summary.discovered} 本")

    # このフォルダーに属していたのに今回見つからなかったもの
    known = database.list_assets_under(source_root)
    vanished = [row["asset_id"] for row in known
                if row["asset_id"] not in seen_asset_ids
                and row["is_available"]]
    if vanished:
        with database.transaction():
            summary.missing = database.mark_assets_unavailable(vanished, now)
        logger.info(f"見つからなくなった動画: {summary.missing} 本"
                    "（台帳からは削除していません）")

    return summary


def _upsert_asset(
    database: db_module.CatalogDatabase,
    found: discovery.DiscoveredFile,
    prepared: _Prepared,
    *,
    now: str,
) -> tuple[str, str]:
    """asset を登録または更新し、``(asset_id, registration_status)`` を返す。"""
    file_fp = prepared.file_fingerprint.value if prepared.file_fingerprint else None

    existing = database.find_asset_by_source(found.source)
    if existing is not None:
        with database.transaction():
            database.update_asset_seen(
                existing["asset_id"], source=found.source, file_size=found.size,
                creation_time_fs=found.creation_time_fs,
                last_write_time_fs=found.last_write_time_fs,
                file_fingerprint=file_fp, quick_fingerprint=None,
                full_sha256=prepared.full_sha256, now=now,
                registration_status=db_module.REG_EXISTING)
        return (existing["asset_id"], db_module.REG_EXISTING)

    # 同じ内容のファイルが「移動」したのかを見る。
    #
    # **元の場所にまだファイルがあるなら、それは移動ではなく複製である。**
    # 内容が同じというだけで束ねると、同一内容の動画が同じフォルダーに
    # 複数あるとき、2 本目以降が 1 本目の行を奪い合い、台帳から消える。
    if file_fp:
        candidates = [
            row for row in database.find_assets_by_file_fingerprint(file_fp)
            if str(row["source_root"]) == str(found.source.root)
        ]
        vanished = [
            row for row in candidates
            if not (Path(row["source_root"]) / row["source_relative"]).is_file()
        ]
        if len(vanished) == 1:
            row = vanished[0]
            with database.transaction():
                database.update_asset_seen(
                    row["asset_id"], source=found.source, file_size=found.size,
                    creation_time_fs=found.creation_time_fs,
                    last_write_time_fs=found.last_write_time_fs,
                    file_fingerprint=file_fp, quick_fingerprint=None,
                    full_sha256=prepared.full_sha256, now=now,
                    registration_status=db_module.REG_MOVED)
            return (row["asset_id"], db_module.REG_MOVED)

    asset_id = database.new_asset_id()
    with database.transaction():
        database.insert_asset(
            asset_id=asset_id, catalog_id=database.next_catalog_id(),
            source=found.source, file_size=found.size,
            creation_time_fs=found.creation_time_fs,
            last_write_time_fs=found.last_write_time_fs,
            file_fingerprint=file_fp, quick_fingerprint=None,
            full_sha256=prepared.full_sha256, now=now,
            registration_status=db_module.REG_NEW)
    return (asset_id, db_module.REG_NEW)


def _probe_and_record(
    database: db_module.CatalogDatabase,
    asset_id: str,
    found: discovery.DiscoveredFile,
    prepared: _Prepared,
    settings: config_module.Settings,
    logger: RunLogger,
    *,
    now: str,
) -> bool:
    """ffprobe を実行し、結果と撮影日時候補を台帳へ書く。"""
    cache_path = (_probe_cache_path(asset_id, settings.probe_cache_gzip)
                  if settings.probe_cache_enabled else None)
    started = local_now_iso()
    result = probe.probe(
        settings.ffprobe_path, found.path,
        timeout=settings.ffprobe_timeout_sec, cache_path=cache_path,
        gzip_enabled=settings.probe_cache_gzip)

    values = dict(result.values)
    values["probe_started_at"] = started
    values["probe_finished_at"] = local_now_iso()

    quick_fp = None
    if result.ok and prepared.file_fingerprint is not None:
        signature = fingerprint.stream_signature(
            duration_seconds=values.get("duration_seconds"),
            video_codec=values.get("video_codec"),
            width=values.get("width"), height=values.get("height"),
            frame_rate_num=values.get("frame_rate_num"),
            frame_rate_den=values.get("frame_rate_den"),
            audio_codec=values.get("audio_codec"),
            sample_rate=values.get("sample_rate"),
            channel_count=values.get("channel_count"))
        quick_fp = fingerprint.compute_quick_fingerprint(
            prepared.file_fingerprint, signature)

    candidates = datetime_candidates.collect_candidates(
        file_name=found.source.file_name,
        folder_names=found.source.parent_names(),
        creation_time_tag=values.get("creation_time_tag"),
        filesystem_creation_time=found.creation_time_fs,
        filesystem_last_write_time=found.last_write_time_fs)

    with database.transaction():
        database.upsert_probe_result(asset_id, values)
        if quick_fp:
            database.update_asset_seen(
                asset_id, source=found.source, file_size=found.size,
                creation_time_fs=found.creation_time_fs,
                last_write_time_fs=found.last_write_time_fs,
                file_fingerprint=(prepared.file_fingerprint.value
                                  if prepared.file_fingerprint else None),
                quick_fingerprint=quick_fp,
                full_sha256=prepared.full_sha256, now=now,
                registration_status=db_module.REG_EXISTING)
        database.replace_capture_candidates(
            asset_id, [c.to_dict() for c in candidates], now)
        database.set_stage_status(
            asset_id, db_module.STAGE_FFPROBE,
            db_module.STATUS_OK if result.ok else db_module.STATUS_FAILED,
            started_at=started, finished_at=local_now_iso(),
            error_message=values.get("error_message"),
            implementation_version=str(values.get("ffprobe_impl_version")))

    if not result.ok:
        logger.warning(f"{found.source.relative}: {values.get('error_message')}")
        logger.event("probe_failed", relative=found.source.relative,
                     error_type=values.get("error_type"))
    return result.ok


# --------------------------------------------------------------------------
# コマンドライン
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_video_catalog.register",
        description="動画を登録して基本情報を取得する（元動画は読むだけ）")
    parser.add_argument("--source-folder", help="解析したい動画のフォルダー")
    parser.add_argument("--config", help="追加の設定ファイル")
    parser.add_argument("--recursive", action="store_true",
                        help="サブフォルダーも対象にする")
    parser.add_argument("--full-hash", action="store_true",
                        help="ファイル全体の SHA-256 も計算する（遅い）")
    parser.add_argument("--dry-run", action="store_true",
                        help="列挙するだけ。台帳もファイルも変更しない")
    return parser


def run(args: argparse.Namespace) -> int:
    configure_stdio_utf8()
    try:
        raw = config_module.load_settings_dict(args.config)
        if args.recursive:
            raw["recursive"] = True
        if args.full_hash:
            raw["full_hash"] = True
        if args.source_folder:
            raw["source_path"] = args.source_folder
        settings = config_module.build_settings(raw)
        config_module.verify_userdata()
    except (config_module.ConfigError, config_module.UserDataError) as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except paths.AppRootError as exc:
        print(f"起動エラー: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not settings.source_path:
        print("解析したい動画のフォルダーを指定してください。", file=sys.stderr)
        return EXIT_NO_SOURCE
    source_root = Path(settings.source_path)
    if not source_root.is_dir():
        print(f"フォルダーがありません: {source_root}", file=sys.stderr)
        return EXIT_NO_SOURCE

    run_id = new_run_id()
    with RunLogger(paths.log_dir(), run_id) as logger:
        logger.info(f"対象フォルダー : {source_root}")
        with db_module.CatalogDatabase() as database:
            if not args.dry_run:
                with database.transaction():
                    database.start_run(
                        run_id=run_id, source_root=str(source_root.resolve()),
                        started_at=local_now_iso(),
                        worker_count=settings.workers,
                        config_snapshot=settings.config_snapshot(),
                        application_version=APPLICATION_VERSION)

            summary = register_folder(
                source_root, settings, database, logger,
                run_id=run_id, dry_run=args.dry_run)

            if not args.dry_run:
                with database.transaction():
                    database.finish_run(
                        run_id, finished_at=local_now_iso(),
                        status=db_module.STATUS_COMPLETED,
                        files_discovered=summary.discovered,
                        files_processed=summary.registered + summary.moved,
                        files_reused=summary.reused,
                        files_failed=summary.failed)

        logger.info("")
        logger.info(f"登録        : {summary.registered} 本")
        logger.info(f"再利用      : {summary.reused} 本")
        logger.info(f"移動を検出  : {summary.moved} 本")
        logger.info(f"失敗        : {summary.failed} 本")
        if summary.missing:
            logger.info(f"見つからない: {summary.missing} 本")

    return EXIT_OK if not summary.failed else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
