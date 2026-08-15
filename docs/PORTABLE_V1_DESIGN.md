# local-video-catalog — 一般配布版 v1 設計

- 作成日: 2026-08-14
- 位置づけ: 本プロジェクトの**最上位設計文書**
- 前提調査: [CURRENT_SYSTEM_AUDIT.md](CURRENT_SYSTEM_AUDIT.md)
- 移植手順: [MIGRATION_PLAN.md](MIGRATION_PLAN.md)

---

## 0. このツールは何か

ユーザーが所有する多数の動画を、**外部クラウドへ動画・画像・音声・文字起こし内容を一切送信せず**、
ローカル AI だけで解析し、

- 動画内容の説明文
- 検索・閲覧できる HTML カタログ

を作る Windows 向けツール。

対象は家族動画に限らない。ホームビデオ / 旅行記録 / イベント / スポーツ / 趣味 /
研究記録 / 業務記録 / 過去の映像アーカイブ を等しく扱う。
**Family 固有の名称・前提・UI・説明は持ち込まない。**

---

## 1. 確定した設計前提（変更禁止）

| # | 前提 |
|---|---|
| 1 | **One-Folder 完結**。外部依存物と元動画を除き、アプリが生成・保持する状態はすべて APP_ROOT 配下 |
| 2 | 実行時データは **`APP_ROOT\userdata\` に集約**する |
| 3 | `%LOCALAPPDATA%` / `%APPDATA%` / ユーザープロファイル直下 / レジストリへ**状態を書かない** |
| 4 | **元動画は読み取り専用**。上書き・改名・移動・削除・metadata 書換え・同一フォルダーへの生成物保存をしない |
| 5 | **cleanup は APP_ROOT の外へ絶対に出ない**。設定値が壊れていても出ない |
| 6 | 解析は **localhost のローカル AI のみ**。外部クラウド AI を使う経路を実装しない |
| 7 | **1 フォルダー = 1 解析環境**。別動画群はフォルダーごとコピーして使う |
| 8 | **開発クローンと運用フォルダーを分離**する（§9） |
| 9 | GUI は **Python + tkinter**（PowerShell 7 依存を第三者に要求しない） |
| 10 | **AI 推定を事実へ昇格させない** |

---

## 2. APP_ROOT の定義と決定方式

### 2-1. 定義

**APP_ROOT = `paths.py` が置かれているパッケージから見たアプリケーションルート**。
すなわちアプリ一式が展開されているフォルダーそのもの。

### 2-2. 決定順序

```
1. 環境変数 LOCAL_VIDEO_CATALOG_ROOT が設定され、実在するディレクトリなら それ
     （テスト・CI・検証専用の明示的上書き。通常運用では使わない）
