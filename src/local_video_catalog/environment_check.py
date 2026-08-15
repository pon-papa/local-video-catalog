"""処理を始める前の環境チェック（完全ローカル・読み取り専用）.

「今の環境で解析を始められるか」を人が読める日本語で並べる。
**外部インターネットへは一切つながない。** LM Studio の確認も localhost だけ。

判定は 3 段階。

  OK   そのまま進められる
  注意 進められるが、一部の工程が飛ばされる・後で困るかもしれない
  NG   このままでは始められない

判定は「今回の実行条件」を見て決める。映像解析を飛ばす設定なら、
LM Studio が止まっていても NG にしない。

**何も書き換えない。**
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import asr_engine
from . import config as config_module
from . import paths, vision_probe, vlm_client
from . import readiness as readiness_module
from .logging_utils import configure_stdio_utf8

LEVEL_OK = "OK"
LEVEL_WARN = "注意"
LEVEL_NG = "NG"

_ORDER = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_NG: 2}

LOW_SPACE_GB = 20.0
CRITICAL_SPACE_GB = 2.0

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_NG = 3


@dataclass
class CheckItem:
    name: str
    level: str
    detail: str = ""
    advice: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "level": self.level,
                "detail": self.detail, "advice": self.advice}


ESSENTIAL_FOR_ANALYSIS = ("ffmpeg", "ffprobe")
"""これが無いと、どの工程も始められない。飛ばすこともできない。"""

WHISPER_FEATURE = "whisper 機能"
WHISPER_MODEL = "文字起こしモデル"
LOCAL_AI = "ローカルAI"
VISUAL_MODEL = "使用モデル"
VISION = "画像入力"

RECOMMENDED_VISUAL_MODEL = "qwen3-vl-8b-instruct"
"""実運用で動作を確認したモデル。

