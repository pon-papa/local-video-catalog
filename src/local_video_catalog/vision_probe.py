"""選んだモデルが**本当に画像を受け取れるか**を確かめる.

**名前で判断しない。** model id に ``vl`` や ``vision`` が入っているかどうかは
根拠にならない。入っていても画像を拒むモデルがあり、入っていなくても
扱えるモデルがある。だから、**実際に小さな画像を 1 枚送って確かめる**。

送るもの:

  - **コードの中で作った 8×8 の PNG** だけ。
  - 家族の動画・代表画像・``userdata/cache/frames`` は**絶対に使わない**。
  - 一時ファイルも作らない（base64 で組み立ててそのまま送る）。

接続先は ``LocalVlmClient`` を通すので localhost に限定される。

判定は 5 つに分ける。**「たぶん使える」で通さない。**

    NOT_CONNECTED   LM Studio へ接続できない
    MODEL_MISSING   選んだ model id が今の LM Studio に無い
    NO_IMAGE        応答はするが画像入力を受け付けない
    UNKNOWN         時間切れなどで**確かめられなかった**
    OK              画像入力を受け付けて、正常に応答した

``UNKNOWN`` は「使える」ではない。一般配布版では安全側に倒し、
開始できないものとして扱う（利用者は環境チェックをやり直せる）。
"""

from __future__ import annotations

import base64
import struct
import zlib
from dataclasses import dataclass

from . import vlm_client

NOT_CONNECTED = "not_connected"
MODEL_MISSING = "model_missing"
NO_IMAGE = "no_image_support"
UNKNOWN = "unknown"
OK = "ok"

DEFAULT_TIMEOUT_SECONDS = 120
"""probe の待ち時間。

送るもの自体は 8×8 の画像 1 枚なので一瞬で終わる。**時間を食うのは
LM Studio 側のモデル読み込み。** 実測（2026-08-15）:

    qwen3-vl-8b-instruct                    1.4 秒（読み込み済み）
    lfm2.5-vl-3b                           28.5 秒（読み込みを含む）
    qwythos-9b-claude-mythos-5-1m          56.7 秒（読み込み → 画像を拒否）

最後の 1 件が効いている。60 秒にしていると、**「画像非対応」と正しく
答えられるはずの場面で「確認できませんでした」に化ける**。
対処が変わってしまうので、読み込みの遅い環境でも間に合う 120 秒にする。

**それでも足りなければ環境チェックをやり直せばよい。**
"""

MAX_TOKENS = 16
"""**内容は見ない。** 受け取れたかどうかだけが分かればよいので最小限。"""

PROMPT = "この画像に色はありますか。10文字以内で答えてください。"


def test_image_png() -> bytes:
    """8×8 の市松模様の PNG を**その場で作る**。

    配布物にも userdata にも画像を置かない。利用者のデータは一切含まれない。
    """
    width = height = 8
    rows = bytearray()
    for y in range(height):
        rows.append(0)                       # フィルタ種別（なし）
        for x in range(width):
            value = 240 if (x // 2 + y // 2) % 2 == 0 else 16
            rows += bytes((value, value, value))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def test_image_base64() -> str:
    return base64.b64encode(test_image_png()).decode("ascii")


@dataclass
class ProbeResult:
    """確かめた結果。"""

    outcome: str
    detail: str = ""
    advice: str = ""
    model_id: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome == OK

    def to_dict(self) -> dict[str, str]:
        return {"outcome": self.outcome, "detail": self.detail,
                "advice": self.advice, "model_id": self.model_id}


def _classify(error: vlm_client.VlmError) -> ProbeResult:
    """API の失敗を、利用者に意味のある区別へ直す。

    **時間切れを「画像非対応」と言い切らない。** 逆に、モデルが画像を
    拒んだのを「確認できませんでした」で濁さない。対処が変わるため。
    """
    kind = error.error_type
    if kind == vlm_client.ERROR_TIMEOUT:
        return ProbeResult(
            UNKNOWN, "画像処理能力を確認できませんでした（時間切れ）",
            "モデルの読み込みに時間がかかっている可能性があります。"
            "少し待ってから「環境チェック」をやり直してください。")
    if kind == vlm_client.ERROR_CONNECTION:
        return ProbeResult(
            NOT_CONNECTED, "LM Studio へ接続できません",
            "LM Studio を起動し、ローカルサーバーを ON にしてください。")
    if kind == vlm_client.ERROR_MODEL_NOT_FOUND:
        return ProbeResult(
            MODEL_MISSING, "選んだモデルを利用できません",
            "「ローカルAI設定」でモデルを選び直してください。")
    if kind in (vlm_client.ERROR_HTTP_4XX, vlm_client.ERROR_SCHEMA,
                vlm_client.ERROR_EMPTY_RESPONSE, vlm_client.ERROR_INVALID_JSON):
        # 画像を含む要求をモデル側が受け付けなかった。
        return ProbeResult(
            NO_IMAGE, "このモデルでは利用できません",
            "映像の解析には画像入力に対応したモデルが必要です。"
            "別のモデルを選択してください。")
    # 5xx など。**一時的な不調かもしれないので「非対応」と決めつけない。**
    return ProbeResult(
        UNKNOWN, "画像処理能力を確認できませんでした",
        "LM Studio 側の状態を確認して、「環境チェック」をやり直してください。")


def probe(settings: vlm_client.VlmSettings, model_id: str, *,
          timeout_seconds: int | None = None) -> ProbeResult:
    """画像を 1 枚送って、受け取れるかを確かめる。

    **内容は評価しない。** 受け付けて応答が返るかどうかだけを見る。
    ここが通っても「このシステムに向いたモデル」だとは限らない。
    """
    # **既定値は呼び出し時に決める。** 引数の既定値にすると定義時に
    # 固定され、設定で変えられなくなる。
    timeout = int(timeout_seconds or settings.vision_probe_timeout_seconds
                  or DEFAULT_TIMEOUT_SECONDS)

    if not str(model_id).strip():
        return ProbeResult(
            MODEL_MISSING, "モデルが選択されていません",
            "映像解析に使用するモデルを選択してください。")

    try:
        client = vlm_client.LocalVlmClient(settings)
    except vlm_client.PrivacyConfigurationError as exc:
        return ProbeResult(NOT_CONNECTED, str(exc),
                           "接続先はこのPCの中だけにしてください。")
    except ValueError as exc:
        return ProbeResult(UNKNOWN, str(exc), "設定を確認してください。")

    message = vlm_client.build_image_message(
        text=PROMPT, image_base64=test_image_base64(),
        media_type="image/png")
    try:
        client.chat(model_id=model_id, messages=[message],
                    max_tokens=MAX_TOKENS, timeout_seconds=timeout)
    except vlm_client.PrivacyConfigurationError as exc:
        return ProbeResult(NOT_CONNECTED, str(exc),
                           "接続先はこのPCの中だけにしてください。")
    except vlm_client.VlmError as exc:
        found = _classify(exc)
        found.model_id = model_id
        return found

    return ProbeResult(OK, "対応しています", "", model_id)
