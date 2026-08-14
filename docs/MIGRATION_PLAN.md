# local-video-catalog — 移植計画

- 作成日: 2026-08-14
- 前提: [CURRENT_SYSTEM_AUDIT.md](CURRENT_SYSTEM_AUDIT.md) / [PORTABLE_V1_DESIGN.md](PORTABLE_V1_DESIGN.md)

---

## 0. 移植の原則

| # | 原則 |
|---|---|
| 1 | 旧個人版リポジトリ（別フォルダーの「動画内容解析システム」）は **read-only 参照**。変更・テスト実行・git 操作を一切しない |
| 2 | **Git 履歴を持ち込まない。** ファイル内容の手作業コピーと書き直しのみ |
| 3 | **土台から順に積む。** 依存の少ないモジュールから移し、各 Phase で必ずテストが通る状態にする |
| 4 | **安全機能を後回しにしない。** cleanup 境界・privacy guard は本格移植より先に固める |
| 5 | 各 Phase の終了時に commit する。**commit 前に必ず `tools\privacy_guard.py` を通す** |
| 6 | 実動画は使わない。合成動画（`testsrc` / `sine`）のみ |
| 7 | 実証済みの仕組みを再発明しない。**動いているものは移す**（プロンプト §28） |

### 0-1. 移植時の共通置換ルール

| 旧 | 新 |
|---|---|
| パッケージ `video_catalog` | `local_video_catalog` |
| `data_root` / `DataRoot` | `paths.userdata_dir()` 等（設定キーとしては廃止） |
| `cache/scenes` | `cache/frames` |
| `FamilyVideoCatalog` | `local-video-catalog` |
| 「家族の映像」「ホームビデオ」「家族が読む」 | 「動画」「利用者が読む」等の中立表現 |
| `is_family_confirmed` | `is_user_confirmed` |
| `confirmed_by_family` | `confirmed_by_user` |
| テストに埋め込まれた個人的な日本語パス・ファイル名（`ホームビデオ` `旅行` 等） | `X:\videos` `20090815_trip.m4v` 等の中立な合成値 |

---

## Phase 0 — 安全基盤（**本セッションで実施済み**）

| 成果物 | 内容 |
|---|---|
| `.gitignore` | `/userdata/` 一括除外を主軸に再構成 |
| `userdata\.gitignore` | `*` + `!.gitignore` の二重防御 |
| `tools\privacy_guard.py` | 個人データ混入検査（CI + ローカル） |
| `.github\workflows\ci.yml` | privacy guard + 標準ライブラリのみ検証 + テスト |
| `docs\*.md` | 調査・設計・移植計画の 3 文書 |
| `app-root.marker` | APP_ROOT 判定用マーカー |
| `README.md` | 一般配布版としての説明 |

**終了条件**: CI が green。`privacy_guard.py` がローカルで green。作業ツリー clean。
**この時点で本格移植へは進まず、ユーザー承認を待つ。**

---

## Phase 1 — 土台（paths / config / logging）

**目的**: One-Folder の骨格を作り、以降のすべてのモジュールが従う保存先の唯一の導出元を確立する。

| 作業 | 出典 |
|---|---|
| `paths.py` を**新規作成** | 旧 `config.Settings` の派生 property（`config.py:117-169`）+ 各モジュールが直接組み立てていた 8 箇所（AUDIT §G）を統合 |
| `config.py` を移植・改造 | 旧 `config.py`。`DEFAULT_DATA_ROOT` 削除、`data_root` 設定キー廃止、`verify_data_root` を `ensure_userdata_tree` へ |
| `logging_utils.py` を移植 | 旧 `logging_utils.py`（196行）。ほぼそのまま |
| `settings.example.json` を新規作成 | 旧 example から実パスを除去し、`vlm` / `asr` / `description` セクションの**コメント（実測根拠つき）を維持** |

`paths.py` の関数（案）:

```python
def app_root() -> Path            # LOCAL_VIDEO_CATALOG_ROOT → app-root.marker 探索
def userdata_dir() -> Path        # app_root() / "userdata"
def config_dir() -> Path
def settings_path() -> Path
def gui_state_path() -> Path
def catalog_dir() -> Path
def database_path() -> Path
def catalog_html_path() -> Path
def export_dir() -> Path
def descriptions_dir() -> Path
def cache_dir() -> Path
def probe_cache_dir() -> Path
def frames_cache_dir() -> Path
def vlm_cache_dir() -> Path
def asr_cache_dir() -> Path
def whisper_models_dir() -> Path
def log_dir() -> Path
def runs_dir() -> Path
def temp_dir() -> Path
def stop_request_path() -> Path
def ensure_userdata_tree() -> None
def to_app_relative(p: Path) -> str | None   # DB 保存用（§8-2）
def from_app_relative(s: str) -> Path
```

**終了条件**

- [ ] `paths.py` の全関数が APP_ROOT 配下だけを返すことをテストで固定
- [ ] `LOCAL_VIDEO_CATALOG_ROOT` による上書きが効く（テスト用）
- [ ] APP_ROOT が**非 ASCII を含むパス**でも全関数が正しく動くテスト
- [ ] マーカーが無い場所から呼ぶと `ConfigError` で停止する
- [ ] 設定の 3 層マージが旧版と同じ挙動（`None` 無視・深いマージ）
- [ ] `settings.json` に `data_root` を書いても**無視される**ことをテストで固定
- [ ] CI green / privacy guard green

---

## Phase 2 — 台帳（database）

| 作業 | 出典 |
|---|---|
| `database.py` を移植 | 旧 `database.py`（2,118行）。**`SCHEMA_SQL` はそのまま**（`SCHEMA_VERSION = 1` として新規に開始） |
| 列名の一般化 | `is_family_confirmed` → `is_user_confirmed`、`confirmed_by_family` → `confirmed_by_user` |
| パス列の相対化 | `output_directory` / `result_file_path` / `description_file_path` を **APP_ROOT 相対**で保存（§8-2）。`current_path` / `original_path` は絶対のまま |
| migration 機構 | 骨格だけ残す（旧版の v1→v7 移行履歴は**持ち込まない**） |

**終了条件**

- [ ] 16 テーブルが作成され、`PRAGMA foreign_keys=ON` / WAL が効いている
- [ ] `catalog_id` の連番発行（`VID-000001`）が旧版と同じ
- [ ] APP_ROOT 相対パスの往復（保存→読み出し）がテストで固定
- [ ] 旧 `test_database.py`(467行) / `test_migration.py`(461行) を移植・調整して green
- [ ] CI green / privacy guard green

---

## Phase 3 — cleanup 境界（**最優先の安全機能。本格移植より先に固める**）

**この Phase を Phase 2 の直後に置く理由**: cleanup は「消す」機能であり、
後から境界を足すと、それまでに書いたコードが境界を前提にしていない状態が残る。
**境界を先に固定し、以降のモジュールがその上に載る形にする。**

| 作業 | 出典 |
|---|---|
| `recycle.py` を移植・改造 | 旧 `recycle.py`（192行）。`is_protected(path, data_root)` → **`is_cleanable(path)`（基点は必ず `paths.app_root()`）** |
| `SHFileOperationW` 実装 | 旧実装をそのまま（3段階検証・完全削除へフォールバックしない） |
| 境界テスト | 設計 §6-5 の 8 項目 |
| `gui_maintenance.py` | **移植しない**（`%LOCALAPPDATA%` を使わないため不要） |

**終了条件**

- [ ] 設計 §6-5 の 8 項目すべてが green
- [ ] **設定ファイルの内容が何であっても cleanup 対象が変わらない**ことをテストで固定
- [ ] symlink / junction / `..` で外を指しても弾かれる
- [ ] ゴミ箱送り失敗時にファイルが残ることを確認（モックで検証）
- [ ] 旧 `test_recycle.py`(180行) を移植・拡張して green
- [ ] CI green / privacy guard green

---

## Phase 4 — 解析コア（登録 → probe → 静止画）

**最初に移植する実処理**。外部 AI に依存せず、ffmpeg / ffprobe だけで完結するため検証しやすい。

