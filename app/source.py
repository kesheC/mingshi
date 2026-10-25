# -*- coding: utf-8 -*-
"""数据源适配层：多源冗余 + 熔断。

为什么必须多源
--------------
构建过程中实测到：东方财富 ``push2`` 家族（实时行情 / clist）在并发拉取全市场后
会对来源 IP 做短时限流，返回 ``RemoteDisconnected``；而 ``push2his``（历史 K 线）、
``push2ex``（涨跌停池）、``datacenter-web``（两融）不受影响。

一个只会「挂」或「静默返回空」的研判系统是不可用的，所以这里做了三件事：

1. **按能力降级**：每个数据能力（指数行情 / 全市场快照 / 板块 / K 线 …）
   都有主源与备源，失败自动切换，并在快照里记录 `provenance`。
2. **进程内熔断**：某源同类请求连续失败 2 次即在本进程内标记 dead，后续直接跳过，
   避免把 20 秒的超时预算浪费在一百个注定失败的请求上。
3. **失败可见**：切换、熔断、最终失败都会写进 ``fetch_log`` 与 ``errors``，
   前端有「数据健康」面板直接展示——不做静默降级。

三个 Provider
-------------
* :class:`EastMoney` —— 主源，字段最全（含主力净流入 f62、行业 f100）。
* :class:`Sina`      —— 备源，全市场快照与行业板块，字段少但稳定。
* :class:`Tencent`   —— 备源，指数 K 线。
* :class:`Fuyao`     —— 题目要求的扶摇适配器；无凭据时永远 ``available()=False``，
  **绝不返回伪造数据**。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.request
from typing import Any

from .net import FetchResult, _SSL_CTX, get_json, parallel, qs

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: 研判指数池：(secid, 名称, 角色)。角色用于风格轮动。
INDEX_POOL: list[tuple[str, str, str]] = [
    ("1.000001", "上证指数", "broad"),
    ("0.399001", "深证成指", "broad"),
    ("0.399006", "创业板指", "growth"),
    ("1.000688", "科创50", "growth"),
    ("1.000300", "沪深300", "large"),
    ("1.000016", "上证50", "large"),
    ("1.000905", "中证500", "mid"),
    ("1.000852", "中证1000", "small"),
    ("0.399303", "国证2000", "small"),
    ("1.000922", "中证红利", "value"),
]

#: 中证全指一级行业指数（11 个），用于行业轮动。
#: 选它而不是东财板块，是因为中证指数口径稳定、有完整历史、可复现。
SECTOR_POOL: list[tuple[str, str]] = [
    ("1.000928", "能源"), ("1.000929", "材料"), ("1.000930", "工业"),
    ("1.000931", "可选消费"), ("1.000932", "主要消费"), ("1.000933", "医药"),
    ("1.000934", "金融"), ("1.000935", "信息技术"), ("1.000936", "通信"),
    ("1.000937", "公用事业"), ("1.000938", "房地产"),
]

FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_QUOTE_FIELDS = "f2,f3,f5,f6,f8,f9,f12,f14,f20,f21,f23,f62,f100,f115"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

#: 进程级熔断表：provider -> 连续失败次数
_HEALTH: dict[str, int] = {}
_DEAD_AFTER = 2


def mark(provider: str, cap: str, ok: bool) -> None:
    """熔断计数按 (provider, capability) 粒度。

    为什么不用 provider 粒度：实测东财 ``push2his`` 被限流时，
    ``push2ex``（涨跌停池）与 ``datacenter-web``（两融）依然可用。
    用整个 provider 做熔断会让一个子域限流误伤全部能力。
    """
    k = f"{provider}:{cap}"
    _HEALTH[k] = 0 if ok else _HEALTH.get(k, 0) + 1


def is_dead(provider: str, cap: str) -> bool:
    return _HEALTH.get(f"{provider}:{cap}", 0) >= _DEAD_AFTER


def reset_health() -> None:
    _HEALTH.clear()


def _num(v: Any) -> float | None:
    if v is None or v in ("-", "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tx_code(secid: str) -> str:
    return ("sh" if secid.startswith("1.") else "sz") + secid.split(".")[1]


# --------------------------------------------------------------------------
class Provider:
    name = "base"
    label = "未配置"

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label,
                "available": self.available(),
                "dead_caps": sorted({k.split(":")[1] for k in _HEALTH
                                     if k.startswith(self.name + ":") and _HEALTH[k] >= _DEAD_AFTER})}

    def available(self) -> bool:
        return True


# --------------------------------------------------------------------------
class EastMoney(Provider):
    """东方财富公开行情接口（主源）。字段最全。"""

    name = "eastmoney"
    label = "东方财富公开行情"
    QUOTE = "https://push2.eastmoney.com/api/qt"
    HIST = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    POOL = "https://push2ex.eastmoney.com"
    DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    def index_quotes(self) -> FetchResult:
        url = qs(f"{self.QUOTE}/ulist.np/get", {
            "fltt": 2, "invt": 2, "secids": ",".join(s for s, _, _ in INDEX_POOL),
            "fields": "f1,f2,f3,f4,f6,f12,f14"})
        r = get_json("em_idx_quotes", url, ttl=180)
        return r

    def index_kline(self, secid: str, lmt: int = 260, klt: int = 101) -> FetchResult:
        url = qs(self.HIST, {
            "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": klt, "fqt": 1,
            "end": "20500101", "lmt": lmt})
        r = get_json(f"em_k_{secid}_{klt}_{lmt}", url, ttl=600)
        return r

    def board_kline(self, secid: str, lmt: int = 120) -> FetchResult:
        url = qs(self.HIST, {
            "secid": secid, "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": 101, "fqt": 1,
            "end": "20500101", "lmt": lmt})
        r = get_json(f"em_bk_{secid}_{lmt}", url, ttl=900)
        return r

    def _market_page(self, page: int, pz: int = 100) -> FetchResult:
        url = qs(f"{self.QUOTE}/clist/get", {
            "pn": page, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": FS_ALL_A, "fields": _QUOTE_FIELDS})
        r = get_json(f"em_mkt_{page}_{pz}", url, ttl=300)
        return r

    def market_snapshot(self, max_pages: int = 60) -> tuple[list[dict], list[FetchResult]]:
        first = self._market_page(1, 100)
        results = [first]
        if not first.ok or not isinstance(first.data, dict) or not first.data.get("data"):
            return [], results
        total = int(first.data["data"].get("total") or 0)
        rows = list(first.data["data"].get("diff") or [])
        pages = min(max_pages, (total + 99) // 100)
        if pages > 1:
            for r in parallel(self._market_page, list(range(2, pages + 1)), max_workers=4):
                if isinstance(r, FetchResult):
                    results.append(r)
                    if r.ok and isinstance(r.data, dict) and r.data.get("data"):
                        rows.extend(r.data["data"].get("diff") or [])
        return rows, results

    def industry_boards(self) -> FetchResult:
        url = qs(f"{self.QUOTE}/clist/get", {
            "pn": 1, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": "m:90+t:2",
            "fields": "f2,f3,f12,f14,f62,f104,f105,f128,f136,f20"})
        r = get_json("em_boards", url, ttl=300)
        return r

    def limit_up_pool(self, date: str) -> FetchResult:
        url = qs(f"{self.POOL}/getTopicZTPool", {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": 0, "pagesize": 300, "sort": "fbt:asc", "date": date})
        r = get_json(f"em_zt_{date}", url, ttl=180, referer="https://quote.eastmoney.com/ztb/")
        return r

    def limit_down_pool(self, date: str) -> FetchResult:
        url = qs(f"{self.POOL}/getTopicDTPool", {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": 0, "pagesize": 300, "sort": "fund:asc", "date": date})
        r = get_json(f"em_dt_{date}", url, ttl=180, referer="https://quote.eastmoney.com/ztb/")
        return r

    def margin(self) -> FetchResult:
        url = qs(self.DC, {
            "reportName": "RPTA_RZRQ_LSHJ", "columns": "ALL", "source": "WEB",
            "client": "WEB", "sortColumns": "DIM_DATE", "sortTypes": -1, "pageSize": 25})
        r = get_json("em_margin", url, ttl=3600, referer="https://data.eastmoney.com/rzrq/")
        return r

    def news(self, count: int = 40) -> FetchResult:
        url = ("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?"
               f"client=web&biz=web_news_col&column=350&order=1&needInteractData=0"
               f"&page_index=1&page_size={count}&req_trace=1")
        r = get_json("em_news", url, ttl=900, referer="https://finance.eastmoney.com/")
        return r


# --------------------------------------------------------------------------
class Sina(Provider):
    """新浪财经行情（备源）。

    全市场分页接口 ``Market_Center.getHQNodeData`` 一次返回 100 条，
    字段含 涨跌幅/成交额/换手率/PE/PB/总市值/流通市值，足以支撑宽度与估值维度。
    缺点是**没有主力资金流与行业归属**，这两项在主源可用时才提供。
    """

    name = "sina"
    label = "新浪财经行情"
    NODE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    HY = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    HQ = "https://hq.sinajs.cn/list="

    def _text(self, key: str, url: str, ttl: float, encoding: str = "utf-8") -> FetchResult:
        from .net import _read_cache, _write_cache

        cached = _read_cache(key, ttl)
        if cached is not None:
            return FetchResult(key, url, True, data=cached["data"], from_cache=True,
                               fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"})
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                raw = resp.read()
            text = raw.decode(encoding, "ignore")
            _write_cache(key, text)
            return FetchResult(key, url, True, data=text,
                               fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"), attempts=1)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(key, url, False, error=f"{type(exc).__name__}: {exc}", attempts=1)

    def _page(self, page: int, num: int = 100) -> FetchResult:
        url = qs(self.NODE, {"page": page, "num": num, "sort": "symbol", "asc": 1,
                             "node": "hs_a", "symbol": "", "_s_r_a": "page"})
        return self._text(f"sina_mkt_{page}_{num}", url, 300)

    def market_snapshot(self, max_pages: int = 60) -> tuple[list[dict], list[FetchResult]]:
        first = self._page(1)
        results = [first]
        if not first.ok:
            return [], results
        try:
            rows = json.loads(first.data) or []
        except Exception:  # noqa: BLE001
            return [], results
        # 总条数：接口不返回 total，用「本页满 100 就继续翻」的保守策略
        page = 2
        while len(rows) and page <= max_pages:
            r = self._page(page)
            results.append(r)
            if not r.ok:
                break
            try:
                batch = json.loads(r.data) or []
            except Exception:  # noqa: BLE001
                break
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        norm = [{
            "code": str(x.get("code") or ""),
            "name": x.get("name"),
            "price": _num(x.get("trade")),
            "pct": _num(x.get("changepercent")),
            "volume": _num(x.get("volume")),
            "amount": _num(x.get("amount")),
            "turnover": _num(x.get("turnoverratio")),
            "pe": _num(x.get("per")),
            "pe_ttm": _num(x.get("per")),
            "pb": _num(x.get("pb")),
            "mcap": (_num(x.get("mktcap")) or 0) * 1e4 or None,
            "float_mcap": (_num(x.get("nmc")) or 0) * 1e4 or None,
            "net_inflow": None,
            "industry": None,
        } for x in rows if x.get("code")]
        return norm, results

    def index_quotes(self) -> FetchResult:
        codes = ",".join(_tx_code(s) for s, _, _ in INDEX_POOL)
        return self._text("sina_idx", self.HQ + codes, 120, encoding="gbk")

    def industry_boards(self) -> FetchResult:
        return self._text("sina_hy", self.HY, 300, encoding="gbk")

    def available(self) -> bool:
        return True


# --------------------------------------------------------------------------
class Tencent(Provider):
    """腾讯行情（K 线备源）。返回日线 [日期, 开, 收, 高, 低, 量]。"""

    name = "tencent"
    label = "腾讯证券行情"
    KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def index_kline(self, secid: str, lmt: int = 260, klt: int = 101) -> FetchResult:
        period = "day" if klt in (101, 102, 103) else "week"
        url = qs(self.KLINE, {"param": f"{_tx_code(secid)},{period},,,{lmt},"})
        r = get_json(f"tx_k_{secid}_{period}_{lmt}", url, ttl=600,
                     referer="https://gu.qq.com/")
        if not r.ok:
            return r
        code = _tx_code(secid)
        node = ((r.data or {}).get("data") or {}).get(code) or {}
        raw = node.get(period) or node.get(f"qfq{period}") or []
        rows = []
        for it in raw:
            if len(it) < 6:
                continue
            rows.append({"date": it[0], "open": _num(it[1]), "close": _num(it[2]),
                         "high": _num(it[3]), "low": _num(it[4]), "volume": _num(it[5]),
                         "amount": None})
        return FetchResult(key=r.key, url=url, ok=True, data={"rows": rows},
                           from_cache=r.from_cache, fetched_at=r.fetched_at, attempts=1)

    def board_kline(self, secid: str, lmt: int = 120) -> FetchResult:
        return self.index_kline(secid, lmt, 101)

    def available(self) -> bool:
        return True


# --------------------------------------------------------------------------
class Fuyao(Provider):
    """扶摇（同花顺 AIClick）适配器 —— 题目指定优先使用的数据工具。

    纪律：未设置 ``FUYAO_API_KEY`` 时 ``available()`` 为 False，**不发起请求、
    不返回任何占位数字**。接口路径按 https://fuyao.aiclick.ai/ 的实现约定编写，
    但本机无可用凭据，**未能端到端验证**（README「已知限制」中如实声明）。
    """

    name = "fuyao"
    label = "同花顺扶摇 AIClick"

    def __init__(self) -> None:
        self.key = os.environ.get("FUYAO_API_KEY", "").strip()
        self.base = os.environ.get("FUYAO_BASE_URL", "https://fuyao.aiclick.ai/api/v1").rstrip("/")

    def available(self) -> bool:
        return bool(self.key)

    def _post(self, path: str, payload: dict) -> FetchResult:
        url = self.base + path
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.key}", "User-Agent": "mingshi/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            return FetchResult(f"fy:{path}", url, True, data=data,
                               fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"), attempts=1)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(f"fy:{path}", url, False,
                               error=f"{type(exc).__name__}: {exc}", attempts=1)

    def index_quotes(self) -> FetchResult:
        return self._post("/market/index/quotes", {"secids": [s for s, _, _ in INDEX_POOL]})

    def index_kline(self, secid: str, lmt: int = 260, klt: int = 101) -> FetchResult:
        return self._post("/market/index/kline", {"secid": secid, "limit": lmt, "period": klt})

    def limit_up_pool(self, date: str) -> FetchResult:
        return self._post("/market/limit/pool", {"date": date, "type": "up"})

    def limit_down_pool(self, date: str) -> FetchResult:
        return self._post("/market/limit/pool", {"date": date, "type": "down"})


# --------------------------------------------------------------------------
def registry() -> dict[str, Provider]:
    return {"eastmoney": EastMoney(), "sina": Sina(), "tencent": Tencent(), "fuyao": Fuyao()}
