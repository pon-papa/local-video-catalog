"""説明文から HTML カタログを作る（完全ローカル・派生物）.

``userdata/descriptions/VID-*.txt`` を読み、1 枚の
``userdata/catalog/catalog.html`` を書き出す。ブラウザーで開くだけで、
数百本の解析結果を検索・絞り込み・並び替えできる。

方針:

  - **正本は元動画・台帳・説明文 txt。** この HTML は派生物で、
    消えても説明文からいつでも作り直せる。
  - **完全ローカル。** 外部 CDN・フォント・スクリプト・API を使わない。
    CSS と JavaScript は HTML の中へ埋め込む。**通信は一切しない。**
  - **勝手に解釈しない。** 説明文に「解釈保留」と書いてあるものを
    HTML 生成時に日付へ読み替えない。並び順では末尾へ置く。
  - **必ずエスケープする。** ファイル名や本文に ``< & "`` が含まれていても、
    HTML 構造も JavaScript も壊れない。
  - 画像は使わない（代表画像は説明文の作成後に片付ける設計のため）。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import APPLICATION_VERSION
from . import description_builder as builder
from . import paths
from .logging_utils import configure_stdio_utf8, local_now_iso

EXIT_OK = 0
EXIT_ERROR = 1

STATUS_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "すべて"),
    ("done", "完了"),
    ("date-unknown", "日付不明"),
    ("date-ambiguous", "日付は解釈保留"),
    ("no-speech", "音声なし"),
    ("no-visual", "映像解析なし"),
    ("template", "定型文"),
)

STATUS_BADGES: dict[str, str] = {
    "date-unknown": "日付不明",
    "date-ambiguous": "解釈保留",
    "no-speech": "音声なし",
    "no-visual": "映像解析なし",
    "template": "定型文",
}


@dataclass
class CatalogRecord:
    """カタログ 1 件。**説明文 txt から読めたものがすべて。**"""

    catalog_id: str = ""
    file_name: str = ""
    source_path: str = ""
    period: str = ""
    duration: str = ""
    content: str = ""
    youtube: str = ""
    analysis: str = ""
    statuses: set[str] = field(default_factory=set)

    @property
    def sort_period(self) -> str:
        return builder.sort_key_for_period(self.period)

    SEARCHABLE = ("catalog_id", "file_name", "period", "content", "youtube")

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id, "file_name": self.file_name,
            "source_path": self.source_path, "period": self.period,
            "duration": self.duration, "content": self.content,
            "youtube": self.youtube, "analysis": self.analysis,
            "statuses": sorted(self.statuses),
        }


def read_record(path: Path) -> CatalogRecord | None:
    """説明文 1 件を読む。読めなければ None（黙って壊れた行を出さない）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = builder.parse_description_text(text)
    if not parsed:
        return None

    record = CatalogRecord(
        catalog_id=parsed.get("catalog_id", ""),
        file_name=parsed.get("file_name", path.stem),
        source_path=parsed.get("source_path", ""),
        period=parsed.get("period", ""),
        duration=parsed.get("duration", ""),
        content=parsed.get("content", ""),
        youtube=parsed.get("youtube", ""),
        analysis=parsed.get("analysis", ""))

    if not record.period or builder.UNKNOWN_PERIOD in record.period:
        record.statuses.add("date-unknown")
    if builder.AMBIGUOUS_MARK in record.period:
        record.statuses.add("date-ambiguous")
    if "文字起こしなし" in record.analysis or "定型句のみ" in record.analysis:
        record.statuses.add("no-speech")
    if "映像解析なし" in record.analysis:
        record.statuses.add("no-visual")
    if any(mark in record.content for mark in builder.FALLBACK_MARKS):
        record.statuses.add("template")
    if not record.statuses:
        record.statuses.add("done")
    return record


def collect_records(directory: Path | None = None) -> list[CatalogRecord]:
    source = Path(directory) if directory else paths.descriptions_dir()
    if not source.is_dir():
        return []
    records = []
    for path in sorted(source.glob("*.txt")):
        record = read_record(path)
        if record is not None:
            records.append(record)
    return records


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 1.5rem;
       font-family: "Segoe UI", "Yu Gothic UI", system-ui, sans-serif;
       line-height: 1.7; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
.meta { opacity: .7; font-size: .85rem; margin-bottom: 1rem; }
.controls { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
            margin-bottom: 1rem; }
input[type=search] { flex: 1 1 18rem; padding: .5rem .75rem; font-size: 1rem;
                     border: 1px solid rgba(128,128,128,.5); border-radius: .4rem; }
select, button { padding: .45rem .6rem; font-size: .95rem;
                 border: 1px solid rgba(128,128,128,.5); border-radius: .4rem;
                 background: transparent; color: inherit; }
.count { opacity: .7; font-size: .9rem; }
.card { border: 1px solid rgba(128,128,128,.35); border-radius: .6rem;
        padding: .9rem 1rem; margin-bottom: .75rem; }
.card h2 { font-size: 1rem; margin: 0 0 .35rem; }
.card .sub { font-size: .85rem; opacity: .75; margin-bottom: .5rem;
             word-break: break-all; }
.card p { margin: .35rem 0; }
.badges { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .5rem; }
.badge { font-size: .75rem; padding: .1rem .5rem; border-radius: 1rem;
         border: 1px solid rgba(128,128,128,.5); opacity: .85; }
