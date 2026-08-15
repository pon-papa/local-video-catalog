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

import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import config as config_module
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
    def __init__(self) -> None:
        self.state = state_module.load()
        self.task: runner_module.BackgroundTask | None = None

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("900x760")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._apply_state()
        self._set_running(False)

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

        self.skip_visual_var = tk.BooleanVar()
        ttk.Checkbutton(run_box, text="映像の解析を飛ばす（ローカルAIを使わない）",
                        variable=self.skip_visual_var).pack(anchor="w", pady=(6, 0))
        self.skip_asr_var = tk.BooleanVar()
        ttk.Checkbutton(run_box, text="文字起こしを飛ばす",
                        variable=self.skip_asr_var).pack(anchor="w")
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
            ("ローカルAI設定…", self._open_ai_settings),
        ):
            ttk.Button(outputs, text=label, command=command).pack(
                side="left", padx=(0, 6))

    # -- 状態の反映 -------------------------------------------------------

    def _apply_state(self) -> None:
        self.source_var.set(self.state.source_folder)
        self.recursive_var.set(self.state.recursive)
        self.minutes_var.set(self.state.time_budget_minutes)
        self.videos_var.set(self.state.max_videos or 10)
        self.no_time_var.set(self.state.no_time_limit)
        self.no_count_var.set(self.state.no_video_limit)
        self.skip_visual_var.set(self.state.skip_visual_analysis)
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
            skip_visual_analysis=self.skip_visual_var.get(),
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
                       self.button_start, self.button_retry):
            button.configure(state="disabled" if running else "normal")
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
        state = self._collect_state()
        self._clear_log()
        self.status_var.set("環境を確認しています…")
        self.root.update_idletasks()

        arguments = []
        if state.source_folder:
            arguments += ["--source-folder", state.source_folder]
        if state.skip_visual_analysis:
            arguments.append("--skip-visual")
        if state.skip_transcription:
            arguments.append("--skip-transcription")

        result = runner_module.check_environment(arguments)
        for line in result.lines:
            self._append(line)
        if result.exit_code == 0:
            self.status_var.set("環境の確認が終わりました。")
        else:
            self.status_var.set(
                "このままでは処理を開始できません。上の NG を直してください。")

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
        self.status_var.set(
            "対象確認が終わりました。よければ「処理開始」を押してください。")

    def _start(self, extra: list[str] | None = None) -> None:
        if not self._require_source():
            return
        state = self._collect_state()
        self._clear_log()
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
        if self.task.running:
            self.root.after(POLL_INTERVAL_MS, self._poll)
            return

        for line in self.task.drain():
            self._append(line)
        self._set_running(False)
        code = self.task.result.exit_code
        if code == 0:
            self.status_var.set("処理が終わりました。")
        else:
            self.status_var.set(
                "一部の処理が終わりませんでした。ログを確認してください。")

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

        ai_settings.show(self.root, self.state)
        state_module.save(self.state)

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
