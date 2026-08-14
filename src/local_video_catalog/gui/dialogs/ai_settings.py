"""ローカルAI設定のダイアログ.

**設定ファイルを書き換えない。** 画面で選んだモデルは実行時に渡す。
設定ファイルを書き換えると、選び直すたびに元の値が失われる。

**外部インターネットへはつながない。** モデルの一覧は localhost の
LM Studio から取り、文字起こしモデルは ``userdata/models/whisper/`` の
実ファイルだけを候補にする。**自動ダウンロードはしない。**
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ... import asr_engine
from ... import config as config_module
from ... import environment_check, paths, vlm_client
from ..state import GuiState

TITLE = "ローカルAI設定"


def available_whisper_models() -> list[str]:
    """``userdata/models/whisper/`` にある使えるモデル。"""
    folder = paths.whisper_models_dir()
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() == ".bin":
            usable, _reason = asr_engine.check_model(
                asr_engine.AsrConfig(model_name=path.name))
            if usable:
                found.append(path.name)
    return found


def show(parent: tk.Misc, state: GuiState) -> None:
    """ダイアログを開く。OK なら ``state`` を書き換える。"""
    raw = config_module.load_settings_dict()
    vlm_settings = vlm_client.VlmSettings.from_settings(raw)

    dialog = tk.Toplevel(parent)
    dialog.title(TITLE)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.geometry("620x360")

    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill="both", expand=True)

    status = ttk.Label(frame, text="モデル一覧を読み込んでいます…")
    status.pack(anchor="w")

    ttk.Label(frame, text="映像の解析に使うモデル").pack(anchor="w", pady=(10, 2))
    visual_var = tk.StringVar(value=state.visual_model or vlm_settings.model_match)
    visual_box = ttk.Combobox(frame, textvariable=visual_var, state="readonly")
    visual_box.pack(fill="x")

    ttk.Label(frame, text="説明文を書くモデル（空なら映像解析と同じ）"
              ).pack(anchor="w", pady=(10, 2))
    description_var = tk.StringVar(value=state.description_model)
    description_box = ttk.Combobox(frame, textvariable=description_var,
                                   state="readonly")
    description_box.pack(fill="x")

    ttk.Label(frame, text="文字起こしに使うモデル").pack(anchor="w", pady=(10, 2))
    whisper_var = tk.StringVar(
        value=state.whisper_model or asr_engine.AsrConfig().model_name)
    whisper_box = ttk.Combobox(frame, textvariable=whisper_var, state="readonly")
    whisper_box.pack(fill="x")

    note = ttk.Label(
        frame, foreground="gray", justify="left",
        text=("接続先はこのPCの中（127.0.0.1）だけです。\n"
              "モデルの自動ダウンロードは行いません。\n"
              "文字起こしモデルは userdata\\models\\whisper\\ に置いてください。"))
    note.pack(anchor="w", pady=(12, 0))

    def reload() -> None:
        models, error = environment_check.list_local_models(
            vlm_settings.base_url)
        if error:
            status.configure(text=error)
            visual_box.configure(values=[visual_var.get()])
            description_box.configure(values=["", visual_var.get()])
        else:
            status.configure(text=f"LM Studio：接続済み（{len(models)} モデル）")
            visual_box.configure(values=models)
            description_box.configure(values=["", *models])
        whisper_models = available_whisper_models()
        whisper_box.configure(values=whisper_models or [whisper_var.get()])
        if not whisper_models:
            status.configure(
                text=status.cget("text")
                + " / 文字起こしモデルが見つかりません")

    buttons = ttk.Frame(frame)
    buttons.pack(side="bottom", fill="x", pady=(16, 0))
    ttk.Button(buttons, text="モデル一覧を再読込", command=reload).pack(side="left")

    def accept() -> None:
        state.visual_model = visual_var.get().strip()
        state.description_model = description_var.get().strip()
        state.whisper_model = whisper_var.get().strip()
        dialog.destroy()

    ttk.Button(buttons, text="キャンセル",
               command=dialog.destroy).pack(side="right")
    ttk.Button(buttons, text="OK", command=accept).pack(side="right", padx=6)

    reload()
    dialog.wait_window()
