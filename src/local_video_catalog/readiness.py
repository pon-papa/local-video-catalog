"""いま解析を始められるか、始められないなら何をすればよいか.

**画面と解析本体で判定を分けない。** ここ 1 箇所で決める。
分けると「ボタンは押せるのに必ず失敗する」状態が生まれる。

v1 の考え方:

    **映像の解析はこのツールの本体なので必須。**
    **文字起こしは補助情報なので任意。**

工程ごとの依存関係（**実装を読んで確認したもの**）:

    登録・基本情報   ffprobe
    代表画像の抽出   ffmpeg
    映像の解析       LM Studio ＋ 映像解析モデル      ← **必須**
    文字起こし       ffmpeg ＋ whisper 機能 ＋ モデル ← 飛ばせる
    説明文の作成     LM Studio があれば使う
    HTMLカタログ     説明文ファイルだけ

映像の解析を飛ばす選択肢は画面に出さない。飛ばすと説明文が定型文だけに
なり、このツールを使う意味がほとんど残らないため。

**文字起こしは LM Studio を使わない。** 以前「LM Studio 未接続」のときに
「文字起こしもできません」と表示していたのは誤りだった。

可用性は 3 値で扱う。

    利用可能   確かめて、使えた
    利用不可   確かめて、使えなかった
    未確認     まだ確かめていない（**「使えない」ではない**）

**未確認を「利用不可」として扱わない。** 確認前に「できません」と
言い切ると、実際には動く環境で開始を止めてしまう。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"
CHECKING = "checking"

# 飛ばす設定の名前（画面のチェックと対応）
SKIP_TRANSCRIPTION = "skip_transcription"

SKIP_LABELS = {
    SKIP_TRANSCRIPTION: "文字起こしを飛ばす",
}


@dataclass
class Blocker:
    """開始できない理由と、その解き方。"""

    problem: str
    remedy: str
    skip_option: str = ""

    @property
    def skip_label(self) -> str:
        return SKIP_LABELS.get(self.skip_option, "")

    def lines(self) -> list[str]:
        found = [f"✕ {self.problem}", f"  {self.remedy}"]
        if self.skip_option:
            found.append(f"  または、「{self.skip_label}」にチェックを入れると、"
                         "この工程を行わずに進められます。")
        return found

    def to_dict(self) -> dict[str, str]:
        return {"problem": self.problem, "remedy": self.remedy,
                "skip_option": self.skip_option}


@dataclass
class RunReadiness:
    """開始できるか。できないなら何が足りないか。"""

    can_start: bool = False
    checking: bool = False
    blockers: list[Blocker] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    performed: list[str] = field(default_factory=list)

    def status_line(self) -> str:
        """状況説明欄の 1 行目。"""
        if self.checking:
            return "環境を確認しています……"
        if not self.can_start:
            return "現在は解析を開始できません。"
        if self.warnings:
            return "解析を開始できます（一部の工程は行いません）。"
        return "解析を開始できます。"

    def detail_lines(self) -> list[str]:
        """**なぜ開始できないのか、どうすれば開始できるのか。**"""
        if self.checking:
            return ["環境の確認が終わるまでお待ちください。"]

        lines: list[str] = []
        if not self.can_start:
            lines.append("現在は解析を開始できません。")
            lines.append("")
            for blocker in self.blockers:
                lines.extend(blocker.lines())
                lines.append("")
        else:
            lines.extend(self.stage_lines())
            lines.append("")
            lines.append("✓ 解析を開始できます。")

        lines.append("動画ライブラリの確認、HTMLカタログの閲覧、"
                     "説明文の確認、設定の変更はいつでもできます。")
        return lines

    def stage_lines(self) -> list[str]:
        """**今回なにを行うのか。** 行う工程と、行わない工程を並べる。"""
        lines = ["今回行う工程:"]
        for stage in self.performed:
            lines.append(f"  ✓ {stage}")
        for warning in self.warnings:
            lines.append(f"  – {warning}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_start": self.can_start, "checking": self.checking,
            "blockers": [b.to_dict() for b in self.blockers],
            "warnings": list(self.warnings),
            "performed": list(self.performed),
        }


def evaluate_run_readiness(
    *,
    ffmpeg: str,
    ffprobe: str,
    whisper_feature: str,
    whisper_model: str,
    local_ai: str,
    visual_model: str,
    skip_transcription: bool = False,
    checking: bool = False,
) -> RunReadiness:
    """開始できるかを決める。**画面も解析本体もここを使う。**

    考え方:

      - **確かめて駄目だったもの**だけを、開始できない理由にする。
        未確認は理由にしない（実際には動くかもしれない）。
      - 文字起こしを「飛ばす」と利用者が明示したなら、理由から外す。
      - **映像の解析は飛ばせない。** v1 ではこれが本体である。
    """
    readiness = RunReadiness(checking=checking)
    if checking:
        return readiness

    # --- 飛ばせない土台 -------------------------------------------------
    if ffprobe == UNAVAILABLE:
        readiness.blockers.append(Blocker(
            problem="ffprobe を利用できません。",
            remedy="動画の基本情報を読むために必要です。"
                   "「参照」から ffprobe.exe の場所を指定してください。"))
    if ffmpeg == UNAVAILABLE:
        readiness.blockers.append(Blocker(
            problem="ffmpeg を利用できません。",
            remedy="代表画像の抽出に必要です。"
                   "「参照」から ffmpeg.exe の場所を指定してください。"))

    # --- 映像の解析（**飛ばせない**）---------------------------------------
    if local_ai == UNAVAILABLE:
        readiness.blockers.append(Blocker(
            problem="ローカルAIに接続できません。",
            remedy="映像の解析には LM Studio が必要です。"
                   "LM Studio を起動し、ローカルサーバーを ON にしてください。"))
    elif visual_model == UNAVAILABLE:
        readiness.blockers.append(Blocker(
            problem="映像解析に使うモデルが見つかりません。",
            remedy="「ローカルAI設定」でモデルを選び直してください。"))
    else:
        readiness.performed.append("映像の解析")

    # --- 文字起こし（LM Studio は関係しない）-----------------------------
    if skip_transcription:
        readiness.warnings.append(
            "文字起こしは行いません（「文字起こしを飛ばす」がオンです）。")
    else:
        if whisper_feature == UNAVAILABLE:
            readiness.blockers.append(Blocker(
                problem="文字起こし機能を利用できません。",
                remedy="この ffmpeg には whisper 機能が入っていません。"
                       "whisper 対応の ffmpeg（8.x 以降のフル版）を"
                       "指定してください。",
                skip_option=SKIP_TRANSCRIPTION))
        elif whisper_model == UNAVAILABLE:
            readiness.blockers.append(Blocker(
                problem="文字起こしに使うモデルがありません。",
                remedy="userdata\\models\\whisper\\ へ ggml-*.bin を"
                       "置いてください。",
                skip_option=SKIP_TRANSCRIPTION))
        else:
            readiness.performed.append("文字起こし")

    # --- 説明文 -----------------------------------------------------------
    #
    # 映像の解析を通った時点でローカルAIは使えているので、通常は
    # そのまま説明文まで到達する。**定型文への切り替えは異常時の保険**
    # であって、利用者が選ぶ動作モードではない。
    readiness.performed.insert(0, "動画ライブラリの確認")
    readiness.performed.insert(1, "代表画像の抽出")
    readiness.performed.append("説明文の作成")

    readiness.can_start = not readiness.blockers
    return readiness


def availability_from_level(level: str, *, ok_level: str, warn_level: str
                            ) -> str:
    """環境チェックの 3 段階を、3 値の可用性へ直す。

    **注意 = 未確認**として扱う。「確かめて駄目だった」わけではないため、
    それだけで開始を止めない。
    """
    if level == ok_level:
        return AVAILABLE
    if level == warn_level:
        return UNKNOWN
    return UNAVAILABLE
