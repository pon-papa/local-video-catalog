# 同梱・利用しているソフトウェアについて

このアプリ（local-video-catalog）本体は MIT License です（[LICENSE](LICENSE)）。
そのほかに、配布物へ**同梱しているもの**と、利用者が**別途用意するもの**があります。

---

## 配布 ZIP に同梱しているもの

### Python 3.13.14（Windows x64）

`runtime\` に入っています。利用者が Python をインストールしなくても
このアプリが動くようにするためのものです。

- 入手元: <https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip>
- ライセンス: PSF License Agreement（`runtime\LICENSE.txt` に全文を同梱）
- 著作権表示: Copyright © 2001-2024 Python Software Foundation. All rights reserved.
  （ほか、`runtime\LICENSE.txt` に記載の各権利者）

Python の Windows バイナリには Microsoft の再頒布可能コードが
リンクされています。`runtime\LICENSE.txt` の
"Additional Conditions for this Windows binary build" に従い、
このアプリは次を守っています。

- 著作権・商標・特許の表示を改変していません
- 製品名に Microsoft の商標を使っていません
- Windows 以外のプラットフォーム向けには配布していません

### Tcl/Tk 8.6

画面（tkinter）に必要です。`runtime\tcl\` および
`runtime\_tkinter.pyd` / `tcl86t.dll` / `tk86t.dll` / `zlib1.dll` に入っています。

- 入手元: <https://www.python.org/ftp/python/3.13.14/amd64/tcltk.msi>
  （python.org が配布する公式コンポーネント）
- ライセンス: BSD 形式（`runtime\tcl\tk8.6\license.terms` に全文を同梱）
- 権利者: the Regents of the University of California, Sun Microsystems, Inc.,
  Scriptics Corporation, ActiveState Corporation, Apple Inc. ほか
  （同ファイルの記載どおり）

同ファイルの条項は「既存の著作権表示を残し、この告知をそのまま
配布物へ含めること」を求めています。本配布物はこれを満たしています。
Tcl と Tk は同一の条項で提供されています。

> **入手元と同一性について**
> 上記 2 つは python.org 公式からのみ取得し、ファイル名・取得元 URL・SHA-256 を
> 開発リポジトリの `tools/runtime_sources.json` に記録しています。
> `tools/build_runtime.py` は記録した SHA-256 と一致する素材でしか
> `runtime\` を組み立てません。

---

## 同梱していないもの（利用者が別途用意します）

配布物に含めていません。それぞれの提供元の条件に従ってご用意ください。

| ソフトウェア | 用途 | 入手元 |
|---|---|---|
| LM Studio | 映像の解析・説明文の生成に使うローカル AI サーバー | <https://lmstudio.ai/> |
| VLM モデル（例: qwen3-vl-8b-instruct） | 映像の解析 | LM Studio 内のモデル検索から取得します |
| ffmpeg / ffprobe | 動画情報の取得・静止画の抽出・文字起こし | <https://ffmpeg.org/download.html> |
| Whisper モデル（`ggml-*.bin`） | 文字起こし | <https://huggingface.co/ggerganov/whisper.cpp> |

### ffmpeg のライセンスについて

**ffmpeg はビルドによってライセンス条件が異なります。**
LGPL のもの、GPL のもの、`--enable-nonfree` を含むものなどがあり、
このプロジェクトから「必ずこの条件です」と断定することはできません。

お使いになるビルドの配布元が示す条件をご確認ください。
このアプリは ffmpeg を同梱せず、リンクもせず、利用者の環境にある
実行ファイルを**外部プロセスとして呼び出すだけ**です。

### モデルについて

VLM モデル・Whisper モデルには、それぞれ配布元が定めるライセンスや
利用条件があります。モデルの入手時に提示される条件をご確認ください。
このアプリはモデルを同梱せず、自動ダウンロードも行いません。

---

## このアプリが送信するもの

映像・画像・音声・文字起こし・説明文を**外部へ送信しません**。
接続先は同じ PC の中（`127.0.0.1` / `localhost` / `::1`）に限定しています。
