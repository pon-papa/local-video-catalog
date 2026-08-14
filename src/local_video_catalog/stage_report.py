"""進み具合の集計と、今回処理する動画の選択.

**Resume の判定はここに集約する。** 台帳の ``stage_status`` だけを見て、
「この動画にまだやることが残っているか」を決める。マニフェストファイルや
ファイルの有無に頼らない。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import database as db_module
from . import paths
from .logging_utils import configure_stdio_utf8

EXIT_OK = 0
EXIT_ERROR = 1


@dataclass
class AssetProgress:
    asset_id: str
    catalog_id: str
    file_name: str
    source_relative: str
    is_available: bool
    stages: dict[str, bool] = field(default_factory=dict)

    def pending(self, ignored: frozenset[str]) -> bool:
        """まだやることが残っているか。

        今回飛ばす工程は「やることあり」に数えない。飛ばす設定のまま
        いつまでも未完了と表示され続けるのを避けるため。
        """
        if not self.is_available:
            return False
        return any(not done for stage, done in self.stages.items()
                   if stage not in ignored)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "asset_id": self.asset_id,
            "catalog_id": self.catalog_id,
            "file_name": self.file_name,
            "source_relative": self.source_relative,
            "is_available": int(self.is_available),
        }
        for stage, done in self.stages.items():
            payload[stage] = int(done)
        return payload


@dataclass
class Report:
    items: list[AssetProgress] = field(default_factory=list)
    ignored_stages: frozenset[str] = frozenset()

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def pending(self) -> list[AssetProgress]:
        return [item for item in self.items if item.pending(self.ignored_stages)]

    @property
    def unavailable(self) -> list[AssetProgress]:
        return [item for item in self.items if not item.is_available]

    def stage_counts(self) -> list[tuple[str, str, int]]:
        counts = []
        for stage, label in db_module.PIPELINE_STAGES:
            done = sum(1 for item in self.items if item.stages.get(stage))
            counts.append((stage, label, done))
        return counts


def collect(
    database: db_module.CatalogDatabase,
    *,
    source_root: Path | str | None = None,
    ignored_stages: frozenset[str] = frozenset(),
    only_catalog_ids: tuple[str, ...] = (),
) -> Report:
    """台帳から進み具合を集める。**何も変更しない。**"""
    if source_root is not None:
        rows = database.list_assets_under(source_root)
    else:
        rows = list(database.connection.execute(
            "SELECT * FROM assets ORDER BY catalog_id").fetchall())

    if only_catalog_ids:
        wanted = set(only_catalog_ids)
        rows = [row for row in rows
                if row["catalog_id"] in wanted or row["asset_id"] in wanted]

    report = Report(ignored_stages=ignored_stages)
    for row in rows:
        stages = {
            stage: database.is_stage_done(row["asset_id"], stage)
            for stage, _label in db_module.PIPELINE_STAGES
        }
        report.items.append(AssetProgress(
            asset_id=row["asset_id"], catalog_id=row["catalog_id"],
            file_name=row["file_name"],
            source_relative=row["source_relative"],
            is_available=bool(row["is_available"]), stages=stages))
    return report


def select_pending(report: Report, *, max_videos: int = 0) -> list[AssetProgress]:
    """今回処理する動画を選ぶ。

    **完了済みは含めない。** ``max_videos`` は「あらたに着手する本数」の
    上限であり、完了済みは数に入らない。
    """
    pending = report.pending
    if max_videos and max_videos > 0:
        return pending[:max_videos]
    return pending


def format_summary(report: Report, *, found_count: int | None = None) -> list[str]:
    lines: list[str] = []
    if found_count is not None:
        lines.append(f"見つかった動画 : {found_count} 本")
    lines.append(f"登録済み       : {report.total} 本")
    for stage, label, done in report.stage_counts():
        note = "（今回は飛ばします）" if stage in report.ignored_stages else ""
        lines.append(f"  {label:<12} {done} / {report.total} 本 {note}".rstrip())
    lines.append(f"今回の処理予定 : {len(report.pending)} 本")
    if report.unavailable:
        lines.append(f"見つからない   : {len(report.unavailable)} 本"
                     "（台帳には残しています）")
    return lines


# --------------------------------------------------------------------------
# コマンドライン
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_video_catalog.stage_report",
        description="進み具合を表示する（読み取り専用）")
    parser.add_argument("--source-folder")
    parser.add_argument("--ignore-stage", action="append", default=[],
                        help="今回飛ばす工程（複数指定可）")
    parser.add_argument("--only-catalog-id", action="append", default=[],
                        help="この台帳 ID だけを対象にする（失敗分の再試行）")
    parser.add_argument("--max-videos", type=int, default=0)
    parser.add_argument("--pending-only", action="store_true")
    parser.add_argument("--found-count", type=int, default=None)
    parser.add_argument("--format", choices=("summary", "tsv", "json"),
                        default="summary")
    return parser


def run(args: argparse.Namespace) -> int:
    configure_stdio_utf8()
    try:
        with db_module.CatalogDatabase() as database:
            report = collect(
                database,
                source_root=args.source_folder,
                ignored_stages=frozenset(args.ignore_stage or ()),
                only_catalog_ids=tuple(args.only_catalog_id or ()))
    except (paths.AppRootError, OSError) as exc:
        print(f"台帳を読めません: {exc}", file=sys.stderr)
        return EXIT_ERROR

    items = (select_pending(report, max_videos=args.max_videos)
             if args.pending_only else report.items)

    if args.format == "summary":
        for line in format_summary(report, found_count=args.found_count):
            print(line)
    elif args.format == "json":
        print(json.dumps([item.to_dict() for item in items], ensure_ascii=False))
    else:
        writer = csv.DictWriter(
            sys.stdout, delimiter="\t", lineterminator="\n",
            fieldnames=list(items[0].to_dict().keys()) if items else
            ["asset_id", "catalog_id", "file_name", "source_relative",
             "is_available"])
        writer.writeheader()
        for item in items:
            writer.writerow(item.to_dict())
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