.youtube { white-space: pre-wrap; font-size: .9rem; opacity: .9;
           border-left: 3px solid rgba(128,128,128,.4); padding-left: .75rem; }
.empty { opacity: .7; padding: 2rem 0; }
footer { margin-top: 2rem; font-size: .8rem; opacity: .7; }
"""

_SCRIPT = """
const records = window.__RECORDS__;
const list = document.getElementById('list');
const count = document.getElementById('count');
const search = document.getElementById('search');
const status = document.getElementById('status');
const order = document.getElementById('order');

function badge(name) {
  const labels = window.__BADGES__;
  return labels[name] ? '<span class="badge">' + labels[name] + '</span>' : '';
}

function render() {
  const term = search.value.trim().toLowerCase();
  const wanted = status.value;
  let shown = records.filter(function (r) {
    if (term && r.search.indexOf(term) === -1) return false;
    if (wanted !== 'all' && r.statuses.indexOf(wanted) === -1) return false;
    return true;
  });
  shown.sort(function (a, b) {
    if (order.value === 'name') return a.file_name.localeCompare(b.file_name);
    if (order.value === 'period-desc') return b.sort_period.localeCompare(a.sort_period);
    return a.sort_period.localeCompare(b.sort_period);
  });
  count.textContent = shown.length + ' / ' + records.length + ' 件';
  if (!shown.length) {
    list.innerHTML = '<p class="empty">条件に合う動画がありません。</p>';
    return;
  }
  list.innerHTML = shown.map(function (r) {
    return '<article class="card">'
      + '<h2>' + r.file_name + '</h2>'
      + '<div class="sub">' + r.catalog_id + ' ・ ' + r.period
      + ' ・ ' + r.duration + '<br>' + r.source_path + '</div>'
      + '<p>' + r.content + '</p>'
      + (r.youtube ? '<p class="youtube">' + r.youtube + '</p>' : '')
      + '<div class="badges">' + r.statuses.map(badge).join('') + '</div>'
      + '</article>';
  }).join('');
}

search.addEventListener('input', render);
status.addEventListener('change', render);
order.addEventListener('change', render);
render();
"""


def _payload(records: list[CatalogRecord]) -> str:
    """JavaScript へ渡すデータ。

    **本文は先にエスケープしておく。** ブラウザー側で innerHTML へ入れるため、
    ここでエスケープしないと本文の ``<`` でカタログが壊れる。

    検索用の文字列も**エスケープ後の値から作る**。生のままにすると、
    表示されている文字列と検索対象が食い違ううえ、ページのデータ部分に
    生のタグが残る。
    """
    items = []
    for record in records:
        escaped = {
            key: (html.escape(value) if isinstance(value, str) else value)
            for key, value in record.to_dict().items()
        }
        escaped["search"] = " ".join(
            str(escaped.get(key, "")) for key in CatalogRecord.SEARCHABLE).lower()
        escaped["sort_period"] = record.sort_period
        items.append(escaped)
    # </script> でスクリプトが閉じられないようにする
    return json.dumps(items, ensure_ascii=False).replace("</", "<\\/")


def render_html(records: list[CatalogRecord]) -> str:
    options = "".join(
        f'<option value="{html.escape(value)}">{html.escape(label)}</option>'
        for value, label in STATUS_FILTERS)
    badges = json.dumps(STATUS_BADGES, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>動画カタログ</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>動画カタログ</h1>
<div class="meta">{len(records)} 件 ・ 作成 {html.escape(local_now_iso())}
 ・ local-video-catalog {html.escape(APPLICATION_VERSION)}</div>

<div class="controls">
  <input type="search" id="search" placeholder="ファイル名・内容で検索">
  <select id="status">{options}</select>
  <select id="order">
    <option value="period-asc">古い順</option>
    <option value="period-desc">新しい順</option>
    <option value="name">ファイル名順</option>
  </select>
  <span class="count" id="count"></span>
</div>

<div id="list"></div>

<footer>
このページはローカルで作られた派生物です。外部へ通信しません。
説明文はローカル AI が解析結果から作成したもので、
人物・場所・行事などは確認されていません。
</footer>

<script>
window.__RECORDS__ = {_payload(records)};
window.__BADGES__ = {badges};
{_SCRIPT}
</script>
</body>
</html>
"""


def write_catalog(records: list[CatalogRecord],
                  target: Path | None = None) -> Path:
    """HTML を**原子的に**書く。途中で失敗しても壊れたページを残さない。"""
    destination = Path(target) if target else paths.catalog_html_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    temp.write_text(render_html(records), encoding="utf-8", newline="\n")
    temp.replace(destination)
    return destination


# --------------------------------------------------------------------------
# コマンドライン
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_video_catalog.html_catalog",
        description="説明文から HTML カタログを作る（完全ローカル）")
    parser.add_argument("--json", action="store_true")
    return parser


def run(args: argparse.Namespace) -> int:
    configure_stdio_utf8()
    try:
        records = collect_records()
        target = write_catalog(records)
    except (paths.AppRootError, OSError) as exc:
        print(f"カタログを作れません: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps({"ok": True, "count": len(records),
                          "path": str(target)}, ensure_ascii=False))
    else:
        print(f"HTML カタログを更新しました: {target}")
        print(f"{len(records)} 件")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
