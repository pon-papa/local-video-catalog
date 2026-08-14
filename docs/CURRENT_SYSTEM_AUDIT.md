# 既存個人版「動画内容解析システム」read-only 調査報告

- 調査日: 2026-08-14
- 調査対象: `C:\Users\User\Documents\動画編集関係\動画内容解析システム`（**read-only 参照。一切変更していない**）
- 調査者視点: 一般配布版 `local-video-catalog` v1 の移植計画立案のため
- 旧版バージョン: `APPLICATION_VERSION = 0.6.1` / `SCHEMA_VERSION = 7`

> この文書は旧版の**実装・テスト・設定・呼び出し側を実際に照合**して書いた。
> README やコメントの記述をそのまま転記した箇所はない。

---

## 0. 調査方法と read-only の担保

| 行為 | 実施 |
|---|---|
| ファイル読み取り（`Read` / `grep`） | 実施 |
| `git log` / `git status` の閲覧 | 実施 |
| ファイル変更・追加・削除 | **していない** |
| formatter / linter の実行 | **していない** |
| **テストスイートの実行** | **していない**（`__pycache__` や `tests/output` を生成するため意図的に回避） |
| 本番処理の開始・停止 | **していない** |
| git add / commit / push / branch 操作 | **していない** |

旧リポジトリの `git status` は調査開始時点・終了時点ともに clean。ブランチは `main` のまま、コミット総数 32 で変化なし。

---

## A. 現行ディレクトリ構造

```
動画内容解析システム\                     ← リポジトリルート（コードのみ）
├─ .github\workflows\tests.yml            CI（unit / integration / privacy-check の3ジョブ）
├─ .gitattributes
├─ .gitignore                             169行。実データを名前で多重遮断
├─ README.md                              29,745バイト（運用手順書を兼ねる）
├─ Start-FamilyVideoCatalog.cmd           ダブルクリック起動口（CP932 エンコード）
├─ config\
│   ├─ settings.example.json              Git 管理・プレースホルダのみ
│   └─ settings.local.json                **Git 管理外・実パスを含む**
├─ docs\                                  Prototype01〜04 の仕様と実装結果
├─ scripts\                               PowerShell 15本 + benchmark_vlm.py
├─ src\video_catalog\                     Python パッケージ 29 モジュール
├─ tests\                                 31 テストファイル（テスト関数 1,018 個）
└─ 環境・既存資産調査_20260806.md          **Git 管理外**（実フォルダー名・実動画名を含む）
```

**重要な構造上の事実**: リポジトリはコードだけを持ち、**実データは一切入っていない**。
実データは `D:\動画内容解析システムデータ`（= DataRoot）に完全分離されている。
これは新版で One-Folder 化するときに**逆転する前提**であり、最大の設計差分になる（§Z / リスク R-1）。

コード規模（`__pycache__` を除く）:

| 区分 | 行数 |
|---|---|
| Python 本体（`src/video_catalog/*.py` 29本） | 約 14,700 |
| PowerShell（`scripts/*.ps1` 15本） | 約 4,200 |
| テスト（`tests/*.py` 31本） | 約 15,900 |
| 合計 | 約 35,000 |

---

## B. 主要 entry point

`scripts\Start-FamilyVideoCatalog.ps1`（595行）が**本番の唯一の入口**であり、
新しい解析ロジックを一切持たない**オーケストレータ**である。動画 1 本ごとに 5 工程を順に呼ぶ。

| 工程 | 呼び出し先 | 実体 | 粒度 |
|---|---|---|---|
| 1/5 動画の登録・ffprobe | `Start-Prototype01.ps1` | `video_catalog.cli` | フォルダー単位で1回 |
| 2/5 代表静止画抽出 | `Start-Prototype02.ps1` | `video_catalog.frame_cli` | 動画ごと |
| 3/5 映像解析（VLM） | `Start-Prototype03.ps1` | `video_catalog.visual_cli` | 動画ごと |
| 4/5 文字起こし（ASR） | `Start-Prototype04.ps1` | `video_catalog.asr_cli` | 動画ごと（チャンク単位で安全停止） |
| 5/5 最終テキスト | `Start-Description.ps1` | `video_catalog.description_cli` | 動画ごと |

補助 entry point:

| スクリプト | Python モジュール | 用途 |
|---|---|---|
| `Test-CatalogEnvironment.ps1` | `environment_check` | 環境チェック |
| `Update-HtmlCatalog.ps1` | `html_catalog` | HTML カタログ生成 |
| `Export-Catalog.ps1` | `exporters` | CSV/JSON/JSONL 出力 |
| `Get-CatalogRunSummary.ps1` | `run_summary` | 実行結果まとめ |
| `Get-LocalAiModels.ps1` | `model_catalog` | 利用可能ローカルモデル一覧 |
| `Start-GuiMaintenance.ps1` | `gui_maintenance` | GUI 作業履歴の整理 |
| `Diagnose-LMStudioGpu.ps1` | （PowerShell 単独） | LM Studio GPU 診断 |

**PowerShell 層の役割は「引数の受け渡し・終了コードの翻訳・工程の直列実行」だけ**であり、
解析ロジックはすべて Python 側にある。この分離は新版でもそのまま活かせる。

---

## C. GUI entry point

```
Start-FamilyVideoCatalog.cmd  （CP932。PowerShell 7 を探して GUI を起動）
    └─ scripts\Start-FamilyVideoCatalog-GUI.ps1  （1,871行 / WinForms）
            └─ scripts\Start-FamilyVideoCatalog.ps1  （別プロセスで実行）
```

GUI は解析処理を持たず、CLI を別プロセスで起動して出力を読み取るだけ。
そのため長時間処理中も画面が固まらない。**この「GUI＝別プロセス起動＋出力取り込み」構造は
新版（Python + tkinter）でも維持すべき中核設計**である。

GUI の実機能（`.Text` 定義から抽出した確定リスト）:

| 分類 | 機能 |
|---|---|
| 入力設定 | 入力元フォルダー選択 / サブフォルダーも含める |
| 保存先 | 台帳・説明文・ログの保存先選択 |
| ローカルAI | 「ローカルAI設定…」ダイアログ（映像解析モデル / 説明文モデル / Whisper モデル選択・一覧再読込） |
| 実行条件 | 稼働時間（分）・本数上限・時間制限なし・本数制限なし・Resume・映像解析を飛ばす・完了後に中間ファイルをゴミ箱へ |
| 操作 | 環境チェック / 対象確認 / 処理開始 / 安全停止 / 閉じる |
| 進捗 | 「進み具合」グループ・状態ラベル・ログ表示 |
| 成果物 | 「結果を見る / お手入れ」グループ（HTMLカタログ更新・HTMLを開く・説明文を開く・元動画位置を開く・最新説明文表示） |
| 保守 | GUI作業履歴の使用量表示 / 整理ダイアログ（ゴミ箱へ移動・GUI設定初期化） |

