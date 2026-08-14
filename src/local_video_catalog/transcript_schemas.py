"""文字起こし結果の正規化と、幻覚の疑いの検出.

whisper は無音・BGM だけの区間でも、それらしい文を作り出すことがある。
実運用で「ご視聴ありがとうございました」「作詞・作曲・編曲 ○○」といった
定型が繰り返し観測された。

**扱い方が要。**

  - 疑わしいセグメントには印（``is_suspected_hallucination``）を付ける
  - **本文は消さない。** 台帳にも出力ファイルにもそのまま残す
  - 説明文の材料から外すのは後段（description）の仕事であって、ここではない

そして、

  **「同じ文が N 回続いた」だけでは疑い扱いにしない。**

日常や会話の動画では、短い語が本当に繰り返されることがある
（呼びかけ、笑い声、相づち）。反復回数は警告文の材料にとどめ、
除外の条件にはしない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TRANSCRIPT_SCHEMA_VERSION = "transcript-v1"

STATUS_COMPLETED = "completed"
STATUS_NO_SPEECH = "no_speech"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

KNOWN_HALLUCINATION_PHRASES = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "チャンネル登録",
    "最後までご視聴",
    "Thanks for watching",
    "Thank you for watching",
    "Amara.org",
)
"""実測で確認された、無音・非音声に対する既知の幻覚。"""

KNOWN_HALLUCINATION_PATTERNS = (
    # BGM だけの区間で出る「楽曲クレジット風」の幻覚。
    # 家庭用・業務用の動画の発話としては現れない定型。
    re.compile(r"作詞[\s・･,、/]*作曲"),
    re.compile(r"作曲[\s・･,、/]*編曲"),
    re.compile(r"(?:Sub(?:title)?s?|Caption)s?\s+by\b", re.IGNORECASE),
    re.compile(r"ご覧いただき(?:まして)?ありがとう"),
    # 記号だけの行（♪ や 〜 の連続）
    re.compile(r"^\s*(?:♪|～|〜|・){2,}\s*$"),
)

REPETITION_THRESHOLD = 3
"""この回数以上続いたら**警告文に書く**。除外の条件にはしない。"""

END_OVERSHOOT_TOLERANCE_SECONDS = 30.0
"""セグメント終端の許容超過。whisper の転写窓ぶんは許容する。"""


class TranscriptValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Segment:
    """正規化された 1 セグメント。"""

    sequence_index: int
    start_seconds: float
    end_seconds: float
    text: str
    confidence: float | None = None
    is_suspected_hallucination: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_index": self.sequence_index,
            "start_seconds": round(self.start_seconds, 3),
            "end_seconds": round(self.end_seconds, 3),
            "text": self.text,
            "confidence": self.confidence,
            "is_suspected_hallucination": self.is_suspected_hallucination,
        }


@dataclass
class NormalizedChunk:
    """1 チャンクぶんの正規化結果。"""

    segments: list[Segment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = STATUS_COMPLETED

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)

    @property
    def suspected_count(self) -> int:
        return sum(1 for s in self.segments if s.is_suspected_hallucination)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "status": self.status,
            "segment_count": len(self.segments),
            "suspected_hallucination_count": self.suspected_count,
            "warnings": list(self.warnings),
            "segments": [s.to_dict() for s in self.segments],
        }


def looks_like_hallucination(text: str) -> bool:
    """既知の幻覚フレーズ・定型に該当するか。

    **「疑い」であって断定ではない。** 該当しても本文は残す。
    """
    cleaned = str(text or "").strip().strip("。.、,！!？? ")
    if not cleaned:
        return False
    if any(phrase in cleaned for phrase in KNOWN_HALLUCINATION_PHRASES):
        return True
    return any(pattern.search(cleaned) for pattern in KNOWN_HALLUCINATION_PATTERNS)


def detect_repetition(texts: list[str]) -> int:
    """同一テキストの最大連続回数。

    **これを除外の条件にしない。** 本当に繰り返される発話があるため、
    警告文に書くだけにとどめる。
    """
    if not texts:
        return 0
    longest = current = 1
    for previous, item in zip(texts, texts[1:]):
        if previous.strip() and previous.strip() == item.strip():
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def _to_seconds(value: Any) -> float | None:
    """ミリ秒の数値を秒へ。数値でなければ None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def normalize_engine_items(
    items: list[dict[str, Any]],
    *,
    chunk_duration_seconds: float | None = None,
) -> NormalizedChunk:
    """whisper フィルターの JSON Lines を正規化する。

    ``{"start": <ms>, "end": <ms>, "text": "..."}`` の並びを想定する。
    壊れた行は捨てるのではなく警告に残す（黙って減らさない）。
    """
    chunk = NormalizedChunk()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            chunk.warnings.append(f"{index} 番目の項目が辞書ではありません。")
            continue
        start = _to_seconds(item.get("start"))
        end = _to_seconds(item.get("end"))
        text = str(item.get("text") or "").strip()
        if start is None or end is None:
            chunk.warnings.append(f"{index} 番目の時刻を読めません。")
            continue
        if not text:
            continue
        if end < start:
            chunk.warnings.append(
                f"{index} 番目の終了時刻が開始より前です。入れ替えます。")
            start, end = end, start
        if (chunk_duration_seconds is not None
                and end > chunk_duration_seconds + END_OVERSHOOT_TOLERANCE_SECONDS):
            chunk.warnings.append(
                f"{index} 番目の終了時刻がチャンク長を大きく超えています。")

        chunk.segments.append(Segment(
            sequence_index=len(chunk.segments),
            start_seconds=start, end_seconds=end, text=text,
            is_suspected_hallucination=looks_like_hallucination(text)))

    if not chunk.segments:
        chunk.status = STATUS_NO_SPEECH
        return chunk

    repetition = detect_repetition([s.text for s in chunk.segments])
    if repetition >= REPETITION_THRESHOLD:
        # **参考情報。** 削除の理由にはしない。
        chunk.warnings.append(
            f"参考: 同一の文が {repetition} 回連続しています。"
            "実際に繰り返されている可能性もあるため、除外はしていません。")

    suspected = chunk.suspected_count
    if suspected == len(chunk.segments):
        # 全部が既知の定型。無音・BGM だけの区間とみなす。
        chunk.status = STATUS_NO_SPEECH
        chunk.warnings.append(
            "既知の定型のみで構成されているため「発話なし」として扱います。"
            "本文は削除せず残しています。")
    elif suspected:
        chunk.warnings.append(
            f"既知の幻覚フレーズを含むセグメントが {suspected} 件あります。"
            "該当セグメントには印を付けています（本文は残します）。")

    return chunk


