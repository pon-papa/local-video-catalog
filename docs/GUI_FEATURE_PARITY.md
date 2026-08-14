# GUI 機能パリティ表

旧個人版の GUI（PowerShell 7 + WinForms・1,871行）は**変更禁止の参照実装**である。
その `.Text` 定義から抽出した全機能を、一般配布版（Python + tkinter）で
どう実現するかをここに対応づける。

**この表は Phase 7 の仕様書。** 表に無いものは v1 で作らない。
表にあるものを落とす場合は「削除する機能」の節へ理由とともに移す。

- 作成日: 2026-08-14
- 旧 GUI: `scripts\Start-FamilyVideoCatalog-GUI.ps1`
- 新 GUI: `src\local_video_catalog\gui\`

---

## 1. 維持する機能（15 項目）

| # | 旧 GUI の表示 | 新版での実現 | 担当 | 検証 |
|---|---|---|---|---|
| 1 | 環境チェック | `environment_check` を別プロセスで実行し、OK/注意/NG を一覧表示 | `gui/app.py` + `environment_check` | 手動（実機） |
| 2 | 対象確認 | `pipeline --dry-run` の出力を進捗欄へ表示 | `gui/runner.py` | `test_gui_runner` |
| 3 | 処理開始 | `pipeline` を別プロセスで起動し、stdout を取り込む | `gui/runner.py` | `test_gui_runner` |
| 4 | 安全停止 | `userdata/control/stop-request` を作る。**プロセスは殺さない** | `pipeline.request_stop` | `test_pipeline` |
| 5 | 前回の続きから処理する | 既定で有効。完了工程は `stage_status` で飛ばす | `stage_report` | `test_pipeline` |
| 6 | 失敗のみ再試行 | `--only-catalog-id` を渡す | `gui/runner.py` + `stage_report` | `test_frames_and_progress` |
| 7 | HTMLカタログを更新 | `html_catalog` を実行 | `html_catalog` | `test_description_and_catalog` |
| 8 | HTMLを開く | 既定ブラウザーで `userdata/catalog/catalog.html` を開く | `gui/app.py` | 手動 |
| 9 | 説明文を開く | エクスプローラーで `userdata/descriptions/` を開く | `gui/app.py` | 手動 |
| 10 | 元動画位置を開く | エクスプローラーで**選択表示**する。**開くだけで変更しない** | `gui/app.py` | 手動 |
| 11 | ローカルAI設定… | 映像解析／説明文／Whisper モデルを選ぶダイアログ。設定ファイルは書き換えず実行時に渡す | `gui/dialogs/` | `test_gui_state` |
| 12 | 進み具合 | 工程別の完了数と、いま処理中の動画 | `gui/app.py` + `stage_report` | 手動 |
| 13 | ログ表示 | 子プロセスの stdout をリアルタイム表示 | `gui/runner.py` | `test_gui_runner` |
| 14 | 実行条件 | 稼働時間・本数上限・制限なし・映像解析を飛ばす・完了後に中間ファイル整理 | `gui/state.py` | `test_gui_state` |
| 15 | 入力元 / サブフォルダーも含める | フォルダー選択ダイアログ | `gui/app.py` + `gui/state.py` | `test_gui_state` |

### 長時間運転中も応答を維持する仕組み（旧版から継承）

| 要素 | 内容 |
|---|---|
| 別プロセス | 解析は `subprocess` で起動する。GUI プロセス内で回さない |
| 別スレッド | stdout の読み取りは別スレッド。`queue.Queue` で GUI スレッドへ渡す |
| 定期取り込み | GUI スレッドは `after()` で queue を drain して表示更新 |
| 文字コード | 子プロセスへ `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` を明示。**日本語 Windows で出力を取り込むと化けるため** |
| 停止 | ファイル生成のみ。`kill` / `terminate` を使わない |

---

## 2. 削除する機能

| 旧 GUI の機能 | 削除する理由 |
|---|---|
| 保存先の選択（台帳・説明文・ログ） | APP_ROOT から導出するため、選ばせる意味がない。選べると cleanup の基点が設定値になり、One-Folder 原則も崩れる |
| GUI作業履歴の整理 | `%LOCALAPPDATA%` を使わなくなったので、掃除する対象自体が存在しない |

---

## 3. v1 で追加しないもの

大幅な装飾変更、テーマ切替、多言語 UI、複数プロジェクトの切替、
グラフ表示、サムネイル一覧。**旧 GUI で実証済みの実用機能を安全に
再現することを優先する。**

---

## 4. 構造の原則

```
gui/app.py     tkinter ウィジェットの組み立てだけ。ロジックを持たない
gui/runner.py  別プロセス起動・stdout 取り込み・停止要求（GUI 非依存）
gui/state.py   画面状態の保存/復元（GUI 非依存）
```

`runner.py` と `state.py` は tkinter を import しない。
**GUI を起動しなくても unittest で検証できることを最優先する。**
旧版（PowerShell WinForms）は自動テストが事実上不可能だった。