GUI 設定の保存先は `%LOCALAPPDATA%\FamilyVideoCatalog\gui-settings.json`（→ §I / 新版では廃止）。
安全停止は `%LOCALAPPDATA%\FamilyVideoCatalog\stop-request.txt` の**ファイル生成**で伝える
（プロセス強制終了をしないため台帳も元動画も壊れない）。

---

## D. Python / PowerShell / batch の関係

```
.cmd (CP932)  →  .ps1 GUI (UTF-8 BOMなし・PowerShell 7 必須)
                      ↓ 別プロセス
                  .ps1 本体  →  .ps1 工程別  →  python -X utf8 -m video_catalog.<module>
```

判明した実装上の制約:

1. **PowerShell 5.1 は使えない。** リポジトリの `.ps1` は UTF-8 (BOM なし) で日本語を含むため、
   5.1 では文字化けして解析に失敗する。GUI 冒頭で pwsh を探し、無ければ日本語メッセージで停止する。
2. **子プロセスの文字コードを毎回明示している。** `Start-FamilyVideoCatalog.ps1:151-157` で
   `[Console]::OutputEncoding` と `$OutputEncoding` を UTF-8 に設定し、
   Python 側へも `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` を渡す。
   これをやらないと日本語 Windows では CP932 として復号され文字化けする。
   **GUI が出力をリダイレクトして受け取るときに顕在化する**と明記されている。
3. Python は `PYTHONPATH=<repo>\src` を一時的に設定して `-m video_catalog.*` で呼ぶ。
   パッケージインストール（pip install -e）はしていない。
4. **サードパーティ Python パッケージをゼロにしている。** CI が
   `python -c "import sqlite3, csv, json, hashlib, gzip, uuid, argparse, unittest, logging, concurrent.futures"`
   で標準ライブラリのみであることを毎回検証している。

---

## E. 設定ファイル

3 層マージ（`config.py:221-235`）。後のものが前を上書きし、`None` は無視される。

```
DEFAULT_SETTINGS (config.py 内)
  → config\settings.local.json （存在すれば・Git 管理外）
  → --config で指定されたファイル
  → コマンドライン引数
```

`settings.example.json`（Git 管理・113行）の主要セクション:

| セクション | 主なキー | 備考 |
|---|---|---|
| ルート | `data_root`, `ffmpeg_path`, `ffprobe_path`, `source_path`, `recursive`, `workers`(上限32), `extensions`(12種), `exclude_patterns`, `min_size_bytes`, `ffprobe_timeout_sec`, `full_hash`, `resume`, `follow_symlinks` | |
| `fingerprint` | `head_bytes`, `tail_bytes`（各 1MiB） | |
| `probe_cache` | `enabled`, `gzip` | |
| `vlm` | `base_url`, `model_match`, `temperature`, `top_p`, `max_tokens_per_frame`, `max_tokens_summary`, **`timeout_seconds`(300)**, **`summary_timeout_seconds`(1200)**, `maximum_concurrent_requests`(1固定) | |
| `asr` | `model_path`, `model_name`, `language`, `queue_seconds`(30), **`vad_enabled`(false)**, `vad_threshold`, `chunk_duration_seconds`(300), `chunk_overlap_seconds`(1.0), `time_budget_minutes`(60), `max_len`, `use_gpu` | |
| `description` | `model_match`（空 = 映像解析と同じモデル） | example には未記載だが `model_catalog.description_model_name()` が読む |

**`settings.local.json` は Git 管理外**で、実測では `data_root` / winget 配下の ffmpeg・ffprobe 絶対パスを持つ。
→ 新版へ**内容を持ち込まない**（§Z）。

---

## F. DB schema（SQLite / `SCHEMA_VERSION = 7`）

`database.py:142-667` の `SCHEMA_SQL` に 16 テーブルが定義されている。
PRAGMA は `journal_mode=WAL` / `synchronous=NORMAL` / `foreign_keys=ON`。

| # | テーブル | 役割 | 一意キーの要点 |
|---|---|---|---|
| — | `schema_meta` | スキーマ版管理 | |
| A | `assets` | 物理ファイル 1 件 = 1 行 | `catalog_id` は `VID-000001` 形式の連番（発行後不変） |
| B | `probe_results` | ffprobe 結果（asset ごと最新1行） | 主映像ストリーム選択結果 4 列を含む |
| C | `capture_time_candidates` | 撮影日時の**候補**と根拠 | `is_family_confirmed` 列あり。**候補は削除しない** |
| D | `asset_relations` | 複数元動画 対 1 変換済み動画 | `sequence_index` で結合順を保持 |
| E | `processing_runs` | 実行 1 回 = 1 行 | `config_snapshot` を JSON で保存 |
| F | `stage_status` | 工程ごとの状態 | **Resume の中核**。`(asset_id, stage_name)` が PK |
| G | `frame_extraction_runs` | 静止画抽出の実行 | |
| H | `extracted_frames` | 静止画 1 枚 | `(asset, impl, config_hash, src_fp, 抽出時刻ms)` で重複行を作らない |
| I | `visual_analysis_runs` | VLM 解析の実行 | `model_api_base` は localhost が分かる最小表現のみ保存 |
| J | `frame_visual_analyses` | 静止画 1 枚の解析結果 | `(asset, frame_sha256, model, prompt, impl, config_hash)` |
| K | `asset_visual_summaries` | 映像全体の視覚概要 | `source_frame_analysis_hash` が同じなら再生成しない |
| L | `asr_runs` | 文字起こし実行 | `vad_enabled`, `stop_reason` を保持 |
| M | `asr_chunks` | チャンク 1 個 | **中断時の損失を最大1チャンクに限定する単位** |
| N | `transcripts` | 統合済み文字起こし | scope（full / 区間）別 |
| O | `transcript_segments` | タイムスタンプ付きセグメント | **`is_suspected_hallucination`** 列を持つ |
| P | `asset_descriptions` | 動画 1 本の最終テキスト | `cache_cleanup_status` / `cache_freed_bytes` を持つ |

