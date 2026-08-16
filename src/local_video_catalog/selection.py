"""今回どの動画を解析するかを決め、**その理由を言葉にする**.

**選び方そのものは ``stage_report`` が持つ。** ここはそれを説明する層。

利用者から見て一番分からないのが「329 本あるのに、なぜこの 3 本？」
という点だった。規則は決まっているのに、外から見えなかった。

現在の規則（実装を確認したうえで、そのまま言葉にしたもの）:

    1. 台帳に登録済みで、まだ手が残っている動画だけを対象にする
    2. 台帳 ID（VID-000001 …）の小さい順に並べる
    3. 上限の本数だけ先頭から取る

台帳 ID は列挙した順に発行され、列挙はフォルダー名・ファイル名の
昇順で行う。**同じフォルダー・同じ台帳なら、何度実行しても同じ順になる。**

**表示順とは別物。** HTML カタログの「古い順」などは見せ方の都合で、
処理の順番とは関係しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import database as db_module
from . import stage_report

RULE_NORMAL = "catalog_id_order"
RULE_RETRY = "failed_only"

RULE_DESCRIPTIONS = {
    RULE_NORMAL: "まだ解析が終わっていない動画を、台帳ID順に上から",
    RULE_RETRY: "前回うまくいかなかった動画だけ",
}

# 選択理由
REASON_NEW = "new"
REASON_RESUME = "resume"
REASON_RETRY = "retry"

_STAGE_LABELS = dict(db_module.PIPELINE_STAGES)


@dataclass
class SelectedVideo:
    """今回処理する動画 1 本と、その理由。"""

    catalog_id: str
    file_name: str
    asset_id: str
    reason: str = REASON_NEW
    done_stages: list[str] = field(default_factory=list)
    next_stage: str = ""
    failed_stages: list[str] = field(default_factory=list)

    def describe_reason(self) -> str:
        """利用者向けの一文。**内部用語を出さない。**"""
        if self.reason == REASON_RETRY:
            if self.failed_stages:
                labels = "・".join(_STAGE_LABELS.get(s, s)
                                   for s in self.failed_stages)
                return f"前回「{labels}」でうまくいかなかったため、やり直します"
            return "前回うまくいかなかったため、やり直します"

        if self.reason == REASON_RESUME:
            done = "・".join(_STAGE_LABELS.get(s, s) for s in self.done_stages)
            following = _STAGE_LABELS.get(self.next_stage, self.next_stage)
            return f"前回「{done}」まで終わっているので、「{following}」から続けます"

        return "まだ手をつけていない動画のうち、台帳IDが小さい順で選ばれました"

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id, "file_name": self.file_name,
            "asset_id": self.asset_id, "reason": self.reason,
            "done_stages": list(self.done_stages),
            "next_stage": self.next_stage,
            "failed_stages": list(self.failed_stages),
        }


@dataclass
class SelectionPlan:
    """今回の選択の全体像。"""

    library_total: int = 0
    outstanding_total: int = 0
    limit: int = 0
    rule: str = RULE_NORMAL
    videos: list[SelectedVideo] = field(default_factory=list)
    unavailable_total: int = 0
    time_budget_minutes: float = 0.0
    """今回の稼働時間（0 は制限なし）。**表示の言葉を選ぶためだけに使う。**

    本数の上限が無いとき、候補は残り全部になる。それを「今回解析する
    319 本」と書くと、実際には時間で止まるので嘘になる。
    """

    DETAIL_LIMIT = 10
    """一覧に並べる本数。**全部並べると画面が埋まって読めない。**

    実運用で 319 本が展開され、その後の進行表示が見えなくなった。
    上限を指定しているとき（数本）は、そのまま全部出す。
    """

    @property
    def rule_text(self) -> str:
        return RULE_DESCRIPTIONS.get(self.rule, self.rule)

    def to_dict(self) -> dict[str, Any]:
        return {
            "library_total": self.library_total,
            "outstanding_total": self.outstanding_total,
            "limit": self.limit,
            "rule": self.rule,
            "rule_text": self.rule_text,
            "selected_count": len(self.videos),
            "time_budget_minutes": self.time_budget_minutes,
            "videos": [video.to_dict() for video in self.videos],
        }

    # -- 表示 --------------------------------------------------------------

    def summary_lines(self) -> list[str]:
        """「今回なにをするか」の要約。"""
        lines = [
            f"動画ライブラリ : {self.library_total} 本",
            f"未処理         : {self.outstanding_total} 本",
        ]
        if self.limit > 0:
            lines.append(f"処理本数       : 最大 {self.limit} 本")
            lines.append(f"今回解析する   : {len(self.videos)} 本")
        else:
            # **「今回解析する 319 本」と言い切らない。**
            # 本数の上限が無ければ、実際に何本進むかは時間しだい。
            lines.append("処理本数       : 制限なし")
            lines.append(f"解析候補       : {len(self.videos)} 本")
        if self.time_budget_minutes > 0:
            lines.append(f"稼働時間       : {self.time_budget_minutes:g} 分")
        lines.append(f"選び方         : {self.rule_text}")
        if self.unavailable_total:
            lines.append(f"見つからない   : {self.unavailable_total} 本"
                         "（台帳には残しています）")
        return lines

    def detail_lines(self) -> list[str]:
        """選ばれた動画と、その理由。

        **多いときは先頭だけ。** 全部並べると、そのあとの進行表示が
        画面から押し出されて何も追えなくなる（実運用で 319 本が
        展開された）。構造化ログ側には全件を残すので、記録は減らない。
        """
        if not self.videos:
            return ["今回解析する動画はありません。"]

        shown = self.videos
        if self.limit <= 0 and len(self.videos) > self.DETAIL_LIMIT:
            shown = self.videos[:self.DETAIL_LIMIT]

        lines = ["", "今回の対象:"]
        for index, video in enumerate(shown, start=1):
            lines.append(f"  {index}. {video.catalog_id}  {video.file_name}")
            lines.append(f"     理由: {video.describe_reason()}")
        remaining = len(self.videos) - len(shown)
        if remaining > 0:
            lines.append(f"  ほか {remaining} 本（台帳ID順に続きます）")
        return lines

    def progress_line(self, index: int) -> list[str]:
        """いま何本目を、なぜ処理しているか。"""
        if not (1 <= index <= len(self.videos)):
            return []
        video = self.videos[index - 1]
        return [
            f"現在: {index} / {len(self.videos)} 本目",
            f"  {video.catalog_id}  {video.file_name}",
            f"  {video.describe_reason()}",
        ]


def _classify(item: stage_report.AssetProgress, *, retry: bool,
              database: db_module.CatalogDatabase,
              ignored: frozenset[str]) -> SelectedVideo:
    """1 本ぶんの選択理由を、台帳の状態から決める。"""
    done = [stage for stage, _label in db_module.PIPELINE_STAGES
            if item.stages.get(stage) and stage not in ignored]
    remaining = [stage for stage, _label in db_module.PIPELINE_STAGES
                 if not item.stages.get(stage) and stage not in ignored]

    failed = []
    for stage, _label in db_module.PIPELINE_STAGES:
        row = database.get_stage_status(item.asset_id, stage)
        if row is not None and row["status"] in (
                db_module.STATUS_FAILED, db_module.STATUS_PARTIAL):
            failed.append(stage)

    if retry:
        reason = REASON_RETRY
    elif done:
        reason = REASON_RESUME
    else:
        reason = REASON_NEW

    return SelectedVideo(
        catalog_id=item.catalog_id, file_name=item.file_name,
        asset_id=item.asset_id, reason=reason, done_stages=done,
        next_stage=remaining[0] if remaining else "", failed_stages=failed)


def build_plan(
    database: db_module.CatalogDatabase,
    *,
    source_root: str | None,
    ignored_stages: frozenset[str] = frozenset(),
    only_catalog_ids: tuple[str, ...] = (),
    max_videos: int = 0,
    time_budget_minutes: float = 0.0,
) -> SelectionPlan:
    """今回の選択を組み立てる。**台帳を変更しない。**

    ここで返した ``videos`` が、そのまま処理される。
    **表示用に別の並べ方をしない**（表示と実処理が食い違わないため）。
    """
    library = stage_report.collect(database, source_root=source_root,
                                   ignored_stages=ignored_stages)
    report = stage_report.collect(database, source_root=source_root,
                                  ignored_stages=ignored_stages,
                                  only_catalog_ids=only_catalog_ids)
    chosen = stage_report.select_pending(report, max_videos=max_videos)
    retry = bool(only_catalog_ids)

    return SelectionPlan(
        library_total=library.total,
        outstanding_total=len(library.pending),
        limit=max_videos,
        rule=RULE_RETRY if retry else RULE_NORMAL,
        unavailable_total=len(library.unavailable),
        time_budget_minutes=time_budget_minutes,
        videos=[_classify(item, retry=retry, database=database,
                          ignored=ignored_stages) for item in chosen])