2. Path(__file__).resolve() から上へ辿り、APP_ROOT マーカーを持つ最初のディレクトリ
3. 見つからなければ ConfigError で停止（代替場所を勝手に決めない）
```

**APP_ROOT マーカー** = `app-root.marker` という空ファイルを APP_ROOT 直下に置き、Git 管理する。
`src/` の相対位置（`parents[2]` 等）に依存させない理由:

- 将来 `src/` の階層を変えたときに静かに壊れる
- zip 展開・フォルダーコピー・py2exe 化など配置が変わる場面で誤検出しうる
- マーカーなら「ここがアプリの根である」という意図が明示される

旧版の `config.project_root()` は `Path(__file__).resolve().parents[2]` の決め打ちだった（AUDIT §G）。
これは階層固定の暗黙依存なので、マーカー方式へ置き換える。

### 2-3. 絶対に守ること

- **APP_ROOT を永続データへ焼き込まない**（§8 フォルダー移動耐性）
- コード各所で `Path(...)` を組み立てず、**`paths.py` の関数経由でのみ**保存先を得る
- `paths.py` は APP_ROOT 配下を返す関数だけを持ち、**外部パス（元動画・ffmpeg・モデル）は返さない**

---

## 3. ディレクトリ構造

```
local-video-catalog\                    ← APP_ROOT
├─ app-root.marker                       APP_ROOT 判定用（空ファイル・Git 管理）
├─ Start.cmd                             ダブルクリック起動（CP932）
├─ README.md
├─ LICENSE
├─ .gitignore
├─ .gitattributes
├─ .github\workflows\ci.yml
│
├─ src\local_video_catalog\              ★ アプリ本体（Python・標準ライブラリのみ）
│   ├─ paths.py                           APP_ROOT と全保存先の唯一の導出元
│   ├─ config.py                          設定の読み込み・検証・既定値
│   ├─ database.py                        SQLite 台帳
│   ├─ discovery.py / probe.py / fingerprint.py / pathinfo.py / datetime_candidates.py
│   ├─ frame_extractor.py / frame_cli.py
│   ├─ vlm_client.py / visual_analyzer.py / visual_schemas.py / visual_prompts.py / visual_cli.py
│   ├─ asr_engine.py / asr_cli.py / audio_streams.py / transcript_schemas.py
│   ├─ description_builder.py / description_cli.py
│   ├─ html_catalog.py / exporters.py
│   ├─ stage_report.py / run_summary.py / logging_utils.py
│   ├─ recycle.py                         中間成果の整理（ゴミ箱送り）
│   ├─ environment_check.py / model_catalog.py
│   ├─ pipeline.py                        ★新規: 5工程オーケストレータ（旧 .ps1 の Python 化）
│   └─ gui\                               ★新規: tkinter GUI
│        ├─ app.py                         画面の組み立て
│        ├─ runner.py                      別プロセス起動と出力取り込み（GUI 非依存・テスト可能）
│        ├─ state.py                       画面状態の保存/復元（GUI 非依存・テスト可能）
│        └─ dialogs\                       ローカルAI設定 / 中間ファイル整理 など
│
├─ config\
│   └─ settings.example.json              Git 管理。実在パスを書かない
│
├─ tools\
│   ├─ privacy_guard.py                   個人データ混入検査（CI + ローカル）
│   └─ launcher\                          Python / ffmpeg / pwsh の検出補助
│
├─ tests\                                 unittest（標準ライブラリのみ）
├─ docs\
│   ├─ CURRENT_SYSTEM_AUDIT.md
│   ├─ PORTABLE_V1_DESIGN.md              ← この文書
│   ├─ MIGRATION_PLAN.md
│   └─ GUI_FEATURE_PARITY.md              ★Phase 6 で作成（旧GUI との機能対応表）
│
└─ userdata\                              ★ 実行時に生成されるすべて（Git 一括除外）
    ├─ .gitignore                          `*` と `!.gitignore` の二重防御
    ├─ config\
    │   ├─ settings.json                   ユーザー設定（GUI が書く）
    │   └─ gui-state.json                  画面状態（前回の入力・選択）
    ├─ catalog\
    │   ├─ video_catalog.sqlite3           台帳（正本）
    │   ├─ catalog.html                    HTML カタログ（派生物）
    │   └─ exports\                        CSV / JSON / JSONL
    ├─ descriptions\                       VID-000001_<元動画名>.txt（最終成果物）
    ├─ cache\
    │   ├─ probe\                           ffprobe 生 JSON (.json.gz)
    │   ├─ frames\                          代表静止画（旧 scenes）
    │   ├─ vlm\                             フレーム解析・視覚概要の中間結果
    │   └─ asr\                             チャンク・transcript の中間結果
    ├─ models\
    │   └─ whisper\                         ユーザーが置く *.bin（同梱しない）
    ├─ logs\                                実行ログ（人間可読 + JSONL）
    ├─ runs\                                実行単位のマニフェスト
    ├─ temp\                                作業用。起動時に古いものを掃除してよい
    └─ control\
        └─ stop-request                     安全停止の要求ファイル
