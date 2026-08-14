"""local-video-catalog — 手元の動画をローカル AI だけで解析してカタログにする.

工程:
    動画の登録 / ffprobe による基本情報 / 代表静止画の抽出 /
    ローカル VLM による映像内容の解析 / ローカル Whisper による文字起こし /
    最終テキストの作成 / HTML カタログの生成

**元動画は読み取り専用でのみ開く。** 変更・移動・削除・改名をしない。
**動画・画像・音声・文字起こしを外部へ送信しない。** 解析は localhost の
ローカル AI だけを使う。
**アプリが生成する状態はすべて APP_ROOT\\userdata\\ の中にある。**
"""

APPLICATION_VERSION = "0.1.0"
"""アプリケーションのバージョン。台帳の processing_runs へ記録する。

0.1.0 = 一般配布版の最初の実装。

**この値は再利用キーに含めない。** 上げても既存の解析結果は
再処理されない（再利用キーは各工程の IMPL_VERSION と config_hash 側にある）。
"""

SCHEMA_VERSION = 2
"""SQLite スキーマのバージョン。互換性のない変更で +1 する。

1 = 一般配布版の初版。
2 = asset_descriptions へ、説明文の材料に使った文字起こしの内訳
    （transcript_segment_count / transcript_excluded_count）を追加。
    **幻覚疑いを何件外したか**を後から確認できるようにするため。
    既存テーブルの列を足すだけなので、既存データはそのまま使える。

旧個人版は SCHEMA_VERSION 7 まで育っていたが、こちらは**新規の台帳**
として 1 から始める。旧版の DB を読み込む機能は持たない
（実データを持ち込まない方針のため）。
既存 DB は削除せずマイグレーションで更新する。
"""

DISCOVERY_IMPL_VERSION = 1
"""列挙処理の実装バージョン。"""

FINGERPRINT_IMPL_VERSION = 1
"""quick fingerprint の生成規則バージョン。規則を変えたら +1 する。
+1 すると既存の fingerprint は再計算対象になる。"""

FFPROBE_IMPL_VERSION = 1
"""ffprobe 結果の解析規則バージョン。解析内容を変えたら +1 する。
+1 すると既存の probe 結果は再取得対象になるが、生 JSON キャッシュが
残っていれば動画を読み直さずに再解析だけで済む。"""

EXPORT_IMPL_VERSION = 1
"""エクスポート形式のバージョン。"""

FRAME_EXTRACTION_IMPL_VERSION = "v1.0.0"
"""代表静止画抽出の実装バージョン。

抽出時刻の決め方・縮小規則・ffmpeg 引数のいずれかを変えたら上げる。
出力先フォルダー名にも使うため、上げると別フォルダーへ出力され、
過去の抽出結果と混ざらない。"""

VISUAL_ANALYSIS_IMPL_VERSION = "v1.0.0"
"""ローカル VLM 解析の実装バージョン。

プロンプト・スキーマ・解析手順のいずれかを変えたら上げる。
config_hash に含まれるため、上げると再解析対象になる。"""

ASR_IMPL_VERSION = "v1.0.0"
"""ローカル ASR の **認識結果互換バージョン**。

**この値を上げると既存チャンクが全件再処理になる。**
config_hash と asr_chunks の一意キー、出力先フォルダー名に含まれるため。
したがって **認識結果そのものが変わるときだけ** 上げる。

上げる対象:
  - チャンク分割の規則
  - 正規化・幻覚検出の規則
  - ffmpeg / whisper へ渡す引数

上げない対象:
  - 派生ファイルの命名や配置
  - 表示・ログ・ドキュメント
  → これらは APPLICATION_VERSION 側で表す。
"""

DESCRIPTION_IMPL_VERSION = "v1.0.0"
"""最終テキスト生成の実装バージョン。"""

__all__ = [
    "APPLICATION_VERSION",
    "SCHEMA_VERSION",
    "DISCOVERY_IMPL_VERSION",
    "FINGERPRINT_IMPL_VERSION",
    "FFPROBE_IMPL_VERSION",
    "EXPORT_IMPL_VERSION",
    "FRAME_EXTRACTION_IMPL_VERSION",
    "VISUAL_ANALYSIS_IMPL_VERSION",
    "ASR_IMPL_VERSION",
    "DESCRIPTION_IMPL_VERSION",
]
