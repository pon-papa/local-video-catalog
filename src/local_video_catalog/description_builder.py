"""最終テキスト（動画 1 本の説明）の材料と組み立て.

**AI の推定を事実へ昇格させない。** これがこのモジュールの一番の役目。

  - material に無いことは書かせない（プロンプトで厳守させる）
  - 人物名・人間関係・学校名・地名・行事名は、明記がない限り書かない
  - 日付は「解釈保留」を残す。読み替えて確定させない
  - AI を使えなかったときは、内容を断定しない定型文にする

文字起こしの材料からは、幻覚の疑いがあるセグメントを外す。
**外すのは材料だけで、保存された本文は消さない。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

TRANSCRIPT_EXCERPT_CHARS = 1200
"""AI へ渡す文字起こしの上限。全文は渡さない（長すぎるため）。"""

AMBIGUOUS_MARK = "解釈保留"
UNKNOWN_PERIOD = "不明"

PROMPT = """あなたは動画の整理を手伝う日本語のアシスタントです。
以下の解析結果だけを material として、動画の説明文を 2 つ書いてください。

【厳守】
- material に無いことを書かない。推測で補わない。
- 人物名・人間関係・学校名・地名・行事名は、material に明記がない限り書かない。
  例: 根拠がなければ「息子の運動会」ではなく「屋外での行事のような様子」と書く。
- 人数・性別・年齢を断定しない。material に無ければ書かない。
- 「AI が解析しました」等のシステムの話は書かない。
- 箇条書きにしない。ファイル名を並べない。

【出力】
次の JSON だけを返してください。前後に説明を付けないでください。
{"content": "...", "youtube": "..."}

- content: 一覧用。簡潔で客観的に 2〜5 文。
- youtube: 動画サイトの概要欄へそのまま貼れる自然な文章。2〜6 文。