```

### 3-1. 旧版からの配置変更点

| 旧 | 新 | 理由 |
|---|---|---|
| `<DataRoot>\cache\scenes\` | `userdata\cache\frames\` | 「scenes」は内部用語。一般利用者に意味が通らない |
| `<DataRoot>\catalog.html`（直下） | `userdata\catalog\catalog.html` | 成果物を catalog\ に集約 |
| `%LOCALAPPDATA%\FamilyVideoCatalog\gui-settings.json` | `userdata\config\gui-state.json` | **One-Folder 化。§1-3** |
| `%LOCALAPPDATA%\FamilyVideoCatalog\stop-request.txt` | `userdata\control\stop-request` | 同上 |
| `%LOCALAPPDATA%\FamilyVideoCatalog\gui-log-*.txt` | `userdata\logs\` | 同上（ログを一箇所へ） |
| `<DataRoot>\tests\{fixtures,output}\` | **廃止** | テストは `tempfile` 内で完結（旧版のテストも実際はそうなっている） |
| `config\settings.local.json`（リポジトリ内） | `userdata\config\settings.json` | 設定も userdata へ。Git 作業ツリーに実パスを置かない |

### 3-2. なぜ `userdata/` へ集約するか

1. **`.gitignore` が `/userdata/` の 1 行で完結する**。除外漏れの経路が構造的に減る
2. **cleanup 境界が 1 行で書ける**（`userdata/cache/{frames,vlm,asr}` 配下のみ）
3. **バックアップ・移設が `userdata\` 単位でできる**（コードだけ更新する運用が可能）
4. **アプリ更新時に上書きしてよい範囲が自明**（`userdata\` 以外は全部差し替えてよい）

---

## 4. 設定保存方式

### 4-1. 3 層マージ（旧版の構造を維持）

```
DEFAULT_SETTINGS（config.py 内・APP_ROOT 相対）
  → userdata\config\settings.json       （ユーザー設定・GUI が書く・Git 管理外）
  → --config で指定されたファイル        （検証・複数プロファイル用）
  → コマンドライン引数
