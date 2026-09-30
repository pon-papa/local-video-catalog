# ToolDock から長時間の解析を実行する（Connector v2 job）

この文書は、Local Video Catalog の解析を **ToolDock Connector v2 の job** として実行するための設計と、
その前提になった「今の長時間解析の仕組み」の調査結果です。

- ToolDock は**任意**です。無くても、画面（`Start.cmd`）とコマンドラインは今までどおり使えます
- ToolDock・MCP をこのアプリの依存にはしません（入口 `tooldock_cli.py` は標準ライブラリとこのアプリだけを使います）
- 入口 `tooldock_cli.py` と `tooldock.tool.json` は配布 ZIP に入れていません（開発クローンから使う任意の接続口）

---

## 1. 今の長時間解析の仕組み（調査・2026-09-30、コードは変えずに確認）

### 入口

- 本体は `python -m local_video_catalog.pipeline`（`pipeline.run()`）。画面は同じものを**子プロセスとして**起動し、出力を1行ずつ表示する（`gui/runner.py`）
- 引数（画面が渡すもの）: `--source-folder`・`--recursive`・`--time-budget-minutes`・`--max-videos`・`--visual-model`・
  `--description-model`・`--whisper-model`・`--skip-transcription`・`--recycle-cache`・`--only-catalog-id`（失敗分の再試行）・`--dry-run`。
  `--skip-visual` は試験専用（画面には出さない。v1 では映像の解析は必須工程）