**これを特別扱いして probe を省かない。** 「名前が合っているから大丈夫」
にした瞬間、実際には読み込まれていない場合を見逃す。表示の注記だけに使う。
"""


@dataclass
class CheckResult:
    items: list[CheckItem] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str = "",
            advice: str = "") -> None:
        self.items.append(CheckItem(name, level, detail, advice))

    @property
    def level(self) -> str:
        worst = LEVEL_OK
        for item in self.items:
            if _ORDER[item.level] > _ORDER[worst]:
                worst = item.level
        return worst

    @property
    def blocking(self) -> list[CheckItem]:
        return [i for i in self.items if i.level == LEVEL_NG]

    def find(self, name: str) -> CheckItem | None:
        for item in self.items:
            if item.name == name:
                return item
        return None

    def is_ok(self, name: str) -> bool:
        item = self.find(name)
        return item is not None and item.level == LEVEL_OK

    def availability(self, name: str) -> str:
        """その項目の可用性（3 値）。

        **確かめていない項目を「利用不可」にしない。**
        """
        item = self.find(name)
        if item is None:
            return readiness_module.UNKNOWN
        return readiness_module.availability_from_level(
            item.level, ok_level=LEVEL_OK, warn_level=LEVEL_WARN)

    def availabilities(self) -> dict[str, str]:
        return {
            "ffmpeg": self.availability("ffmpeg"),
            "ffprobe": self.availability("ffprobe"),
            "whisper_feature": self.availability(WHISPER_FEATURE),
            "whisper_model": self.availability(WHISPER_MODEL),
            "local_ai": self.availability(LOCAL_AI),
            "visual_model": self.visual_model_availability(),
            "vision": self.availability(VISION),
        }

    def visual_model_availability(self) -> str:
        """モデルの状態。**「未選択」と「見つからない」を分ける。**

        対処が違う（選ばせる／選び直させる）ので、同じ「利用不可」に
        まとめない。
        """
        item = self.find(VISUAL_MODEL)
        if item is not None and item.detail == NOT_SELECTED_DETAIL:
            return readiness_module.NOT_SELECTED
        return self.availability(VISUAL_MODEL)

    def readiness(self, *, skip_transcription: bool = False
                  ) -> readiness_module.RunReadiness:
        """**開始できるか。** 画面も解析本体もこれを使う。

        映像の解析は v1 では必須なので、飛ばす指定は受け付けない。
        """
        return readiness_module.evaluate_run_readiness(
            **self.availabilities(), skip_transcription=skip_transcription)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "ok": self.level != LEVEL_NG,
            "items": [i.to_dict() for i in self.items],
            "blocking": [i.to_dict() for i in self.blocking],
            "availability": self.availabilities(),
        }


def list_local_models(base_url: str, *, timeout: int = 5
                      ) -> tuple[list[str], str]:
    """LM Studio のモデル一覧。失敗したら (空, 理由)。**localhost 限定。**"""
    try:
        vlm_client.assert_local_base_url(base_url)
    except vlm_client.PrivacyConfigurationError as exc:
        return ([], str(exc))
    try:
        client = vlm_client.LocalVlmClient(
            vlm_client.VlmSettings(base_url=base_url, timeout_seconds=timeout))
        return (sorted(client.list_models()), "")
    except vlm_client.VlmError as exc:
        return ([], friendly_error(str(exc)))
    except Exception as exc:                       # 接続不可など
        return ([], friendly_error(str(exc)))


NOT_SELECTED_DETAIL = "未選択"
"""``VISUAL_MODEL`` がこの内容なら「まだ選ばれていない」。"""


def apply_model_choices(raw: dict[str, Any], *, visual_model: str | None,
                        whisper_model: str | None = None,
                        description_model: str | None = None) -> None:
    """画面で選んだモデルを設定へ反映する。**環境チェックと解析で共通。**

    ここを通さないと「画面に出ているモデル」と「実際に使うモデル」が
    食い違う。``None`` は「指定なし（設定ファイルのまま）」、空文字は
    **「未選択」**として扱う。
    """
    if visual_model is not None:
        raw["vlm"] = {**dict(raw.get("vlm") or {}),
                      "model_match": str(visual_model).strip()}
    if description_model is not None:
        raw["description"] = {**dict(raw.get("description") or {}),
                              "model_match": str(description_model).strip()}
    if whisper_model is not None and str(whisper_model).strip():
        raw["asr"] = {**dict(raw.get("asr") or {}),
                      "model_name": str(whisper_model).strip()}


def friendly_error(message: str) -> str:
    """長い生エラーではなく、対処できる日本語にする。"""
    text = str(message)
    lowered = text.lower()
    if any(word in lowered for word in ("refused", "timed out", "timeout",
                                        "urlopen")) or "接続" in text:
        return ("LM Studio へ接続できません。LM Studio を起動して、"
                "ローカルサーバーを ON にしてください。")
    if "vram" in lowered or "out of memory" in lowered:
        return ("モデルを読み込めませんでした。"
                "LM Studio 側でモデルが読み込めるか確認してください。")
    if "not found" in lowered or "見つかりません" in text:
        return "指定したモデルが LM Studio にありません。"
    return "モデルを利用できませんでした。LM Studio 側の状態を確認してください。"


# ---------------------------------------------------------------------
# 個々の確認
# ---------------------------------------------------------------------


def check_python(result: CheckResult) -> None:
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info < (3, 13):
        result.add("Python", LEVEL_NG, version,
                   "Python 3.13 以降が必要です。")
    else:
        result.add("Python", LEVEL_OK, version)


def check_tool(result: CheckResult, name: str, path: Path | None, *,
               required: bool) -> bool:
    if path and Path(path).is_file():
        result.add(name, LEVEL_OK, str(path))
        return True
    result.add(name, LEVEL_NG if required else LEVEL_WARN, "見つかりません",
               f"「参照」から {name}.exe の場所を指定してください。")
    return False


def check_free_space(result: CheckResult) -> None:
    try:
        usage = shutil.disk_usage(str(paths.userdata_dir()))
    except OSError as exc:
        result.add("空き容量", LEVEL_WARN, f"確認できません（{exc}）")
        return
    free_gb = usage.free / (1024 ** 3)
    detail = f"{free_gb:,.1f} GB"
    if free_gb < CRITICAL_SPACE_GB:
        result.add("空き容量", LEVEL_NG, detail,
                   "空き容量がほとんどありません。先に整理してください。")
    elif free_gb < LOW_SPACE_GB:
        result.add("空き容量", LEVEL_WARN, detail, "残りが少なくなっています。")
    else:
        result.add("空き容量", LEVEL_OK, detail)


def check_local_ai(result: CheckResult, raw: dict[str, Any], *,
                   probe_vision: bool = True) -> None:
    """ローカルAI・使用モデル・画像入力を、この順に確かめる。

    **接続できただけでは「映像を解析できる」と言わない。**
    モデルが選ばれていて、それが今も存在し、実際に画像を受け取れる
    ところまで確かめる。
    """
    settings = vlm_client.VlmSettings.from_settings(raw)
    try:
        vlm_client.assert_local_base_url(settings.base_url)
    except vlm_client.PrivacyConfigurationError as exc:
        result.add(LOCAL_AI, LEVEL_NG, str(exc),
                   "接続先はこのPCの中だけにしてください。")
        return

    available, error = list_local_models(settings.base_url)
    if error:
        # **確かめて駄目だったので「利用不可」。**
        # ここで「注意」に落とすと、未確認と区別できなくなる。
        result.add(LOCAL_AI, LEVEL_NG, "未接続", error)
        return

    result.add(LOCAL_AI, LEVEL_OK, f"接続済み（{len(available)} モデル）")

    selected = str(settings.model_match or "").strip()
    if not selected:
        result.add(VISUAL_MODEL, LEVEL_NG, NOT_SELECTED_DETAIL,
                   "LM Studio を起動し、画像を扱えるモデルを読み込んだ後、"
                   "「ローカルAI設定」で使用するモデルを選択してください。")
        return

    if selected not in available:
        # **勝手に別モデルへ切り替えない。** 部分一致で似た名前を拾うと、
        # 利用者が選んだつもりのないモデルで解析が進んでしまう。
        result.add(VISUAL_MODEL, LEVEL_NG, f"{selected}（見つかりません）",
                   "前回使用したモデルを利用できません。"
                   "「ローカルAI設定」で使用するモデルを選び直してください。")
        return

    note = "（動作確認済み）" if selected == RECOMMENDED_VISUAL_MODEL else ""
    result.add(VISUAL_MODEL, LEVEL_OK, f"{selected}{note}")

    if not probe_vision:
        result.add(VISION, LEVEL_WARN, "未確認",
                   "「環境チェック」で確認できます。")
        return

    found = vision_probe.probe(settings, selected)
    if found.outcome == vision_probe.OK:
        result.add(VISION, LEVEL_OK, found.detail)
    elif found.outcome in (vision_probe.NO_IMAGE, vision_probe.NOT_CONNECTED,
                           vision_probe.MODEL_MISSING):
        # **確かめて駄目だった。** 「このモデルでは使えません」と言い切る。
        result.add(VISION, LEVEL_NG, found.detail, found.advice)
    else:
        # **確かめられなかった。** 「非対応」とは書かない（対処が違う）。
        # 注意どまりにするが、readiness はこれでも開始を許さない。
        result.add(VISION, LEVEL_WARN, found.detail, found.advice)


def check_transcription(result: CheckResult, raw: dict[str, Any], *,
                        ffmpeg_path: Path | None, skip: bool,
                        quick: bool) -> None:
    """文字起こしの環境。**LM Studio は関係しない。**

    必要なのは ffmpeg・その whisper 機能・モデルの 3 つだけ。
    """

    if not ffmpeg_path:
        result.add("whisper 機能", LEVEL_NG, "ffmpeg が無いため確認できません")
    elif quick:
        result.add("whisper 機能", LEVEL_WARN, "未確認")
    elif config_module.ffmpeg_has_whisper(ffmpeg_path):
        result.add("whisper 機能", LEVEL_OK, "利用できます")
    else:
        result.add("whisper 機能", LEVEL_NG, "この ffmpeg には入っていません",
                   "whisper 対応の ffmpeg（8.x 以降のフル版）を指定してください。")

    config = asr_engine.AsrConfig.from_settings(raw)
    usable, reason = asr_engine.check_model(config)
    if usable:
        size_mb = asr_engine.model_path(config).stat().st_size / (1024 * 1024)
        result.add("文字起こしモデル", LEVEL_OK,
                   f"{config.model_name}（{size_mb:,.1f} MB）")
    else:
        result.add("文字起こしモデル", LEVEL_NG, reason,
                   "userdata\\models\\whisper\\ へモデルを置いてください。"
                   "（このまま進めると文字起こしだけが飛ばされます）")


def check_environment(
    *,
    raw: dict[str, Any],
    settings: config_module.Settings,
    source_folder: str | None = None,
    skip_transcription: bool = False,
    quick: bool = False,
) -> CheckResult:
    """今の環境を一通り確かめる。**何も書き換えない。**"""
    result = CheckResult()
    check_python(result)

    ffmpeg_ok = check_tool(result, "ffmpeg", settings.ffmpeg_path, required=True)
    check_tool(result, "ffprobe", settings.ffprobe_path, required=True)

    check_transcription(result, raw,
                        ffmpeg_path=settings.ffmpeg_path if ffmpeg_ok else None,
                        skip=skip_transcription, quick=quick)
    # 映像の解析は必須工程なので、常に必要なものとして確かめる。
    # ``--quick`` のときは画像 probe を省く（そのぶん「未確認」が残る）。
    check_local_ai(result, raw, probe_vision=not quick)

    result.add("保存先", LEVEL_OK, str(paths.userdata_dir()))
    check_free_space(result)

    descriptions = paths.descriptions_dir()
    if descriptions.is_dir():
        count = len(list(descriptions.glob("*.txt")))
        result.add("説明文", LEVEL_OK, f"{count} 件")
    else:
        result.add("説明文", LEVEL_WARN, "まだありません",
                   "最初の動画を処理すると作られます。")

    if source_folder:
        if Path(source_folder).is_dir():
            result.add("入力元", LEVEL_OK, source_folder)
        else:
            result.add("入力元", LEVEL_NG, f"{source_folder} がありません",
                       "フォルダーを選び直してください。")
    else:
        result.add("入力元", LEVEL_WARN, "未指定",
                   "解析したい動画のフォルダーを選んでください。")

    if paths.database_path().is_file():
        result.add("台帳", LEVEL_OK, str(paths.database_path()))
    else:
        result.add("台帳", LEVEL_WARN, "まだありません",
                   "最初の処理で作られます。")

    if paths.catalog_html_path().is_file():
        result.add("HTMLカタログ", LEVEL_OK, str(paths.catalog_html_path()))
    else:
        result.add("HTMLカタログ", LEVEL_WARN, "まだありません",
                   "「HTMLカタログを更新」で作れます。")

    return result


# ---------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------


def display_width(text: str) -> int:
    """全角を 2 文字ぶんとして数える（等幅フォントで桁を揃えるため）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1
               for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