```

`None` の値は無視して下位の値を残す（旧版 `_deep_merge` の挙動を維持）。

### 4-2. 設定に書くもの / 書かないもの

| 種別 | 例 | 保存形式 |
|---|---|---|
| **外部の絶対パス**（APP_ROOT 外） | `source_path`, `ffmpeg_path`, `ffprobe_path` | **絶対パスで可**（§8） |
| **APP_ROOT 内の場所** | 台帳・cache・logs 等 | **設定に書かない**。`paths.py` が導出する |
| 動作パラメータ | `workers`, `extensions`, `vlm.*`, `asr.*` | 値のみ |
| モデル指定 | `vlm.model_match`, `description.model_match`, `asr.model_name` | **名前のみ**（パスではない） |

**`data_root` 設定キーは廃止する。** APP_ROOT から導出するため、設定で上書きできてはいけない。
（旧版で `data_root` を任意に指定できたことが、cleanup の基点を設定値へ委ねる原因になっていた → R-2）

### 4-3. 初回起動体験

v1 では設定ファイルの手編集を要求しない方向を目指す。初回起動時に GUI から:

1. 元動画フォルダー
2. ffmpeg / ffprobe（自動検出 → 見つからなければ参照ボタン）
3. LM Studio URL（既定 `http://127.0.0.1:1234/v1`）
4. VLM モデル（LM Studio から一覧取得して選択）
5. Whisper モデル（`userdata\models\whisper\` の実ファイルから選択）
6. 中間 cache 整理の有無

を設定でき、`userdata\config\settings.json` へ保存する。
**巨大な installer は作らない。**

---

## 5. 外部依存物の扱い

### 5-1. APP_ROOT の外に存在してよいもの

ユーザーの元動画フォルダー / ffmpeg / ffprobe / LM Studio / ローカル VLM モデル /
Whisper モデル本体の入手元 / Python runtime / その他ユーザーが明示指定したもの。

**これらの場所へ local-video-catalog が生成物を書き込むことは禁止。**

### 5-2. v1 の方針: 自動インストールより「検出と案内」

| 依存 | 検出方法 | 不足時の表示 |
|---|---|---|
| Python 3.13+ | ランチャーが `py -3` → `python` → 環境変数 `LOCAL_VIDEO_CATALOG_PYTHON` の順で探索 | 「Python 3.13 以降が必要です。python.org または Microsoft Store から入れてください」 |
| ffmpeg / ffprobe | 設定値 → `shutil.which` の順。**PATH 依存にしない**（複数版が混在しうるため） | 「ffmpeg が見つかりません。参照ボタンで ffmpeg.exe を指定してください」 |
| ffmpeg の whisper フィルター | `ffmpeg -filters` の出力を検査（判定を **1 箇所に統合**） | 「この ffmpeg には whisper フィルターがありません。8.x 以降のフル版が必要です」 |
| LM Studio | `GET <base_url>/models`（localhost のみ） | 「LM Studio を起動し、ローカルサーバーを ON にしてください」 |
| VLM モデル（選択） | 画面で選ばれているか。**空なら未選択**として開始不可（既定を勝手に選んだことにしない） | 「映像解析に使用するローカルAIモデルが選択されていません」 |
| VLM モデル（存在） | `/models` の一覧に**完全一致**で在るか。部分一致で似た名前を拾わない | 「前回使用したモデルを利用できません。選び直してください」 |
| VLM の画像入力対応 | **その場で生成した 8×8 PNG** を 1 枚送って確認（ユーザーの画像は使わない）。model id の文字列では判定しない | 拒否＝「このモデルでは利用できません」／時間切れ＝「画像処理能力を確認できませんでした」 |
| Whisper モデル | `userdata\models\whisper\*.bin`（1MiB 以上・ASCII 相対で表せること） | 「userdata\models\whisper\ へ .bin を置いてください」 |

**モデルの自動ダウンロードは実装しない。** インターネットへ出る経路をコードに持たない。

#### 映像解析の開始条件（4 段）

**「LM Studio が起動している」＝「映像を解析できる」ではない。**
接続できてもモデルが未選択／今は無い／画像を扱えないなら、1 本目から必ず失敗する。
そこで次の 4 段すべてを満たすまで「処理開始」を有効にしない。

1. LM Studio へ接続できる
2. 使うモデルが選ばれている
3. 選んだモデルが今も利用できる（**完全一致**。別モデルへ自動で替えない）
4. そのモデルが**実際に画像入力を処理できた**（`vision_probe`）

4 の確認は「内容が正しいか」ではなく「受け付けたか」だけを見る。
**通っても推奨モデルという意味にはならない**（品質は別問題）。

時間切れは「非対応」ではなく「**確認できませんでした**」として区別する。
どちらも開始はできないが、対処が違う（別のモデルを選ぶ／やり直す）。
**「たぶん使える」で開始させない。** 通してしまうと全動画が映像解析で落ちる。

実測（2026-08-15・probe 1 回あたり）:

| モデル | 結果 | 所要 |
|---|---|---|
| `qwen3-vl-8b-instruct` | 対応 | 1.4 秒（読み込み済み） |
| `lfm2.5-vl-3b` | 対応 | 28.5 秒（読み込みを含む） |
| `qwythos-9b-claude-mythos-5-1m` | 非対応 | 56.7 秒（読み込み → 拒否） |
| `text-embedding-nomic-embed-text-v1.5` | 非対応 | 0.1 秒 |

最後から 2 番目が効いて、待ち時間の既定は **120 秒**。60 秒だと
「非対応」と答えられる場面が「確認できませんでした」に化ける。

### 5-3. 推奨構成（既定値。hard-code しない）

| 役割 | 推奨 | 根拠 |
|---|---|---|
| VLM | `qwen3-vl-8b-instruct` | 実運用で検証済み。LFM2.5-VL-3B Q6_K は約5倍高速だが schema 適合率低下・存在しない frame 参照・JSON 途中切断・根拠のない断定があり不採用（AUDIT 付録） |
| VLM 接続先 | `http://127.0.0.1:1234/v1` | LM Studio 既定 |
| Whisper | `ggml-large-v3-turbo-q5_0.bin` | 実運用で検証済み |
| VAD | **無効** | 有効時: 無音60秒 3秒→598秒、日本語 CER 0.000→0.737 |

これらは `DEFAULT_SETTINGS` の既定値としてのみ持ち、**モデル固有の分岐をコードに書かない**。

---

## 6. cleanup 設計（最重要安全仕様）

### 6-1. 境界

```python
# paths.py
def cleanable_cache_root() -> Path:
    return app_root() / "userdata" / "cache"

CLEANABLE_CACHE_NAMES = ("frames", "vlm", "asr")   # probe は残す（再解析に使う）
```

`is_cleanable(path)` が True を返す条件（**すべて満たすときだけ**）:

1. `path.resolve()` が `app_root().resolve()` の**配下**である（`relative_to` が成功する）
2. 相対パスの第 1 要素が `userdata`
3. 第 2 要素が `cache`
4. 第 3 要素が `{frames, vlm, asr}` のいずれか
5. 相対パスの深さが **4 以上**（= `userdata/cache/vlm` のような親フォルダー自体は消さない）

**基点は必ず `app_root()`。設定値・引数・DB の記録値を cleanup の基点にしない。**
これが旧版との最大の違いであり、「設定が壊れていても外へ出ない」を構造で担保する。