- **保存先は引数で渡さない。** APP_ROOT（`app-root.marker` のあるフォルダー）の `userdata\` だけに書く（One-Folder）。
  `LOCAL_VIDEO_CATALOG_ROOT` は試験・CI 専用

### 流れ

1. 設定（`userdata/config/settings.json`）＋画面で選んだモデル（`environment_check.apply_model_choices`）
2. `verify_userdata()`、実行ログ `userdata/logs/run_<run_id>.log`（人向け）と `.jsonl`（構造化）
3. **登録**（`register.register_folder`）: `os.walk`＋名前順で動画を列挙し、指紋（先頭・末尾 1MB）と ffprobe（並列 8）で台帳へ。
   ffprobe の生 JSON は `userdata/cache/probe`
4. **選択**（`stage_report.collect` → `select_pending`）: 未完了の動画を台帳 ID 順に。`max_videos` は「新たに着手する本数」
5. 実行記録の開始（`processing_runs`）
6. 動画ごとに 4 工程（`pipeline.run_pipeline` / `_run_one`）

| 工程 | 使うもの | 中間成果 | 時間の上限 |
|---|---|---|---|
| 代表画像の抽出 | ffmpeg（子プロセス） | `userdata/cache/frames/<asset>` | 1 回 120 秒 |
| 映像の解析 | **LM Studio**（`localhost` だけ、HTTP）。画像つきの要求＝**GPU** | `userdata/cache/vlm/<asset>`、`frame_visual_analyses` | 1 枚 300 秒、視覚概要 1200 秒 |
| 文字起こし | **ffmpeg の whisper フィルター**（子プロセス）＋ `userdata/models/whisper` のモデル | `userdata/cache/asr/<asset>`、`asr_chunks` | 5 分ごとのチャンク、1 回 3600 秒 |
| 説明文の作成 | LM Studio（テキスト）または定型文 | `userdata/descriptions/*.txt`、`asset_descriptions` | 1200 秒 |

7. 実行記録の終了（`stop_reason`・処理本数・失敗本数）→ 整理（選んだときだけ、完了した動画の中間成果をごみ箱へ）→ HTML カタログの作り直し

### 止めどき・停止

- **停止要求** = `userdata/control/stop-request` ファイル（`pipeline.request_stop()`）。**プロセスを殺さない**
- 確かめる場所: 動画と動画の間、工程と工程の間、**工程の中**（代表画像・映像の解析はフレームごと、文字起こしはチャンクごと）。
  1 回の呼び出し（例: 視覚概要の最大 1200 秒）の途中では止まらない
- ほかの止めどき: 稼働時間（`--time-budget-minutes`）、本数（`--max-videos`）、**同じ種類の設備障害が 3 本続いたら安全停止**
- 停止要求は開始時と終了時に消す（次回がいきなり止まらない）
- **同じ APP_ROOT で 2 つの解析を同時に動かさない**仕組みは無い（画面が自分で二重起動を防いでいるだけ）

### 再開（Resume）と checkpoint

- **正本は台帳（SQLite、WAL）の `stage_status`**。完了した工程（`DONE_STATUSES`）は次回飛ばす
- 途中の工程は、中間成果を再利用して続きから: 映像の解析は解析済みのフレームを、文字起こしは済んだチャンクを使う。
  止めた映像の解析は `partial`（部分的なフレームで完了扱いにしない）
- 停止・時間切れは失敗に数えない（`StageOutcome.interrupted`）
- **強制終了されても**、工程の結果は工程が終わったときにトランザクションで書くので、台帳は壊れない。
  途中だった工程は未完了のまま残り、次回やり直す（中間成果が残っていれば再利用）
- 自動の再試行は無い。失敗した工程は**次の実行で**また対象になる。画面の「失敗分を再試行」は `--only-catalog-id`

### 元動画・保存先

- 元動画は**読むだけ**（ffprobe・ffmpeg で読む）。元動画のフォルダーに何も書かない（試験 `HSourceIntegrityTests`）
- 書くのは APP_ROOT の `userdata\` の中だけ。整理（ごみ箱へ移動）の対象は `userdata\cache\{frames,vlm,asr}\<asset>` だけ（`paths.is_cleanable`）

### プロセス

- 解析は 1 つの Python プロセス。子プロセスは ffprobe（並列）・ffmpeg（代表画像・whisper）。すべて `process_utils`（窓を出さない）
- LM Studio は**別のアプリ**（このアプリも ToolDock も所有しない）。HTTP で呼ぶだけ

---

## 2. job としての設計（LVC-J2 で実装）

### 分担

| ToolDock（Connector v2・Job Runner） | Local Video Catalog |
|---|---|
| job の受付・確認・状態・資源の予約・中止の要求・結果の受け渡し・プロセスの持ち主（後始末） | 解析そのもの・工程の順序・止めどき・**再開（どこまで済んだか）**・中間成果・台帳・ログ |
| 取り消しの合図（`TOOLDOCK_CANCEL_FILE`） | 合図を自分の停止要求（`stop-request`）へつなぎ、区切りで止まる |
| job の記録（`~/.tooldock/jobs`）に要約だけ | 台帳・説明文・HTML・実行ログ（`userdata\`） |

ToolDock は台帳や中間成果の形式を知りません。

### job の操作 `videocatalog_analyze`（`tooldock.tool.json`、`connector_version: 2`）

- 入力（宣言したものだけ）: `source_folder`（ToolDock の media root の中のフォルダー）・`recursive`・`max_videos`（0＝制限なし）・
  `time_budget_minutes`（1〜1320）・`skip_transcription`
- 受け付けないもの: 保存先（常にこのフォルダーの `userdata\`）・モデル名（**画面で人が選んだモデル**を使う）・ffmpeg やモデルのパス・
  整理（ごみ箱への移動。画面で行う）・任意のオプション
- `confirmation_required: true`、`resources: {max_concurrent: 1, gpu_exclusive: true, cpu_heavy: true}`、
  `cancel: cooperative`（猶予 600 秒）、`resumable: false`（再開はこのアプリ自身が行う。下記）
- 登録しただけでは始まらない。人が ToolDock Job Runner を起動すると始まる

### 入口 `tooldock_cli.py analyze` がすること

1. 引数の検査（宣言外は拒否）。このフォルダーが APP_ROOT であることの確認
2. **既存の環境チェック**（`environment_check.check_environment`、画像の確認を含む・何も書き換えない）と
   `readiness.can_start`。開始できなければ `environment_not_ready` で止まる（台帳に何も書かない）
3. 画面と同じ引数で `pipeline.run()` を呼ぶ（工程の実体は既存のまま）。進捗は工程の開始ごとに
   `{"event": "progress", "phase": "<台帳ID> <工程>", "done": i, "total": N}` を標準エラーへ
4. `TOOLDOCK_CANCEL_FILE` が現れたら `pipeline.request_stop()`（既存の停止要求）。区切りで止まり、`cancelled` を返す
5. 終わったら要約（本数・完了・失敗した台帳 ID・止まった理由・ライブラリ全体の残り）を返す。台帳や説明文の中身は返さない

### 結果の読み方

- 1 本以上失敗しても、解析が最後まで（または止めどきまで）進めば job は `succeeded`。`result.outcome` で区別する:
  `completed`・`completed_with_errors`・`stopped_time_budget`・`stopped_max_videos`・`stopped_repeated_failure`・`nothing_to_do`・`no_videos`
- 失敗した動画・途中の動画は、次に同じ job（または画面の解析）を実行すると続きから処理される

### 再開について（job_resume はまだ作らない）

中断（`interrupted`）・中止（`cancelled`）の後も、このアプリの checkpoint（台帳の `stage_status` と中間成果）は残る。
続けるには、**同じ入力の job をもう一度登録する**だけでよい（このアプリが済んだ工程を飛ばす）。
ToolDock が将来 `job_resume` を作る場合も、ToolDock が持つのは「どの job の続きか（元の job id・同じ入力）」だけで、
「どこまで済んだか」はこのアプリの台帳が持つ。
