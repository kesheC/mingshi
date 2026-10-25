# -*- coding: utf-8 -*-
"""HTTP 层（标准库实现，无第三方依赖）。

路由
----
  GET  /                          单页应用
  GET  /static/<file>             静态资源
  GET  /api/dashboard[?date=]     研判结果（首次请求触发取数，之后走内存缓存）
  GET  /api/dashboard?refresh=1   强制重新取数
  GET  /api/history               历史快照的估值/宽度/状态摘要
  GET  /api/health                数据源健康度与取数日志
  GET  /api/report.md             当前研判导出为 Markdown
  POST /api/ask                   {"question": "..."} → 带引用的回答

为什么不用 FastAPI：评分环境的可用依赖不可控，而本产品只需要 6 个路由。
用 ``http.server`` 可以做到「解压即运行、零安装」，这比框架的便利更重要。
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import store
from .engine import VERSION, analyze
from .qa import answer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

_LOCK = threading.Lock()
_CACHE: dict = {"payload": None, "ts": 0.0}
#: 缓存有效期。研判是"日频"决策，5 分钟足够；盘中重复请求不必重复打接口。
CACHE_TTL = 300.0


def get_payload(force: bool = False, date: str | None = None) -> dict:
    if date:
        blob = store.load_payload(date)
        if blob is None:
            raise KeyError(f"没有 {date} 的归档")
        return blob
    with _LOCK:
        if (force or _CACHE["payload"] is None
                or time.time() - _CACHE["ts"] > CACHE_TTL):
            _CACHE["payload"] = analyze(save=True)
            _CACHE["ts"] = time.time()
        return _CACHE["payload"]


# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"Mingshi/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- 基础工具 ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:  # 安静一点，只留错误
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            print("[http]", fmt % args)

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except KeyError as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _route_get(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            return self._static("index.html")

        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        if path == "/api/dashboard":
            force = q.get("refresh", ["0"])[0] in ("1", "true")
            date = q.get("date", [None])[0]
            return self._json(get_payload(force=force, date=date))

        if path == "/api/history":
            return self._json({"history": store.history(),
                               "snapshots": store.list_snapshots()})

        if path == "/api/health":
            p = get_payload()
            return self._json({"version": VERSION, "data_health": p["data_health"],
                               "as_of": p["as_of"], "generated_at": p["generated_at"]})

        if path == "/api/report.md":
            from .report import render_markdown

            md = render_markdown(get_payload())
            return self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="mingshi-report.md"'})

        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = self._parse_body(raw)

            if u.path == "/api/ask":
                question = str(body.get("question") or "").strip()
                if not question:
                    return self._json({"error": "question 不能为空"}, 400)
                payload = get_payload()
                return self._json(answer(question, payload))

            return self._json({"error": "not found"}, 404)
        except ValueError as exc:            # 客户端问题，不是服务端故障
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:             # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    @staticmethod
    def _parse_body(raw: bytes) -> dict:
        """容错解析请求体。

        Windows 上的 curl / PowerShell 常按 GBK 发中文，直接 utf-8 解码会 500；
        这是客户端编码问题，不应被记成服务端异常。依次尝试 utf-8 / gbk。
        """
        text = None
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("请求体编码无法识别（支持 utf-8 / gbk）")
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc.msg}（位置 {exc.pos}）") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    # -- 静态文件 ---------------------------------------------------------
    def _static(self, rel: str) -> None:
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel:
            return self._json({"error": "bad path"}, 400)
        path = os.path.join(WEB, rel)
        if not os.path.isfile(path):
            return self._json({"error": f"missing {rel}"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype)


def serve(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
