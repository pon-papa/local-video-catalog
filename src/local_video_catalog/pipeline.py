"""動画 1 本ごとに 5 工程を順に走らせる（本体の入口）.

**新しい解析ロジックを持たない。** 各工程を呼び、止めどきを判断するだけ。

止めどきは 3 つある。どれも**プロセスを強制終了しない**ので、台帳も
元動画も壊れない。次回は同じ操作で続きから処理する。

  時間予算       稼働時間に達したら、いま動いている工程を終えて止まる
  停止要求       ``userdata/control/stop-request`` が現れたら止まる
  同種障害の連続 同じ設備障害が 3 本続いたら止まる

3 本目で止めるのは、1 本目は個別の動画の問題かもしれず、2 本目でも
偶然が残るが、**成功を 1 本も挟まずに 3 本続けて同じ種類で落ちるのは
設備側の問題**だからである。1 本あたり最大 20 分（視覚概要の待ち時間）
と見ても、無駄は 1 時間で止まる。

GUI から使えるように、工程の実体は差し替えられる形にしてある
（``StageRunners``）。これにより **GUI を起動せず、外部ソフトも無しで
止めどきの判断だけを試験できる。**
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import APPLICATION_VERSION
from . import config as config_module
from . import database as db_module
from . import paths, recycle, register, selection, stage_report
from .logging_utils import RunLogger, configure_stdio_utf8, local_now_iso, new_run_id

EXIT_OK = 0
EXIT_SOME_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_NO_SOURCE = 3

# 止まった理由
STOP_FINISHED = "finished"
STOP_TIME_BUDGET = "time_budget"
STOP_REQUESTED = "stop_requested"
STOP_REPEATED_FAILURE = "repeated_failure"
STOP_MAX_VIDEOS = "max_videos"

# 障害の種類。**同じ種類が続いたときだけ**安全停止の対象にする。
FAILURE_CONNECTION = "connection"
FAILURE_TIMEOUT = "timeout"
FAILURE_MODEL = "model"
FAILURE_PRIVACY = "privacy"
FAILURE_NO_FRAMES = "no_frames"
FAILURE_OTHER = "other"

INFRASTRUCTURE_FAILURES = frozenset({
    FAILURE_CONNECTION, FAILURE_TIMEOUT, FAILURE_MODEL, FAILURE_PRIVACY,
})
"""設備側の問題を示す種類。これが続いたら止める。

``no_frames`` や ``other`` は動画ごとの事情なので、続いても止めない。
"""

INFRASTRUCTURE_STAGES = frozenset({
    db_module.STAGE_VISUAL_ANALYSIS,
    db_module.STAGE_AUDIO_TRANSCRIPTION,
    db_module.STAGE_DESCRIPTION,
})
"""外部のソフト（LM Studio・whisper）に依存する工程。