**設計思想として重要な点**（新版でも守るべき）:

- `capture_time_candidates` は「候補」であり、`is_family_confirmed` で人の確認と AI 推定を明確に分けている。
- `asset_relations.confirmed_by_family` も同様。**AI 推定を事実へ昇格させない構造が schema レベルで担保されている。**
- 各成果物テーブルが `implementation_version` + `config_hash` + `source_quick_fingerprint` を持ち、
  **再利用（Resume）と再解析の判定がすべて DB で完結する**。マニフェストファイルに依存しない。

---

## G. DataRoot の決め方

```python
# config.py:27
DEFAULT_DATA_ROOT = r"D:\動画内容解析システムデータ"
```

決定順: `--data-root` 引数 → `settings.local.json` の `data_root` → 上記既定値。

派生パスは `Settings` の property として一元定義されている（`config.py:117-169`）:

| property | 実パス |
|---|---|
| `catalog_dir` | `<DataRoot>\catalog` |
| `export_dir` | `<DataRoot>\catalog\exports` |
| `database_path` | `<DataRoot>\catalog\video_catalog.sqlite3` |
| `log_dir` | `<DataRoot>\logs` |
| `cache_dir` | `<DataRoot>\cache` |
| `probe_cache_dir` | `<DataRoot>\cache\probe` |
| `runs_dir` | `<DataRoot>\runs` |
| `tests_dir` / `fixtures_dir` / `test_output_dir` | `<DataRoot>\tests\...` |

property に載っていない派生先（各モジュールが直接組み立てている）:

| 場所 | 定義位置 |
|---|---|
| `<DataRoot>\descriptions` | `description_cli.py:313` |
| `<DataRoot>\catalog.html` | `html_catalog.py:36` + `environment_check.py:282` |
| `<DataRoot>\models\whisper\` | `model_catalog.py:34,167` / `asr_cli.py:170` |
| `<DataRoot>\cache\scenes\<asset_id>\...` | `frame_extractor.py:264-276` |
| `<DataRoot>\cache\vlm\<asset_id>\...` | `visual_analyzer.py:126` |
| `<DataRoot>\cache\asr\<asset_id>\<impl>\<config_hash[:12]>\` | `asr_engine.py:314` |

**`verify_data_root()`（`config.py:416`）の方針が重要**:
DataRoot 自体は**作らない**（ユーザーが手動作成した場所を使う）。存在しなければ
`DataRootError` で停止し、**代替場所を勝手に決めない**。不足サブフォルダーだけを作る。
書き込み確認は一時ファイルを作って必ず消す。

→ 新版では「APP_ROOT は必ず存在する（実行ファイルの場所だから）」ため、この関数の
「代替場所を勝手に決めない」思想だけを継承し、存在チェックの意味は変わる。

---

## H. APP_ROOT 外へ書き込んでいる箇所

旧版に「APP_ROOT」という概念はなく、**リポジトリ外の DataRoot へ書くのが正常設計**である。
新版の基準（= APP_ROOT 配下のみ）で棚卸しすると、外部書き込みは次の 2 系統に整理できる。

### H-1. DataRoot 配下（新版では `APP_ROOT\userdata\` へ集約すべきもの）

`catalog\` / `catalog\exports\` / `descriptions\` / `logs\` / `cache\{probe,scenes,vlm,asr}\` /
`runs\` / `models\whisper\` / `tests\{fixtures,output}\` / `catalog.html` / `.write_check.tmp`（即削除）

### H-2. DataRoot の外（新版では**完全に廃止すべきもの**）

| パス | 用途 | 生成箇所 |
|---|---|---|
| `%LOCALAPPDATA%\FamilyVideoCatalog\gui-settings.json` | GUI 設定の永続化 | `Start-FamilyVideoCatalog-GUI.ps1:35-36,84-94` |
| `%LOCALAPPDATA%\FamilyVideoCatalog\gui-log-*.txt` | GUI 実行ログ | 同 GUI |
| `%LOCALAPPDATA%\FamilyVideoCatalog\stop-request.txt` | 安全停止の要求ファイル | 同 GUI:37 |
| `%LOCALAPPDATA%\FamilyVideoCatalog\gui-settings.json.*` | 設定バックアップ | 同 GUI |

**これが「アプリを消しても残骸が残る」唯一の原因**であり、
旧版はそれを自覚して `gui_maintenance.py`（272行）という専用の掃除機能をわざわざ作っている。
新版で `%LOCALAPPDATA%` を使わなければ、**`gui_maintenance.py` に相当する機能自体が不要になる**。

### H-3. 元動画フォルダーへの書き込み

**存在しない。** `discovery.py` / `probe.py` / `frame_extractor.py` / `asr_engine.py` のいずれも
元動画を読み取り専用で開くだけで、同一フォルダーへの生成物出力・改名・移動・削除・
メタデータ書換えは行っていない。ffmpeg も入力としてのみ渡している。
→ **この最重要安全仕様は旧版で既に守られており、そのまま継承できる。**

---

## I. `%LOCALAPPDATA%` 等への依存

| 依存 | 箇所 | 種別 |
|---|---|---|
| `%LOCALAPPDATA%\FamilyVideoCatalog\` | GUI（§H-2） | **状態の永続化。新版では廃止** |
| `%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe` | PowerShell 8本（Start-*.ps1, Test-*, Update-*, Get-*） | **Python の探索。Microsoft Store 版 Python 3.13 の決め打ち** |
| `%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe` | GUI:46 / .cmd:20 | pwsh の探索候補（読み取りのみ・許容範囲） |
| `%ProgramFiles%\PowerShell\7\pwsh.exe` | GUI:45 / .cmd:18 | 同上 |
| 環境変数 `VIDEO_CATALOG_PYTHON` | `Start-FamilyVideoCatalog.ps1:164` | 上書き用（良い設計・継承する） |
| 環境変数 `VIDEO_CATALOG_FFMPEG` / `_FFPROBE` | `tests/_support.py:67,72` | テスト用の明示的上書き（継承する） |

Python 探索の実装（全 8 スクリプトで重複コピーされている）:

```powershell
if ($env:VIDEO_CATALOG_PYTHON -and (Test-Path ...)) { return $env:VIDEO_CATALOG_PYTHON }
$known = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe'
if (Test-Path -LiteralPath $known) { return $known }
$fromPath = Get-Command python -ErrorAction SilentlyContinue
```

→ **第三者環境ではこの `$known` はまず一致しない**。新版では「同梱ランチャー1箇所で解決」へ集約する。

---

## J. 固定絶対パス（棚卸し全件）

### J-1. コード中（必ず修正が必要）

| 値 | 箇所 | 対応 |
|---|---|---|
| `D:\動画内容解析システムデータ` | `config.py:27` (`DEFAULT_DATA_ROOT`) | **削除**。APP_ROOT から導出 |
| `D:\動画内容解析システムデータ` | `Start-FamilyVideoCatalog-GUI.ps1:117` (`$DefaultDataRoot`) | GUI ごと作り直し |
| `D:\動画内容解析システムデータ` | `cli.py:83`（`--data-root` の help 文字列） | 文言修正 |
| `D:\動画内容解析システムデータ` | `settings.example.json:12` | プレースホルダ化 |
| `D:\動画内容解析システムデータ` | `scripts\Export-Catalog.ps1:12,18` / `Get-LocalAiModels.ps1:16` / `Start-Prototype01.ps1:25` の help | 文言修正 |
| MS Store Python 3.13 の絶対パス | PowerShell 8本 | ランチャーへ集約 |

### J-2. `settings.local.json`（Git 管理外・**新版へ持ち込み禁止**）

- `data_root`: `D:\動画内容解析システムデータ`
- `ffmpeg_path` / `ffprobe_path`: `C:\Users\User\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_...\bin\ffmpeg.exe` / `ffprobe.exe`

### J-3. ドキュメント／テスト中（**害はないが一般版では書き換える**）

- `README.md`: `D:\動画内容解析システムデータ\...` が 12 箇所、`F:\MP4に変換済動画` / `F:\ホームビデオ` が 8 箇所
- `tests/`: `F:\ホームビデオ\TAPE-001.mp4`、`F:\videos\20090815_旅行.m4v`、`D:\データ 保存先\日本語 フォルダー\clip name.mp4` 等
  → これらは**実在パスではなく合成テストデータ**だが、`ホームビデオ` `旅行` 等の語は一般版では中立な語へ置換する
- `tests/test_gui_defaults.py:25`: `EXPECTED_DATA_ROOT = "D:\\動画内容解析システムデータ"` を**テストで固定している**
  → 新版では「APP_ROOT から導出されること」を検証するテストへ置き換える
- `環境・既存資産調査_20260806.md`（114KB）: **実フォルダー名・実動画ファイル名を多数含む。Git 管理外。持ち込み厳禁**

---

## K. ffmpeg 検出方法

```python
# config.py:288-326
resolve_ffprobe(): 設定値があれば is_file() 検証 → resolve()。無ければ shutil.which("ffprobe")。
                   どちらも駄目なら ConfigError（PATH に頼らず絶対パス設定を促す）