### 6-2. 保護対象（絶対に消さない）

元動画（そもそも APP_ROOT 外なので条件 1 で弾かれる） / `userdata\catalog\`（DB・HTML・exports） /
`userdata\descriptions\` / `userdata\models\` / `userdata\logs\` / `userdata\config\` /
`userdata\cache\probe\` / `userdata\runs\` / APP_ROOT 直下のコード・docs・tests

### 6-3. 実行条件

**`asset_descriptions` に最終テキストが正常記録された動画だけ**が対象。
処理中・未完了・失敗した動画の cache は Resume に必要なので**保護される**。

### 6-4. 削除方式

Windows のゴミ箱を優先する。`SHFileOperationW`（shell32）を ctypes で呼ぶ標準ライブラリのみの実装
（旧 `recycle.py` を移植）。理由と検証:

- `FOF_ALLOWUNDO` でゴミ箱行き
- 戻り値 / `fAnyOperationsAborted` / **実際に消えたかの再確認**の 3 段階
- **完全削除へフォールバックしない。** 失敗したらファイルを残してエラーを返す
- 非 Windows では `RecycleError`（v1 の対象は Windows）

**代替案の検討結果**: `send2trash` パッケージを使えば実装は短くなるが、
サードパーティ依存ゼロという旧版の方針（CI で検証済み）を崩す。
既存実装が実運用で動いているため、**再発明せず移植する**（プロンプト §28）。

### 6-5. テストで固定する境界（Phase 3 で必須）

| # | 検証 |
|---|---|
| 1 | APP_ROOT の外のパスは、どう指定しても `is_cleanable` が False |
| 2 | `userdata\descriptions` / `catalog` / `models` / `logs` / `config` / `cache\probe` は False |
| 3 | `userdata\cache\vlm`（親そのもの）は False |
| 4 | `userdata\cache\vlm\<asset_id>` は True |
| 5 | シンボリックリンク・ジャンクションで外を指しても `resolve()` 後に False |
| 6 | `..` を含むパスを渡しても False |
| 7 | 設定ファイルの内容が何であっても判定が変わらない |
| 8 | ゴミ箱送りに失敗したらファイルが残り、エラーが返る |

---

## 7. Resume 設計

**正本は SQLite の `stage_status`。マニフェストファイルに依存しない。**（旧版の構造を維持）

```
工程: register → frame_extraction → visual_analysis → audio_transcription → description
```

### 7-1. 再利用キー（旧版から変更しない）

| 層 | キー |
|---|---|
| 工程完了 | `stage_status(asset_id, stage_name).status` |
| 静止画 | `(asset, impl_version, config_hash, source_quick_fingerprint, target_time_ms)` |
| フレーム解析 | `(asset, frame_sha256, model_id, prompt_version, impl_version, config_hash)` |
| 視覚概要 | `(asset, source_frame_analysis_hash, model_id, prompt_version, impl_version, config_hash)` |
| ASR チャンク | `(asset, src_fp, audio_idx, chunk_index, start, duration, engine, impl, model_sha256, config_hash)` |

**timeout 値を `config_hash` に含めない。** 待ち時間は生成内容を変えないため、
値を変えても保存済み解析はそのまま再利用される（R-3 の再発防止に直結）。

### 7-2. 安全停止の 3 系統

| 種別 | 契機 | 停止粒度 |
|---|---|---|
| 時間予算 | `time_budget_minutes` 到達 | 動画の切れ目 / ASR はチャンク境界 |
| 停止要求 | `userdata\control\stop-request` の出現 | 工程の切れ目 |
| 同種障害の連続 | VLM が `connection/timeout/model/privacy` で **3 本連続**失敗 | 即座に停止 |

**プロセスを強制終了しない。** 台帳も元動画も壊れない。次回は同じ操作で続きから。

### 7-3. 失敗のみ再試行

`--only-catalog-id` で対象を絞る。**工程の再利用ルールは変えない**
（選ばれた動画の中でも完了済み工程はこれまでどおり再利用される）。

### 7-4. 障害分類（人が対処できる言葉へ）

| 終了コード | 種別 | 表示 |
|---|---|---|
| 9 | connection | LM Studio へ接続できません。起動してローカルサーバーを ON にしてください |
| 8 | timeout | LM Studio へはつながっていますが、視覚概要の生成が制限時間を超えました |
| 6 | model | 指定したモデルが LM Studio に見つかりません |
| 7 | privacy | 接続先がローカルではありません |
| 5 | frames | 解析できる代表静止画がありませんでした |

**「制限時間を超えた」を「LM Studio が起動していない」と言わない。**

---

## 8. フォルダー移動耐性

### 8-1. 原則

**APP_ROOT の絶対パスを永続データへ焼き込まない。**
`C:\AAA\local-video-catalog` → `D:\Tools\local-video-catalog` へ移動しても内部状態を保って起動する。

### 8-2. 具体策

| 対象 | 扱い |
|---|---|
| `paths.py` の全関数 | 実行のたびに `app_root()` から導出。値をキャッシュしない |
| `userdata\config\settings.json` | APP_ROOT 内の場所を書かない（§4-2） |
| DB の `output_directory` 等のパス列 | **APP_ROOT からの相対パスで保存する**（旧版は絶対パス。ここは変更点） |
| DB の `current_path` / `original_path`（元動画） | 外部なので**絶対パス**のまま |
| DB の `description_file_path` | **相対パス**で保存 |
| `processing_runs.config_snapshot` | APP_ROOT 絶対パスを含めない |
| ログ | 表示上は絶対パスでよい（永続状態ではない） |

### 8-3. 移動検証（Phase 8 の受け入れ条件）

一時フォルダー A で最小データセットを作る → フォルダーごと B へコピー →
B で起動して `stage_status` がすべて再利用されること（再解析が発生しないこと）をテストで確認する。

---

## 9. 開発クローンと運用フォルダーの分離

```
【開発クローン】 pon-papa/local-video-catalog を clone したフォルダー
   ・Git 管理される唯一のフォルダー
   ・開発・テスト・CI 検証に使う
   ・実動画は使わない。合成動画（testsrc / sine）のみ
   ・userdata\ は生成されうるが、内容は合成データのみ