**連続失敗のカウントは、この工程の結果だけを見る。**
代表画像の抽出は ffmpeg だけで完結するので、それが成功しても
LM Studio が健全である証拠にはならない。すべての工程の成功で
カウンタを戻すと、毎回リセットされてガードが働かなくなる。
"""

FAILURE_MESSAGES: dict[str, str] = {
    FAILURE_CONNECTION: (
        "LM Studio へ接続できません。LM Studio を起動し、"
        "ローカルサーバーを ON にしてください。"),
    # **「制限時間を超えた」を「起動していない」と言わない。**
    FAILURE_TIMEOUT: (
        "LM Studio へはつながっていますが、応答が制限時間を超えました。"),
    FAILURE_MODEL: (
        "指定したモデルが LM Studio に見つかりません。"
        "「ローカルAI設定」で選び直してください。"),
    FAILURE_PRIVACY: (
        "接続先がこのPCの中ではありません。設定を確認してください。"),
    FAILURE_NO_FRAMES: "解析できる代表画像がありませんでした。",
    FAILURE_OTHER: "この動画は次回やり直します。",
}


@dataclass
class StageOutcome:
    """1 工程の結果。

    ``done`` が False なら失敗。``failure_kind`` で「設備側か、
    この動画固有か」を区別する。
    """

    done: bool
    status: str
    failure_kind: str = FAILURE_OTHER
    message: str = ""

    @classmethod
    def ok(cls, status: str = db_module.STATUS_COMPLETED) -> "StageOutcome":
        return cls(done=True, status=status)

    @classmethod
    def failed(cls, kind: str = FAILURE_OTHER, message: str = "") -> "StageOutcome":
        return cls(done=False, status=db_module.STATUS_FAILED,
                   failure_kind=kind,
                   message=message or FAILURE_MESSAGES.get(kind, ""))


class StageRunner(Protocol):
    def __call__(self, asset_id: str, context: "RunContext") -> StageOutcome: ...


@dataclass
class RunContext:
    """1 回の実行を通して使うもの。"""

    settings: config_module.Settings
    database: db_module.CatalogDatabase
    logger: RunLogger
    run_id: str
    raw: dict[str, Any] = field(default_factory=dict)
    deadline: float | None = None

    def time_left(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def out_of_time(self) -> bool:
        left = self.time_left()
        return left is not None and left <= 0


@dataclass
class StageRunners:
    """工程の実体。**差し替えられる。**

    差し替え可能にしてあるのは、止めどきの判断だけを
    外部ソフト無しで試験するため。
    """

    frame_extraction: StageRunner | None = None
    visual_analysis: StageRunner | None = None
    audio_transcription: StageRunner | None = None
    description: StageRunner | None = None

    def for_stage(self, stage: str) -> StageRunner | None:
        return getattr(self, stage, None)


def default_runners() -> StageRunners:
    """本番で使う工程の実体。

    ``stages`` を遅延して読み込むのは、循環参照を避けるため
    （各工程は ``pipeline`` の ``StageOutcome`` を使う）。
    """
    from . import stages

    return StageRunners(
        frame_extraction=stages.run_frame_extraction,
        visual_analysis=stages.run_visual_analysis,
        audio_transcription=stages.run_transcription,
        description=stages.run_description,
    )


@dataclass
class PipelineResult:
    stop_reason: str = STOP_FINISHED
    processed: int = 0
    planned: int = 0
    failures: list[str] = field(default_factory=list)
    repeated_failure_message: str = ""
    cleaned_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason, "processed": self.processed,
            "planned": self.planned, "failures": len(self.failures),
            "cleaned_bytes": self.cleaned_bytes,
        }


def stop_requested() -> bool:
    return paths.stop_request_path().exists()


def request_stop() -> Path:
    """安全停止を要求する。**プロセスは終了させない。**"""
    target = paths.stop_request_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(local_now_iso() + "\n", encoding="utf-8")
    return target


def clear_stop_request() -> None:
    try:
        paths.stop_request_path().unlink(missing_ok=True)
    except OSError:
        pass


class _FailureGuard:
    """同じ種類の設備障害が続いていないか数える。

    **成功を挟んだら 0 に戻す。** 続いたときだけ意味を持つ。
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.count = 0
        self.kind = ""

    def record_success(self, stage: str) -> None:
        """設備を使う工程が成功したときだけカウンタを戻す。"""
        if stage in INFRASTRUCTURE_STAGES:
            self.count = 0
            self.kind = ""

    def record_failure(self, stage: str, kind: str) -> bool:
        """上限に達したら True（＝止めるべき）。"""
        if stage not in INFRASTRUCTURE_STAGES:
            return False
        if kind not in INFRASTRUCTURE_FAILURES:
            # 動画ごとの事情。設備障害として数えない。
            self.count = 0
            self.kind = ""
            return False
        if kind == self.kind:
            self.count += 1
        else:
            self.count = 1
            self.kind = kind
        return self.count >= self.limit


def run_pipeline(
    context: RunContext,
    targets: list[stage_report.AssetProgress],
    runners: StageRunners,
    *,
    skip_stages: frozenset[str] = frozenset(),
    consecutive_failure_limit: int = 3,
    recycle_cache: bool = False,
    plan: "selection.SelectionPlan | None" = None,
) -> PipelineResult:
    """動画ごとに工程を進める。**止めどきの判断はここに集約する。**

    ``plan`` があれば、1 本ごとに「いま何本目を、なぜ処理しているか」を
    書き出す。利用者が経過を追えるようにするため。
    """
    result = PipelineResult(planned=len(targets))
    guard = _FailureGuard(consecutive_failure_limit)

    for target in targets:
        if stop_requested():
            result.stop_reason = STOP_REQUESTED
            break
        if context.out_of_time():
            result.stop_reason = STOP_TIME_BUDGET
            break

        result.processed += 1
        context.logger.info("")
        if plan is not None:
            for line in plan.progress_line(result.processed):
                context.logger.info(line)
        else:
            context.logger.info(
                f"{target.catalog_id}  [{result.processed}/{len(targets)}]  "
                f"{target.file_name}")

        stopped, completed = _run_one(
            target, context, runners, result, guard, skip_stages=skip_stages)
        if stopped:
            result.stop_reason = stopped
            break

        if completed and recycle_cache:
            cleanup = recycle.cleanup_intermediate_cache(target.asset_id)
            if cleanup.ok:
                result.cleaned_bytes += cleanup.freed_bytes
                with context.database.transaction():
                    context.database.record_cache_cleanup(
                        target.asset_id, status=cleanup.status,
                        at=local_now_iso(), freed_bytes=cleanup.freed_bytes)
            else:
                context.logger.warning(
                    f"  中間ファイルの整理に失敗しました: {cleanup.error}")

    clear_stop_request()
    return result