def merge_chunks(chunks: list[tuple[float, NormalizedChunk]]) -> NormalizedChunk:
    """チャンクごとの結果を、絶対時刻へ直しながら 1 本へまとめる。

    ``chunks`` は ``(チャンクの絶対開始秒, 正規化結果)`` の並び。
    """
    merged = NormalizedChunk()
    for offset, chunk in chunks:
        merged.warnings.extend(chunk.warnings)
        for segment in chunk.segments:
            merged.segments.append(Segment(
                sequence_index=len(merged.segments),
                start_seconds=segment.start_seconds + offset,
                end_seconds=segment.end_seconds + offset,
                text=segment.text, confidence=segment.confidence,
                is_suspected_hallucination=segment.is_suspected_hallucination))

    if not merged.segments:
        merged.status = STATUS_NO_SPEECH
    elif merged.suspected_count == len(merged.segments):
        merged.status = STATUS_NO_SPEECH
    return merged


def usable_text(segments: list[Segment]) -> tuple[str, int]:
    """**説明文の材料に使ってよい本文**を組み立てる。

    幻覚の疑いがあるセグメントを外す。無音や BGM の区間で作り出された
    文は動画の内容ではないため、AI へ渡す材料には含めない。

    **消すわけではない。** 元の本文もセグメントも印もそのまま残る。
    ここで作るのは「今回渡す材料」だけ。

    Returns:
        (材料に使う本文, 外したセグメント数)
    """
    kept: list[str] = []
    excluded = 0
    for segment in segments:
        if segment.is_suspected_hallucination:
            excluded += 1
            continue
        kept.append(segment.text)
    return ("".join(kept).strip(), excluded)
