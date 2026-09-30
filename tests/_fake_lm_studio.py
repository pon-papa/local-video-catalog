"""試験用の偽 LM Studio（127.0.0.1 のみ）.

**本物の LM Studio を必要とせずに、画像入力の確認を試す**ための最小の
HTTP サーバー。OpenAI 互換の 2 つの経路だけを持つ。

    GET  /v1/models
    POST /v1/chat/completions

受け取った要求は ``requests`` に残るので、**何を送ったか**（＝利用者の
データを送っていないこと、接続先がループバックであること）を試験から
確かめられる。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OK = "ok"
TEXT_ONLY = "text_only"
"""画像つきの要求を 400 で拒む。テキスト専用モデルの再現。"""

SLOW = "slow"
SERVER_ERROR = "server_error"
EMPTY = "empty"
CATALOG = "catalog"
"""解析の全工程に答える（フレーム解析・視覚概要・説明文）。

応答は ``test_end_to_end`` の ``FakeClient`` と同じ。別プロセスで動く解析
（コマンドラインや ToolDock の job の入口）を、本物の LM Studio 無しで最後まで
通すため。``delay_seconds`` を付けると 1 要求ごとに待つ（長い解析の代わり）。
"""

FRAME_REPLY = ('{"caption": "屋外で人が歩いている様子。",'
               ' "setting": "屋外", "readable": true}')
SUMMARY_REPLY = ('{"title_candidate": "屋外の記録",'
                 ' "visual_summary": "屋外で人が歩いている様子が続く。",'
                 ' "main_activity": "歩いている"}')
DESCRIPTION_REPLY = ('{"content": "屋外で人が歩いている記録です。",'
                     ' "youtube": "屋外で撮影した記録です。"}')


def catalog_reply(payload: dict) -> str:
    """要求の中身で工程を見分ける（``FakeClient.chat`` と同じ規則）。"""
    messages = payload.get("messages") or [{}]
    content = messages[0].get("content")
    if isinstance(content, list):
        return FRAME_REPLY
    if "動画全体の概要" in str(content or ""):
        return SUMMARY_REPLY
    return DESCRIPTION_REPLY


class FakeLmStudio:
    """``with FakeLmStudio([...]) as server:`` で使う。"""

    def __init__(self, models: list[str], *, behaviour: str = OK,
                 delay_seconds: float = 0.0) -> None:
        self.models = list(models)
        self.behaviour = behaviour
        self.delay_seconds = delay_seconds
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 起動と停止 -------------------------------------------------------

    def __enter__(self) -> "FakeLmStudio":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:      # 出力を汚さない
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:                   # noqa: N802
                owner.paths.append(self.path)
                if self.path.endswith("/models"):
                    self._send(200, {"data": [{"id": m} for m in owner.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:                  # noqa: N802
                owner.paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    owner.requests.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    owner.requests.append({})

                if owner.behaviour in (SLOW, CATALOG) and owner.delay_seconds:
                    time.sleep(owner.delay_seconds)
                if owner.behaviour == CATALOG:
                    self._send(200, {
                        "choices": [{"message": {"content": catalog_reply(
                            owner.requests[-1])}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                                  "total_tokens": 30},
                    })
                    return
                if owner.behaviour == TEXT_ONLY:
                    self._send(400, {"error": {
                        "message": "this model does not support images"}})
                    return
                if owner.behaviour == SERVER_ERROR:
                    self._send(500, {"error": {"message": "internal"}})
                    return
                if owner.behaviour == EMPTY:
                    self._send(200, {"choices": []})
                    return
                self._send(200, {
                    "choices": [{"message": {"content": "はい"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                              "total_tokens": 12},
                })

        class QuietServer(ThreadingHTTPServer):
            """時間切れの試験では、こちらが書く前に相手が切る。

            それは**確かめたい動作そのもの**なので、traceback を
            出さない（出すと試験の出力が読めなくなる）。
            """

            def handle_error(self, *_args) -> None:
                pass

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- 情報 -------------------------------------------------------------

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def images_sent(self) -> list[str]:
        """送られてきた画像（data URL）を全部集める。"""
        found: list[str] = []
        for payload in self.requests:
            for message in payload.get("messages") or []:
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    url = (part.get("image_url") or {}).get("url", "")
                    if url:
                        found.append(url)
        return found