resolve_ffmpeg():  同様。ただし **見つからなくても None を返して続行**（Prototype01 は ffmpeg 不要）
```

**PATH に依存させない理由がコードのモジュール docstring に明記されている**（`config.py:9-11`）:
このPCには複数の ffmpeg があり、**whisper フィルターを持つのは片方だけ**のため。

検証系:

- `probe_tool_version()` — `-hide_banner -version` の1行目を取得
- `ffmpeg_has_whisper()`（`config.py:273`）と `environment_check.whisper_filter_available()`（:110）が
  **同じ判定を別実装で 2 箇所持っている**（前者は `line.split()[1:2] == ["whisper"]`、後者は単純な部分一致）
  → 新版では 1 箇所へ統合すべき軽微な重複

---

## L. LM Studio 検出方法

`model_catalog.list_chat_models(base_url)` が localhost の OpenAI 互換 `/v1/models` を叩くだけ。
**プロセス検出・ポートスキャン・インストール検出はしていない。**

- 既定 `base_url` = `http://127.0.0.1:1234/v1`
- 呼ぶ前に必ず `vlm_client.assert_local_base_url()` を通す
- 失敗理由は `friendly_error()` で日本語化（接続拒否 / VRAM / モデル未検出を区別）
- **重要な修正履歴**: 旧版は timeout を「LM Studio 未起動」と誤表示していた。
  現在は `Get-VisualFailureInfo`（`Start-FamilyVideoCatalog.ps1:366-381`）で終了コードを
  `connection(9) / timeout(8) / model(6) / privacy(7) / frames(5) / other` へ分類し、
  **「制限時間を超えた」を「LM Studio が起動していない」と言わない**とコメントで明示している

---

## M. VLM model 指定方法

| 経路 | 実装 |
|---|---|
| 設定 | `vlm.model_match`（既定 `qwen3-vl-8b-instruct`、`model_catalog.py:212-214`） |
| CLI | `Start-Prototype03.ps1 -ModelMatch` / `Start-FamilyVideoCatalog.ps1 -VisualModel` |
| GUI | 「ローカルAI設定…」ダイアログ。**設定ファイルを書き換えず**、`environment_check.apply_model_overrides()` で実行時に上書き |
| 選択 | `vlm_client.select_model(available, wanted)` が一意に決められなければ `ModelSelectionError` で停止（**勝手に選ばない**） |

説明文生成モデルは `description.model_match`。**空文字なら「映像解析と同じモデル」**を意味する
（`model_catalog.SAME_AS_VISUAL = ""`）。この工程は画像を送らないので文章専用モデルでもよい。

**モデル名は `config_hash` に含まれる**ため、モデルを変えると別モデルの解析結果が再利用されることはない。
一方 **timeout 値は `config_hash` に含まれない**（`vlm_client.py:321-323` に明記）ので、
待ち時間を変えても保存済みフレーム解析はそのまま再利用される。

→ **hard-code は `model_catalog.py:214` と `visual_prompts` の既定値程度**で、
差し替え可能な設計になっている。新版でも既定値としてだけ持つ。

---

## N. Whisper model 指定方法

```python
# model_catalog.py:34
WHISPER_DIRECTORY = ("models", "whisper")   # <DataRoot>\models\whisper\*.bin
```

`check_whisper_choice()`（:187）が課す条件が**実装上きわめて重要**:

1. ファイル名だけで指定すること（`Path(name).name != name` なら拒否）
2. `<DataRoot>\models\whisper\` 配下の実ファイルであること
3. 1 MiB 以上であること
4. **`to_relative_ascii(target, DataRoot)` が None でないこと**

条件 4 の理由（`asr_engine.py:5-10`）:

> **whisper.cpp は非 ASCII を含むパスのファイルを開けない。**
> `D:\動画内容解析システムデータ\models\...` を渡すと `failed to open` になる。
> 8.3 短縮名も D: では無効だった。
> → **ffmpeg の作業ディレクトリをデータルートにし、model と destination を
>    ASCII の相対パスで渡す**ことで回避する。
> 入力動画のパスは ffmpeg 自身が扱うため、日本語のままで問題ない。

`common_ascii_base()`（:495）は model と出力先が別階層でも共通の親を探して cwd にする。

**新版への含意**: APP_ROOT が `...\動画編集関係\local-video-catalog` のように
非 ASCII を含んでいても、**cwd を APP_ROOT にして ASCII 相対パスを渡す方式はそのまま成立する**
（`userdata/models/whisper/...`、`userdata/cache/asr/...` はすべて ASCII）。
むしろ One-Folder 化すると model と cache が確実に同一 root 配下に来るため、
`common_ascii_base()` の探索が不要になり**単純化される**。

既定モデル: `ggml-large-v3-turbo-q5_0.bin`。

---

## O. Resume / manifest / state 管理

**Resume の正本は SQLite の `stage_status` テーブル**であり、マニフェストファイルではない。

```
stage_report.PIPELINE_STAGES = [
    frame_extraction, visual_analysis, audio_transcription, description
]
```

処理対象の選択（`Start-FamilyVideoCatalog.ps1:311-319`）:

1. `python -m video_catalog.stage_report --format tsv --pending-only` で未完了動画を取得
2. `--ignore-stage` で「今回飛ばす工程」を未完了に数えない
3. `--only-catalog-id` で**失敗分だけの再試行**（工程の再利用ルールは変えない）
4. `--max-videos` で本数上限

多層の再利用キー:

| 層 | キー |
|---|---|
| 工程完了 | `stage_status(asset_id, stage_name).status` |
| 静止画 | `(asset, impl_version, config_hash, source_quick_fingerprint, target_time_ms)` |
| フレーム解析 | `(asset, frame_sha256, model_id, prompt_version, impl_version, config_hash)` |
| 視覚概要 | `(asset, source_frame_analysis_hash, model_id, prompt_version, impl_version, config_hash)` |
| ASR チャンク | `(asset, src_fp, audio_stream_idx, chunk_index, start, duration, engine, impl, model_sha256, config_hash)` |

安全停止の3系統（すべて**次回そのまま続きから**）:

| 種別 | 契機 | 粒度 |
|---|---|---|
| 時間予算 | `-TimeBudgetMinutes` 到達 | 動画の切れ目 / ASR はチャンク境界 |
| 停止要求 | `StopRequestFile` の出現 | 工程の切れ目 |
| 同種障害の連続 | VLM が `connection/timeout/model/privacy` で **3 本連続**失敗 | 即座に break |

連続失敗ガードのコメント（`Start-FamilyVideoCatalog.ps1:354-358`）は判断根拠まで残っている:

> 3 本にしたのは、1 本目は個別の動画の問題かもしれず、2 本目でも偶然が残るが、
> 「成功を 1 本も挟まずに 3 本続けて同じ種類で落ちる」のは設備側の問題だから。
> 1 本あたり最大 20 分（視覚概要の待ち時間）と見ても、無駄は 1 時間で止まる。

---

## P. cache 構造

```
<DataRoot>\cache\
├─ probe\<asset_id>.json.gz                        ffprobe 生 JSON（gzip）
├─ scenes\<asset_id>\<impl>\<config_hash>\...       代表静止画 JPEG + manifest
├─ vlm\<asset_id>\<impl>\<config_hash>\...          frame_*.analysis.json / asset_visual_summary.json
└─ asr\<asset_id>\<impl>\<config_hash[:12]>\
       └─ src_<64桁hex>\                            元動画 fingerprint ごとの名前空間
              ├─ work\                              ffmpeg 作業用
              ├─ chunk_*.json
              └─ transcript_<scope>.json
```

`src_<fingerprint>` 名前空間は v0.4.3 で導入。**元ファイルの内容が差し替わっても新旧が同じパスを奪い合わない**ため。

`probe_cache` は「動画を読み直さずキャッシュの再解析だけで新項目を補完する」経路に使われる
（`FFPROBE_IMPL_VERSION` を 2 へ上げたときに実際に使った実績がある）。

---

## Q. cleanup / recycle bin 処理

`recycle.py`（192行）— **新版へほぼそのまま移植できる最良の資産のひとつ**。

```python
PROTECTED_DIRECTORY_NAMES = {"models", "logs", "catalog", "descriptions", "config"}
CLEANABLE_CACHE_NAMES     = ("scenes", "vlm", "asr")
```

`is_protected(path, data_root)` の判定順（`recycle.py:48-68`）:

1. `path.resolve().relative_to(data_root.resolve())` が失敗 → **保護**（DataRoot の外は絶対に触らない）
2. 先頭要素が `PROTECTED_DIRECTORY_NAMES` → 保護
3. 先頭要素が `cache` でない → 保護
4. 2 番目が `{scenes, vlm, asr}` でない → 保護
5. 深さ 3 未満（= `cache/vlm` のような親フォルダー自体） → 保護

**完全削除へフォールバックしない**（`send_to_recycle_bin`）:

- Windows `SHFileOperationW` を ctypes で呼び、`FOF_ALLOWUNDO` でゴミ箱送り
- 非 Windows なら `RecycleError`
- 戻り値・`fAnyOperationsAborted`・**実際に消えたかの再確認**の 3 段階で検証
- どこかで失敗したらファイルを残して `RecycleError`

呼び出し条件: **`asset_descriptions` に最終テキストが正常記録された動画だけ**
（`description_cli.py` の `--recycle-cache`、GUI の「完了後、中間ファイルをゴミ箱へ移動する」）。
処理中・失敗した動画の cache は残るので Resume が壊れない。

`gui_maintenance.py`（272行）は `%LOCALAPPDATA%` 側の掃除。二重の安全確認をしている:

- フォルダー名が `FamilyVideoCatalog` であること
- 直下に `descriptions/catalog/models/cache/logs/config` の**いずれも無い**こと
  （= DataRoot を誤って指したら触らない）

→ **新版では `%LOCALAPPDATA%` を使わないためこのモジュールごと不要**（§H-2）。ただし
「フォルダーの正体を名前と中身の両方で確かめてから消す」という発想は cleanup テストへ継承する。

---

## R. GUI 設定保存

`%LOCALAPPDATA%\FamilyVideoCatalog\gui-settings.json` に以下を保存（`GUI.ps1:99-114`）:

`SourceFolder` / `DataRoot` / `Recursive` / `TimeBudgetMinutes` / `MaxVideos` /
`NoTimeLimit` / `NoVideoLimit` / `SkipVisualAnalysis` / `RecycleCache` /
`VisualModel` / `DescriptionModel` / `WhisperModel`

保存失敗しても解析には影響しないので**黙って続ける**（:91-93）。この寛容さは継承してよい。
保存先だけが問題（→ 新版は `APP_ROOT\userdata\config\`）。

---

## S. HTML 生成

`html_catalog.py`（777行）。**入力は SQLite ではなく `<DataRoot>\descriptions\VID-*.txt`**、
出力は単一ファイル `<DataRoot>\catalog.html`。

守られている方針（モジュール docstring）:

- **正本は元動画・SQLite 台帳・説明文 txt。HTML は派生物**で、消しても作り直せる
- **完全ローカル。外部 CDN・フォント・スクリプト・API を使わない。** CSS と JS は埋め込み
- **勝手に解釈しない。** 説明文に「解釈保留」とあるものを HTML 生成時に日付へ読み替えない（並び順は末尾）
- **必ずエスケープする。** ファイル名や本文の `< & "` で HTML も JS も壊れない
- **画像を使わない**（代表静止画は説明文作成後にゴミ箱へ送る設計と整合）