MARKS = {LEVEL_OK: "✓", LEVEL_WARN: "–", LEVEL_NG: "✕"}
"""行頭の記号。OK / 注意 / NG を一目で分かるようにする。"""


def format_lines(result: CheckResult) -> list[str]:
    width = max((display_width(i.name) for i in result.items), default=10)
    lines = []
    for item in result.items:
        mark = MARKS.get(item.level, " ")
        lines.append(f"{mark} {pad(item.name, width)}  {item.detail}".rstrip())
        if item.advice and item.level != LEVEL_OK:
            lines.append(f"  {' ' * width}  → {item.advice}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_video_catalog.environment_check",
        description="処理を始める前の環境チェック（読み取り専用・完全ローカル）")
    parser.add_argument("--config")
    parser.add_argument("--source-folder")
    parser.add_argument("--skip-transcription", action="store_true")
    parser.add_argument("--visual-model", default=None,
                        help="映像解析に使用するモデル（画面で選んだもの）")
    parser.add_argument("--whisper-model", default=None)
    parser.add_argument("--quick", action="store_true",
                        help="時間のかかる確認を省く（画像 probe も省く）")
    parser.add_argument("--json", action="store_true")
    return parser


def run(args: argparse.Namespace) -> int:
    configure_stdio_utf8()
    try:
        raw = config_module.load_settings_dict(args.config)
        if args.source_folder:
            raw["source_path"] = args.source_folder
        apply_model_choices(raw, visual_model=args.visual_model,
                            whisper_model=args.whisper_model)
        settings = config_module.build_settings(raw, require_ffprobe=False)
    except (config_module.ConfigError, paths.AppRootError) as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    result = check_environment(
        raw=raw, settings=settings,
        source_folder=args.source_folder or raw.get("source_path"),
        skip_transcription=args.skip_transcription, quick=args.quick)
    readiness = result.readiness(skip_transcription=args.skip_transcription)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        print("環境チェック完了")
        print("")
        for line in format_lines(result):
            print(line)
        print("")
        # **OK / NG だけでなく「いま何ができて、どうすればよいか」を書く。**
        for line in readiness.detail_lines():
            print(line)

    # **開始できないなら、終了コードでもそう伝える。**
    # 「画像処理能力を確認できなかった」は注意どまりだが開始はできない。
    # ここを result.level だけで決めると、成功したように見えてしまう。
    return EXIT_NG if (result.level == LEVEL_NG
                       or not readiness.can_start) else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
