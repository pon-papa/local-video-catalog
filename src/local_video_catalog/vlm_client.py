"""ローカル AI（LM Studio）への接続（localhost 限定）.

動画から取り出した静止画をモデルへ渡すため、**外部へ送信されないことを
コード側で保証する**必要がある。

このモジュールの設計:

  1. 接続先ホストを 127.0.0.1 / localhost / ::1（とループバック IP）へ
     限定する。それ以外なら、**画像を組み立てる前に**例外で停止する。
  2. **HTTP リダイレクトを一切辿らない。** ローカルへ送ったつもりが
     302 で外部へ転送される事故を防ぐ。
  3. 環境変数の HTTP_PROXY / HTTPS_PROXY を明示的に無効化する。
     プロキシ経由で外部へ出る経路を塞ぐ。
  4. **「開発時だけ外部を許可する」オプションを実装しない。**
     逃げ道があれば、いつか誰かが使う。
  5. 要求のたびに URL を再検証する（base_url を後から書き換えられても守る）。
  6. 記録には scheme://host:port だけを残す。認証情報やパスの詳細は残さない。
  7. ホスト名を名前解決に頼らない。hosts の書き換えで外へ向く余地を消す。
  8. 標準ライブラリの urllib のみを使う（HTTP クライアントを追加しない）。
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

ALLOWED_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
"""これ以外のホストへは、いかなる理由でも接続しない。"""

ALLOWED_SCHEMES = frozenset({"http", "https"})

ERROR_CONNECTION = "connection_error"
ERROR_TIMEOUT = "timeout"
ERROR_MODEL_NOT_FOUND = "model_not_found"
ERROR_HTTP_4XX = "http_4xx"
ERROR_HTTP_5XX = "http_5xx"
ERROR_INVALID_JSON = "invalid_json"
ERROR_SCHEMA = "schema_validation_error"
ERROR_EMPTY_RESPONSE = "empty_response"
ERROR_PRIVACY_CONFIG = "privacy_configuration_error"
ERROR_UNKNOWN = "unknown"


class PrivacyConfigurationError(Exception):
    """接続先がローカルでない。**画像を送る前に必ずここで止める。**"""


class VlmError(Exception):
    """API 呼び出しの失敗。``error_type`` に分類を持つ。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class ModelSelectionError(Exception):
    """モデルを一意に決められない。**勝手に選ばず停止する。**"""


# --------------------------------------------------------------------------
# 接続先の検証
# --------------------------------------------------------------------------


def is_local_host(host: str | None) -> bool:
    """ホストがループバックか。

    ホスト名は許可リストの完全一致のみ。IP アドレスはループバック範囲を許可。
    **名前解決に頼らない。** 頼ると hosts の書き換えで外部へ向く余地が生まれる。
    """
    if not host:
        return False
    normalized = str(host).strip().strip("[]").lower()
    if normalized in ALLOWED_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback


def assert_local_base_url(base_url: str) -> str:
    """base_url がローカル向けであることを検証する。

    問題があれば ``PrivacyConfigurationError``。
    **画像を組み立てる前に必ず呼ぶこと。**

    Returns:
        末尾のスラッシュを取り除いた base_url
    """
    if not base_url or not str(base_url).strip():
        raise PrivacyConfigurationError("接続先が設定されていません。")

    parsed = urlparse(str(base_url).strip())

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise PrivacyConfigurationError(
            f"許可されていない方式です: {parsed.scheme!r}。"
            "http または https のローカル接続のみ使用できます。")

    if not is_local_host(parsed.hostname):
        raise PrivacyConfigurationError(
            f"このPCの外への接続は禁止されています: {parsed.hostname!r}\n"
            "動画の内容を外部へ送信しないため、接続先は "
            "127.0.0.1 / localhost / ::1 に限定されています。")

    return str(base_url).strip().rstrip("/")