絞り込み: すべて / 完了 / 日付不明 / 日付解釈保留 / 音声なし / ASR未完了 / 映像解析未完了 / 説明文未生成 / 要確認

---

## T. description 生成

`description_cli.py`（566行）+ `description_builder.py`（384行）。

- **既にある解析結果だけを材料にする。元動画をもう一度 AI へ渡さない。**
- 出力: `<DataRoot>\descriptions\VID-000001_<元動画名>.txt`（**原子的書き込み**: `.tmp` へ書いて `replace`）
- 生成物は 2 種類: `content`（台帳用・2〜5文）と `youtube`（概要欄用・2〜6文）
- LM Studio が使えなければ**定型文へフォールバック**（`GENERATOR_FALLBACK`）。
  定型文には「内容は確認できていません」等の目印が入り、HTML 側で「定型文」バッジになる

プロンプトの厳守事項（`description_cli.py:52-70`）— **§9「AI推定を事実へ昇格させない」の実装本体**:

> - material に無いことを書かない。推測で補わない。
> - 人物名・家族関係・学校名・地名・行事名は、material に明記がない限り書かない。
>   例: 根拠がなければ「息子の運動会」ではなく「屋外での行事のような様子」と書く。

`usable_transcript_text()`（:120-147）が**幻覚対策の中核**:

```python
for segment in segments:
    if segment["is_suspected_hallucination"]:
        excluded += 1
        continue
    kept.append(segment["text"] or "")
```

docstring に実測値が残っている:

> 2026-08-13 の実測では 21 本中 9 本で、この種の本文が先頭 1200 字の抜粋へ入り込んでいた（最大 9.4%）。
> **消すわけではない。** `transcripts.full_text` も `transcript_segments` も印もそのまま残る。
> ここで作るのは「今回 AI へ渡す材料」だけ。

材料が幻覚だけだった場合は「音声: 内容として使える発話は確認できていません。」として扱い、
**無理に使わない**（:92-94）。

判定側（`transcript_schemas.py`）: `looks_like_hallucination(text)`（:206）が既知定型を判定し、
`is_suspected_hallucination` を**セグメント単位**で立てる。
`max_consecutive_repetition`（:221）は**警告文の材料に使うだけで削除条件にしていない**
（`transcript_schemas.py:360` の「参考: 同一の文が N 回連続しています」）。
→ 家族・日常・会話動画では短い語が本当に繰り返されるため、という設計判断がそのまま実装されている。

---

## U. privacy guard

旧版の privacy guard は**コード側 3 層 + CI 側 4 チェック**で構成されている。

### U-1. コード側（`vlm_client.py:1-155`）

| # | 仕組み | 実装 |
|---|---|---|
| 1 | ホスト限定 | `ALLOWED_HOSTNAMES = {localhost, 127.0.0.1, ::1}` + ループバック IP。**名前解決に頼らない**（hosts 書換えで外部へ向く余地を消す） |
| 2 | リダイレクト全拒否 | `_NoRedirectHandler` が 3xx を `PrivacyConfigurationError` に変換（302 で外部へ転送される事故を防ぐ） |
| 3 | プロキシ無効化 | `ProxyHandler({})` で `HTTP_PROXY` / `HTTPS_PROXY` を無視 |
| 4 | 逃げ道を作らない | 「開発時だけ外部を許可する」オプションを**実装していない** |
| 5 | 毎回検証 | `_request()` が呼ばれるたびに `assert_local_base_url(url)`（base_url を後から書き換えられても守る） |
| 6 | 記録の最小化 | `safe_api_base_for_record()` が scheme://host:port だけを DB へ残す |
| 7 | 画像入力の検査 | `model_catalog.synthetic_png()` が**その場で単色 PNG を生成**。家族の画像を検査に使わない |

### U-2. `.gitignore`（169行）

「実データは D ドライブにありリポジトリ外だが、将来の設定変更・シンボリックリンク・
誤コピーに備えて**ここでも同種のファイルを名前で遮断する**」という多重防御の方針。
メディア 20 種、`descriptions/`、`VID-*.txt`、`catalog.html`、`src_*/`、
`transcript*.json`、`cache/{asr,vlm,scenes}/`、`*.sqlite3`、`*.bin`、`*.gguf` 等。

### U-3. CI（`.github/workflows/tests.yml` の `privacy-check` ジョブ）

`git ls-files` に対して 4 種の検査:

1. メディア・台帳・モデル拡張子が追跡されていないか
2. `config/settings.local.json` が追跡されていないか
3. 文字起こし・解析キャッシュ・`src_<hex>` 名前空間・`descriptions/`・`catalog.html` が追跡されていないか
4. `環境・既存資産調査*` が追跡されていないか

加えて `integration` ジョブの最後に
「`git status --porcelain --untracked-files=all` が空であること」= **テストがリポジトリを汚さない**検証がある。

---

## V. GitHub Actions

`tests.yml`（182行）— 3 ジョブ。`permissions: contents: read`。