【運用フォルダー】 ユーザーが決めた任意の場所（例: D:\LocalVideoCatalog）
   ・配布物一式（= .git を除いた APP_ROOT）をコピーして作る
   ・実動画を使う本番運用はここだけで行う
   ・One-Folder 完結は運用フォルダー側でも必須
   ・Git を意識しない。git コマンドを一切使わない
```

### 9-1. 配布物の作り方（Phase 8 で `tools\make_release.py` を用意）

`.git` / `.github` / `tests` / `docs` を除いた APP_ROOT を zip にする。
`userdata\` は `.gitignore` と空ディレクトリ構造だけを含める。

### 9-2. 「1 フォルダー = 1 解析環境」

別の動画群を完全に独立して管理したければ、**運用フォルダーごとコピーする**。
v1 では 1 アプリから複数プロジェクトを切り替える機能は作らない。

---

## 10. Privacy 設計

### 10-1. 送信しないもの

動画 / 代表画像 / 音声 / transcript / 説明文の材料 / 個人・業務内容
→ **外部クラウド AI へ送信しない。** localhost のローカル AI のみ。

### 10-2. コード側の多層防御（旧 `vlm_client.py` を移植）

| # | 仕組み |
|---|---|
| 1 | 接続先を `localhost` / `127.0.0.1` / `::1` とループバック IP に限定。**名前解決に頼らない** |
| 2 | **HTTP リダイレクトを一切辿らない**（302 で外部へ転送される事故を防ぐ） |
| 3 | `ProxyHandler({})` で `HTTP_PROXY` / `HTTPS_PROXY` を無効化 |
| 4 | 「開発時だけ外部を許可する」オプションを**実装しない** |
| 5 | 要求のたびに URL を再検証（base_url を後から書き換えられても守る） |
| 6 | DB へは `scheme://host:port` だけを記録（認証情報・パス詳細を残さない） |
| 7 | 画像入力の検査には**その場で作った単色 PNG** を使う |
| 8 | 標準ライブラリの `urllib` のみ。HTTP クライアントの追加依存を入れない |

### 10-3. Git への混入防止（多層）

| 層 | 仕組み |
|---|---|
| 1 | `.gitignore` の `/userdata/` 一括除外 |
| 2 | `userdata\.gitignore`（`*` + `!.gitignore`）による二重防御 |
| 3 | `tools\privacy_guard.py` — 追跡ファイルに対する禁止パターン検査 |
| 4 | CI で `privacy_guard.py` を実行 |
| 5 | CI でテスト後の作業ツリーが clean であることを検証 |
| 6 | **開発クローンと運用フォルダーの分離**（§9）— 実動画由来データが Git 作業ツリーに現れない |