def safe_api_base_for_record(base_url: str) -> str:
    """記録用に、ローカルであることが分かる最小限へ変換する。

    認証情報やパスの詳細は残さない。
    """
    parsed = urlparse(str(base_url))
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """リダイレクトを一切辿らない。

    ローカルへ送ったつもりのリクエストが 302 で外部へ転送されると、
    画像が外へ出てしまう。リダイレクトはすべて失敗として扱う。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise PrivacyConfigurationError(
            f"HTTP リダイレクトが返されました（{code} → {newurl}）。"
            "外部へ転送される恐れがあるため、追跡せずに中止します。")


def build_opener() -> urllib.request.OpenerDirector:
    """プロキシを無効化し、リダイレクトを拒否する opener を作る。

    ``ProxyHandler({})`` で環境変数の HTTP_PROXY / HTTPS_PROXY を無視させる。
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),      # 環境プロキシを使わない
        _NoRedirectHandler(),
    )


# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------


@dataclass
class VlmSettings:
    base_url: str = "http://127.0.0.1:1234/v1"
    model_match: str = "qwen3-vl-8b-instruct"
    temperature: float = 0.1
    top_p: float = 0.9
    max_tokens_per_frame: int = 1000
    max_tokens_summary: int = 1600
    timeout_seconds: int = 300
    """フレーム 1 枚あたりの待ち時間（実測 20〜40 秒）。"""

    summary_timeout_seconds: int = 1200
    """視覚概要の待ち時間。

    **フレーム 1 枚と分けている。** 概要は枚数に比例して伸び（実測 約16秒/枚）、
    24 枚なら 390 秒前後になる。フレームと同じ 300 秒を適用すると、
    22〜24 枚の動画がまとめて失敗する。

    **この値は config_hash に含めない。** 待ち時間は生成内容を変えないので、
    変更しても保存済みの解析はそのまま再利用できる。
    """

    maximum_concurrent_requests: int = 1

    def validate(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds は 1 以上にしてください。")
        if self.summary_timeout_seconds < 1:
            raise ValueError("summary_timeout_seconds は 1 以上にしてください。")
        if self.maximum_concurrent_requests != 1:
            raise ValueError(
                "同時要求数は 1 のみ対応しています"
                "（VRAM の少ない環境で同時送信しないため）。")

    def generation_identity(self) -> dict[str, Any]:
        """**生成内容に影響する設定だけ**を返す。

        config_hash の材料。待ち時間や同時要求数はここへ入れない。
        入れてしまうと、待ち時間を変えただけで解析をやり直すことになる。
        """
        return {
            "model_match": self.model_match,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens_per_frame": self.max_tokens_per_frame,
            "max_tokens_summary": self.max_tokens_summary,
        }

    @classmethod
    def from_settings(cls, raw: dict[str, Any]) -> "VlmSettings":
        section = dict(raw.get("vlm") or {})
        frame_timeout = int(section.get("timeout_seconds", 300))
        return cls(
            base_url=str(section.get("base_url", cls.base_url)),
            model_match=str(section.get("model_match", cls.model_match)),
            temperature=float(section.get("temperature", cls.temperature)),
            top_p=float(section.get("top_p", cls.top_p)),
            max_tokens_per_frame=int(section.get(
                "max_tokens_per_frame", cls.max_tokens_per_frame)),
            max_tokens_summary=int(section.get(
                "max_tokens_summary", cls.max_tokens_summary)),
            timeout_seconds=frame_timeout,
            # フレームより短い概要待ちには意味がないので、下限を揃える。
            # 明示的に大きく設定している利用者の意図は尊重する。
            summary_timeout_seconds=int(section.get(
                "summary_timeout_seconds",
                max(cls.summary_timeout_seconds, frame_timeout))),
            maximum_concurrent_requests=int(section.get(
                "maximum_concurrent_requests", 1)),
        )


# --------------------------------------------------------------------------
# メッセージ
# --------------------------------------------------------------------------


def build_image_message(*, text: str, image_base64: str,
                        media_type: str = "image/jpeg") -> dict[str, Any]:
    """画像 1 枚つきのユーザーメッセージ（OpenAI 互換）。"""
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url",
             "image_url": {"url": f"data:{media_type};base64,{image_base64}"}},
        ],
    }