| ジョブ | 内容 |
|---|---|
| `unit` | Python 3.13。標準ライブラリのみで動くことを確認 → `VIDEO_CATALOG_FFMPEG=/nonexistent/ffmpeg` を設定して **ffmpeg 不在を再現** → `unittest discover` |
| `integration` | apt で ffmpeg を入れて全テスト → **作業ツリーが clean であることを検証** |
| `privacy-check` | §U-3 |

**CI が依存しないもの**（ワークフロー冒頭に明記）: 実際の家族動画 / D ドライブ・ユーザー固有パス /
LM Studio・外部 AI API / モデルのダウンロード / `settings.local.json`。
使うデータは ffmpeg の `testsrc`（カラーバー）と `sine`（正弦波）だけ。

---

## W. 現在のテスト構造

- 31 テストファイル（+ `_support.py` + `make_fixtures.py`）、**テスト関数 1,018 個**（静的カウント）
- 実行: `PYTHONPATH=src python -X utf8 -m unittest discover -s tests -p "test_*.py"`
- **サードパーティ依存ゼロ**（pytest すら使わない）

`tests/_support.py` の設計（**新版へそのまま移植可能**）:

| 要素 | 内容 |
|---|---|
| `TempDirTestCase` | `tempfile.TemporaryDirectory` を必ず後始末する土台 |
| `find_ffmpeg/ffprobe` | 環境変数 → `settings.local.json` → PATH の順。**環境変数が設定されていれば最終決定**として扱い、存在しなければ None を返してフォールバックしない（CI で「ツール不在」を再現するため） |
| `requires_ffmpeg` / `requires_ffprobe` | `skipUnless` デコレータ |
| `make_synthetic_video` | `testsrc` + `sine` で合成動画を生成。**実動画は一切使わない** |

安全性テストの存在を確認した主なもの:
`test_recycle.py`(180行) / `test_gui_maintenance.py`(303行) / `test_visual_summary_timeout.py`(230行) /
`test_description_material.py`(254行・幻覚除外) / `test_migration.py`(461行) / `test_gui_defaults.py`(566行)

---

## X. 一般版へそのまま再利用できるコード

| モジュール | 行 | 再利用度 | 備考 |
|---|---|---|---|
| `vlm_client.py` | 456 | **ほぼそのまま** | privacy guard の中核。文言のみ「家族の映像」→中立表現 |
| `recycle.py` | 192 | **ほぼそのまま** | 保護名リストを `userdata` 基準へ調整するのみ |
| `transcript_schemas.py` | 528 | **ほぼそのまま** | 幻覚判定・正規化 |
| `visual_schemas.py` | 538 | **ほぼそのまま** | VLM 応答の JSON スキーマ検証 |
| `fingerprint.py` | 181 | そのまま | |
| `audio_streams.py` | 158 | そのまま | |
| `pathinfo.py` | 167 | そのまま | |
| `datetime_candidates.py` | 347 | そのまま | 日本語ファイル名の日付解釈規則 |
| `probe.py` | 577 | そのまま | 主映像ストリーム選択（attached_pic 除外）を含む |
| `discovery.py` | 185 | そのまま | |
| `frame_extractor.py` | 758 | **出力先の導出のみ変更** | |
| `visual_analyzer.py` | 740 | **出力先の導出のみ変更** | |
| `visual_prompts.py` | 225 | そのまま | 文言の一般化のみ |
| `asr_engine.py` | 883 | **ほぼそのまま** | ASCII 相対パス方式は One-Folder でむしろ単純化 |
| `database.py` | 2,118 | **schema はそのまま** | 新規 DB として作る（migration 履歴は不要） |
| `stage_report.py` | 320 | そのまま | Resume の中核 |
| `html_catalog.py` | 777 | 文言の一般化 | |
| `description_builder.py` | 384 | 文言の一般化 | |
| `exporters.py` / `run_summary.py` / `logging_utils.py` / `model_catalog.py` | — | ほぼそのまま | |
| `tests/_support.py` | 151 | **そのまま** | |
| `tests/test_*.py` 31本 | — | **大半が再利用可能** | パス期待値と日本語固有名詞のみ差し替え |
| `.github/workflows/tests.yml` | 182 | 骨格を継承 | userdata 除外の検査を追加 |

---

## Y. 一般版向けに変更が必要なコード

| 対象 | 必要な変更 | 難度 |
|---|---|---|
| `config.py` | `DEFAULT_DATA_ROOT` を廃止し **APP_ROOT 導出**へ。`Settings` の派生 property を `userdata/` 基準へ。`verify_data_root` の意味を変更 | 中 |
| `description_cli.py:313` / `html_catalog.py:36,740` / `model_catalog.py:167` / `frame_extractor.py:264` / `visual_analyzer.py:126` / `asr_engine.py:314` / `asr_cli.py:170` / `recycle.py:153` | **DataRoot 直下組み立てを 1 箇所（paths モジュール）へ集約** | 中 |
| PowerShell 15本 | **全廃**（Python + tkinter へ）。ただし**仕様書として保存し機能パリティ表を作る** | 大 |
| `Start-FamilyVideoCatalog-GUI.ps1` 1,871行 | Python + tkinter へ移植（§C の機能表が仕様） | **最大** |
| `gui_maintenance.py` | **不要**（`%LOCALAPPDATA%` を使わないため） | — |
| `environment_check.py` | PowerShell 7 検出を削除、Python 同梱前提の項目へ。ffmpeg 検出の重複統合 | 小 |
| `settings.example.json` | `<APP_ROOT>` 相対の既定へ。日本語コメントを一般化 | 小 |
| `.gitignore` | `/userdata/` 一括除外を主軸に再構成 | 小 |
| すべての文言 | 「家族」「ホームビデオ」→ 中立表現。`FamilyVideoCatalog` → `local-video-catalog` | 中 |
| `catalog_id` | `VID-` プレフィクスは中立なので**維持**（変更不要） | — |

---

## Z. 一般版へ持ち込むべきでないファイル／データ

### Z-1. 絶対に持ち込まない（実データ・個人情報）

