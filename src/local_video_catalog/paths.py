"""APP_ROOT と、その配下のすべての保存先を導出する唯一の場所.

このアプリは One-Folder 完結型である。**アプリが生成・保持する状態は
すべて APP_ROOT\\userdata\\ の中にある。** ``%LOCALAPPDATA%`` や
``%APPDATA%``、レジストリなどへは何も書かない。

保存先を組み立てるコードをこのモジュールへ集約する理由:

  1. 各所で ``Path(...)`` を組み立てると、One-Folder 原則が破れたときに
     どこが原因か分からなくなる。
  2. cleanup（ファイルを消す機能）の境界を、設定値ではなく
     **必ず APP_ROOT から**決めるため。設定が壊れていても外へ出ない。
  3. フォルダーごと移動しても動くようにするため。絶対パスを永続データへ
     焼き込まず、実行のたびに導出する。

**外部のパス（元動画・ffmpeg・モデルの入手元）はこのモジュールが扱わない。**
それらは APP_ROOT の外にあり、性質がまったく違う。元動画の位置は
``source_ref`` モジュールが扱う。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_ROOT_MARKER = "app-root.marker"
"""APP_ROOT を示す目印のファイル名。

``Path(__file__).parents[N]`` のような階層の決め打ちにしない。
決め打ちは、``src\\`` の構成を変えたときや、配布物を別の形へ
詰め直したときに**静かに壊れる**。目印なら意図が明示され、
無ければ気づける。
"""

ROOT_ENVIRONMENT_VARIABLE = "LOCAL_VIDEO_CATALOG_ROOT"
"""APP_ROOT の明示的な上書き。**テストと CI 専用。**

通常運用では設定しない。設定されていても、実在するフォルダーで
なければ無視せずエラーにする（黙って別の場所へ書かない）。
"""

USERDATA_DIRECTORY_NAME = "userdata"
"""実行時データの入れ物。この名前は cleanup の境界判定にも使う。"""

CLEANABLE_CACHE_NAMES = ("frames", "vlm", "asr")
"""片付けてよい中間キャッシュ。

``probe`` は含めない。ffprobe の生 JSON は、解析規則を更新したときに
**動画を読み直さずに再解析するため**に使う。消すと元動画の再読み込みが
必要になり、失うものの方が大きい。
"""

_CLEANABLE_DEPTH = 4
"""``userdata/cache/<名前>/<何か>`` の 4 階層。