def build_text_message(text: str, *, role: str = "user") -> dict[str, Any]:
    return {"role": role, "content": text}


# --------------------------------------------------------------------------
# クライアント
# --------------------------------------------------------------------------


def select_model(available: list[str], wanted: str) -> str:
    """使うモデルを一意に決める。

    完全一致 → 部分一致（1 件のみ）の順。**曖昧なら選ばずに停止する。**
    黙ってどれかを選ぶと、別モデルの結果が混ざる。
    """
    if not wanted:
        raise ModelSelectionError("使うモデルが指定されていません。")
    if wanted in available:
        return wanted
    matches = [name for name in available if wanted.lower() in name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ModelSelectionError(
            f"指定したモデルが見つかりません: {wanted}\n"
            "LM Studio でこのモデルを読み込むか、"
            "「ローカルAI設定」で選び直してください。")
    raise ModelSelectionError(
        f"モデルを一意に決められません: {wanted} に {len(matches)} 件が一致します"
        f"（{', '.join(matches[:5])}）。正確な名前を指定してください。")


class LocalVlmClient:
    """LM Studio の OpenAI 互換 API を叩く（localhost 限定）。"""

    def __init__(self, settings: VlmSettings) -> None:
        settings.validate()
        self.settings = settings
        self.base_url = assert_local_base_url(settings.base_url)
        self._opener = build_opener()

    # -- 低レベル --------------------------------------------------------

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        # 念のため毎回検証する（base_url が後から書き換えられても守る）
        assert_local_base_url(url)

        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(
            url, data=data, headers=headers, method=method)

        # 待ち時間は要求の種類で変わる（フレーム 1 枚と視覚概要では桁が違う）。
        # 生成内容には影響しないため config_hash には含めない。
        timeout = timeout_seconds or self.settings.timeout_seconds
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read()
        except PrivacyConfigurationError:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            error_type = ERROR_HTTP_4XX if 400 <= status < 500 else ERROR_HTTP_5XX
            raise VlmError(error_type, f"HTTP {status} が返されました。") from None
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "timed out" in reason.lower():
                raise VlmError(ERROR_TIMEOUT,
                               "応答が制限時間を超えました。") from None
            raise VlmError(
                ERROR_CONNECTION,
                f"LM Studio へ接続できません（{reason}）。"
                "LM Studio を起動し、ローカルサーバーを ON にしてください。"
            ) from None
        except TimeoutError:
            raise VlmError(ERROR_TIMEOUT, "応答が制限時間を超えました。") from None
        except OSError as exc:
            raise VlmError(ERROR_CONNECTION,
                           f"通信に失敗しました: {exc}") from None

        if not body:
            raise VlmError(ERROR_EMPTY_RESPONSE, "空の応答が返されました。")
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise VlmError(ERROR_INVALID_JSON,
                           f"応答を解釈できません: {exc}") from None

    # -- 高レベル --------------------------------------------------------

    def list_models(self) -> list[str]:
        payload = self._request("/models", timeout_seconds=min(
            self.settings.timeout_seconds, 30))
        data = payload.get("data") or []
        return [str(item.get("id")) for item in data if item.get("id")]

    def chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        timeout_seconds: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """1 回の会話要求。``(本文, 使用トークンなどの情報)`` を返す。"""
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": self.settings.temperature,
            "top_p": self.settings.top_p,
            "max_tokens": max_tokens or self.settings.max_tokens_per_frame,
            "stream": False,
        }
        started = time.monotonic()
        response = self._request("/chat/completions", payload, method="POST",
                                 timeout_seconds=timeout_seconds)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        choices = response.get("choices") or []
        if not choices:
            raise VlmError(ERROR_EMPTY_RESPONSE, "返答が空でした。")
        content = (choices[0].get("message") or {}).get("content") or ""
        if not str(content).strip():
            raise VlmError(ERROR_EMPTY_RESPONSE, "返答の本文が空でした。")

        usage = response.get("usage") or {}
        return (str(content), {
            "request_duration_ms": elapsed_ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        })