### 10-4. `privacy_guard.py` の検査項目

1. メディア拡張子（mp4/mov/m2ts/wav/mp3/jpg/png 等）が追跡されていない
2. `*.sqlite3` / `*.db` / `*.bin` / `*.gguf` が追跡されていない
3. `userdata/` 配下が 1 件も追跡されていない
4. `VID-*.txt` / `catalog.html` / `transcript*.json` / `segments*.jsonl` / `chunk_*.json` が追跡されていない
5. `src_<hex>` 名前空間が追跡されていない
6. **追跡ファイルの中身**にユーザー固有の絶対パス（`C:\Users\<name>\`、ドライブ直下の日本語フォルダー等）が含まれていない
7. `settings.json`（ユーザー設定）が追跡されていない

### 10-5. AI 推定を事実へ昇格させない

| 層 | 仕組み |
|---|---|
| プロンプト | 「material に無いことを書かない」「人物名・関係・学校名・地名・行事名は明記がない限り書かない」 |
| DB schema | `capture_time_candidates.is_family_confirmed` / `asset_relations.confirmed_by_family`（列名は一般化して `is_user_confirmed` / `confirmed_by_user` へ） |
| ASR | `is_suspected_hallucination` は**疑い**であり確定ではない。元データは消さない |
| HTML | 「解釈保留」を日付へ読み替えない。並び順では末尾へ |
| 表示 | AI 生成であることが分かる注記を残す |

### 10-6. ASR 幻覚対策（構造を変えない）

```
保存: full_text / segments / is_suspected_hallucination を すべて残す
使用: 説明文の材料からだけ is_suspected_hallucination=true を除外する
```

- 「同一文が N 回続いた」**だけ**では除外対象にしない
  （日常・会話動画では短い語が本当に繰り返されるため）。反復回数は警告文の材料にとどめる
- 材料が幻覚だけになった場合は「内容として使える発話は確認できていません」として扱い、無理に使わない

---

## 11. GUI 設計（Python + tkinter）

### 11-1. 構造原則

```
gui\app.py        ← tkinter ウィジェットの組み立てだけ。ロジックを持たない
gui\runner.py     ← 別プロセス起動・stdout 取り込み・停止要求。GUI 非依存 → unittest 可能
gui\state.py      ← 画面状態の保存/復元。GUI 非依存 → unittest 可能
pipeline.py       ← 解析オーケストレータ。GUI から独立して CLI でも動く
```

**GUI を起動しなくても主要ロジックを unittest で検証できること**を最優先する。
旧版（PowerShell WinForms 1,871行）は自動テストが事実上不可能だった。

### 11-2. 長時間運転中も応答を維持する仕組み（旧版の構造を継承）

- 解析は **`subprocess` で別プロセス**として起動する（GUI プロセス内で回さない）
- stdout を**別スレッド**で読み、`queue.Queue` 経由で GUI スレッドへ渡す
- GUI スレッドは `after()` で定期的に queue を drain して表示更新
- **`subprocess` に `encoding="utf-8"`、子プロセスへ `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` を明示**
  （日本語 Windows での文字化けは、出力をリダイレクトしたときに顕在化する — AUDIT §D）
- 安全停止は**ファイル生成**（`userdata\control\stop-request`）。プロセスを kill しない

### 11-3. 機能パリティ（詳細表は Phase 6 で `docs\GUI_FEATURE_PARITY.md` に作成）

v1 で**必ず維持する**機能:

| # | 機能 | 対応する処理 |
|---|---|---|
| 1 | 環境チェック | `environment_check` |
| 2 | 解析対象の確認（DryRun） | `stage_report --format summary` |
| 3 | 処理開始 | `pipeline` を別プロセス起動 |
| 4 | 安全停止 | `stop-request` ファイル生成 |
| 5 | Resume（続きから） | 既定で有効。完了工程を飛ばす |
| 6 | 失敗動画のみ再試行 | `--only-catalog-id` |
| 7 | HTML カタログ更新 | `html_catalog` |
| 8 | HTML カタログを開く | 既定ブラウザーで開く |
| 9 | 説明文を開く | エクスプローラーで開く |
| 10 | 元動画の場所を開く | エクスプローラーで選択表示（**開くだけ。変更しない**） |
| 11 | ローカルAI設定 | VLM / 説明文 / Whisper モデル選択・一覧再読込 |
| 12 | 進捗表示 | 工程別の完了数・現在処理中の動画 |
| 13 | ログ表示 | 実行ログのリアルタイム表示 |
| 14 | 実行条件 | 稼働時間・本数上限・制限なし・文字起こしを飛ばす・完了後に中間ファイル整理 |
| 15 | 入力元 / サブフォルダーも含める | フォルダー選択 |

**削除する機能**: 「保存先の選択」（APP_ROOT から導出するため不要）、
「GUI 作業履歴の整理」（`%LOCALAPPDATA%` を使わないため不要）。

### 11-4. 文言方針

開発者向け内部用語を使わない。
`scenes` → 「代表画像」、`VLM` → 「映像解析」、`ASR` → 「文字起こし」、
`asset` → 「動画」、`config_hash` → 表示しない。
「家族」「ホームビデオ」等の限定表現は使わない。

v1 の UI 言語は日本語。多言語化は将来候補（v1 スコープ外）。

---

## 12. v1 完成条件

第三者が新しく local-video-catalog を入手して、次の 14 項目がすべて成立すること。

| # | 条件 | 検証方法 |
|---|---|---|
| 1 | フォルダーへ展開できる | zip を展開して起動 |
| 2 | `Start.cmd` から起動する | ダブルクリック |
| 3 | 必要な外部環境を確認できる | 環境チェックが OK/注意/NG を表示 |
| 4 | 不足箇所を GUI から設定できる | 設定ファイルを手編集せずに完了 |
| 5 | 自分の動画フォルダーを指定できる | フォルダー選択 |
| 6 | 解析を開始できる | 5 工程が順に走る |
| 7 | 途中で安全停止できる | 「安全停止」→ 区切りで停止 |
| 8 | 続きから Resume できる | 再開時に完了工程が飛ばされる |
| 9 | 最終説明文が生成される | `userdata\descriptions\VID-*.txt` |
| 10 | HTML カタログが生成される | `userdata\catalog\catalog.html` |
| 11 | HTML から閲覧・検索できる | ブラウザーで開く。**外部通信ゼロ** |
| 12 | 正常完了後に中間成果を整理できる | ゴミ箱へ移動。**保護対象は残る** |
| 13 | アプリフォルダーを移動しても起動する | フォルダーごと移動して再開 |
| 14 | フォルダー削除で生成物が片付く | 削除後、`%LOCALAPPDATA%` 等に残骸がゼロ |

### 12-1. 追加の受け入れ条件（安全性）

| # | 条件 |
|---|---|
| 15 | cleanup が APP_ROOT の外へ出ないことがテストで固定されている |
| 16 | 元動画フォルダーへの書き込みが一切ないことがテストで固定されている |
| 17 | localhost 以外への接続が例外で止まることがテストで固定されている |
| 18 | privacy guard が CI で走り、実データが追跡されていないことを検証している |
| 19 | サードパーティ Python パッケージがゼロであることが CI で検証されている |
| 20 | 全テストが実動画・ユーザー固有パス・LM Studio に依存せず走る |

---

## 13. v1 でやらないこと（将来候補）

| 項目 | 理由 |
|---|---|
| クラウド AI 対応 | 設計思想に反する（永久に作らない） |
| 自動人物認識・顔認識 | AI 推定の事実化リスク |
| NAS 専用最適化 / Docker 化 / Web サービス化 / スマートフォン対応 | スコープ外 |
| 巨大 installer / モデル自動ダウンロード | §5-2 の方針 |
| Python 同梱（embeddable distribution） | 検討価値はあるが v1 では「検出と案内」で足りる |
| 高度な多言語 UI | v1 は日本語 |
| 1 アプリからの複数プロジェクト切替 | 「フォルダーごとコピー」で足りる |
| 大規模リファクタ / framework 全面移行 | 実証済みの仕組みを再発明しない |
| `send2trash` 等の依存追加 | 標準ライブラリのみの方針を崩さない |

**良いアイデアが見つかっても、v1 完成に不要ならこの表へ追記し、実装範囲を広げない。**