| 対象 | 所在 |
|---|---|
| 本番 SQLite | `D:\動画内容解析システムデータ\catalog\video_catalog.sqlite3` |
| 実説明文 | `D:\...\descriptions\VID-*.txt`（生活内容 + 元動画フルパスを含む） |
| 実 HTML カタログ | `D:\...\catalog.html` |
| 本番 cache | `D:\...\cache\{probe,scenes,vlm,asr}\` |
| 本番 log / runs | `D:\...\logs\`, `D:\...\runs\` |
| Whisper モデル | `D:\...\models\whisper\*.bin`（500MB超・ライセンス的にも同梱しない） |
| 実動画 | `F:\...` 等 |
| CSV/JSON/JSONL エクスポート | `D:\...\catalog\exports\` |

### Z-2. リポジトリ内にあるが持ち込まない

| ファイル | 理由 |
|---|---|
| `config/settings.local.json` | 実 data_root・winget 配下の実 ffmpeg 絶対パス |
| `環境・既存資産調査_20260806.md`（114KB） | **実フォルダー名・実動画ファイル名を多数含む**。旧版でも Git 管理外 |
| `docs/Prototype0*_実装結果_*.md` | 実運用の測定値・実データ由来の記述が混在しうる。**再利用するなら該当箇所を確認して抜粋のみ** |
| `README.md`（29.7KB） | 実パス 20 箇所以上。新規に書き起こす |
| `scripts/benchmark_vlm.py` | benchmark 実データ前提。必要なら作り直す |
| `src/**/__pycache__/`, `tests/**/__pycache__/` | ビルド生成物 |

### Z-3. Git 履歴

**旧リポジトリの Git 履歴（32 コミット）を新リポジトリへ一切持ち込まない。**
`git subtree` / `git filter-repo` / cherry-pick も使わない。新リポジトリは
`1441dc7 Initial commit` から始まる**新しい履歴のみ**を維持する。
移植はファイル内容の手作業コピーと書き直しで行う。

---

## 最も危険な移植ポイント（リスク登録）

| ID | リスク | 影響 | 緩和策 |
|---|---|---|---|
| **R-1** | **One-Folder 化により実データが Git 作業ツリー内に来る**（旧版では構造的にあり得なかった） | 個人情報の Git 混入 | ① `/userdata/` 一括除外 ② privacy guard を CI と pre-commit の両方で ③ **開発クローンと運用フォルダーを分離**（確定事項）④ `userdata/` に `.gitignore`(`*`) を置く二重化 |
| **R-2** | **cleanup が APP_ROOT の外へ出る** | 元動画・モデル・ユーザーの他フォルダーを喪失 | `recycle.is_protected` を「`APP_ROOT/userdata/cache/{...}` の深さ3以上」に限定し、**設定値が壊れていても APP_ROOT を基準にする**（設定由来のパスを cleanup の基点にしない）。テストで境界を固定 |
| **R-3** | **summary timeout の分離を失う** | 22〜24フレーム動画が大量失敗（2026-08-12 に実際に発生） | `summary_timeout_seconds` を独立設定として移植。`test_visual_summary_timeout.py` を必ず移植。**timeout を `config_hash` に含めない**ことも維持 |
| **R-4** | **ASR の VAD を「良かれと思って」有効化** | 無音60秒 3秒→598秒、日本語 CER 0.000→0.737 | 既定 `vad_enabled: false` を維持し、**理由をコメントと設定コメントの両方に残す**。テストで既定値を固定 |
| **R-5** | **幻覚セグメントの扱いを単純化** | 実在の繰り返し発話を削除 / 幻覚が説明文へ混入 | 「保存はする・材料からのみ除外する」二段構えを維持。`is_suspected_hallucination` 列と `usable_transcript_text()` を対で移植。**「同一文が N 回連続」を削除条件にしない** |
| **R-6** | **whisper.cpp の非 ASCII パス制約を失念** | 第三者環境で ASR が `failed to open` で全滅 | cwd=APP_ROOT + ASCII 相対パス方式を移植。`check_whisper_choice` の 4 条件を維持。**APP_ROOT 自身が非ASCII でも成立する**ことをテストで固定 |
| **R-7** | **連続失敗の安全停止を落とす** | 一晩中同じ失敗を繰り返す | `ConsecutiveVlmFailureLimit=3` と終了コード分類（9/8/6/7/5）を Python 側オーケストレータへ移植 |
| **R-8** | **GUI 移植時に安全機能が欠落**（1,871行 → tkinter） | 安全停止・Resume・失敗のみ再試行が失われる | **機能パリティ表を先に作り**、各項目にテストまたは手動確認手順を紐づける。GUI ロジックと解析本体を分離し unittest 可能に |
| **R-9** | **子プロセスの文字コード指定漏れ** | 日本語 Windows で GUI のログが文字化け | `PYTHONIOENCODING` / `PYTHONUTF8` の設定を移植。tkinter 側でも `subprocess` の `encoding="utf-8"` を明示 |
| **R-10** | **AI 推定の事実化** | 誤った家族関係・場所・日付が確定情報として残る | プロンプトの厳守事項、`is_family_confirmed` / `confirmed_by_family` 列、HTML の「解釈保留」非解釈をすべて移植 |
| **R-11** | 旧版を誤って変更する | 現役の本番運用が壊れる | 旧フォルダーは read-only 参照のみ。移植はファイル内容の手コピーで行い、旧側で formatter・テスト・git 操作をしない |
| **R-12** | 第三者環境で Python / ffmpeg / LM Studio が見つからない | 起動すらできない | 自動インストールはせず、環境チェックで「見つかった/見つからない/バージョン/何を用意すればよいか」を明示（プロンプト §14） |

---

## 付録: 実運用で得られた数値（新版でも失わない）

| 項目 | 実測値 |
|---|---|
| VLM フレーム解析 | 20〜40 秒/枚（timeout 300 秒） |
| VLM 視覚概要 | 約 16 秒/枚 → 24 枚で約 390 秒。**300 秒 timeout では不足**（timeout 1200 秒） |
| LFM2.5-VL-3B Q6_K | Qwen より約 5 倍高速だが schema 適合率低下・存在しない frame 参照・JSON 途中切断・根拠のない人物関係/性別断定・「画像が提供されていません」→**不採用** |
| Whisper VAD 有効時 | 無音 60 秒: 約 3 秒 → 約 598 秒。日本語 CER: 0.000 → 0.737 → **無効が既定** |
| Whisper `queue` | 既定 3 秒では窓が重なり同内容が繰り返し出力される → **30 秒** |
| ASR チャンク | 300 秒。中断時の損失を最大 1 チャンクに限定 |
| 幻覚混入率 | 2026-08-13 実測 21 本中 9 本（抜粋先頭 1200 字に対し最大 9.4%） |
| VLM 同時要求数 | 1 固定（VRAM 6GB 環境で同時送信しない） |