| 作業 | 出典 | 変更量 |
|---|---|---|
| `pathinfo.py` | 旧 167行 | なし |
| `fingerprint.py` | 旧 181行 | なし |
| `datetime_candidates.py` | 旧 347行 | なし |
| `discovery.py` | 旧 185行 | なし |
| `probe.py` | 旧 577行 | なし（attached_pic 除外の主映像選択を含む） |
| `audio_streams.py` | 旧 158行 | なし |
| `frame_extractor.py` | 旧 758行 | 出力先を `paths.frames_cache_dir()` へ |
| `frame_cli.py` | 旧 533行 | `--data-root` を削除 |
| `cli.py`（登録・ffprobe） | 旧 1,074行 | `--data-root` を削除、help 文言修正 |
| `exporters.py` | 旧 482行 | 出力先を `paths.export_dir()` へ |
| `stage_report.py` | 旧 320行 | なし |
| `tests/_support.py` | 旧 151行 | **そのまま**（環境変数名のみ `LOCAL_VIDEO_CATALOG_*` へ） |

**終了条件**

- [ ] 合成動画を登録 → ffprobe → 静止画抽出 → CSV/JSON/JSONL 出力 が通る
- [ ] 生成物が `userdata\` の外に 1 件も出ないことをテストで固定
- [ ] **元動画フォルダーに一切書き込まないことをテストで固定**（元動画の mtime / サイズ / 内容が不変）
- [ ] ffmpeg 不在でも unit テストが走る（`requires_ffmpeg` でスキップ）
- [ ] 旧 `test_discovery/fingerprint/pathinfo/datetime_candidates/primary_video_stream/frame_planning/frame_extraction/frame_cli/exporters/stage_report` を移植して green
- [ ] CI green / privacy guard green

---

## Phase 5 — ローカル AI（VLM / ASR / 説明文 / HTML）

**privacy guard の中核と、実運用知見が最も濃い部分。**

### 5-1. VLM

| 作業 | 出典 | 注意 |
|---|---|---|
| `vlm_client.py` | 旧 456行 | **ほぼそのまま。** 8 層の防御（設計 §10-2）を 1 つも落とさない |
| `visual_schemas.py` | 旧 538行 | なし |
| `visual_prompts.py` | 旧 225行 | 文言の一般化のみ |
| `visual_analyzer.py` | 旧 740行 | 出力先を `paths.vlm_cache_dir()` へ |
| `visual_cli.py` | 旧 921行 | **`summary_timeout_seconds` の分離を必ず移植**（R-3） |

### 5-2. ASR

| 作業 | 出典 | 注意 |
|---|---|---|
| `transcript_schemas.py` | 旧 528行 | **幻覚判定・「N回連続は削除条件にしない」を維持**（R-5） |
| `asr_engine.py` | 旧 883行 | **ASCII 相対パス方式を維持**（R-6）。cwd は `app_root()` |
| `asr_cli.py` | 旧 1,770行 | 出力先を `paths.asr_cache_dir()` へ。**VAD 既定 false を維持**（R-4） |
| `model_catalog.py` | 旧 313行 | `whisper_directory()` を `paths.whisper_models_dir()` へ |

### 5-3. 説明文 / HTML

| 作業 | 出典 | 注意 |
|---|---|---|
| `description_builder.py` | 旧 384行 | 文言の一般化 |
| `description_cli.py` | 旧 566行 | **`usable_transcript_text()` の幻覚除外を維持**（R-5）。プロンプトの厳守事項を維持（R-10） |
| `html_catalog.py` | 旧 777行 | 入力を `paths.descriptions_dir()`、出力を `paths.catalog_html_path()` へ |
| `run_summary.py` | 旧 345行 | なし |

**終了条件**

- [ ] `assert_local_base_url` が localhost 以外を**画像を組み立てる前に**弾くことをテストで固定
- [ ] リダイレクト拒否・プロキシ無効化がテストで固定
- [ ] **frame timeout と summary timeout が独立**していることをテストで固定（旧 `test_visual_summary_timeout.py` を移植）
- [ ] **timeout を変えても `config_hash` が変わらない**ことをテストで固定
- [ ] **VAD 既定が false** であることをテストで固定
- [ ] **APP_ROOT が非 ASCII でも whisper へ ASCII 相対パスが渡る**ことをテストで固定
- [ ] `is_suspected_hallucination` が保存され、かつ材料からのみ除外されることをテストで固定（旧 `test_description_material.py` を移植）
- [ ] HTML が外部リソースを 1 つも参照しないことをテストで固定
- [ ] HTML が `<` `&` `"` を含む入力で壊れないことをテストで固定
- [ ] CI green / privacy guard green