--- material ---
"""

SECTION_KEYS: dict[str, str] = {
    "ファイル名：": "file_name",
    "元ファイル：": "source_path",
    "台帳ID：": "catalog_id",
    "記録時期：": "period",
    "再生時間：": "duration",
    "内容：": "content",
    "概要欄用：": "youtube",
    "解析情報：": "analysis",
}
FOOTER_MARKER = "-" * 50

FALLBACK_MARKS = ("内容は確認できていません", "内容を記載できません")
"""AI を使えなかったときの目印。**内容を断定しない。**"""

_DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?")


@dataclass
class RecordingPeriod:
    """記録時期。**確定とは限らない。**"""

    text: str = UNKNOWN_PERIOD
    basis: str = ""
    is_ambiguous: bool = False
    note: str = ""

    def describe(self) -> str:
        if self.is_ambiguous:
            return f"{self.text}（{AMBIGUOUS_MARK}）"
        return self.text


@dataclass
class DescriptionMaterial:
    """説明文を書くための材料。**既存の解析結果だけから作る。**"""

    catalog_id: str = ""
    file_name: str = ""
    source_path: str = ""
    duration_seconds: float | None = None
    period: RecordingPeriod = field(default_factory=RecordingPeriod)
    visual_title: str = ""
    visual_summary: str = ""
    visual_activity: str = ""
    visual_model: str = ""
    transcript_excerpt: str = ""
    transcript_excluded_count: int = 0
    transcript_segment_count: int = 0
    transcript_status: str = ""

    @property
    def transcript_used_count(self) -> int:
        """説明文の材料に実際に使ったセグメント数。"""
        return max(0, self.transcript_segment_count
                   - self.transcript_excluded_count)

    @property
    def has_visual(self) -> bool:
        return bool(self.visual_summary or self.visual_title)

    @property
    def has_speech(self) -> bool:
        return bool(self.transcript_excerpt)


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "不明"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}時間{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


def resolve_recording_period(
    *,
    candidates: list[dict[str, Any]],
    embedded: date | None = None,
    embedded_raw: str = "",
) -> RecordingPeriod:
    """記録時期を決める。**根拠が食い違うなら確定させない。**

    動画に埋め込まれた日時があればそれを優先する。ファイル名や
    フォルダー名の数字は候補どまりで、埋め込み日時と食い違う場合は
    「解釈保留」として残す。読み替えて 1 つに決めない。
    """
    if embedded is not None:
        period = RecordingPeriod(
            text=f"{embedded.year}年{embedded.month}月{embedded.day}日",
            basis="動画に記録された日時")
        conflicting = [
            c for c in candidates
            if c.get("candidate_datetime", "")[:10] != embedded.isoformat()
            and str(c.get("source_type", "")).startswith(("filename", "folder"))
        ]
        if conflicting:
            period.is_ambiguous = True
            period.note = (
                "ファイル名やフォルダー名の日付と食い違うため、"
                "確定していません。")
        return period

    named = [c for c in candidates
             if str(c.get("source_type", "")).startswith(("filename", "folder"))]
    if not named:
        return RecordingPeriod(
            text=UNKNOWN_PERIOD,
            basis="手がかりがありません",
            note="ファイル日時は撮影日時とは限らないため使っていません。")

    values = {str(c.get("candidate_datetime", ""))[:10] for c in named}
    best = max(named, key=lambda c: float(c.get("confidence") or 0.0))
    text = str(best.get("candidate_datetime", ""))[:10]
    try:
        parsed = date.fromisoformat(text)
        formatted = f"{parsed.year}年{parsed.month}月{parsed.day}日"
    except ValueError:
        formatted = text or UNKNOWN_PERIOD

    period = RecordingPeriod(text=formatted, basis="ファイル名・フォルダー名")
    if len(values) > 1:
        period.is_ambiguous = True
        period.note = (f"候補が {len(values)} 件あり、どれか決められません。")
    return period


def build_material_prompt(material: DescriptionMaterial) -> str:
    """AI へ渡す材料を組み立てる。**ここに無いことは書かせない。**"""
    parts = [PROMPT, f"ファイル名: {material.file_name}"]

    period = material.period.describe()
    if material.period.is_ambiguous:
        parts.append(f"記録時期: 不明（手がかりの文字列は {period}）")
    else:
        parts.append(f"記録時期: {period}")

    parts.append(f"再生時間: {format_duration(material.duration_seconds)}")

    if material.visual_title:
        parts.append(f"映像の見出し候補: {material.visual_title}")
    if material.visual_activity:
        parts.append(f"映像の主な動き: {material.visual_activity}")
    if material.visual_summary:
        parts.append(f"映像の概要: {material.visual_summary}")
    else:
        parts.append("映像の概要: （映像解析なし）")

    if material.transcript_excerpt:
        parts.append(f"音声の書き起こし（抜粋）: {material.transcript_excerpt}")
    elif material.transcript_excluded_count:
        # 定型句だけだった。無理に使わず「発話なし」として扱う。
        parts.append("音声: 内容として使える発話は確認できていません。")
    elif material.transcript_status:
        parts.append(f"音声: {material.transcript_status}（発話は確認できていません）")
    else:
        parts.append("音声: （文字起こしなし）")

    return "\n".join(parts)


def fallback_content_text(material: DescriptionMaterial) -> str:
    """AI を使えなかったときの一覧用テキスト。**内容を断定しない。**"""
    pieces = [f"{material.file_name} の記録です。"]
    if material.period.text != UNKNOWN_PERIOD:
        pieces.append(f"記録時期は{material.period.describe()}とみられます。")
    pieces.append(f"再生時間は{format_duration(material.duration_seconds)}です。")
    pieces.append("映像の内容は確認できていません。")
    return "".join(pieces)


def fallback_youtube_text(material: DescriptionMaterial) -> str:
    return (f"{material.file_name}（{format_duration(material.duration_seconds)}）。"
            "解析が完了していないため、内容を記載できません。")


def build_description_text(
    material: DescriptionMaterial,
    *,
    content: str,
    youtube: str,
    generator: str,
    model_id: str = "",
) -> str:
    """保存する最終テキストを組み立てる。

    どのモデルで作ったかを残す。**あとから「これは AI が書いた」と
    分かるようにするため。**
    """
    analysis_parts = []
    if material.has_visual:
        analysis_parts.append("映像解析あり")
    else:
        analysis_parts.append("映像解析なし")
    if material.has_speech:
        analysis_parts.append("文字起こしあり")
    elif material.transcript_excluded_count:
        analysis_parts.append("文字起こしは定型句のみ")
    else:
        analysis_parts.append("文字起こしなし")
    analysis_parts.append(f"生成={generator}")
    if model_id:
        analysis_parts.append(f"モデル={model_id}")

    lines = [
        f"ファイル名：{material.file_name}",
        f"元ファイル：{material.source_path}",
        f"台帳ID：{material.catalog_id}",
        f"記録時期：{material.period.describe()}",
        f"再生時間：{format_duration(material.duration_seconds)}",
        "",
        f"内容：{content}",
        "",
        f"概要欄用：{youtube}",
        "",
        f"解析情報：{' / '.join(analysis_parts)}",
    ]
    if material.period.note:
        lines.append(f"補足：{material.period.note}")
    lines.append(FOOTER_MARKER)
    lines.append("この説明文はローカル AI が解析結果から作成したものです。"
                 "人物・場所・行事などは確認されていません。")
    return "\n".join(lines) + "\n"


def parse_description_text(text: str) -> dict[str, str]:
    """保存済みの最終テキストを読み戻す（HTML カタログが使う）。"""
    found: dict[str, str] = {}
    current: str | None = None
    for line in str(text).splitlines():
        if line.startswith(FOOTER_MARKER):
            break
        matched = False
        for prefix, key in SECTION_KEYS.items():
            if line.startswith(prefix):
                current = key
                found[key] = line[len(prefix):].strip()
                matched = True
                break
        if matched:
            continue
        if current and line.strip():
            found[current] = (found.get(current, "") + line.strip())
    return found


def sort_key_for_period(period_text: str) -> str:
    """並び替え用のキー。

    **「解釈保留」を日付へ読み替えない。** 読めないものは末尾へ置く。
    """
    if not period_text or UNKNOWN_PERIOD in period_text:
        return "9999-99-99"
    if AMBIGUOUS_MARK in period_text:
        return "9999-99-98"
    match = _DATE_PATTERN.search(period_text)
    if not match:
        return "9999-99-99"
    year, month, day = match.group(1), match.group(2), match.group(3) or "00"
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
