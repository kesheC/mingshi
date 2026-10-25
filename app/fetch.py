# -*- coding: utf-8 -*-
"""取数计划（Acquisition Plan）：按能力调度、失败降级、结果固化。

一次研判需要 8 类数据能力。每类能力都有主源与备源（见 ``CAPS``），
调度器逐个尝试，成功即止，并把「最终由谁供数」记进 ``provenance``。

取数计划
--------
  C1 指数日线 ×10        → 行情结构 / 风格轮动 / 波动率
  C2 行业指数日线 ×11    → 行业轮动（中证全指一级行业）
  C3 指数实时快照        → 当日涨跌与成交额（失败可由 C1 末根 K 线回推）
  C4 全市场个股快照      → 市场宽度、估值分布、行业聚合
  C5 板块行情            → 板块层面的广度与领涨
  C6 涨停池 / 跌停池     → 情绪、连板高度、赚钱效应
  C7 两融余额历史        → 杠杆资金
  C8 资讯                → 重要事件（供 AI 抽取）

失败不是异常，是常态。任一能力全源失败都会进入 ``errors``，
对应证据维度降级为 missing/partial，最终反映到置信度与「数据健康」面板。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import source as S
from .net import FetchResult, parallel

#: 能力 -> 按优先级排列的 provider 名
CAPS: dict[str, list[str]] = {
    "index_kline": ["fuyao", "eastmoney", "tencent"],
    "sector_kline": ["eastmoney", "tencent"],
    "index_quotes": ["fuyao", "eastmoney", "sina"],
    "market_snapshot": ["eastmoney", "sina"],
    "industry_boards": ["eastmoney", "sina"],
    "limit_up": ["fuyao", "eastmoney"],
    "limit_down": ["fuyao", "eastmoney"],
    "margin": ["eastmoney"],
    "news": ["eastmoney"],
}


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def _num(v: Any) -> float | None:
    if v is None or v in ("-", "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_em_kline(raw: list) -> list[dict]:
    out = []
    for line in raw or []:
        p = str(line).split(",")
        if len(p) < 7:
            continue
        out.append({"date": p[0], "open": _num(p[1]), "close": _num(p[2]),
                    "high": _num(p[3]), "low": _num(p[4]),
                    "volume": _num(p[5]), "amount": _num(p[6])})
    return [r for r in out if r["close"] is not None]


def rows_of(res: FetchResult) -> list[dict]:
    """把任一 provider 的 K 线结果统一成 [{date, open, close, high, low, ...}]。"""
    if not res or not res.ok or res.data is None:
        return []
    data = res.data
    if isinstance(data, dict):
        if "rows" in data:
            return data["rows"]
        node = data.get("data")
        if isinstance(node, dict) and node.get("klines"):
            return parse_em_kline(node["klines"])
    if isinstance(data, list):
        return data
    return []


def normalize_stock_em(row: dict) -> dict:
    return {
        "code": row.get("f12"), "name": row.get("f14"),
        "price": _num(row.get("f2")), "pct": _num(row.get("f3")),
        "volume": _num(row.get("f5")), "amount": _num(row.get("f6")),
        "turnover": _num(row.get("f8")), "pe": _num(row.get("f9")),
        "pe_ttm": _num(row.get("f115")), "pb": _num(row.get("f23")),
        "mcap": _num(row.get("f20")), "float_mcap": _num(row.get("f21")),
        "net_inflow": _num(row.get("f62")), "industry": row.get("f100") or None,
    }


def normalize_limit(row: dict, kind: str) -> dict:
    """涨停/跌停池归一化。lbc=连板数, zbc=炸板次数, fbt/lbt=首/末次封板时间。"""
    zt = row.get("zttj") or {}
    return {
        "code": row.get("c"), "name": row.get("n"),
        "pct": _num(row.get("zdp")),
        "price": round((_num(row.get("p")) or 0) / 1000.0, 3),
        "amount": _num(row.get("amount")), "float_mcap": _num(row.get("ltsz")),
        "turnover": _num(row.get("hs")), "pe": _num(row.get("pe")),
        "boards": int(row.get("lbc") or zt.get("days") or 1),
        "first_seal": row.get("fbt"), "last_seal": row.get("lbt"),
        "broken": int(row.get("zbc") or 0), "seal_amount": _num(row.get("fund")),
        "industry": row.get("hybk") or "未分类", "kind": kind,
    }


def parse_sina_industry(text: str) -> list[dict]:
    """解析新浪行业板块：``var S_Finance_bankuai_sinaindustry = {"code":"a,b,c,..."}``。"""
    if not text:
        return []
    m = re.search(r"=\s*(\{.*\})", text, re.S)
    if not m:
        return []
    try:
        blob = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for _, v in blob.items():
        parts = str(v).split(",")
        if len(parts) < 13:
            continue
        out.append({
            "code": parts[0], "name": parts[1],
            "count": _num(parts[2]), "avg_price": _num(parts[3]),
            "pct": _num(parts[5]), "amount": _num(parts[7]),
            "leader": parts[12], "leader_pct": _num(parts[9]),
            "net_inflow": None, "up": None, "down": None,
        })
    out.sort(key=lambda x: x["pct"] or -99, reverse=True)
    return out


# --------------------------------------------------------------------------
@dataclass
class Snapshot:
    as_of: str = ""
    generated_at: str = ""
    providers: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    indices: dict = field(default_factory=dict)
    sectors: list = field(default_factory=list)
    market: list = field(default_factory=list)
    boards: list = field(default_factory=list)
    limit_up: list = field(default_factory=list)
    limit_down: list = field(default_factory=list)
    margin: list = field(default_factory=list)
    news: list = field(default_factory=list)
    fetch_log: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def ok_ratio(self) -> float:
        caps = self.provenance
        if not caps:
            return 0.0
        ok = sum(1 for v in caps.values() if v not in ("failed", ""))
        return ok / len(caps)

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of, "generated_at": self.generated_at,
            "providers": self.providers, "provenance": self.provenance,
            "indices": self.indices, "sectors": self.sectors,
            "market": self.market, "boards": self.boards,
            "limit_up": self.limit_up, "limit_down": self.limit_down,
            "margin": self.margin, "news": self.news,
            "fetch_log": self.fetch_log, "errors": self.errors,
            "ok_ratio": round(self.ok_ratio, 4),
        }


class Runner:
    """按能力调度多个 provider 的取数器。"""

    def __init__(self, prefer: str | None = None) -> None:
        self.providers = S.registry()
        self.prefer = prefer or ("fuyao" if self.providers["fuyao"].available() else None)
        self.snap = Snapshot(
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            providers=[p.describe() for p in self.providers.values()],
        )

    # -- 调度 -------------------------------------------------------------
    def _order(self, cap: str) -> list[S.Provider]:
        names = list(CAPS.get(cap, []))
        if self.prefer == "eastmoney":
            # 显式指定东财时把它提到最前，其余顺序不变（扶摇仍作最后兜底）。
            names = [n for n in names if n != "eastmoney"]
            names.insert(0, "eastmoney")
        out = []
        for n in names:
            p = self.providers.get(n)
            if p is None or not p.available() or S.is_dead(n, cap):
                continue
            out.append(p)
        return out

    def call(self, cap: str, *args, **kwargs) -> tuple[FetchResult, str]:
        method = {
            "index_kline": "index_kline", "sector_kline": "board_kline",
            "index_quotes": "index_quotes", "market_snapshot": "market_snapshot",
            "industry_boards": "industry_boards", "limit_up": "limit_up_pool",
            "limit_down": "limit_down_pool", "margin": "margin", "news": "news",
        }[cap]
        tried = []
        for p in self._order(cap):
            fn = getattr(p, method, None)
            if fn is None:
                continue
            try:
                res = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                res = FetchResult(f"{p.name}:{cap}", "", False, error=f"{type(exc).__name__}: {exc}")
            # market_snapshot 返回 (rows, page_results)，统一包成单次结果
            if isinstance(res, tuple):
                rows, pages = res
                ok = bool(rows)
                first_err = next((x.error for x in pages if not x.ok), "") if pages else ""
                res = FetchResult(f"{p.name}:{cap}", "", ok, data=(rows, pages),
                                  error="" if ok else (first_err or "空响应"),
                                  attempts=len(pages) or 1,
                                  fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"))
                for x in pages:
                    self.snap.fetch_log.append({**x.to_meta(), "cap": cap, "provider": p.name})
            tried.append(p.name)
            ok = bool(res.ok) and self._has_payload(cap, res)
            S.mark(p.name, cap, ok)
            if not isinstance(res.data, tuple):
                self.snap.fetch_log.append({**res.to_meta(), "cap": cap, "provider": p.name})
            if ok:
                self.snap.provenance[cap] = p.name
                if len(tried) > 1:
                    self.snap.provenance.setdefault("fallbacks", [])
                    if isinstance(self.snap.provenance["fallbacks"], list):
                        self.snap.provenance["fallbacks"].append({"cap": cap, "used": p.name,
                                                                  "skipped": tried[:-1]})
                return res, p.name
            self.snap.errors.append({"cap": cap, "provider": p.name,
                                     "error": res.error or "空响应"})
        self.snap.provenance[cap] = "failed"
        self.snap.errors.append({"cap": cap, "provider": "-", "error": "全部数据源均失败"})
        return FetchResult(cap, "", False, error="no provider available"), ""

    @staticmethod
    def _has_payload(cap: str, res: FetchResult) -> bool:
        d = res.data
        if d is None:
            return False
        if cap in ("market_snapshot",):
            return isinstance(d, tuple) and len(d[0]) > 0
        if cap in ("index_kline", "sector_kline"):
            return bool(rows_of(res))
        if cap == "industry_boards":
            return bool(d) if isinstance(d, (list, dict, str)) else False
        if cap in ("limit_up", "limit_down"):
            return isinstance(d, dict) and isinstance(d.get("data"), dict)
        if cap == "margin":
            return isinstance(d, dict) and bool((d.get("result") or {}).get("data"))
        if cap == "news":
            return isinstance(d, dict) and bool(d.get("data"))
        if cap == "index_quotes":
            return True
        return True


# --------------------------------------------------------------------------
def collect(prefer: str | None = None) -> Snapshot:
    S.reset_health()
    run = Runner(prefer)
    snap = run.snap

    # ---- C1 指数日线 ----------------------------------------------------
    secids = [s for s, _, _ in S.INDEX_POOL]

    def _idx(secid: str):
        return secid, run.call("index_kline", secid, 260)

    klines: dict[str, list[dict]] = {}
    for item in parallel(_idx, secids, max_workers=5):
        if isinstance(item, tuple):
            secid, (res, _prov) = item
            klines[secid] = rows_of(res)

    # as_of：以上证指数最后一根日线定交易日
    main = klines.get("1.000001") or []
    snap.as_of = main[-1]["date"] if main else time.strftime("%Y-%m-%d")

    for secid, name, role in S.INDEX_POOL:
        snap.indices[secid] = {"name": name, "role": role, "kline": klines.get(secid, [])}

    # ---- C2 行业指数日线 ------------------------------------------------
    def _sec(item):
        secid, _nm = item
        return secid, run.call("sector_kline", secid, 120)

    for item in parallel(_sec, list(S.SECTOR_POOL), max_workers=4):
        if isinstance(item, tuple):
            secid, (res, _prov) = item
            snap.sectors.append({"secid": secid,
                                 "name": dict(S.SECTOR_POOL)[secid],
                                 "kline": rows_of(res)})

    # ---- C3 指数实时快照 ------------------------------------------------
    if snap.indices.get("1.000001", {}).get("kline"):
        latest = snap.indices["1.000001"]["kline"][-1]
        prev = snap.indices["1.000001"]["kline"][-2]["close"] if len(snap.indices["1.000001"]["kline"]) > 1 else None
        if prev:
            snap.provenance["hot_quote_derived"] = "kline_tail"
            snap.as_of = latest["date"]

    res, prov = run.call("index_quotes")
    if res.ok and isinstance(res.data, dict):
        for it in ((res.data.get("data") or {}).get("diff") or []):
            code = it.get("f12")
            for secid, name, _r in S.INDEX_POOL:
                if secid.endswith(str(code)):
                    snap.indices.setdefault(secid, {"name": name, "role": _r, "kline": []})
                    snap.indices[secid]["quote"] = {
                        "price": _num(it.get("f2")), "pct": _num(it.get("f3")),
                        "amount": _num(it.get("f6"))}
                    break
    elif res.ok and isinstance(res.data, str):
        # 新浪文本格式：名称,今开,昨收,现价,最高,最低,买,卖,成交量,成交额,...
        for line in res.data.splitlines():
            m = re.search(r'hq_str_(\w+)="([^"]*)"', line)
            if not m:
                continue
            code, body = m.group(1), m.group(2).split(",")
            if len(body) < 10:
                continue
            for secid, name, _r in S.INDEX_POOL:
                if S._tx_code(secid) == code:
                    prev, price = _num(body[2]), _num(body[3])
                    snap.indices.setdefault(secid, {"name": name, "role": _r, "kline": []})
                    snap.indices[secid]["quote"] = {
                        "price": price,
                        "pct": round((price - prev) / prev * 100, 2) if prev and price else None,
                        "amount": _num(body[9])}

    # ---- C4 全市场快照 --------------------------------------------------
    res, prov = run.call("market_snapshot")
    rows = res.data[0] if (res.ok and isinstance(res.data, tuple)) else []
    if prov == "eastmoney":
        snap.market = [normalize_stock_em(r) for r in rows if r.get("f12")]
    else:
        snap.market = rows
    # 过滤停牌与北交所，保持口径一致
    snap.market = [s for s in snap.market
                   if s.get("price") is not None and not str(s.get("code", "")).startswith("92")]

    # ---- C5 板块 --------------------------------------------------------
    res, prov = run.call("industry_boards")
    if res.ok:
        if prov == "eastmoney" and isinstance(res.data, dict):
            for b in ((res.data.get("data") or {}).get("diff") or []):
                snap.boards.append({
                    "code": b.get("f12"), "name": b.get("f14"),
                    "pct": _num(b.get("f3")), "net_inflow": _num(b.get("f62")),
                    "up": _num(b.get("f104")), "down": _num(b.get("f105")),
                    "leader": b.get("f128"), "leader_pct": _num(b.get("f136")),
                    "mcap": _num(b.get("f20"))})
        elif isinstance(res.data, str):
            snap.boards = parse_sina_industry(res.data)

    # ---- C6 涨跌停池 ----------------------------------------------------
    date_compact = snap.as_of.replace("-", "")
    res, prov = run.call("limit_up", date_compact)
    if prov == "eastmoney" and res.ok and isinstance(res.data, dict):
        snap.limit_up = [normalize_limit(r, "up")
                         for r in ((res.data.get("data") or {}).get("pool") or [])]
    res, prov = run.call("limit_down", date_compact)
    if prov == "eastmoney" and res.ok and isinstance(res.data, dict):
        snap.limit_down = [normalize_limit(r, "down")
                           for r in ((res.data.get("data") or {}).get("pool") or [])]

    # ---- C7 两融 --------------------------------------------------------
    res, _prov = run.call("margin")
    if res.ok and isinstance(res.data, dict):
        for row in ((res.data.get("result") or {}).get("data") or []):
            snap.margin.append({
                "date": str(row.get("DIM_DATE") or "")[:10],
                "rzye": _num(row.get("RZYE")), "rzrqye": _num(row.get("RZRQYE")),
                "rzmre": _num(row.get("RZMRE")), "zdf": _num(row.get("ZDF")),
                "ltsz": _num(row.get("LTSZ")), "rzyezb": _num(row.get("RZYEZB"))})
        snap.margin.sort(key=lambda x: x["date"])

    # ---- C8 资讯 --------------------------------------------------------
    res, _prov = run.call("news")
    if res.ok and isinstance(res.data, dict):
        lst = res.data.get("data") or []
        if isinstance(lst, dict):
            lst = lst.get("list") or []
        for it in lst[:60]:
            title = it.get("title") or it.get("Art_Title") or ""
            if not title:
                continue
            snap.news.append({
                "title": title, "time": it.get("showTime") or it.get("Art_ShowTime") or "",
                "url": it.get("url") or it.get("Art_Url") or "",
                "summary": (it.get("digest") or it.get("Art_Summary") or "")[:160]})

    snap.provenance["market_rows"] = str(len(snap.market))
    return snap