---

## Phase 6 — オーケストレータ（`pipeline.py`）

旧 `Start-FamilyVideoCatalog.ps1`（595行）の PowerShell ロジックを **Python へ移す**。
GUI から独立して CLI としても動く。

| 移植項目 | 出典 |
|---|---|
| 5 工程の直列実行と工程スキップ | `Start-FamilyVideoCatalog.ps1:383-538` |
| 時間予算による安全停止 | 同 :388-392, 476-482 |
| 停止要求ファイルによる安全停止 | 同 :217-225, 384-387 |
| **同種障害 3 本連続での安全停止** | 同 :354-362, 446-456（R-7） |
| **終了コード → 日本語メッセージの分類** | 同 :366-381（R-7） |
| 本数上限 | 同 :215, 313 |
| 失敗のみ再試行（`--only-catalog-id`） | 同 :291-293 |
| DryRun（対象確認） | 同 :296-309 |
| 終了報告の文面 | 同 :548-583 |
| 子プロセスの UTF-8 指定 | 同 :151-157, 278-281（R-9） |

**同時に `docs\GUI_FEATURE_PARITY.md` を作成する**（Phase 7 の仕様書になるため）。
旧 GUI の `.Text` 定義から抽出した全機能（AUDIT §C）を表にし、各項目に
「新版での実現方法 / 担当モジュール / 検証方法」を記入する。

**終了条件**

- [ ] `python -m local_video_catalog.pipeline --source-folder <合成動画> --dry-run` が動く
- [ ] 3 系統の安全停止すべてに自動テストがある
- [ ] 連続失敗ガードが 3 本目で止まることをテストで固定（VLM をモック）
- [ ] 停止 → 再実行で完了工程が飛ばされることをテストで固定
- [ ] `GUI_FEATURE_PARITY.md` が全 15 項目を網羅
- [ ] CI green / privacy guard green

---

## Phase 7 — GUI（Python + tkinter）

**最も工数が大きい。** `GUI_FEATURE_PARITY.md` を仕様書として進める。

| 手順 | 内容 |
|---|---|
| 7-1 | `gui/state.py`（画面状態の保存/復元）を先に作り、**GUI なしで unittest** |
| 7-2 | `gui/runner.py`（別プロセス起動・stdout 取り込み・停止要求）を作り、**GUI なしで unittest** |
| 7-3 | `gui/app.py` で画面を組み立て、7-1/7-2 を呼ぶだけにする |
| 7-4 | ローカルAI設定ダイアログ |
| 7-5 | 中間ファイル整理ダイアログ（確認 → ゴミ箱へ移動） |
| 7-6 | `Start.cmd`（CP932。Python を探して GUI を起動） |

**終了条件**

- [ ] `GUI_FEATURE_PARITY.md` の 15 項目すべてが実装済み、または「削除する機能」として明記済み
- [ ] `state.py` / `runner.py` が GUI を起動せず unittest で検証できる
- [ ] 長時間処理中も画面が応答することを実機確認（合成動画で 10 分以上）
- [ ] 安全停止がプロセス kill でなくファイル生成であることをコードで確認
- [ ] 日本語の出力が文字化けしないことを実機確認
- [ ] PowerShell 7 が無い環境でも起動できることを実機確認
- [ ] CI green / privacy guard green

---

## Phase 8 — 配布・移動耐性・受け入れ

| 作業 | 内容 |
|---|---|
| 8-1 | **フォルダー移動テスト**（設計 §8-3）。A で解析 → B へコピー → 再解析が発生しないこと |
| 8-2 | **残骸ゼロ確認**。実行前後で `%LOCALAPPDATA%` / `%APPDATA%` / レジストリに差分がないこと |
| 8-3 | `tools\make_release.py`（`.git` / `.github` / `tests` / `docs` を除いた zip を作る） |
| 8-4 | `README.md` を配布版として仕上げる（実パスを書かない） |
| 8-5 | 第三者環境シミュレーション: 別フォルダーへ zip 展開して §12 の 14 項目を通す |

