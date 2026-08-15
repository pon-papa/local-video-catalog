"""ローカルAI設定のダイアログ.

**設定ファイルを書き換えない。** 画面で選んだモデルは実行時に渡す。
設定ファイルを書き換えると、選び直すたびに元の値が失われる。

**外部インターネットへはつながない。** モデルの一覧は localhost の
LM Studio から取り、文字起こしモデルは ``userdata/models/whisper/`` の
実ファイルだけを候補にする。**自動ダウンロードはしない。**

**候補は LM Studio が実際に返した model id だけ。** ここで存在しない
名前を選べるようにすると、開始できない状態を利用者自身が作れてしまう。

**選ばれていない状態を許す。** 初回は空（未選択）で、そのままでは
解析を開始できない。勝手に既定のモデルを選んだことにしない。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ... import asr_engine
from ... import config as config_module
from ... import environment_check, paths, vlm_client
from ..state import GuiState

TITLE = "ローカルAI設定"

NOT_SELECTED = "（未選択）"
"""空の選択を、目に見える文字として見せる。"""


def label_for(model_id: str) -> str:
    """一覧に出す表示。**推奨かどうかの注記だけを足す。**

    注記は目安であって、これで開始可否を決めない。画像を扱えるかは
    ``vision_probe`` が実際に確かめる。
    """
    if model_id == environment_check.RECOMMENDED_VISUAL_MODEL:
        return f"{model_id}  ← 動作確認済み"
    return model_id


def model_from_label(label: str) -> str:
    """表示から model id へ戻す。"""
    text = str(label).split("  ←", 1)[0].strip()
    return "" if text == NOT_SELECTED else text


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

    ttk.Label(frame, text="映像の解析に使うモデル（必須）"
              ).pack(anchor="w", pady=(10, 2))
    visual_var = tk.StringVar(value=label_for(state.visual_model)
                              if state.visual_model else NOT_SELECTED)
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
              "文字起こしモデルは userdata\\models\\whisper\\ に置いてください。\n"
              "「動作確認済み」以外のモデルでも画像を扱えれば使えますが、"
              "結果の品質は保証されません。"))
    note.pack(anchor="w", pady=(12, 0))

    def reload() -> None:
        models, error = environment_check.list_local_models(
            vlm_settings.base_url)
        if error:
            status.configure(text=error)
            # **存在しないモデルを選べるようにしない。**
            visual_box.configure(values=[NOT_SELECTED])
            description_box.configure(values=[""])
        else:
            status.configure(text=f"LM Studio：接続済み（{len(models)} モデル）")
            visual_box.configure(
                values=[NOT_SELECTED, *(label_for(m) for m in models)])
            description_box.configure(values=["", *models])
            if model_from_label(visual_var.get()) not in models:
                # 前回のモデルが今は無い。**勝手に別のモデルへ変えない。**
                # 未選択へ戻し、利用者に選び直してもらう。
                visual_var.set(NOT_SELECTED)
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
        state.visual_model = model_from_label(visual_var.get())
        state.description_model = description_var.get().strip()
        state.whisper_model = whisper_var.get().strip()
        dialog.destroy()

    ttk.Button(buttons, text="キャンセル",
               command=dialog.destroy).pack(side="right")
    ttk.Button(buttons, text="OK", command=accept).pack(side="right", padx=6)

    reload()
    dialog.wait_window()
