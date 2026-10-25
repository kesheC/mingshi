# -*- coding: utf-8 -*-
"""网络层：IPv4 强制、重试、超时、磁盘缓存、可复现快照。

设计约束（工程取舍记录）
------------------------
1. **强制 IPv4**：本机到东财 CDN 的 IPv6 路由不通，urllib 会长时间挂起直到超时。
   通过替换 ``socket.getaddrinfo`` 让所有解析只返回 A 记录，是唯一不需要改业务代码的做法。
   该改动在进程级别生效，因此放在模块导入时执行一次。
2. **磁盘缓存 + 时间戳**：研判结论必须可复现。同一交易日内重复请求走缓存，
   快照落盘后即使断网也能重放当日结论（README 中的「离线重放」能力）。
3. **不做静默降级**：任何一次取数失败都会以 ``FetchResult.ok=False`` 呈现，
   最终进入证据链的 ``freshness`` 字段，直接影响置信度，而不是被吞掉。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# IPv4 强制
# --------------------------------------------------------------------------
_ORIG_GAI = socket.getaddrinfo


def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _ORIG_GAI(host, port, socket.AF_INET, type, proto, flags)


def enforce_ipv4() -> None:
    """幂等地把进程内 DNS 解析限制为 IPv4。"""
    if socket.getaddrinfo is not _ipv4_only:
        socket.getaddrinfo = _ipv4_only  # type: ignore[assignment]


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SSL_CTX = ssl.create_default_context()
_CACHE_LOCK = threading.Lock()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cache")


@dataclass
class FetchResult:
    """一次取数的完整结果，成功与失败都带元信息。"""

    key: str
    url: str
    ok: bool
    data: Any = None
    error: str = ""
    from_cache: bool = False
    elapsed_ms: int = 0
    fetched_at: str = ""
    attempts: int = 0
    extra: dict = field(default_factory=dict)

    def to_meta(self) -> dict:
        """给证据链用的溯源信息（不含 data 本体，避免响应膨胀）。"""
        return {
            "key": self.key,
            "ok": self.ok,
            "from_cache": self.from_cache,
            "elapsed_ms": self.elapsed_ms,
            "fetched_at": self.fetched_at,
            "attempts": self.attempts,
            "error": self.error,
            **({"total": self.extra.get("total")} if "total" in self.extra else {}),
        }


def _cache_path(key: str) -> str:
    safe = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{safe}.json")


def _read_cache(key: str, ttl: float) -> dict | None:
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if ttl > 0 and time.time() - blob.get("_ts", 0) > ttl:
            return None
        if blob.get("_key") != key:
            return None
        return blob
    except Exception:
        return None


def _write_cache(key: str, data: Any) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(key)
    blob = {"_key": key, "_ts": time.time(), "data": data}
    tmp = path + ".tmp"
    with _CACHE_LOCK:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, ensure_ascii=False)
        os.replace(tmp, path)


def get_json(
    key: str,
    url: str,
    *,
    referer: str = "https://quote.eastmoney.com/",
    ttl: float = 600.0,
    timeout: float = 15.0,
    retries: int = 2,
    force: bool = False,
    headers: dict | None = None,
) -> FetchResult:
    """带缓存的 JSON 取数。失败不抛异常，返回 ok=False 的结果对象。"""
    enforce_ipv4()
    if not force:
        cached = _read_cache(key, ttl)
        if cached is not None:
            res = FetchResult(
                key=key, url=url, ok=True, data=cached["data"], from_cache=True,
                elapsed_ms=0, fetched_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cached["_ts"])),
            )
            if isinstance(cached["data"], dict):
                t = cached["data"].get("_total")
                if t is not None:
                    res.extra["total"] = t
            return res

    last_err = ""
    started = time.time()
    for attempt in range(1, retries + 2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer,
                "Connection": "close",
                **(headers or {}),
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", "ignore"))
            _write_cache(key, data)
            res = FetchResult(
                key=key, url=url, ok=True, data=data,
                elapsed_ms=int((time.time() - started) * 1000),
                fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"), attempts=attempt,
            )
            if isinstance(data, dict) and data.get("_total") is not None:
                res.extra["total"] = data["_total"]
            return res
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一收敛
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt <= retries:
                time.sleep(0.25 * attempt)

    return FetchResult(
        key=key, url=url, ok=False, error=last_err,
        elapsed_ms=int((time.time() - started) * 1000),
        fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"), attempts=retries + 1,
    )


def parallel(fn, items, max_workers: int = 8):
    """轻量并发执行，保持输入顺序。用于全市场分页拉取。"""
    from concurrent.futures import ThreadPoolExecutor

    out = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, it): i for i, it in enumerate(items)}
        for fut in list(futures):
            idx = futures[fut]
            try:
                out[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001
                out[idx] = exc
    return out


def qs(url: str, params: dict) -> str:
    """拼查询串；None 值会被丢弃。"""
    clean = {k: v for k, v in params.items() if v is not None}
    return url + "?" + urllib.parse.urlencode(clean)
