"""いま解析を始められるか、始められないなら何をすればよいか.

**画面と解析本体で判定を分けない。** ここ 1 箇所で決める。
分けると「ボタンは押せるのに必ず失敗する」状態が生まれる。

工程ごとの依存関係（**実装を読んで確認したもの**）:

    登録・基本情報   ffprobe
    代表画像の抽出   ffmpeg
    映像の解析       LM Studio ＋ 映像解析モデル
    文字起こし       ffmpeg ＋ whisper 機能 ＋ 文字起こしモデル
    説明文の作成     LM Studio があれば使う。**無くても定型文で完了する**
    HTMLカタログ     説明文ファイルだけ

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
SKIP_VISUAL = "skip_visual_analysis"
SKIP_TRANSCRIPTION = "skip_transcription"

SKIP_LABELS = {
    SKIP_VISUAL: "映像の解析を飛ばす",
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
            if self.performed:
                lines.append("今回行う工程: " + " / ".join(self.performed))
            for warning in self.warnings:
                lines.append(f"– {warning}")
            if lines:
                lines.append("")
            lines.append("✓ 解析を開始できます。")

        lines.append("動画ライブラリの確認、HTMLカタログの閲覧、"
                     "説明文の確認、設定の変更はいつでもできます。")
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
    skip_visual: bool = False,
    skip_transcription: bool = False,
    checking: bool = False,
) -> RunReadiness:
    """開始できるかを決める。**画面も解析本体もここを使う。**

    考え方:

      - **確かめて駄目だったもの**だけを、開始できない理由にする。
        未確認は理由にしない（実際には動くかもしれない）。
      - その工程を「飛ばす」と利用者が明示したなら、理由から外す。
      - 説明文は LM Studio が無くても定型文で完了するので、
        開始できない理由にはせず、**そうなると伝える**。
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

    # --- 映像の解析 -------------------------------------------------------
    if skip_visual:
        readiness.warnings.append(
            "映像の解析は行いません（「映像の解析を飛ばす」がオンです）。")
    else:
        if local_ai == UNAVAILABLE:
            readiness.blockers.append(Blocker(
                problem="ローカルAIに接続できません。",
                remedy="映像の解析を行うには LM Studio を起動して、"
                       "ローカルサーバーを ON にしてください。",
                skip_option=SKIP_VISUAL))
        elif visual_model == UNAVAILABLE:
            readiness.blockers.append(Blocker(
                problem="映像解析に使うモデルが見つかりません。",
                remedy="「ローカルAI設定」でモデルを選び直してください。",
                skip_option=SKIP_VISUAL))
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

    # --- 説明文（LM Studio が無くても完了する）---------------------------
    #
    # **開始できない理由にしない。** ただし内容が定型文になることは伝える。
    if local_ai != AVAILABLE and not skip_visual:
        pass          # 上で既に理由として挙げている
    elif local_ai != AVAILABLE:
        readiness.warnings.append(
            "ローカルAIを使えないため、説明文は決まった文面になります"
            "（映像の内容は書かれません）。")

    readiness.performed.insert(0, "動画の登録")
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
