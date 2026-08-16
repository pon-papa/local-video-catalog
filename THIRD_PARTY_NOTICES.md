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
> Python と Tcl/Tk は python.org 公式からのみ取得し、ファイル名・取得元 URL・
> SHA-256 を開発リポジトリの `tools/runtime_sources.json` に記録しています。
> 文字起こしモデルは `tools/whisper_model_source.json` に同じ形で記録しています。
> 配布物を作る工程は、記録した SHA-256 と一致する素材でしか進みません。

### Whisper 文字起こしモデル

`userdata\models\whisper\ggml-large-v3-turbo-q5_0.bin` に入っています。
利用者がモデルを探して配置しなくても、文字起こしを使えるようにするためです。

- ファイル名: `ggml-large-v3-turbo-q5_0.bin`（574,041,195 バイト）
- SHA-256: `394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2`
- 入手元: <https://huggingface.co/ggerganov/whisper.cpp>
  （`resolve/main/ggml-large-v3-turbo-q5_0.bin`）
- 内容: OpenAI Whisper **large-v3-turbo** を ggml 形式へ変換し、q5_0 で量子化したもの
- 上流: <https://github.com/openai/whisper>
- ライセンス: **MIT**
  - 配布元のモデルカードが `license: mit` を宣言しています
  - 上流の OpenAI Whisper も MIT（Copyright © 2022 OpenAI）
- 著作権表示: Copyright © 2022 OpenAI
  （許諾文の全文を `userdata\models\whisper\WHISPER_MODEL_LICENSE.txt` に同梱）

変換物の配布元は whisper.cpp プロジェクト（ggerganov）です。

> 別のモデルを使いたい場合は、`userdata\models\whisper\` へ置いて
> 「ローカルAI設定」で選べます。同梱モデルの削除も自由です。

---

## 同梱していないもの（利用者が別途用意します）

配布物に含めていません。それぞれの提供元の条件に従ってご用意ください。

| ソフトウェア | 用途 | 入手元 |
|---|---|---|
| LM Studio | 映像の解析・説明文の生成に使うローカル AI サーバー | <https://lmstudio.ai/> |
| VLM モデル（例: qwen3-vl-8b-instruct） | 映像の解析 | LM Studio 内のモデル検索から取得します |
| ffmpeg / ffprobe | 動画情報の取得・静止画の抽出・文字起こし | <https://ffmpeg.org/download.html> |

### ffmpeg のライセンスについて

**ffmpeg はビルドによってライセンス条件が異なります。**
LGPL のもの、GPL のもの、`--enable-nonfree` を含むものなどがあり、
このプロジェクトから「必ずこの条件です」と断定することはできません。

お使いになるビルドの配布元が示す条件をご確認ください。
このアプリは ffmpeg を同梱せず、リンクもせず、利用者の環境にある
実行ファイルを**外部プロセスとして呼び出すだけ**です。

### モデルについて

VLM モデルには配布元が定めるライセンスや利用条件があります。
モデルの入手時に提示される条件をご確認ください。

このアプリは **VLM モデルを同梱せず、自動ダウンロードも行いません**。
文字起こしモデルだけは上記のとおり同梱しています（MIT）。

---

## このアプリが送信するもの

映像・画像・音声・文字起こし・説明文を**外部へ送信しません**。
接続先は同じ PC の中（`127.0.0.1` / `localhost` / `::1`）に限定しています。
