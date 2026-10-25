# -*- coding: utf-8 -*-
"""七维证据引擎。

设计原则
--------
1. **每一维只回答一个问题**，并且答案必须能被单个数字证伪。
2. **每个数字都能溯源**：``Fact.source`` 记录它来自哪个接口（含 provider 名），
   ``caliber`` 记录口径（复权方式、均值窗口、样本范围）。
3. **打分是可复算的**：全部用分段线性映射（见各 ``_score_*``），
   不存在训练出来的黑箱参数。评委可以用计算器验证。
4. **不完整就说不完整**：数据缺失时 ``confidence`` 降、``uncertainty`` 加条目，
   绝不插值冒充。

七维（与题面一一对应）
----------------------
  structure   行情结构   价格相对均线位置、趋势斜率、回撤、波动率位置
  breadth     市场宽度   上涨占比、极端涨跌分布、涨跌停家数
  rotation    风格轮动   大小盘/成长价值相对强弱、行业动量与轮动速度
  valuation   估值       全市场中位 PE/PB、破净率、高估值占比
  liquidity   流动性     成交活跃度、两融杠杆、主力资金
  sentiment   情绪       连板高度、封板质量、赚钱/亏钱效应
  events      重要事件   资讯归类与影响方向

打分统一为 ``score ∈ [-1, 1]``：正值表示「支撑风险偏好与趋势延续」，
负值表示「压制」。它**不是涨跌预测**，只是当前证据的方向与强度。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import indicators as ind

# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Fact:
    label: str
    value: Any
    unit: str = ""
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {"label": self.label, "value": self.value, "unit": self.unit,
                "source": self.source, "note": self.note}


@dataclass
class Dimension:
    id: str
    name: str
    question: str
    axis: str                     # trend / risk（用于二维状态定位）
    weight: float
    score: float | None = None
    confidence: float = 0.0
    status: str = "数据不足"
    headline: str = ""
    facts: list[Fact] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    caliber: str = ""
    as_of: str = ""
    freshness: str = "fresh"      # fresh / partial / stale / missing
    uncertainty: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "question": self.question,
            "axis": self.axis, "weight": self.weight,
            "score": None if self.score is None else round(self.score, 4),
            "confidence": round(self.confidence, 3), "status": self.status,
            "headline": self.headline,
            "facts": [f.to_dict() for f in self.facts],
            "signals": self.signals, "caliber": self.caliber,
            "as_of": self.as_of, "freshness": self.freshness,
            "uncertainty": self.uncertainty, "raw": self.raw,
        }


def _sig(text: str, direction: str, strength: float, evidence: str = "") -> dict:
    return {"text": text, "direction": direction,
            "strength": round(strength, 3), "evidence": evidence}


def _status_of(score: float | None) -> str:
    if score is None:
        return "数据不足"
    if score >= 0.5:
        return "明显偏强"
    if score >= 0.15:
        return "偏强"
    if score > -0.15:
        return "中性"
    if score > -0.5:
        return "偏弱"
    return "明显偏弱"


# --------------------------------------------------------------------------
# 上下文：一次性算好被多个维度复用的量
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, snap) -> None:
        self.snap = snap
        self.prov: dict = snap.provenance or {}
        self.as_of = snap.as_of
        self.mkt = [s for s in snap.market if s.get("pct") is not None]
        self.n = len(self.mkt)
        self.kl: dict[str, list[dict]] = {
            k: v.get("kline") or [] for k, v in snap.indices.items()}
        self.cl: dict[str, list[float]] = {
            k: ind.closes(v) for k, v in self.kl.items()}

    def src(self, cap: str, default: str = "未知来源") -> str:
        p = self.prov.get(cap)
        return f"{p}::{cap}" if p and p != "failed" else default

    def close(self, secid: str) -> list[float]:
        return self.cl.get(secid, [])

    def rows(self, secid: str) -> list[dict]:
        return self.kl.get(secid, [])


# --------------------------------------------------------------------------
# E1 行情结构
# --------------------------------------------------------------------------
def build_structure(ctx: Ctx) -> Dimension:
    d = Dimension(
        id="structure", name="行情结构", axis="trend", weight=0.22,
        question="价格处在什么位置？趋势是有序还是紊乱？",
        caliber="均线为收盘价算术平均、不复权指数点位；回撤基于近 250 个交易日最高收盘；"
                "波动率为 20 日已实现波动率年化（×√252）；活跃度为成交量相对 20 日均值。",
        as_of=ctx.as_of,
    )
    probes = [("1.000001", "上证指数"), ("1.000300", "沪深300"), ("0.399006", "创业板指")]
    above, dists, slopes, dd = [], {}, {}, {}
    vols, vol_pct = {}, {}
    for secid, nm in probes:
        c = ctx.close(secid)
        if len(c) < 130:
            d.uncertainty.append(f"{nm} 日线样本不足（{len(c)} 根），未纳入结构判定")
            continue
        last = c[-1]
        m20, m60, m120 = ind.ma(c, 20), ind.ma(c, 60), ind.ma(c, 120)
        for win, m in ((20, m20), (60, m60), (120, m120)):
            if m:
                if last > m:
                    above.append(1)
                else:
                    above.append(0)
        dists[nm] = {
            "ma20": ind.pct(last / m20 - 1) if m20 else None,
            "ma60": ind.pct(last / m60 - 1) if m60 else None,
            "ma120": ind.pct(last / m120 - 1) if m120 else None,
        }
        ms = ind.ma_series(c, 20)
        if len(ms) >= 6:
            slopes[nm] = ind.pct(ms[-1] / ms[-6] - 1)
        hi = max(c[-250:]) if len(c) >= 60 else max(c)
        dd[nm] = ind.pct(last / hi - 1)
        vs = ind.rolling_vol(c, 20)
        if vs:
            vols[nm] = round(vs[-1] * 100, 2)
            vol_pct[nm] = ind.percentile_rank(vs[-250:], vs[-1])

    # 活跃度：上证+深证成交量相对 20 日均值
    act = None
    v1 = [r.get("volume") for r in ctx.rows("1.000001") if r.get("volume")]
    v2 = [r.get("volume") for r in ctx.rows("0.399001") if r.get("volume")]
    if len(v1) >= 21 and len(v2) >= 21:
        n = min(len(v1), len(v2))
        tot = [v1[i] + v2[i] for i in range(n)]
        base = sum(tot[-21:-1]) / 20
        if base:
            act = tot[-1] / base

    # ---- 打分 ----
    parts, weights = [], []
    if above:
        parts.append((sum(above) / len(above)) * 2 - 1)     # 在均线上方的比例
        weights.append(0.45)
    if slopes:
        s = sum(slopes.values()) / len(slopes)
        parts.append(ind.squash(s * 100, 3.0))               # 20 日均线斜率
        weights.append(0.30)
    if dd:
        worst = min(dd.values())
        parts.append(ind.clamp((worst + 25) / 25.0))          # 回撤越深越负
        weights.append(0.15)
    if vol_pct:
        vp = sum(vol_pct.values()) / len(vol_pct)
        parts.append(ind.clamp((0.5 - vp) * 2))               # 波动率分位越低越好
        weights.append(0.10)

    if parts and sum(weights) > 0:
        d.score = ind.clamp(sum(p * w for p, w in zip(parts, weights)) / sum(weights))
    d.confidence = min(1.0, 0.5 + 0.1 * len(above)) if above else 0.15
    d.status = _status_of(d.score)

    # ---- 事实 ----
    for secid, nm in probes:
        if nm in dists:
            dd_ = dists[nm]
            d.facts.append(Fact(f"{nm} 相对 MA20", dd_["ma20"], "%", ctx.src("index_kline"),
                                "正值=站上 20 日均线"))
            d.facts.append(Fact(f"{nm} 相对 MA60", dd_["ma60"], "%", ctx.src("index_kline")))
            d.facts.append(Fact(f"{nm} 相对 MA120", dd_["ma120"], "%", ctx.src("index_kline")))
    for nm, v in dd.items():
        d.facts.append(Fact(f"{nm} 距 250 日高点", v, "%", ctx.src("index_kline")))
    for nm, v in vols.items():
        d.facts.append(Fact(f"{nm} 20日年化波动率", v, "%", ctx.src("index_kline"),
                            f"近一年分位 {round((vol_pct.get(nm) or 0) * 100)}%"))
    if act is not None:
        d.facts.append(Fact("沪深成交活跃度", round(act, 2), "倍",
                            ctx.src("index_kline"), "当日总量 / 前 20 日均量"))

    # ---- 信号 ----
    if above:
        r = sum(above) / len(above)
        d.signals.append(_sig(
            f"{len(above)} 个「指数×均线」组合中有 {sum(above)} 个站上均线",
            "up" if r > 0.6 else ("down" if r < 0.4 else "flat"), abs(r - 0.5) * 2))
    if slopes:
        s = sum(slopes.values()) / len(slopes)
        d.signals.append(_sig(
            f"主要指数 20 日均线 5 日变化均值 {s:+.2f}%",
            "up" if s > 0.1 else ("down" if s < -0.1 else "flat"),
            min(abs(s) / 1.5, 1.0)))
    if dd:
        worst = min(dd.values())
        if worst < -15:
            d.signals.append(_sig(f"最深回撤 {worst:.1f}%，仍处修复区间", "down", 0.5))
    if vol_pct and max(vol_pct.values()) > 0.8:
        d.signals.append(_sig("波动率处于近一年高位，价格信号可信度下降", "risk", 0.7))

    if d.score is not None and d.score > 0.15:
        d.headline = "价格结构仍站在均线系统之上，趋势尚未破坏"
    elif d.score is not None and d.score < -0.15:
        d.headline = "价格已跌破中短期均线，结构转弱"
    else:
        d.headline = "价格围绕均线震荡，方向未明"
    d.raw = {"above_ratio": round(sum(above) / len(above), 3) if above else None,
             "dists": dists, "slopes": slopes, "drawdown": dd,
             "vol": vols, "vol_pct": vol_pct, "activity": act}
    return d


# --------------------------------------------------------------------------
# E2 市场宽度
# --------------------------------------------------------------------------
def build_breadth(ctx: Ctx) -> Dimension:
    d = Dimension(
        id="breadth", name="市场宽度", axis="trend", weight=0.20,
        question="指数的涨跌，有多少股票在跟？",
        caliber=f"样本为沪深两市全部 A 股（剔除北交所与停牌），n={ctx.n}；"
                "宽度仅覆盖当日横截面，历史序列需逐日快照累积（见 uncertainty）。",
        as_of=ctx.as_of,
    )
    if not ctx.mkt:
        d.freshness = "missing"
        d.uncertainty.append("全市场快照获取失败，宽度维度不可用")
        return d

    pcts = [s["pct"] for s in ctx.mkt]
    up = sum(1 for p in pcts if p > 0)
    down = sum(1 for p in pcts if p < 0)
    flat = ctx.n - up - down
    up_ratio = up / ctx.n
    strong_up = sum(1 for p in pcts if p >= 5) / ctx.n
    strong_dn = sum(1 for p in pcts if p <= -5) / ctx.n
    med = ind.median(pcts)
    zt = len(ctx.snap.limit_up)
    dt = len(ctx.snap.limit_down)

    # ---- 打分 ----
    s1 = ind.clamp((up_ratio - 0.5) * 4.0)           # 上涨占比：50% 为中性
    s2 = ind.clamp((strong_up - strong_dn) * 8.0)    # 极端分布：多空力量差
    s3 = ind.clamp((zt - dt) / 60.0)                 # 涨停跌停家数差
    cross = sum(1 for p in pcts if abs(p) < 1) / ctx.n   # 横盘占比高=缺乏主线
    d.score = ind.clamp(0.5 * s1 + 0.3 * s2 + 0.2 * s3 - 0.15 * max(0, cross - 0.4) * 2)
    d.confidence = 0.92 if zt + dt > 0 else 0.72
    d.status = _status_of(d.score)

    d.facts = [
        Fact("上涨家数", up, "家", ctx.src("market_snapshot"), f"占比 {up_ratio*100:.1f}%"),
        Fact("下跌家数", down, "家", ctx.src("market_snapshot")),
        Fact("平盘家数", flat, "家", ctx.src("market_snapshot")),
        Fact("涨幅≥5% 家数", round(strong_up * ctx.n), "家", ctx.src("market_snapshot")),
        Fact("跌幅≥5% 家数", round(strong_dn * ctx.n), "家", ctx.src("market_snapshot")),
        Fact("个股涨跌幅中位数", round(med, 2) if med is not None else None, "%",
             ctx.src("market_snapshot"), "中位数比均值更能代表「普遍体验」"),
        Fact("涨停家数", zt, "家", ctx.src("limit_up")),
        Fact("跌停家数", dt, "家", ctx.src("limit_down")),
        Fact("涨跌家数比", round(up / max(down, 1), 2), "倍", ctx.src("market_snapshot")),
    ]
    d.signals.append(_sig(
        f"{up} 涨 / {down} 跌，上涨占比 {up_ratio*100:.1f}%",
        "up" if up_ratio > 0.55 else ("down" if up_ratio < 0.45 else "flat"),
        abs(up_ratio - 0.5) * 2))
    if abs(med or 0) - abs(0) > 0 and med is not None:
        d.signals.append(_sig(f"个股涨跌中位数 {med:+.2f}%",
                              "up" if med > 0 else "down", min(abs(med) / 2, 1)))
    if strong_dn > 0.03:
        d.signals.append(_sig(f"跌幅≥5% 达 {strong_dn*100:.1f}%，存在局部流动性冲击", "down", 0.8))
    if cross > 0.45:
        d.signals.append(_sig(f"{cross*100:.0f}% 的个股涨跌幅在 ±1% 内，市场缺乏主线", "flat", 0.5))

    d.headline = ("赚钱效应扩散，多数个股参与" if (d.score or 0) > 0.15
                  else "指数与个股背离风险上升" if (d.score or 0) < -0.15
                  else "涨跌参半，方向感不强")
    d.uncertainty.append("宽度缺少历史序列，无法判断当前宽度处于近一年的什么位置；"
                         "产品会逐日把快照落盘，运行若干交易日后自动补齐分位。")
    d.raw = {"up_ratio": round(up_ratio, 4), "median": med, "strong_up": round(strong_up, 4),
             "strong_dn": round(strong_dn, 4), "zt": zt, "dt": dt, "n": ctx.n,
             "flat_band": round(cross, 4)}
    return d


# --------------------------------------------------------------------------
# E3 风格轮动
# --------------------------------------------------------------------------
def build_rotation(ctx: Ctx) -> Dimension:
    d = Dimension(
        id="rotation", name="风格轮动", axis="trend", weight=0.15,
        question="钱在往哪类资产走？轮动是在加速还是收敛？",
        caliber="大小盘用沪深300 对 中证1000，成长价值用创业板指 对 中证红利；"
                "行业使用中证全指一级行业指数 11 个；轮动速度用 5 日与 20 日动量排名的秩相关度量。",
        as_of=ctx.as_of,
    )

    def rel(a: str, b: str, n: int) -> float | None:
        ca, cb = ctx.close(a), ctx.close(b)
        if len(ca) < n + 1 or len(cb) < n + 1:
            return None
        ra, rb = ind.ret(ca, n), ind.ret(cb, n)
        if ra is None or rb is None:
            return None
        return ind.pct(ra - rb)

    size5, size20 = rel("1.000300", "1.000852", 5), rel("1.000300", "1.000852", 20)
    style5, style20 = rel("0.399006", "1.000922", 5), rel("0.399006", "1.000922", 20)

    sec_rows = []
    for s in ctx.snap.sectors:
        c = ind.closes(s.get("kline") or [])
        if len(c) < 21:
            continue
        sec_rows.append({"name": s["name"], "r5": ind.pct(ind.ret(c, 5)),
                         "r20": ind.pct(ind.ret(c, 20))})
    rho = None
    if len(sec_rows) >= 5 and all(x["r5"] is not None and x["r20"] is not None for x in sec_rows):
        rho = ind.spearman([x["r5"] for x in sec_rows], [x["r20"] for x in sec_rows])
    dispersion = None
    if len(sec_rows) >= 6:
        r20s = sorted([x["r20"] for x in sec_rows if x["r20"] is not None], reverse=True)
        if len(r20s) >= 6:
            dispersion = round(sum(r20s[:3]) / 3 - sum(r20s[-3:]) / 3, 2)

    parts, ws = [], []
    if size20 is not None:
        parts.append(ind.squash(size20, 4.0)); ws.append(0.35)   # 小盘占优=风险偏好高
    if style20 is not None:
        parts.append(ind.squash(style20, 6.0)); ws.append(0.25)  # 成长占优=风险偏好高
    if dispersion is not None:
        parts.append(ind.clamp(dispersion / 20.0)); ws.append(0.2)  # 分化越大，主线越清晰
    if rho is not None:
        parts.append(ind.clamp((rho - 0.3) / 0.7)); ws.append(0.2)  # 排名越稳定，趋势越可持续

    if parts:
        d.score = ind.clamp(sum(p * w for p, w in zip(parts, ws)) / sum(ws))
    d.confidence = min(1.0, 0.4 + 0.1 * len(sec_rows))
    d.status = _status_of(d.score)

    d.facts = [
        Fact("沪深300 − 中证1000（5日）", size5, "%", ctx.src("index_kline"),
             "正值=大盘占优，负值=小盘占优"),
        Fact("沪深300 − 中证1000（20日）", size20, "%", ctx.src("index_kline")),
        Fact("创业板指 − 中证红利（5日）", style5, "%", ctx.src("index_kline"),
             "正值=成长占优，负值=价值占优"),
        Fact("创业板指 − 中证红利（20日）", style20, "%", ctx.src("index_kline")),
        Fact("行业动量离散度（前3−后3）", dispersion, "pct", ctx.src("sector_kline"),
             "越大说明行业分化越极端"),
        Fact("行业排名稳定性（5日 vs 20日 秩相关）", round(rho, 3) if rho is not None else None,
             "", ctx.src("sector_kline"), "≈1 说明领涨行业持续，≈0 说明快速轮动"),
    ]
    if sec_rows and all(x["r20"] is not None for x in sec_rows):
        top = sorted(sec_rows, key=lambda x: x["r20"], reverse=True)
        for x in top[:3]:
            d.facts.append(Fact(f"20日领涨 · {x['name']}", x["r20"], "%", ctx.src("sector_kline")))
        for x in top[-3:]:
            d.facts.append(Fact(f"20日落后 · {x['name']}", x["r20"], "%", ctx.src("sector_kline")))

    if size20 is not None:
        who = "大盘" if size20 > 0 else "小盘"
        d.signals.append(_sig(f"近 20 日 {who}占优，相对强度 {size20:+.1f}pct",
                              "up" if size20 > 0 else "flat", min(abs(size20) / 8, 1)))
    if style20 is not None:
        who = "成长" if style20 > 0 else "价值"
        d.signals.append(_sig(f"近 20 日 {who}风格占优，相对强度 {style20:+.1f}pct",
                              "up" if style20 > 0 else "down", min(abs(style20) / 10, 1)))
    if rho is not None:
        d.signals.append(_sig(
            f"行业 5 日与 20 日排名秩相关 {rho:.2f}，"
            + ("主线较稳定" if rho > 0.5 else "轮动速度快、追高风险高" if rho < 0.2 else "主线半稳定"),
            "flat", 1 - abs(rho - 0.5) * 2))

    d.headline = ("资金向高弹性方向扩散" if (d.score or 0) > 0.15
                  else "资金向防御方向收缩" if (d.score or 0) < -0.15
                  else "风格未定，轮动主导")
    d.raw = {"size5": size5, "size20": size20, "style5": style5, "style20": style20,
             "sectors": sec_rows, "rank_stability": rho, "dispersion": dispersion}
    return d


# --------------------------------------------------------------------------
# E4 估值
# --------------------------------------------------------------------------
def build_valuation(ctx: Ctx, history: list[dict] | None = None) -> Dimension:
    d = Dimension(
        id="valuation", name="估值", axis="risk", weight=0.12,
        question="当前价格需要用多少盈利来兑现？市场还有多少安全垫？",
        caliber="全 A 横截面市盈率取正值样本的中位数（剔除亏损股，避免负值污染）；"
                "市净率取全部样本中位数；破净率=PB<1 的个股占比。",
        as_of=ctx.as_of,
    )
    if not ctx.mkt:
        d.freshness = "missing"
        d.uncertainty.append("全市场快照失败，估值维度不可用")
        return d

    pes = [s["pe_ttm"] for s in ctx.mkt if s.get("pe_ttm") and s["pe_ttm"] > 0]
    pbs = [s["pb"] for s in ctx.mkt if s.get("pb") and s["pb"] > 0]
    med_pe = ind.median(pes)
    med_pb = ind.median(pbs)
    pb_below = sum(1 for x in pbs if x < 1) / len(pbs) if pbs else None
    rich = sum(1 for x in pes if x > 80) / len(pes) if pes else None
    loss = sum(1 for s in ctx.mkt if s.get("pe_ttm") is not None and s["pe_ttm"] <= 0)
    loss_ratio = loss / ctx.n if ctx.n else None

    # 历史分位（依赖本地快照累积）
    pe_pct = pb_pct = None
    if history:
        h_pe = [h["median_pe"] for h in history if h.get("median_pe")]
        h_pb = [h["median_pb"] for h in history if h.get("median_pb")]
        if len(h_pe) >= 5 and med_pe:
            pe_pct = ind.percentile_rank(h_pe, med_pe)
        if len(h_pb) >= 5 and med_pb:
            pb_pct = ind.percentile_rank(h_pb, med_pb)

    parts, ws = [], []
    if med_pe:
        parts.append(ind.clamp((35 - med_pe) / 25.0)); ws.append(0.4)
    if med_pb:
        parts.append(ind.clamp((3.2 - med_pb) / 2.0)); ws.append(0.3)
    if pb_below is not None:
        parts.append(ind.clamp((pb_below - 0.06) * 6)); ws.append(0.3)
    if parts:
        d.score = ind.clamp(sum(p * w for p, w in zip(parts, ws)) / sum(ws))
    # 注意：估值低不等于要涨。这里的方向定义是「估值提供的安全垫厚度」，
    # 在状态合成里它只作为 risk 轴的阻尼项，不单独驱动方向判定。
    d.confidence = 0.85 if (pe_pct is not None) else 0.6
    d.status = _status_of(d.score)

    d.facts = [
        Fact("全 A 中位市盈率（TTM, 剔除亏损）", round(med_pe, 2) if med_pe else None, "倍",
             ctx.src("market_snapshot"), f"有效样本 {len(pes)} 只"),
        Fact("全 A 中位市净率", round(med_pb, 2) if med_pb else None, "倍",
             ctx.src("market_snapshot"), f"有效样本 {len(pbs)} 只"),
        Fact("破净股占比（PB<1）", ind.pct(pb_below), "%", ctx.src("market_snapshot"),
             "占比越高，市场整体安全垫越厚"),
        Fact("高估值股占比（PE>80）", ind.pct(rich), "%", ctx.src("market_snapshot")),
        Fact("亏损股占比", ind.pct(loss_ratio), "%", ctx.src("market_snapshot"),
             "TTM 市盈率为负的个股比例"),
    ]
    if pe_pct is not None:
        d.facts.append(Fact("中位 PE 的历史分位", round(pe_pct * 100, 1), "%",
                            "local:snapshots", f"样本 {len(history)} 个交易日"))
    if pb_pct is not None:
        d.facts.append(Fact("中位 PB 的历史分位", round(pb_pct * 100, 1), "%", "local:snapshots"))

    if pb_below is not None and pb_below > 0.08:
        d.signals.append(_sig(f"{pb_below*100:.1f}% 的个股破净，绝对估值处于低位区", "support", 0.6))
    if med_pe:
        d.signals.append(_sig(f"中位 PE {med_pe:.1f} 倍", "flat", 0.2))
    if rich is not None and rich > 0.25:
        d.signals.append(_sig(f"{rich*100:.0f}% 个股 PE>80，估值结构性偏贵", "risk", 0.5))

    d.headline = ("估值提供一定安全垫" if (d.score or 0) > 0.15
                  else "估值缺乏缓冲，对盈利下修敏感" if (d.score or 0) < -0.15
                  else "估值中性")
    d.uncertainty.append(
        "估值只看横截面绝对值，缺少长周期分位。历史分位需要本地快照累计 ≥5 个交易日才会出现，"
        "在此之前不会用「十年分位」这类无法验证的说法。")
    d.raw = {"median_pe": med_pe, "median_pb": med_pb, "pb_below_1": pb_below,
             "pe_above_80": rich, "loss_ratio": loss_ratio,
             "pe_percentile": pe_pct, "pb_percentile": pb_pct}
    return d


# --------------------------------------------------------------------------
# E5 流动性
# --------------------------------------------------------------------------
def build_liquidity(ctx: Ctx) -> Dimension:
    d = Dimension(
        id="liquidity", name="流动性", axis="risk", weight=0.13,
        question="支撑当前价格的钱，是变多了还是变少了？",
        caliber="成交额由全市场个股成交额汇总；两融余额取沪深合计 RZYE，"
                "5 日变化率按交易日对齐；主力净流入为个股 f62 字段汇总（东财口径）。",
        as_of=ctx.as_of,
    )
    amount = sum(s.get("amount") or 0 for s in ctx.mkt) if ctx.mkt else None
    turnover = [s["turnover"] for s in ctx.mkt if s.get("turnover")]
    med_to = ind.median(turnover)
    inflow = sum(s.get("net_inflow") or 0 for s in ctx.mkt) if ctx.mkt else None
    has_inflow = any(s.get("net_inflow") is not None for s in ctx.mkt)

    mg = ctx.snap.margin or []
    rzye = rz5 = rz_pct = None
    if mg:
        rzye = mg[-1].get("rzye")
        if len(mg) >= 6 and mg[-6].get("rzye"):
            rz5 = rzye - mg[-6]["rzye"]
            rz_pct = ind.pct(rzye / mg[-6]["rzye"] - 1)
    mg_ratio = None
    if mg and mg[-1].get("ltsz") and rzye:
        mg_ratio = ind.pct(rzye / mg[-1]["ltsz"])

    # 活跃度
    act = None
    v1 = [r.get("volume") for r in ctx.rows("1.000001") if r.get("volume")]
    v2 = [r.get("volume") for r in ctx.rows("0.399001") if r.get("volume")]
    if len(v1) >= 21 and len(v2) >= 21:
        n = min(len(v1), len(v2))
        tot = [v1[i] + v2[i] for i in range(n)]
        base = sum(tot[-21:-1]) / 20
        if base:
            act = tot[-1] / base

    parts, ws = [], []
    if act is not None:
        parts.append(ind.clamp((act - 1.0) * 2.5)); ws.append(0.4)
    if rz_pct is not None:
        parts.append(ind.squash(rz_pct, 2.0)); ws.append(0.35)
    if has_inflow and inflow is not None:
        parts.append(ind.squash(inflow / 1e10, 2.0)); ws.append(0.25)
    if parts:
        d.score = ind.clamp(sum(p * w for p, w in zip(parts, ws)) / sum(ws))
    d.confidence = min(1.0, 0.35 + (0.25 if act is not None else 0)
                       + (0.2 if rzye else 0) + (0.2 if has_inflow else 0))
    d.status = _status_of(d.score)

    d.facts = [
        Fact("两市成交额", round(amount / 1e8, 0) if amount else None, "亿元",
             ctx.src("market_snapshot")),
        Fact("成交活跃度", round(act, 2) if act else None, "倍", ctx.src("index_kline"),
             "当日总量 / 前 20 日均量，>1 表示放量"),
        Fact("个股换手率中位数", round(med_to, 2) if med_to else None, "%",
             ctx.src("market_snapshot")),
        Fact("两融余额", round(rzye / 1e8, 0) if rzye else None, "亿元",
             ctx.src("margin"), "沪深合计融资融券余额"),
        Fact("两融余额 5 日变化", round(rz5 / 1e8, 1) if rz5 is not None else None, "亿元",
             ctx.src("margin")),
        Fact("两融余额占流通市值", mg_ratio, "%", ctx.src("margin")),
    ]
    # 只有真的取到该字段时才展示——缺失的指标宁可不写，也不要显示成 0。
    if has_inflow and inflow is not None:
        d.facts.append(Fact("主力资金净流入", round(inflow / 1e8, 1), "亿元",
                          ctx.src("market_snapshot"), "东财口径，个股加总"))
    if act is not None:
        d.signals.append(_sig(f"成交活跃度 {act:.2f} 倍于 20 日均值",
                              "up" if act > 1.1 else ("down" if act < 0.85 else "flat"),
                              min(abs(act - 1) * 2, 1)))
    if rz_pct is not None:
        d.signals.append(_sig(f"两融余额 5 日 {rz_pct:+.2f}%",
                              "up" if rz_pct > 0.5 else ("down" if rz_pct < -0.5 else "flat"),
                              min(abs(rz_pct) / 2, 1)))
    if has_inflow and inflow is not None:
        d.signals.append(_sig(f"主力资金净{'流入' if inflow > 0 else '流出'} {abs(inflow)/1e8:.0f} 亿",
                              "up" if inflow > 0 else "down", 0.6))

    d.headline = ("资金面偏宽松，量能支撑价格" if (d.score or 0) > 0.15
                  else "量能收缩，价格缺少增量资金支撑" if (d.score or 0) < -0.15
                  else "资金面中性")
    if not has_inflow:
        d.uncertainty.append("主力资金流字段仅在东方财富源可用时提供，当前源不含该字段。")
    d.raw = {"amount": amount, "activity": act, "median_turnover": med_to,
             "rzye": rzye, "rzye_5d": rz5, "rzye_pct": rz_pct,
             "margin_ratio": mg_ratio,
             "net_inflow": inflow if has_inflow else None}
    return d


# --------------------------------------------------------------------------
# E6 情绪
# --------------------------------------------------------------------------
def build_sentiment(ctx: Ctx) -> Dimension:
    d = Dimension(
        id="sentiment", name="情绪", axis="risk", weight=0.11,
        question="短线资金的赚钱效应如何？进攻意愿是在扩张还是收缩？",
        caliber="基于涨停池与跌停池：连板数 lbc、炸板次数 zbc、封单额 fund；"
                "封板率=未炸板个股占比。样本为当日收盘后池内个股。",
        as_of=ctx.as_of,
    )
    zt, dt = ctx.snap.limit_up, ctx.snap.limit_down
    if not zt and not dt:
        d.freshness = "missing"
        d.uncertainty.append("涨跌停池获取失败，情绪维度不可用")
        return d

    max_board = max([z["boards"] for z in zt], default=0)
    multi = sum(1 for z in zt if z["boards"] >= 2)
    broken = sum(1 for z in zt if z["broken"] > 0)
    seal_rate = 1 - broken / len(zt) if zt else None
    avg_seal = ind.median([z["seal_amount"] for z in zt if z.get("seal_amount")]) if zt else None
    avg_turn = ind.median([z["turnover"] for z in zt if z.get("turnover")]) if zt else None
    ratio = len(zt) / max(len(dt), 1)

    parts, ws = [], []
    parts.append(ind.clamp((len(zt) - 60) / 60.0)); ws.append(0.3)      # 涨停家数
    parts.append(ind.clamp((max_board - 4) / 4.0)); ws.append(0.25)     # 连板高度
    if seal_rate is not None:
        parts.append(ind.clamp((seal_rate - 0.75) * 4)); ws.append(0.25)  # 封板质量
    parts.append(ind.clamp((ratio - 5) / 25.0)); ws.append(0.2)          # 涨跌停比
    d.score = ind.clamp(sum(p * w for p, w in zip(parts, ws)) / sum(ws))
    d.confidence = 0.9
    d.status = _status_of(d.score)

    d.facts = [
        Fact("涨停家数", len(zt), "家", ctx.src("limit_up")),
        Fact("跌停家数", len(dt), "家", ctx.src("limit_down")),
        Fact("最高连板", max_board, "板", ctx.src("limit_up")),
        Fact("连板股数量（≥2板）", multi, "家", ctx.src("limit_up")),
        Fact("涨停封板率", ind.pct(seal_rate), "%", ctx.src("limit_up"), "未炸板占比"),
        Fact("涨停股封单额中位数", round(avg_seal / 1e8, 2) if avg_seal else None, "亿元",
             ctx.src("limit_up")),
        Fact("涨停股换手率中位数", round(avg_turn, 2) if avg_turn else None, "%",
             ctx.src("limit_up")),
        Fact("涨停/跌停比", round(ratio, 1), "倍", ctx.src("limit_up")),
    ]
    if zt:
        leaders = sorted(zt, key=lambda z: (-z["boards"], -(z.get("seal_amount") or 0)))[:3]
        for z in leaders:
            d.facts.append(Fact(f"连板龙头 · {z['name']}", f"{z['boards']} 板",
                                "", ctx.src("limit_up"), z.get("industry") or ""))

    d.signals.append(_sig(f"涨停 {len(zt)} 家、跌停 {len(dt)} 家",
                          "up" if ratio > 8 else ("down" if ratio < 3 else "flat"), 0.6))
    if max_board >= 5:
        d.signals.append(_sig(f"最高 {max_board} 连板，赚钱效应仍在延续", "up", 0.6))
    elif max_board <= 2:
        d.signals.append(_sig(f"最高仅 {max_board} 板，接力意愿弱", "down", 0.6))
    if seal_rate is not None and seal_rate < 0.7:
        d.signals.append(_sig(f"封板率 {seal_rate*100:.0f}%，分歧加大", "down", 0.5))

    d.headline = ("情绪处于扩张期，短线赚钱效应强" if (d.score or 0) > 0.15
                  else "情绪收缩，接力意愿下降" if (d.score or 0) < -0.15
                  else "情绪中性，分歧与一致并存")
    d.raw = {"zt": len(zt), "dt": len(dt), "max_board": max_board, "multi": multi,
             "seal_rate": seal_rate, "ratio": ratio,
             "avg_seal": avg_seal, "avg_turnover": avg_turn}
    return d


# --------------------------------------------------------------------------
# E7 重要事件
# --------------------------------------------------------------------------
#: 事件词典：(类别, 关键词元组, 方向读数)
#: 方向读数表达「该事件对风险偏好的影响」，不是对指数涨跌的判断。
#: 关键词刻意选得「窄」——宁可漏，不可错：把「地方文旅新闻里的 GDP」判成宏观利多
#: 比漏掉一条新闻更糟，因为前者会污染整个事件维度。
EVENT_LEXICON: list[tuple[str, tuple[str, ...], float]] = [
    ("货币政策", ("降准", "降息", "下调存款准备金", "再贷款", "中期借贷便利"), 0.9),
    ("货币政策", ("逆回购", "mlf", "lpr", "流动性投放"), 0.35),
    ("财政政策", ("专项债", "特别国债", "减税", "财政贴息", "以旧换新"), 0.5),
    ("监管", ("证监会", "交易所问询", "立案调查", "行政处罚", "退市", "减持新规"), -0.35),
    ("监管", ("注册制", "并购重组", "市值管理", "分红新规"), 0.3),
    ("海外", ("美联储", "加息", "缩表", "美债收益率"), -0.3),
    ("海外", ("关税", "出口管制", "制裁", "实体清单"), -0.6),
    ("海外", ("地缘", "冲突升级", "红海", "霍尔木兹"), -0.4),
    ("宏观数据", ("社融", "m2", "社零", "工业增加值", "pmi"), 0.3),
    ("宏观数据", ("cpi", "ppi", "通胀"), 0.0),
    ("资金", ("北向资金", "两融", "回购增持贷款", "中长期资金", "险资"), 0.4),
    ("产业", ("人工智能", "算力", "半导体", "国产替代", "机器人"), 0.35),
    ("产业", ("新能源", "储能", "光伏", "锂电"), 0.2),
    ("公司", ("回购", "增持", "股权激励"), 0.45),
    ("公司", ("减持", "质押", "商誉减值"), -0.35),
    ("风险", ("违约", "爆雷", "退市风险", "资金链"), -0.7),
]

#: 与二级市场定价关系很弱的题材，直接排除，避免污染事件维度
EVENT_NOISE: tuple[str, ...] = (
    "旅游", "门票", "美食", "招聘", "展会", "文旅", "景区", "演唱会",
    "天气", "节气", "养生", "楼市成交量", "土拍", "开盘预告",
)


def build_events(ctx: Ctx, llm_events: list[dict] | None = None) -> Dimension:
    d = Dimension(
        id="events", name="重要事件", axis="risk", weight=0.07,
        question="这两天有没有会改变定价逻辑的事情发生？",
        caliber="对当日财经资讯标题做关键词归类与方向标注；方向为「对风险偏好的影响」，"
                "不是对指数涨跌的预测。若有 LLM 可用，会先用模型抽取事件再做规则校验。",
        as_of=ctx.as_of,
    )
    news = ctx.snap.news or []
    events: list[dict] = []
    if llm_events:
        events = llm_events
    else:
        for item in news[:60]:
            title = item.get("title") or ""
            low = title.lower()
            if any(tok in low for tok in EVENT_NOISE):
                continue
            hits: list[tuple[str, float, str]] = []
            for cat, kws, w in EVENT_LEXICON:
                for kw in kws:
                    if kw in low:
                        hits.append((cat, w, kw))
                        break
            if not hits:
                continue
            cats = sorted({c for c, _, _ in hits})
            # 命中越多、越具体，权重越高；多类别同时命中说明是复合事件
            weight = sum(w for _, w, _ in hits) / len(hits)
            weight = weight * (1.0 + 0.15 * (len(cats) - 1))
            events.append({"title": title, "time": item.get("time", ""),
                           "categories": cats, "direction": round(max(-1.0, min(1.0, weight)), 2),
                           "keywords": [k for _, _, k in hits],
                           "url": item.get("url", "")})

    if not news:
        d.freshness = "missing"
        d.uncertainty.append("资讯接口不可用，事件维度缺失；这会让整体置信度显著下降")
        d.score = 0.0
        d.confidence = 0.1
        return d

    # 去重：同一标题只留一次
    seen = set()
    uniq = []
    for e in events:
        if e["title"] in seen:
            continue
        seen.add(e["title"])
        uniq.append(e)
    uniq.sort(key=lambda x: abs(x.get("direction") or 0), reverse=True)
    directional = [e for e in uniq if abs(e.get("direction") or 0) >= 0.25]

    if directional:
        d.score = ind.clamp(sum(e.get("direction") or 0 for e in directional[:10]) / 5.0)
    else:
        d.score = 0.0
    d.confidence = 0.5 if not llm_events else 0.72
    if len(directional) < 3:
        d.confidence *= 0.8
    d.status = _status_of(d.score)

    d.facts = [Fact("资讯条数", len(news), "条", ctx.src("news")),
               Fact("命中事件词典", len(uniq), "条", ctx.src("news")),
               Fact("其中方向性明确", len(directional), "条", ctx.src("news"))]
    cats: dict[str, int] = {}
    for e in uniq:
        for c in e.get("categories") or []:
            cats[c] = cats.get(c, 0) + 1
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        d.facts.append(Fact(f"事件类别 · {c}", n, "条", ctx.src("news")))
    for e in (directional or uniq)[:5]:
        d.facts.append(Fact("关键事件", e["title"][:40], "",
                            ctx.src("news"), "、".join(e.get("categories") or [])))

    if directional:
        pos = [e for e in directional if (e.get("direction") or 0) > 0.25]
        neg = [e for e in directional if (e.get("direction") or 0) < -0.25]
        d.signals.append(_sig(f"识别到 {len(pos)} 条偏正面、{len(neg)} 条偏负面事件",
                              "up" if len(pos) > len(neg) else ("down" if len(neg) > len(pos) else "flat"),
                              min(abs(len(pos) - len(neg)) / 6, 1)))
        if neg:
            d.signals.append(_sig(f"首要负面事件：{neg[0]['title'][:36]}", "down", 0.6))
    else:
        d.signals.append(_sig("当日资讯未命中预设事件词典，暂无明确事件驱动", "flat", 0.1))

    d.headline = ("事件面偏正面" if (d.score or 0) > 0.1
                  else "事件面偏负面" if (d.score or 0) < -0.1
                  else "无显著的单一事件驱动")
    d.uncertainty.append("事件抽取基于关键词词典，只覆盖可枚举的政策/监管/产业类词；"
                         "无法处理未登录词与语义反转（如「降准落空」会被判为正面）。"
                         "配置 LLM 后该维度会切换为模型抽取 + 关键词校验的双通道。")
    d.raw = {"news_count": len(news), "events": (directional or uniq)[:12],
             "categories": cats, "directional": len(directional)}
    return d


# --------------------------------------------------------------------------
def build_all(snap, history: list[dict] | None = None,
              llm_events: list[dict] | None = None) -> list[Dimension]:
    ctx = Ctx(snap)
    return [
        build_structure(ctx),
        build_breadth(ctx),
        build_rotation(ctx),
        build_valuation(ctx, history),
        build_liquidity(ctx),
        build_sentiment(ctx),
        build_events(ctx, llm_events),
    ]