def _run_one(
    target: stage_report.AssetProgress,
    context: RunContext,
    runners: StageRunners,
    result: PipelineResult,
    guard: _FailureGuard,
    *,
    skip_stages: frozenset[str],
) -> tuple[str, bool]:
    """1 本ぶんの工程を進める。``(止めるべき理由 or "", 完了したか)``。"""
    for stage, label in db_module.PIPELINE_STAGES:
        if stage in skip_stages:
            context.logger.info(f"  {label}: 今回は飛ばします")
            continue
        if target.stages.get(stage):
            context.logger.info(f"  {label}: 完了済みのため飛ばします")
            continue
        if stop_requested():
            context.logger.info(f"  {label}: 停止要求のため次回へ回します")
            return (STOP_REQUESTED, False)
        if context.out_of_time():
            context.logger.info(f"  {label}: 稼働時間に達したため次回へ回します")
            return (STOP_TIME_BUDGET, False)

        runner = runners.for_stage(stage)
        if runner is None:
            context.logger.info(f"  {label}: 実行できないため飛ばします")
            continue

        context.logger.info(f"  {label} …")
        started = local_now_iso()
        outcome = runner(target.asset_id, context)

        with context.database.transaction():
            context.database.set_stage_status(
                target.asset_id, stage, outcome.status,
                started_at=started, finished_at=local_now_iso(),
                error_message=outcome.message or None)

        if outcome.done:
            guard.record_success(stage)
            continue

        result.failures.append(f"{target.catalog_id} {label}")
        context.logger.warning(f"    失敗しました。{outcome.message}")
        context.logger.warning("    これまでの成果は保存済みです。"
                               "次回は残りだけ行います。")

        if guard.record_failure(stage, outcome.failure_kind):
            result.repeated_failure_message = outcome.message
            return (STOP_REPEATED_FAILURE, False)
        return ("", False)

    context.logger.info(f"  {target.catalog_id} 完了")
    return ("", True)


def describe_stop(result: PipelineResult, *, limit: int = 3) -> list[str]:
    """終わり方を、人が読める言葉にする。"""
    lines: list[str] = []
    if result.stop_reason == STOP_REPEATED_FAILURE:
        lines.append(f"同じ種類のエラーが {limit} 本続いたため、"
                     "後続の動画を無駄に失敗させないよう安全停止しました。")
        if result.repeated_failure_message:
            lines.append(f"理由: {result.repeated_failure_message}")
        lines.append("原因を直してから、もう一度実行すると続きから処理します。")
    elif result.stop_reason == STOP_REQUESTED:
        lines.append("停止要求により、区切りのよいところで停止しました。")
        lines.append("完了した処理は保存済みです。"
                     "もう一度実行すると続きから処理します。")
    elif result.stop_reason == STOP_TIME_BUDGET:
        lines.append("稼働時間に達したため、区切りのよいところで停止しました。")
        lines.append("完了した処理は保存済みです。"
                     "もう一度実行すると続きから処理します。")
    elif result.stop_reason == STOP_MAX_VIDEOS:
        lines.append("指定した本数を処理し終えました。")
    else:
        lines.append("このフォルダーの処理が終わりました。")

    lines.append(f"処理した動画 : {result.processed} / {result.planned} 本")
    if result.failures:
        lines.append("")
        lines.append(f"うまくいかなかった処理 {len(result.failures)} 件"
                     "（次回やり直します）:")
        lines.extend(f"  {item}" for item in result.failures[:10])
    return lines


# --------------------------------------------------------------------------
# コマンドライン
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_video_catalog.pipeline",
        description="動画フォルダーをまとめて解析する（元動画は読むだけ）")
    parser.add_argument("--source-folder")
    parser.add_argument("--config")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--time-budget-minutes", type=float, default=None,
                        help="稼働時間。0 で制限なし")
    parser.add_argument("--max-videos", type=int, default=None,
                        help="今回あらたに処理する本数。0 で制限なし")
    parser.add_argument("--skip-visual", action="store_true")
    parser.add_argument("--skip-transcription", action="store_true")
    parser.add_argument("--recycle-cache", action="store_true",
                        help="完了した動画の中間ファイルをゴミ箱へ移動する")
    parser.add_argument("--only-catalog-id", action="append", default=[],
                        help="この台帳 ID だけを処理する（失敗分の再試行）")
    parser.add_argument("--dry-run", action="store_true",
                        help="予定を表示するだけ。何も変更しない")
    return parser


def _skip_stages(args: argparse.Namespace, raw: dict[str, Any]) -> frozenset[str]:
    run_section = dict(raw.get("run") or {})
    skip: set[str] = set()
    if args.skip_visual or run_section.get("skip_visual_analysis"):
        skip.add(db_module.STAGE_VISUAL_ANALYSIS)
    if args.skip_transcription or run_section.get("skip_transcription"):
        skip.add(db_module.STAGE_AUDIO_TRANSCRIPTION)
    return frozenset(skip)