**終了条件**

- [ ] 設計 §12 の完成条件 14 項目すべて達成
- [ ] 設計 §12-1 の安全性条件 20 項目すべて達成
- [ ] CI green / privacy guard green / 作業ツリー clean

---

## 各 Phase 共通の終了条件

すべての Phase で以下を満たしてから次へ進む。

1. `python -m unittest discover -s tests -p "test_*.py"` が green
2. `python tools/privacy_guard.py` が green
3. `git status --porcelain --untracked-files=all` が空
4. サードパーティ Python パッケージが 0
5. **旧リポジトリに差分がゼロ**（`git -C <旧> status --porcelain` が空）
6. 意味のある単位で commit 済み

---

## 既存テストの再利用方針

旧版のテストは 31 ファイル・テスト関数 1,018 個。**大半が再利用できる。**

| 分類 | 方針 |
|---|---|
| `_support.py` | **そのまま移植**（環境変数名のみ変更）。合成動画生成・一時フォルダー土台・ツール検出は完成度が高い |
| 純粋ロジックのテスト（`fingerprint` / `datetime_candidates` / `transcript_schemas` / `visual_schemas` / `pathinfo` / `audio_streams` / `description_builder`） | **ほぼそのまま**。日本語の固有名詞を中立な値へ置換するのみ |
| パスを検証するテスト（`test_gui_defaults.py` の `EXPECTED_DATA_ROOT` 等） | **書き直す**。「D ドライブの固定値」→「APP_ROOT から導出されること」 |
| GUI のテスト（`test_gui_defaults.py` / `test_gui_maintenance.py`） | `test_gui_maintenance.py` は**破棄**（機能自体が不要）。`test_gui_defaults.py` は新 `gui/state.py` のテストへ作り直す |
| DB テスト（`test_database.py` / `test_migration.py`） | 移植。migration は新規スキーマ前提へ調整 |
| 安全性テスト（`test_recycle.py` / `test_visual_summary_timeout.py` / `test_description_material.py`） | **必ず移植し、さらに強化する**（R-2 / R-3 / R-5） |
| 統合テスト（`test_integration.py` 530行） | 移植。5 工程の一気通貫を合成動画で |

**移植の順序**: 対応するモジュールと**同じ Phase で**移す。テストを後回しにしない。

---

## 新しく必要なテスト（旧版に無かったもの）

