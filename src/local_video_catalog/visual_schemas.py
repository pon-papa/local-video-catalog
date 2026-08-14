"""映像解析の指示文と、応答の検証.

**モデルの返答をそのまま信じない。** 実運用では次が起きた:

  - 存在しないフレーム番号を参照する
  - JSON の型が違う（数値のはずが文字列、配列のはずが文字列）
  - JSON が途中で切れる
  - 画像を渡しているのに「画像が提供されていません」と答える
  - 根拠のない人物関係・性別・年齢を断定する

したがって、受け取った JSON は必ずここで検証してから台帳へ入れる。
**壊れた応答を「解析できた」ことにしない。**
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

FRAME_PROMPT_VERSION = "frame-v1"
SUMMARY_PROMPT_VERSION = "summary-v1"

FRAME_PROMPT = """あなたは動画の整理を手伝う日本語のアシスタントです。
これは動画から取り出した静止画 1 枚です。**見えるものだけ**を書いてください。

【厳守】
- 見えないことを書かない。推測で補わない。
- 人物名・人間関係・年齢・性別・国籍を断定しない。
  「人が 2 人」のように、見たままの数と様子だけを書く。
- 場所・行事・日付を断定しない。「屋内」「屋外」程度にとどめる。
- 画像が読み取れない場合は caption を空文字にし、readable を false にする。

【出力】
次の JSON だけを返してください。前後に説明を付けないでください。
{"caption": "...", "setting": "...", "readable": true}

- caption: 見えるものの説明。1〜3 文。
- setting: "屋内" / "屋外" / "不明" のいずれか。
- readable: 画像を読み取れたかどうか。
"""

SUMMARY_PROMPT = """あなたは動画の整理を手伝う日本語のアシスタントです。
以下は 1 本の動画から取り出した静止画それぞれの説明です。
これだけを material として、動画全体の概要をまとめてください。

【厳守】
- material に無いことを書かない。推測で補わない。
- 人物名・人間関係・年齢・性別・学校名・地名・行事名を断定しない。
- 存在しないフレーム番号に言及しない。
- 「AI が解析しました」等のシステムの話は書かない。

【出力】
次の JSON だけを返してください。前後に説明を付けないでください。
{"title_candidate": "...", "visual_summary": "...", "main_activity": "..."}

- title_candidate: 短い見出し候補。20 文字程度。
- visual_summary: 全体の様子。2〜5 文。
- main_activity: 主な動き。1 文。

--- material ---
"""

SETTING_VALUES = frozenset({"屋内", "屋外", "不明"})

ERROR_INVALID_JSON = "invalid_json"
ERROR_SCHEMA = "schema_validation_error"
ERROR_UNREADABLE = "model_reported_unreadable"
ERROR_PHANTOM_FRAME = "phantom_frame_reference"

_FRAME_REFERENCE = re.compile(r"(?:フレーム|コマ|frame)\s*#?(\d{1,3})",
                              re.IGNORECASE)


class SchemaError(Exception):
    """応答が期待した形でない。**「解析できた」ことにしない。**"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def extract_json_object(text: str) -> dict[str, Any]:
    """返答から JSON を取り出す。

    前後に説明を付けるモデルがあるため、最初の ``{`` から最後の ``}``
    までを試す。**途中で切れた JSON は例外にする。**
    """
    stripped = str(text or "").strip()
    if not stripped:
        raise SchemaError(ERROR_INVALID_JSON, "返答が空です。")

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise SchemaError(ERROR_INVALID_JSON,
                          "返答に JSON が含まれていません。")
    try:
        parsed = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError as exc:
        raise SchemaError(ERROR_INVALID_JSON,
                          f"JSON を解釈できません（途中で切れている可能性）: {exc}"
                          ) from None
    if not isinstance(parsed, dict):
        raise SchemaError(ERROR_SCHEMA, "JSON のトップレベルが辞書ではありません。")
    return parsed


def _as_text(value: Any, field_name: str) -> str:
    """文字列として受け取る。**型が違えば黙って直さず失敗させる。**"""
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (int, float, list, dict)):
        raise SchemaError(ERROR_SCHEMA,
                          f"{field_name} が文字列ではありません（{type(value).__name__}）。")
    return str(value).strip()


@dataclass
class FrameAnalysis:
    caption: str = ""
    setting: str = "不明"
    readable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"caption": self.caption, "setting": self.setting,
                "readable": self.readable}


def parse_frame_analysis(text: str) -> FrameAnalysis:
    """1 枚ぶんの応答を検証して返す。"""
    payload = extract_json_object(text)

    readable = payload.get("readable", True)
    if not isinstance(readable, bool):
        raise SchemaError(ERROR_SCHEMA, "readable が真偽値ではありません。")

    caption = _as_text(payload.get("caption"), "caption")
    setting = _as_text(payload.get("setting"), "setting") or "不明"
    if setting not in SETTING_VALUES:
        setting = "不明"

    if not readable:
        # 画像を渡しているのに読み取れないと言われた。
        # **成功として扱わない。**
        raise SchemaError(ERROR_UNREADABLE,
                          "モデルが画像を読み取れないと回答しました。")
    if not caption:
        raise SchemaError(ERROR_SCHEMA, "caption が空です。")

    return FrameAnalysis(caption=caption, setting=setting, readable=True)


@dataclass
class VisualSummary:
    title_candidate: str = ""
    visual_summary: str = ""
    main_activity: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title_candidate": self.title_candidate,
            "visual_summary": self.visual_summary,
            "main_activity": self.main_activity,
            "warnings": list(self.warnings),
        }


def parse_visual_summary(text: str, *, frame_count: int) -> VisualSummary:
    """全体の概要を検証して返す。

    ``frame_count`` を渡すのは、**存在しないフレーム番号への言及**を
    見つけるため。実運用で観測された誤りのひとつ。
    """
    payload = extract_json_object(text)

    summary = VisualSummary(
        title_candidate=_as_text(payload.get("title_candidate"),
                                 "title_candidate"),
        visual_summary=_as_text(payload.get("visual_summary"),
                                "visual_summary"),
        main_activity=_as_text(payload.get("main_activity"), "main_activity"))

    if not summary.visual_summary:
        raise SchemaError(ERROR_SCHEMA, "visual_summary が空です。")

    phantom = [
        int(match.group(1))
        for match in _FRAME_REFERENCE.finditer(summary.visual_summary)
        if not (1 <= int(match.group(1)) <= frame_count)
    ]
    if phantom:
        raise SchemaError(
            ERROR_PHANTOM_FRAME,
            f"存在しないフレーム番号を参照しています: {sorted(set(phantom))}"
            f"（実際は {frame_count} 枚）。")

    return summary


def build_summary_material(captions: list[str]) -> str:
    """フレームごとの説明を、概要生成の材料へ組み立てる。"""
    lines = [SUMMARY_PROMPT]
    for index, caption in enumerate(captions, start=1):
        lines.append(f"フレーム{index}: {caption}")
    return "\n".join(lines)