def run(args: argparse.Namespace,
        runners: StageRunners | None = None) -> int:
    configure_stdio_utf8()
    try:
        raw = config_module.load_settings_dict(args.config)
        if args.recursive:
            raw["recursive"] = True
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

    if not settings.source_path or not Path(settings.source_path).is_dir():
        print("解析したい動画のフォルダーを指定してください。", file=sys.stderr)
        return EXIT_NO_SOURCE
    source_root = Path(settings.source_path)

    run_section = dict(raw.get("run") or {})
    budget_minutes = (args.time_budget_minutes
                      if args.time_budget_minutes is not None
                      else float(run_section.get("time_budget_minutes", 60)))
    max_videos = (args.max_videos if args.max_videos is not None
                  else int(run_section.get("max_videos", 0)))
    limit = int(run_section.get("consecutive_failure_limit", 3))
    skip = _skip_stages(args, raw)
    recycle_cache = bool(args.recycle_cache or run_section.get("recycle_cache"))

    clear_stop_request()
    run_id = new_run_id()

    with RunLogger(paths.log_dir(), run_id) as logger:
        logger.info("=" * 62)
        logger.info(" 動画をまとめて解析します")
        logger.info("=" * 62)
        logger.info(f"対象フォルダー : {source_root}")
        logger.info(f"稼働時間       : "
                    + (f"{budget_minutes:g} 分" if budget_minutes > 0 else "制限なし"))
        logger.info(f"処理本数       : "
                    + (f"最大 {max_videos} 本" if max_videos > 0 else "制限なし"))
        logger.info(f"中間ファイル   : "
                    + ("完了後にゴミ箱へ移動する" if recycle_cache else "そのまま残す"))

        with db_module.CatalogDatabase() as database:
            context = RunContext(
                settings=settings, database=database, logger=logger,
                run_id=run_id, raw=raw,
                deadline=(time.monotonic() + budget_minutes * 60
                          if budget_minutes > 0 else None))

            logger.info("")
            logger.info("--- 1/5 動画の登録と基本情報 ---")
            summary = register.register_folder(
                source_root, settings, database, logger,
                run_id=run_id, dry_run=args.dry_run)

            report = stage_report.collect(
                database, source_root=source_root, ignored_stages=skip,
                only_catalog_ids=tuple(args.only_catalog_id or ()))
            targets = stage_report.select_pending(report, max_videos=max_videos)

            # **選んだ結果と理由を、利用者にも記録にも残す。**
            # 「329 本あるのに、なぜこの 3 本？」に答えられるようにする。
            plan = selection.build_plan(
                database, source_root=str(source_root), ignored_stages=skip,
                only_catalog_ids=tuple(args.only_catalog_id or ()),
                max_videos=max_videos)

            logger.info("")
            for line in plan.summary_lines():
                logger.info(line)
            for line in plan.detail_lines():
                logger.info(line)
            logger.event("selection_plan", **plan.to_dict())

            if args.dry_run:
                logger.info("")
                logger.info("※ 本数の上限は、映像の解析・文字起こし・説明文まで"
                            "進める動画の数です。")
                logger.info("　 動画ライブラリ全体の確認は毎回行います"
                            "（すでに確認済みの動画はすぐ終わります）。")
                logger.info("")
                logger.info("[予定のみ] 台帳も結果ファイルも変更していません。")
                return EXIT_OK
            if not targets:
                logger.info("")
                logger.info("すべての動画の解析が完了しています。")
                return EXIT_OK

            with database.transaction():
                database.start_run(
                    run_id=run_id, source_root=str(source_root.resolve()),
                    started_at=local_now_iso(), worker_count=settings.workers,
                    config_snapshot=settings.config_snapshot(),
                    application_version=APPLICATION_VERSION)

            result = run_pipeline(
                context, targets, runners or default_runners(),
                skip_stages=skip, consecutive_failure_limit=limit,
                recycle_cache=recycle_cache, plan=plan)

            if max_videos and result.stop_reason == STOP_FINISHED \
                    and result.processed >= max_videos:
                result.stop_reason = STOP_MAX_VIDEOS

            with database.transaction():
                database.finish_run(
                    run_id, finished_at=local_now_iso(),
                    status=(db_module.STATUS_COMPLETED if result.ok
                            else db_module.STATUS_PARTIAL),
                    files_discovered=summary.discovered,
                    files_processed=result.processed,
                    files_failed=len(result.failures),
                    stop_reason=result.stop_reason)

        logger.info("")
        logger.info("=" * 62)
        for line in describe_stop(result, limit=limit):
            logger.info(line)
        logger.info("=" * 62)

    return EXIT_OK if result.ok else EXIT_SOME_FAILED


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
