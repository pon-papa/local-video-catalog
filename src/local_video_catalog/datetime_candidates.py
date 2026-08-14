"""撮影日時の**候補**を集める.

**候補であって確定ではない。** ファイル名やフォルダー名の数字列が
撮影日時とは限らない（連番・型番・解像度のこともある）。だから

  - 候補は削除せず、根拠（source_type / source_value / parser_rule）
    とともに残す
  - 確からしさを confidence で表す
  - **確定は利用者の確認でのみ行う**（``is_user_confirmed``）

ファイルシステムの作成日時・更新日時は最も低い信頼度で扱う。
コピーや変換で簡単に書き換わり、撮影日時とは無関係になりうるため。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

SOURCE_METADATA_CREATION_TIME = "metadata_creation_time"
SOURCE_FILENAME_14DIGIT = "filename_14digit"
SOURCE_FILENAME_8DIGIT = "filename_8digit"
SOURCE_FOLDER_NAME = "folder_name"
SOURCE_FILESYSTEM_CREATION_TIME = "filesystem_creation_time"
SOURCE_FILESYSTEM_LAST_WRITE_TIME = "filesystem_last_write_time"

CONFIDENCE_BY_SOURCE: dict[str, float] = {
    SOURCE_METADATA_CREATION_TIME: 0.90,
    SOURCE_FILENAME_14DIGIT: 0.85,
    SOURCE_FILENAME_8DIGIT: 0.60,
    SOURCE_FOLDER_NAME: 0.50,
    # ファイル日時はコピー・変換で書き換わる。撮影日時の根拠としては弱い。
    SOURCE_FILESYSTEM_CREATION_TIME: 0.15,
    SOURCE_FILESYSTEM_LAST_WRITE_TIME: 0.15,
}

MINIMUM_YEAR = 1950
MAXIMUM_YEAR = 2100
"""この範囲外の年は、日付ではない数字列とみなす。"""

RE_14DIGIT = re.compile(r"(?<!\d)(\d{14})(?!\d)")
RE_8DIGIT_SEP_6DIGIT = re.compile(r"(?<!\d)(\d{8})[-_ tT](\d{6})(?!\d)")
RE_8DIGIT = re.compile(r"(?<!\d)(\d{8})(?!\d)")
RE_DASHED_DATE = re.compile(r"(?<!\d)(\d{4})[-_./](\d{1,2})[-_./](\d{1,2})(?!\d)")


@dataclass(frozen=True)
class CaptureTimeCandidate:
    candidate_datetime: str
    source_type: str
    source_value: str
    parser_rule: str
    confidence: float
    has_time: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_datetime": self.candidate_datetime,
            "source_type": self.source_type,
            "source_value": self.source_value,
            "parser_rule": self.parser_rule,
            "confidence": self.confidence,
            "has_time": self.has_time,
        }


def _valid_date(year: int, month: int, day: int) -> bool:
    if not (MINIMUM_YEAR <= year <= MAXIMUM_YEAR):
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _valid_time(hour: int, minute: int, second: int) -> bool:
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def parse_8digit(text: str) -> str | None:
    """``20090815`` → ``2009-08-15``。日付として不正なら None。"""
    if len(text) != 8 or not text.isdigit():
        return None
    year, month, day = int(text[:4]), int(text[4:6]), int(text[6:8])
    if not _valid_date(year, month, day):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_6digit_time(text: str) -> str | None:
    """``143005`` → ``14:30:05``。時刻として不正なら None。"""
    if len(text) != 6 or not text.isdigit():
        return None
    hour, minute, second = int(text[:2]), int(text[2:4]), int(text[4:6])
    if not _valid_time(hour, minute, second):
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_14digit(text: str) -> str | None:
    """``20090815143005`` → ``2009-08-15T14:30:05``。"""
    if len(text) != 14 or not text.isdigit():
        return None
    day_part = parse_8digit(text[:8])
    time_part = parse_6digit_time(text[8:])
    if day_part is None or time_part is None:
        return None
    return f"{day_part}T{time_part}"


def parse_metadata_creation_time(value: str) -> tuple[str, bool] | None:
    """ffprobe の ``creation_time`` タグを解釈する。

    Returns:
        (ISO 文字列, 時刻を含むか)。解釈できなければ None。
    """
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if not (MINIMUM_YEAR <= parsed.year <= MAXIMUM_YEAR):
        return None
    return (parsed.replace(tzinfo=None).isoformat(timespec="seconds"), True)


def extract_from_name(name: str, *, is_folder: bool) -> list[CaptureTimeCandidate]:
    """ファイル名・フォルダー名から日付らしき並びを拾う。

    **拾えたことは、それが撮影日時である証明ではない。** 根拠を残し、
    信頼度を下げて渡す。判断は後段と利用者に委ねる。
    """
    found: list[CaptureTimeCandidate] = []
    seen: set[tuple[str, str]] = set()

    def add(candidate: CaptureTimeCandidate) -> None:
        key = (candidate.candidate_datetime, candidate.parser_rule)
        if key not in seen:
            seen.add(key)
            found.append(candidate)

    text = str(name)

    for match in RE_14DIGIT.finditer(text):
        parsed = parse_14digit(match.group(1))
        if parsed:
            source = SOURCE_FOLDER_NAME if is_folder else SOURCE_FILENAME_14DIGIT
            add(CaptureTimeCandidate(
                candidate_datetime=parsed, source_type=source,
                source_value=match.group(1), parser_rule="14digit",
                confidence=CONFIDENCE_BY_SOURCE[source], has_time=True))

    for match in RE_8DIGIT_SEP_6DIGIT.finditer(text):
        parsed = parse_14digit(match.group(1) + match.group(2))
        if parsed:
            source = SOURCE_FOLDER_NAME if is_folder else SOURCE_FILENAME_14DIGIT
            add(CaptureTimeCandidate(
                candidate_datetime=parsed, source_type=source,
                source_value=match.group(0), parser_rule="8digit_sep_6digit",
                confidence=CONFIDENCE_BY_SOURCE[source], has_time=True))

    for match in RE_8DIGIT.finditer(text):
        parsed = parse_8digit(match.group(1))
        if parsed:
            source = SOURCE_FOLDER_NAME if is_folder else SOURCE_FILENAME_8DIGIT
            add(CaptureTimeCandidate(
                candidate_datetime=parsed, source_type=source,
                source_value=match.group(1), parser_rule="8digit",
                confidence=CONFIDENCE_BY_SOURCE[source], has_time=False))

    for match in RE_DASHED_DATE.finditer(text):
        year, month, day = (int(match.group(1)), int(match.group(2)),
                            int(match.group(3)))
        if _valid_date(year, month, day):
            source = SOURCE_FOLDER_NAME if is_folder else SOURCE_FILENAME_8DIGIT
            add(CaptureTimeCandidate(
                candidate_datetime=f"{year:04d}-{month:02d}-{day:02d}",
                source_type=source, source_value=match.group(0),
                parser_rule="dashed_date",
                confidence=CONFIDENCE_BY_SOURCE[source], has_time=False))

    return found


def collect_candidates(
    *,
    file_name: str,
    folder_names: list[str],
    creation_time_tag: str | None = None,
    filesystem_creation_time: str | None = None,
    filesystem_last_write_time: str | None = None,
) -> list[CaptureTimeCandidate]:
    """1 本ぶんの候補をすべて集める。**どれも確定ではない。**"""
    candidates: list[CaptureTimeCandidate] = []

    if creation_time_tag:
        parsed = parse_metadata_creation_time(creation_time_tag)
        if parsed:
            value, has_time = parsed
            candidates.append(CaptureTimeCandidate(
                candidate_datetime=value,
                source_type=SOURCE_METADATA_CREATION_TIME,
                source_value=str(creation_time_tag),
                parser_rule="iso8601",
                confidence=CONFIDENCE_BY_SOURCE[SOURCE_METADATA_CREATION_TIME],
                has_time=has_time))

    candidates.extend(extract_from_name(file_name, is_folder=False))
    for folder in folder_names:
        candidates.extend(extract_from_name(folder, is_folder=True))

    for value, source in (
        (filesystem_creation_time, SOURCE_FILESYSTEM_CREATION_TIME),
        (filesystem_last_write_time, SOURCE_FILESYSTEM_LAST_WRITE_TIME),
    ):
        if not value:
            continue
        parsed = parse_metadata_creation_time(value)
        if parsed:
            candidates.append(CaptureTimeCandidate(
                candidate_datetime=parsed[0], source_type=source,
                source_value=str(value), parser_rule="filesystem",
                confidence=CONFIDENCE_BY_SOURCE[source], has_time=True))

    return candidates


def best_candidate(
    candidates: list[CaptureTimeCandidate],
) -> CaptureTimeCandidate | None:
    """最も確からしい候補。**同点なら決めない（None を返さず先頭を返す）。**

    ここで返すのはあくまで表示・並び替えの都合であり、確定ではない。
    """
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.confidence, c.has_time))


def metadata_candidates(candidates: list[CaptureTimeCandidate]
                        ) -> list[CaptureTimeCandidate]:
    """動画に埋め込まれた日時だけを取り出す。

    ファイル日時はコピーや変換で書き換わるため、内部日時として扱わない。
    """
    return [c for c in candidates
            if c.source_type == SOURCE_METADATA_CREATION_TIME]
