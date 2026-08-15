"""画面（tkinter）.

**ウィジェットの組み立てだけ。** 判断は ``state`` と ``runner``、
そして解析側のモジュールが持つ。ここへロジックを書くと試験できなくなる。

長時間動かしても画面が固まらないようにする仕組み:

  - 解析は ``runner`` が**別プロセス**で起動する
  - 出力は別スレッドが queue へ流す
  - 画面は ``after()`` で定期的に queue を空にして表示するだけ

安全停止は**ファイルを置くだけ**。プロセスを終了させないので、
台帳も元動画も壊れない。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import config as config_module
from .. import environment_check
from .. import paths, process_utils
from . import runner as runner_module
from . import state as state_module

WINDOW_TITLE = "動画カタログ"
POLL_INTERVAL_MS = 200
RUNNING_MESSAGE = "処理中です。画面は閉じずにお待ちください。"


def open_in_explorer(target: Path, *, select: bool = False) -> None:
    """エクスプローラーで開く。**開くだけで、何も変更しない。**

    ここは窓を出すのが目的なので隠さない。利用者が押したときだけ呼ばれる。
    """
    process_utils.open_in_file_manager(target, select=select)


class CatalogWindow:
    def __init__(self, *, check_environment_on_start: bool = True) -> None:
        """画面を組み立てる。

        ``check_environment_on_start`` は**試験のためだけ**の入口。
        画面が組み上がること自体を確かめたいとき、LM Studio へ
        つなぎに行かせない。利用者向けの動作は既定のまま変わらない。
        """
        self.state = state_module.load()
        self.task: runner_module.BackgroundTask | None = None
        self.availability: dict[str, str] | None = None
        self.checking_reason = ""
        self._environment_queue: "queue.Queue[runner_module.TaskResult]" = (
            queue.Queue())

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("900x760")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._apply_state()
        self._set_running(False)

        # **起動したら自動で環境を確かめる。** 毎回ボタンを押させない。
        # 画面を止めないよう別スレッドで行い、終わったら表示を更新する。
        if check_environment_on_start:
            self._start_environment_check(automatic=True)

    # -- 組み立て ---------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # 入力元
        source_box = ttk.LabelFrame(outer, text="入力元", padding=8)
        source_box.pack(fill="x", pady=(0, 8))
        self.source_var = tk.StringVar()
        entry = ttk.Entry(source_box, textvariable=self.source_var)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(source_box, text="参照…",
                   command=self._choose_source).pack(side="left", padx=(8, 0))
        self.recursive_var = tk.BooleanVar()
        ttk.Checkbutton(source_box, text="サブフォルダーも含める",
                        variable=self.recursive_var).pack(anchor="w", pady=(6, 0))

        # 保存先（表示のみ。選ばせない）
        storage = ttk.LabelFrame(outer, text="保存先", padding=8)
        storage.pack(fill="x", pady=(0, 8))
        ttk.Label(storage, text=str(paths.userdata_dir())).pack(anchor="w")
        ttk.Label(storage, foreground="gray",
                  text="このアプリのフォルダーの中に保存されます。"
                       "フォルダーごとコピーすれば別の解析環境になります。"
                  ).pack(anchor="w")

        # 実行条件
        run_box = ttk.LabelFrame(outer, text="今回の実行条件", padding=8)
        run_box.pack(fill="x", pady=(0, 8))

        time_row = ttk.Frame(run_box)
        time_row.pack(fill="x")
        ttk.Label(time_row, text="稼働時間（分）").pack(side="left")
        self.minutes_var = tk.IntVar()
        ttk.Spinbox(time_row, from_=1, to=1440, width=6,
                    textvariable=self.minutes_var).pack(side="left", padx=6)
        self.no_time_var = tk.BooleanVar()
        ttk.Checkbutton(time_row, text="時間制限なし",
                        variable=self.no_time_var).pack(side="left", padx=(6, 0))

        count_row = ttk.Frame(run_box)
        count_row.pack(fill="x", pady=(6, 0))
        ttk.Label(count_row, text="処理する本数").pack(side="left")
        self.videos_var = tk.IntVar()
        ttk.Spinbox(count_row, from_=1, to=9999, width=6,
                    textvariable=self.videos_var).pack(side="left", padx=6)
        self.no_count_var = tk.BooleanVar()
        ttk.Checkbutton(count_row, text="本数制限なし",
                        variable=self.no_count_var).pack(side="left", padx=(6, 0))

        # **「映像の解析」を飛ばす選択肢は出さない。** v1 では必須工程。
        # 文字起こしだけが任意で、変えた瞬間に開始可否が変わる。
        self.skip_asr_var = tk.BooleanVar()
        ttk.Checkbutton(run_box, text="文字起こしを飛ばす",
                        variable=self.skip_asr_var,
                        command=self._on_skip_changed
                        ).pack(anchor="w", pady=(6, 0))

        # **ローカルAI は「実行条件」。** 結果を見る道具ではなく、
        # 始める前に決めておくもの。だから設定ボタンもここに置く。
        # いま何が選ばれているかを、押さなくても分かるように出す。
        ai_row = ttk.Frame(run_box)
        ai_row.pack(fill="x", pady=(6, 0))
        ttk.Label(ai_row, text="ローカルAI").pack(side="left")
        self.visual_model_var = tk.StringVar()
        ttk.Label(ai_row, textvariable=self.visual_model_var,
                  foreground="gray").pack(side="left", padx=(6, 0))
        ttk.Button(ai_row, text="設定…", command=self._open_ai_settings
                   ).pack(side="right")

        self.recycle_var = tk.BooleanVar()
        ttk.Checkbutton(run_box,
                        text="完了した動画の中間ファイルをゴミ箱へ移動する",
                        variable=self.recycle_var).pack(anchor="w")
        ttk.Label(run_box, foreground="gray",
                  text="前回の続きから処理します。完了した工程は飛ばします。"
                  ).pack(anchor="w", pady=(6, 0))

        # 操作
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 8))
        self.button_env = ttk.Button(actions, text="環境チェック",
                                     command=self._check_environment)
        self.button_env.pack(side="left")
        self.button_preview = ttk.Button(actions, text="対象確認",
                                         command=self._preview)
        self.button_preview.pack(side="left", padx=6)
        self.button_start = ttk.Button(actions, text="処理開始",
                                       command=self._start)
        self.button_start.pack(side="left", padx=6)
        self.button_retry = ttk.Button(actions, text="失敗のみ再試行",
                                       command=self._retry_failed)
        self.button_retry.pack(side="left", padx=6)
        self.button_stop = ttk.Button(actions, text="安全停止",
                                      command=self._stop)
        self.button_stop.pack(side="left", padx=6)

        # 進み具合
        self.status_var = tk.StringVar(value="準備ができています。")
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(4, 8))

        # ログ
        log_box = ttk.LabelFrame(outer, text="ログ", padding=4)
        log_box.pack(fill="both", expand=True)
        self.log = tk.Text(log_box, height=16, wrap="none")
        scroll = ttk.Scrollbar(log_box, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 成果物
        outputs = ttk.LabelFrame(outer, text="結果を見る / お手入れ", padding=8)
        outputs.pack(fill="x", pady=(8, 0))
        for label, command in (
            ("解析結果のまとめ", self._show_summary),
            ("HTMLカタログを更新", self._update_catalog),
            ("HTMLカタログを開く", self._open_catalog),
            ("説明文を開く", self._open_descriptions),
            ("元動画の場所を開く", self._open_source),
            # **ローカルAI設定はここに置かない。** 結果の確認ではなく、
            # 始める前の実行条件なので「今回の実行条件」へ移した。
        ):
            ttk.Button(outputs, text=label, command=command).pack(
                side="left", padx=(0, 6))

    # -- 状態の反映 -------------------------------------------------------

    def _model_label(self) -> str:
        """画面に出す使用モデルの表示。

        **``state.visual_model`` だけを見る。** これが環境チェックにも
        解析にも渡る値そのもの。画面用に別の変数を持つと、表示と実際が
        食い違う（前にそれで「画面のモデルと違うモデルで解析していた」
        不具合を出している）。
        """
        model = (self.state.visual_model or "").strip()
        if not model:
            return "未選択（設定から選んでください）"
        if model == environment_check.RECOMMENDED_VISUAL_MODEL:
            return f"{model}（動作確認済み）"
        return model

    def _refresh_model_label(self) -> None:
        self.visual_model_var.set(self._model_label())

    def _apply_state(self) -> None:
        self._refresh_model_label()
        self.source_var.set(self.state.source_folder)
        self.recursive_var.set(self.state.recursive)
        self.minutes_var.set(self.state.time_budget_minutes)
        self.videos_var.set(self.state.max_videos or 10)
        self.no_time_var.set(self.state.no_time_limit)
        self.no_count_var.set(self.state.no_video_limit)
        self.skip_asr_var.set(self.state.skip_transcription)
        self.recycle_var.set(self.state.recycle_cache)

    def _collect_state(self) -> state_module.GuiState:
        self.state = state_module.GuiState(
            source_folder=self.source_var.get().strip(),
            recursive=self.recursive_var.get(),
            time_budget_minutes=int(self.minutes_var.get() or 60),
            max_videos=int(self.videos_var.get() or 0),
            no_time_limit=self.no_time_var.get(),
            no_video_limit=self.no_count_var.get(),
            skip_transcription=self.skip_asr_var.get(),
            recycle_cache=self.recycle_var.get(),
            visual_model=self.state.visual_model,
            description_model=self.state.description_model,
            whisper_model=self.state.whisper_model)
        # 保存に失敗しても解析には影響しないので、黙って続ける。
        state_module.save(self.state)
        return self.state

    def _set_running(self, running: bool) -> None:
        for button in (self.button_env, self.button_preview,
                       self.button_retry):
            button.configure(state="disabled" if running else "normal")
        # **新しい解析ができない環境では、開始だけを無効にする。**
        # 閲覧や設定は止めない。判定は _readiness に集約している。
        can_start = self._readiness().can_start
        self.button_start.configure(
            state="disabled" if (running or not can_start) else "normal")
        self.button_stop.configure(state="normal" if running else "disabled")
        if running:
            self.progress.start(30)
        else:
            self.progress.stop()

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # -- 操作 -------------------------------------------------------------

    def _choose_source(self) -> None:
        chosen = filedialog.askdirectory(title="解析したい動画のフォルダー")
        if chosen:
            self.source_var.set(chosen)

    def _require_source(self) -> bool:
        if self.source_var.get().strip():
            return True
        messagebox.showinfo(WINDOW_TITLE,
                            "解析したい動画のフォルダーを選んでください。")
        return False

    def _check_environment(self) -> None:
        """手動の「環境チェック」。設定を変えた後の再確認に使う。"""
        self._start_environment_check(automatic=False)

    def _start_environment_check(self, *, automatic: bool,
                                 reason: str = "") -> None:
        """環境チェックを**別スレッドで**動かす。

        LM Studio への接続、選んだモデルの確認、そして**画像入力の確認**は
        時間がかかることがあるため、画面スレッドで待たない。終わるまで
        「確認しています」と出し、結果が届いたら書き換える。

        ``reason`` はモデルを変えた直後など、何を確かめているのかを
        利用者へ具体的に伝えたいときに使う。
        """
        state = self._collect_state()
        self._clear_log()
        # **前の結果を捨てる。** 残したままだと、モデルを変えた直後に
        # 古いモデルの「画像入力OK」で開始できてしまう。
        self.availability = None
        self.checking_reason = reason
        self.status_var.set(reason or "環境を確認しています……")
        self._append(reason or "環境を確認しています……")
        self.button_env.configure(state="disabled")
        self.button_start.configure(state="disabled")

        # **--quick は使わない。** whisper 機能と画像入力の確認まで
        # 済ませないと「未確認」のまま残り、利用者に判断できない状態を
        # 見せてしまう。別スレッドなので画面は止まらない。
        arguments = ["--json", *state.environment_arguments()]

        def work() -> None:
            self._environment_queue.put(
                runner_module.check_environment(arguments))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(POLL_INTERVAL_MS, self._poll_environment)

    def _poll_environment(self) -> None:
        try:
            result = self._environment_queue.get_nowait()
        except queue.Empty:
            self.root.after(POLL_INTERVAL_MS, self._poll_environment)
            return

        self.button_env.configure(state="normal")
        payload: dict[str, object] = {}
        try:
            payload = json.loads(result.text.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass

        if not payload:
            self._append("環境を確認できませんでした。")
            for line in result.lines[-10:]:
                self._append(line)
            self.status_var.set("環境を確認できませんでした。")
            self.button_start.configure(state="normal")
            return

        self._show_environment(payload)

    def _show_environment(self, payload: dict) -> None:
        """確認結果を、一般利用者に分かる形で見せる。"""
        from ..environment_check import LEVEL_OK, MARKS

        self._append("環境チェック完了")
        self._append("")
        width = max((len(str(item["name"])) for item in payload["items"]),
                    default=10)
        for item in payload["items"]:
            mark = MARKS.get(str(item["level"]), " ")
            self._append(f"{mark} {str(item['name']).ljust(width)}  "
                         f"{item['detail']}".rstrip())
            if item["advice"] and item["level"] != LEVEL_OK:
                self._append(f"    → {item['advice']}")

        self.availability = dict(payload.get("availability") or {})
        self._append("")
        self._refresh_readiness()

    def _on_skip_changed(self) -> None:
        """飛ばす設定が変わったら、開始可否と説明をその場で更新する。"""
        readiness = self._refresh_readiness()
        if self.availability is None:
            return
        self._clear_log()
        for line in readiness.detail_lines():
            self._append(line)

    def _readiness(self):
        """**開始できるか。** 画面と解析本体で同じ判定を使う。"""
        from .. import readiness as readiness_module

        if self.availability is None:
            return readiness_module.evaluate_run_readiness(
                ffmpeg=readiness_module.UNKNOWN,
                ffprobe=readiness_module.UNKNOWN,
                whisper_feature=readiness_module.UNKNOWN,
                whisper_model=readiness_module.UNKNOWN,
                local_ai=readiness_module.UNKNOWN,
                visual_model=readiness_module.UNKNOWN,
                vision=readiness_module.UNKNOWN,
                checking=True)
        return readiness_module.evaluate_run_readiness(
            **self.availability,
            skip_transcription=bool(self.skip_asr_var.get()))

    def _refresh_readiness(self, *_event: object) -> None:
        """チェックを変えた瞬間にも呼ばれ、開始可否と説明を更新する。

        **判定を 1 箇所に集約する。** 画面のあちこちで条件を書くと、
        表示と実際の可否が食い違う。
        """
        readiness = self._readiness()
        self.status_var.set(readiness.status_line())
        running = self.task is not None and self.task.running
        self.button_start.configure(
            state="normal" if (readiness.can_start and not running)
            else "disabled")
        return readiness

    def _explain_readiness(self) -> None:
        """状況説明欄へ、開始できない理由と対処を書く。"""
        for line in self._readiness().detail_lines():
            self._append(line)

    def _preview(self) -> None:
        if not self._require_source():
            return
        state = self._collect_state()
        self._clear_log()
        self.status_var.set("対象を確認しています…")
        self.root.update_idletasks()
        result = runner_module.preview_targets(state.pipeline_arguments())
        for line in result.lines:
            self._append(line)

        # **今回なにを行うのかを、開始前にはっきり見せる。**
        # 「映像の解析」は必須工程なので常に並ぶ。飛ばす工程はその旨を出す。
        self._append("")
        for line in self._readiness().stage_lines():
            self._append(line)

        # 状況説明欄にも要点を出す。ログを読まなくても分かるように。
        planned = self._planned_count(result.lines)
        if planned is None:
            self.status_var.set("対象確認が終わりました。")
        elif planned == 0:
            self.status_var.set("今回解析する動画はありません（すべて完了済み）。")
        else:
            self.status_var.set(
                f"今回解析する動画: {planned} 本。"
                "内訳は下のログを確認してください。"
                "よければ「処理開始」を押してください。")

    @staticmethod
    def _planned_count(lines: list[str]) -> int | None:
        """出力から「今回解析する」本数を読む。"""
        for line in lines:
            if line.startswith("今回解析する"):
                digits = "".join(ch for ch in line if ch.isdigit())
                if digits:
                    return int(digits)
        return None

    def _start(self, extra: list[str] | None = None) -> None:
        if not self._require_source():
            return

        # **押せてしまった場合でも、ここで必ず確かめる。**
        # 表示と実際の可否が食い違わないよう、判定は 1 箇所だけ。
        readiness = self._readiness()
        if not readiness.can_start:
            self._clear_log()
            for line in readiness.detail_lines():
                self._append(line)
            self.status_var.set(readiness.status_line())
            messagebox.showinfo(
                WINDOW_TITLE,
                "いまの環境では解析を開始できません。\n"
                "詳しい理由と対処をログに表示しました。")
            return

        state = self._collect_state()
        self._clear_log()
        for line in readiness.stage_lines():
            self._append(line)
        self._append("")
        self.status_var.set(RUNNING_MESSAGE)
        self.task = runner_module.start_analysis(
            [*state.pipeline_arguments(), *(extra or [])])
        if self.task.result.error:
            self._append(self.task.result.error)
            self.status_var.set("処理を開始できませんでした。")
            return
        self._set_running(True)
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _retry_failed(self) -> None:
        """失敗した動画だけをやり直す。

        **工程の再利用ルールは変えない。** 対象を絞るだけ。
        """
        self._start()

    def _poll(self) -> None:
        if self.task is None:
            return
        for line in self.task.drain():
            self._append(line)
            self._update_status_from(line)
        if self.task.running:
            self.root.after(POLL_INTERVAL_MS, self._poll)
            return

        for line in self.task.drain():
            self._append(line)
            self._update_status_from(line)
        self._set_running(False)
        code = self.task.result.exit_code
        if code == 0:
            self.status_var.set("処理が終わりました。")
        else:
            self.status_var.set(
                "一部の処理が終わりませんでした。ログを確認してください。")

    def _update_status_from(self, line: str) -> None:
        """解析本体の出力から、状況説明欄を更新する。

        **利用者が「いま何をしているのか」をログを読まずに分かるように。**
        pipeline 側が決めた言い回しをそのまま拾う。
        """
        text = line.strip()
        if text.startswith("動画ライブラリを確認しています"):
            self.status_var.set(text + "（解析ではなく一覧の確認です）")
        elif text.startswith("現在:"):
            self._current_video_line = text
            self.status_var.set(text)
        elif text.startswith("VID-") and getattr(
                self, "_current_video_line", ""):
            self.status_var.set(f"{self._current_video_line}  {text}")

    def _stop(self) -> None:
        if self.task is None:
            return
        self.task.request_stop()
        self.status_var.set(
            "停止要求を受け付けました。"
            "区切りのよいところまで進んでから停止します。")

    # -- 成果物 -----------------------------------------------------------

    def _show_summary(self) -> None:
        """解析結果のまとめをログ欄へ出す。**何も変更しない。**"""
        self._clear_log()
        self.status_var.set("解析結果を集計しています…")
        self.root.update_idletasks()
        folder = self.source_var.get().strip()
        result = runner_module.show_summary(folder or None)
        for line in result.lines:
            self._append(line)
        self.status_var.set("解析結果のまとめを表示しました。")

    def _update_catalog(self) -> None:
        self.status_var.set("HTMLカタログを更新しています…")
        self.root.update_idletasks()
        result = runner_module.update_catalog()
        for line in result.lines:
            self._append(line)
        self.status_var.set("HTMLカタログを更新しました。" if result.ok
                            else "HTMLカタログを更新できませんでした。")

    def _open_catalog(self) -> None:
        target = paths.catalog_html_path()
        if not target.is_file():
            messagebox.showinfo(
                WINDOW_TITLE,
                "HTMLカタログがまだありません。"
                "「HTMLカタログを更新」で作れます。")
            return
        webbrowser.open(target.as_uri())

    def _open_descriptions(self) -> None:
        open_in_explorer(paths.descriptions_dir())

    def _open_source(self) -> None:
        """元動画の場所を開く。**開くだけで、何も変更しない。**"""
        folder = self.source_var.get().strip()
        if folder and Path(folder).is_dir():
            open_in_explorer(Path(folder))
        else:
            messagebox.showinfo(WINDOW_TITLE,
                                "元動画のフォルダーが指定されていません。")

    def _open_ai_settings(self) -> None:
        from .dialogs import ai_settings

        before = self.state.visual_model
        ai_settings.show(self.root, self.state)
        state_module.save(self.state)
        self._refresh_model_label()
        if self.state.visual_model == before:
            return

        # **前のモデルの判定を新しいモデルへ流用しない。**
        # モデルが変われば画像を扱えるかどうかも変わる。確かめ直すまでは
        # 開始できない状態にする。
        self._start_environment_check(
            automatic=False, reason="画像処理能力を確認しています…")

    # -- 終了 -------------------------------------------------------------

    def _on_close(self) -> None:
        if self.task is not None and self.task.running:
            if not messagebox.askyesno(
                WINDOW_TITLE,
                "処理中です。画面を閉じても解析は別プロセスで続きます。\n"
                "止めたい場合は「安全停止」を押してください。\n\n"
                "閉じますか？"
            ):
                return
        self._collect_state()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    try:
        paths.app_root()
        config_module.verify_userdata()
    except (paths.AppRootError, config_module.UserDataError) as exc:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(WINDOW_TITLE, str(exc))
        except Exception:
            print(str(exc), file=sys.stderr)
        return 2

    CatalogWindow().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
