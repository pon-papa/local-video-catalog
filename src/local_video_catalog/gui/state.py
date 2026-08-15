"""画面の状態（前回の入力・選択）の保存と復元.

**tkinter を import しない。** 画面を起動せずに検証できるようにするため。

保存先は ``userdata/config/gui-state.json``。旧個人版は
``%LOCALAPPDATA%\\FamilyVideoCatalog\\gui-settings.json`` に書いていたが、
One-Folder 原則により APP_ROOT の外へは何も書かない。

**保存に失敗しても解析には影響しない。** 黙って続ける。
次回の既定値が前回のままにならないだけで、処理は正しく動く。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths


@dataclass
class GuiState:
    """画面のいまの状態。**解析結果には影響しない。**"""

    source_folder: str = ""
    recursive: bool = False
    time_budget_minutes: int = 60
    max_videos: int = 0
    no_time_limit: bool = False
    no_video_limit: bool = True
    # **「映像の解析」を飛ばす設定は持たない。** v1 では必須工程であり、
    # 画面に選択肢を出さない。古い設定に残っていても from_dict が捨てる。
    skip_transcription: bool = False
    recycle_cache: bool = False
    visual_model: str = ""
    description_model: str = ""
    whisper_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GuiState":
        """知らないキーは捨て、欠けたキーは既定値で補う。

        古い版で保存された状態を読んでも落ちないようにする。
        """
        known = {f for f in cls().to_dict()}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        state = cls()
        for key, value in clean.items():
            current = getattr(state, key)
            try:
                if isinstance(current, bool):
                    setattr(state, key, bool(value))
                elif isinstance(current, int):
                    setattr(state, key, int(value))
                else:
                    setattr(state, key, str(value))
            except (TypeError, ValueError):
                continue          # 壊れた値は既定のまま
        return state

    # -- 実行条件へ変換 ---------------------------------------------------

    def effective_time_budget(self) -> float:
        """実際に渡す稼働時間（分）。0 は制限なし。"""
        return 0.0 if self.no_time_limit else float(max(0, self.time_budget_minutes))

    def effective_max_videos(self) -> int:
        """実際に渡す本数上限。0 は制限なし。"""
        return 0 if self.no_video_limit else max(0, self.max_videos)

    def pipeline_arguments(self) -> list[str]:
        """``pipeline`` へ渡す引数。

        **保存先は渡さない。** APP_ROOT から導出されるため、画面から
        指定させない（指定できると One-Folder 原則が崩れる）。
        """
        args: list[str] = []
        if self.source_folder:
            args += ["--source-folder", self.source_folder]
        if self.recursive:
            args.append("--recursive")
        args += ["--time-budget-minutes", f"{self.effective_time_budget():g}"]
        args += ["--max-videos", str(self.effective_max_videos())]
        # **画面で選んだモデルを必ず渡す。** 渡さないと設定ファイルの
        # 既定が使われ、画面の表示と実際に使うモデルが食い違う。
        args += self.model_arguments()
        if self.description_model.strip():
            args += ["--description-model", self.description_model.strip()]
        if self.skip_transcription:
            args.append("--skip-transcription")
        if self.recycle_cache:
            args.append("--recycle-cache")
        return args

    def model_arguments(self) -> list[str]:
        """使うモデルの指定。**環境チェックと解析の両方へ同じものを渡す。**

        映像解析モデルは空文字でも渡す。空＝**未選択**という意味があり、
        「指定なし（設定ファイルのまま）」と区別する必要があるため。
        """
        args = ["--visual-model", self.visual_model.strip()]
        if self.whisper_model.strip():
            args += ["--whisper-model", self.whisper_model.strip()]
        return args

    def environment_arguments(self) -> list[str]:
        """環境チェックへ渡す引数。**開始判定と同じモデルで確かめる。**"""
        args: list[str] = []
        if self.source_folder:
            args += ["--source-folder", self.source_folder]
        args += self.model_arguments()
        if self.skip_transcription:
            args.append("--skip-transcription")
        return args


def load(path: Path | None = None) -> GuiState:
    """保存済みの状態を読む。読めなければ既定値（**エラーにしない**）。"""
    target = Path(path) if path else paths.gui_state_path()
    if not target.is_file():
        return GuiState()
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return GuiState()
    if not isinstance(data, dict):
        return GuiState()
    return GuiState.from_dict(data)


def save(state: GuiState, path: Path | None = None) -> bool:
    """状態を**原子的に**保存する。成功したかを返す。

    失敗しても呼び出し側は続行してよい。次回の既定値が前回のままに
    ならないだけで、解析そのものには影響しない。
    """
    target = Path(path) if path else paths.gui_state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        temp.replace(target)
    except OSError:
        return False
    return True