| # | テスト | 対応リスク | Phase |
|---|---|---|---|
| 1 | `paths.*` が APP_ROOT 配下だけを返す | R-1 | 1 |
| 2 | APP_ROOT が非 ASCII を含んでも全関数が動く | R-6 | 1 |
| 3 | マーカー不在時に `ConfigError` で停止する | — | 1 |
| 4 | `settings.json` の `data_root` が無視される | R-2 | 1 |
| 5 | **`is_cleanable` が APP_ROOT 外を必ず False にする**（symlink / junction / `..` 含む） | R-2 | 3 |
| 6 | **設定内容を変えても cleanup 対象が変わらない** | R-2 | 3 |
| 7 | DB のパス列が APP_ROOT 相対で保存・復元される | R-1 / 移動耐性 | 2 |
| 8 | **フォルダー移動後に再解析が発生しない** | 移動耐性 | 8 |
| 9 | **元動画の mtime / サイズ / 内容が処理前後で不変** | 最重要安全仕様 | 4 |
| 10 | 実行前後で `%LOCALAPPDATA%` / `%APPDATA%` に差分がない | One-Folder | 8 |
| 11 | `userdata\` の外へ 1 バイトも書かない（全工程で） | One-Folder | 4,5 |
| 12 | `privacy_guard.py` 自身のテスト（既知の悪いパターンを検出できる） | R-1 | 0 |
| 13 | 追跡ファイルにユーザー固有絶対パスが含まれない | R-1 | 0 |
| 14 | 連続失敗ガードが 3 本目で停止する | R-7 | 6 |
| 15 | `gui/runner.py` の停止要求がプロセス kill でないこと | R-8 | 7 |
| 16 | 子プロセスへ UTF-8 環境変数が渡ること | R-9 | 6,7 |
| 17 | `whisper` へ渡る model / destination が ASCII であること | R-6 | 5 |

---

## 最初に移植するもの / 最後に移植するもの

| | 内容 | 理由 |
|---|---|---|
| **最初** | `paths.py` / `config.py` / `logging_utils.py`（Phase 1） | すべての保存先の導出元。ここが決まらないと他のモジュールを書けない |
| **その次** | `database.py`（Phase 2）→ **`recycle.py` の境界**（Phase 3） | 消す機能の境界を、消される対象が増える前に固定する |
| **中盤** | 解析コア（Phase 4）→ ローカル AI（Phase 5） | 外部依存の少ない順 |
| **後半** | `pipeline.py`（Phase 6） | 上記すべてが揃ってから |
| **最後** | GUI（Phase 7）→ 配布（Phase 8） | 最も工数が大きく、下層が固まっていないと作り直しになる |

---

## Phase 間の依存関係

```
Phase 0 (安全基盤) ──┐
                     ├→ Phase 1 (paths/config) ──→ Phase 2 (database) ──→ Phase 3 (cleanup境界)
                     │                                                          │
                     │                                                          ▼
                     │                                              Phase 4 (解析コア)
                     │                                                          │
                     │                                                          ▼
                     │                                              Phase 5 (ローカルAI)
                     │                                                          │
                     │                                                          ▼
                     │                                              Phase 6 (pipeline + パリティ表)
                     │                                                          │
                     │                                                          ▼
                     └───────────────────────────────────────────→ Phase 7 (GUI)
                                                                                │
                                                                                ▼
                                                                     Phase 8 (配布・受け入れ)
```

---

## 進捗管理

| Phase | 状態 | commit |
|---|---|---|
| 0 安全基盤 | 完了 | `c98e2f6` / `0736f0e` |
| 1 土台（paths / config / source_ref / logging） | 完了 | `d39f28c` |
| 2 台帳 | 完了 | `0bf90da` |
| 3 cleanup 境界 | 完了 | `7f15fc1` |
| 4 解析コア | 完了 | `5d1ce4e` |
| 5 ローカル AI | 完了 | `8544d02` |
| 6 pipeline + 機能パリティ表 | 完了 | `c047d3e` |
| 7 GUI（Python + tkinter） | 完了 | `b1ce65a` |
| 8 配布・移動耐性・受け入れ | 完了 | `a3a0775` / `726a054` / `9ec9b1a` |
| — stage runner 4 本 | 完了 | `726a054` |

### Phase 8 の内訳

| 項目 | 状態 |
|---|---|
| stage runner 4 本（frames / visual / transcription / description） | 完了 |
| フォルダー移動テスト（再解析が起きないこと） | 完了・自動テスト |
| 残骸ゼロ確認（`%LOCALAPPDATA%` 等を使わないこと） | 完了・自動テスト |
| `tools/make_release.py`（配布 zip） | 完了 |
| 配布物の展開・APP_ROOT 解決・環境チェック | 完了 |
| **配布フォルダーでの合成動画による通し試験** | 完了（登録→解析→停止→Resume→説明文→カタログ→整理→移動） |
| GUI から pipeline 全工程が起動すること | 完了・自動テスト |
| Python runtime の配布方式 | 比較・推奨まで完了（[PYTHON_RUNTIME.md](PYTHON_RUNTIME.md)） |
| **実動画での確認** | **未実施**（方針どおり。利用者が運用フォルダーで行う） |

### v1 として未完了な点

| 項目 | 状態 |
|---|---|
| 文字起こしの通し試験 | 合成動画に発話が無く、Whisper モデルも要るため end-to-end からは外している。エンジン側（チャンク分割・非 ASCII 回避・幻覚判定・VAD 既定）は単体テストで固定済み |
| Python runtime の同梱 | 方式は決めた（embeddable package）。実装は未着手 |
| 実動画での長時間運転 | 未実施 |