これ未満（= ``userdata/cache/vlm`` のような親フォルダー自体）は
片付け対象にしない。1 本の動画に属するフォルダーだけを消す。
"""


class AppRootError(Exception):
    """APP_ROOT を決められない。**代替場所を勝手に選ばずに停止する。**"""


# --------------------------------------------------------------------------
# APP_ROOT の決定
# --------------------------------------------------------------------------


def _from_environment() -> Path | None:
    configured = os.environ.get(ROOT_ENVIRONMENT_VARIABLE)
    if not configured:
        return None
    candidate = Path(configured)
    if not candidate.is_dir():
        raise AppRootError(
            f"{ROOT_ENVIRONMENT_VARIABLE} が指す場所がありません: {candidate}\n"
            "代替場所は自動で決めません。値を確認してください。"
        )
    return candidate.resolve()


def _search_upwards(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / APP_ROOT_MARKER).is_file():
            return candidate
    return None


def app_root() -> Path:
    """APP_ROOT を返す。

    決定順:
        1. 環境変数 ``LOCAL_VIDEO_CATALOG_ROOT``（テスト・CI 用）
        2. このファイルから上へ辿って ``app-root.marker`` を持つ最初の場所
        3. どちらも駄目なら ``AppRootError``

    毎回導出する。**値をキャッシュしない**（フォルダー移動やテストでの
    差し替えに追随するため）。
    """
    from_env = _from_environment()
    if from_env is not None:
        return from_env

    found = _search_upwards(Path(__file__).resolve().parent)
    if found is not None:
        return found

    raise AppRootError(
        f"{APP_ROOT_MARKER} が見つからないため、アプリの場所を決められません。\n"
        "配布されたフォルダーの構成が崩れている可能性があります。\n"
        "代替場所は自動で決めません。"
    )


# --------------------------------------------------------------------------
# userdata 配下（アプリが生成・保持するすべて）
# --------------------------------------------------------------------------


def userdata_dir() -> Path:
    return app_root() / USERDATA_DIRECTORY_NAME


def config_dir() -> Path:
    return userdata_dir() / "config"


def settings_path() -> Path:
    """ユーザー設定。GUI が書き、CLI が読む。"""
    return config_dir() / "settings.json"


def gui_state_path() -> Path:
    """画面の状態（前回の入力・選択）。解析結果には影響しない。"""
    return config_dir() / "gui-state.json"


def catalog_dir() -> Path:
    return userdata_dir() / "catalog"


def database_path() -> Path:
    """SQLite 台帳。**正本。**"""
    return catalog_dir() / "video_catalog.sqlite3"


def catalog_html_path() -> Path:
    """HTML カタログ。派生物であり、説明文からいつでも作り直せる。"""
    return catalog_dir() / "catalog.html"


def export_dir() -> Path:
    return catalog_dir() / "exports"


def descriptions_dir() -> Path:
    """動画 1 本ごとの最終テキスト。**派生物ではなく成果物。**"""
    return userdata_dir() / "descriptions"


def cache_dir() -> Path:
    return userdata_dir() / "cache"


def probe_cache_dir() -> Path:
    return cache_dir() / "probe"


def frames_cache_dir() -> Path:
    """代表静止画。旧個人版の ``cache/scenes`` に相当する。"""
    return cache_dir() / "frames"


def vlm_cache_dir() -> Path:
    return cache_dir() / "vlm"


def asr_cache_dir() -> Path:
    return cache_dir() / "asr"


def models_dir() -> Path:
    return userdata_dir() / "models"


def whisper_models_dir() -> Path:
    """文字起こしモデルの置き場。**同梱しない。** 利用者が置く。"""
    return models_dir() / "whisper"


def log_dir() -> Path:
    return userdata_dir() / "logs"


def runs_dir() -> Path:
    return userdata_dir() / "runs"


def temp_dir() -> Path:
    return userdata_dir() / "temp"


def control_dir() -> Path:
    return userdata_dir() / "control"


def stop_request_path() -> Path:
    """安全停止の要求ファイル。

    **プロセスを強制終了しない。** このファイルが現れたら、区切りの
    よいところで停止する。台帳も元動画も壊れない。
    """
    return control_dir() / "stop-request"


def userdata_subdirectories() -> list[Path]:
    """起動時に用意するフォルダー一式。"""
    return [
        config_dir(),
        catalog_dir(),
        export_dir(),
        descriptions_dir(),
        cache_dir(),
        probe_cache_dir(),
        frames_cache_dir(),
        vlm_cache_dir(),
        asr_cache_dir(),
        models_dir(),
        whisper_models_dir(),
        log_dir(),
        runs_dir(),
        temp_dir(),
        control_dir(),
    ]


def ensure_userdata_tree() -> list[Path]:
    """不足しているフォルダーだけを作る。作ったものの一覧を返す。

    APP_ROOT 自体は作らない（既に存在しているはずの場所である）。
    """
    created: list[Path] = []
    for directory in userdata_subdirectories():
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)
    return created


# --------------------------------------------------------------------------
# APP_ROOT 相対のパス表現（フォルダー移動耐性）
# --------------------------------------------------------------------------


def to_app_relative(path: Path | str) -> str | None:
    """APP_ROOT からの相対パス（POSIX 表記）を返す。外なら None。

    **台帳へ内部生成物の位置を保存するときは必ずこれを通す。**
    絶対パスを保存すると、フォルダーごと移動したときに過去の成果物を
    見失い、再解析が起きる。

    **元動画のパスにこれを使ってはいけない。** 元動画は APP_ROOT の外に
    ある外部入力であり、``source_ref`` が扱う。
    """
    try:
        relative = Path(path).resolve().relative_to(app_root().resolve())
    except (ValueError, OSError):
        return None
    return relative.as_posix()


def from_app_relative(relative: str) -> Path:
    """``to_app_relative`` の逆。現在の APP_ROOT を基準に組み立てる。"""
    return app_root() / Path(relative)


def to_relative_ascii(path: Path | str, base: Path | str) -> str | None:
    """base からの相対パスを返す。ASCII でなければ None。

    **whisper.cpp は非 ASCII を含むパスのファイルを開けない**（実測。
    8.3 短縮名でも回避できなかった）。そのため ffmpeg の作業ディレクトリを
    base にして、model と出力先を ASCII の相対パスで渡す。

    One-Folder 化により model も出力先も必ず APP_ROOT 配下へ来るので、
    base は常に APP_ROOT でよい。``userdata/models/whisper/...`` や
    ``userdata/cache/asr/...`` はすべて ASCII なので、**APP_ROOT 自身が
    日本語を含んでいても成立する。**
    """
    try:
        relative = Path(path).resolve().relative_to(Path(base).resolve())
    except (ValueError, OSError):
        return None
    text = relative.as_posix()
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return None
    return text


# --------------------------------------------------------------------------
# cleanup の境界
# --------------------------------------------------------------------------


def is_cleanable(path: Path | str) -> bool:
    """片付けてよい場所か。

    **基点は必ず ``app_root()``。** 設定値・引数・台帳の記録値を
    基点にしない。設定が壊れていても、元動画・モデル・利用者の他の
    フォルダーへ波及しないようにするため。

    True になるのは、すべて満たすときだけ:

        1. ``app_root()`` の配下である（``resolve()`` 後に判定するので
           シンボリックリンクやジャンクションで外を指しても弾かれる）
        2. 第 1 要素が ``userdata``
        3. 第 2 要素が ``cache``
        4. 第 3 要素が ``frames`` / ``vlm`` / ``asr`` のいずれか
        5. 深さが 4 以上（親フォルダー自体は消さない）
    """
    try:
        relative = Path(path).resolve().relative_to(app_root().resolve())
    except (ValueError, OSError):
        return False                      # APP_ROOT の外は触らない

    parts = relative.parts
    if len(parts) < _CLEANABLE_DEPTH:
        return False
    if parts[0] != USERDATA_DIRECTORY_NAME:
        return False
    if parts[1] != "cache":
        return False
    return parts[2] in CLEANABLE_CACHE_NAMES


def cache_directories_for_asset(asset_id: str) -> list[Path]:
    """1 本の動画に属する中間キャッシュのフォルダー（実在するものだけ）。"""
    found: list[Path] = []
    for name in CLEANABLE_CACHE_NAMES:
        candidate = cache_dir() / name / asset_id
        if candidate.is_dir():
            found.append(candidate)
    return found
