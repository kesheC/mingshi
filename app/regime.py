# -*- coding: utf-8 -*-
"""状态机：把七维证据映射为「市场状态 / 主要矛盾 / 证伪条件」。

这个模块是产品的判断核心，也是我认为最需要被质疑的地方，所以把全部规则写死、
写具体、写成可复算的形式，而不是让语言模型"感觉"出一个结论。

三个输出
--------
1. **状态（regime）**：把七维压到两条正交轴上——
   `trend`（趋势一致性：结构/宽度/轮动）与 `risk`（风险偏好：估值/流动性/情绪/事件），
   再落到四个象限 + 两个特殊态。这样「指数强但个股弱」这类背离天然可表达。
2. **主要矛盾**：寻找方向相反且权重大的证据对，解释"为什么这个状态不稳"。
3. **证伪条件**：给出可监控的阈值清单，回答"什么情况下我该改主意"。

关于置信度
----------
置信度不是一个装饰数字，它由三块可分解的因子相乘后再加权：
  coverage   有多少维真正拿到了数据（缺数据必须掉分）
  consistency 七维彼此有多一致（互相打架说明状态不稳）
  freshness  数据有多新
三者都会在输出里单独给出，便于用户判断「是这个结论弱，还是这个数据弱」。
"""

from __future__ import annotations

import math
from typing import Any

from . import indicators as ind
from .evidence import Ctx, Dimension

# --------------------------------------------------------------------------
# 状态定义
# --------------------------------------------------------------------------
STATES: dict[str, dict] = {
    "resonance_up": {
        "name": "共振偏强",
        "quadrant": "右上 · 趋势与风险偏好同向",
        "meaning": "价格结构、市场宽度与资金情绪指向同一方向，市场处在自我强化的阶段。",
        "watch": "共振状态下最需要警惕的不是回调，而是「内部先坏」：宽度先掉、量能先缩，"
                 "但指数因为权重股托举还在涨。",
        "risk": "拥挤交易。当某一维开始反向而指数仍在创新高时，往往是共振末端。",
    },
    "structural": {
        "name": "结构性偏强",
        "quadrant": "右下 · 趋势在，风险偏好不在",
        "meaning": "指数或部分强势方向仍在向上，但情绪与流动性已不再配合，"
                   "上涨的载体在收窄。",
        "watch": "观察宽度是否继续恶化。若上涨家数占比连续走低而指数不跌，"
                 "说明市场已进入「少数人赚钱」的状态。",
        "risk": "一旦强势方向补跌，缺乏承接。",
    },
    "bottom_probe": {
        "name": "底部博弈",
        "quadrant": "左上 · 风险偏好在，趋势不在",
        "meaning": "情绪与资金出现修复迹象，但价格结构尚未走出下行，"
                   "属于「先有情绪、后有结构」的早期阶段。",
        "watch": "关键看情绪修复能否转化为价格结构：能否站回中期均线、宽度能否持续回升。",
        "risk": "情绪修复失败率高，容易出现二次探底。",
    },
    "resonance_down": {
        "name": "共振偏弱",
        "quadrant": "左下 · 趋势与风险偏好同向向下",
        "meaning": "价格结构、宽度、情绪同时走弱，市场处在负向自我强化阶段。",
        "watch": "这个阶段最有价值的信号是「跌不动」：跌幅收窄、跌停家数下降、"
                 "成交极度萎缩后的第一根放量。",
        "risk": "流动性负反馈。若成交持续萎缩，修复所需时间会被拉长。",
    },
    "range_split": {
        "name": "震荡分化",
        "quadrant": "中枢 · 任一轴不显著",
        "meaning": "趋势与风险偏好至少有一项处在中性区间，市场没有形成一致预期。",
        "watch": "分化市里，指数的信息量低于宽度与轮动速度。关注主线是否在收敛。",
        "risk": "频繁切换带来的两边打脸。",
    },
    "disorder": {
        "name": "高波动失序",
        "quadrant": "特殊态 · 证据互相矛盾或波动率跃升",
        "meaning": "波动率处在高位或七维证据彼此冲突，此时任何单一方向的判断都不可靠。",
        "watch": "先等波动率回落、证据重新收敛，再做方向判断。",
        "risk": "把噪音当信号。",
    },
}

TREND_IDS = ("structure", "breadth", "rotation")
RISK_IDS = ("valuation", "liquidity", "sentiment", "events")

#: 轴内权重（与维度自身权重不同，二者分工明确）：
#: 维度权重 w 决定「它在一份完整报告里的分量」；轴内权重决定「它在这个轴里的分量」。
#: 轴内权重偏向长周期证据：宽度只有当日横截面，一天的数据不应当抵消数月的结构。
TREND_AXIS_W = {"structure": 0.45, "breadth": 0.35, "rotation": 0.20}
RISK_AXIS_W = {"valuation": 0.20, "liquidity": 0.30, "sentiment": 0.30, "events": 0.20}

NEUTRAL_BAND = 0.15


# --------------------------------------------------------------------------
def _weighted(dims: list[Dimension], ids: tuple[str, ...],
              axis_w: dict | None = None) -> tuple[float | None, float]:
    sel = [d for d in dims if d.id in ids and d.score is not None]
    if not sel:
        return None, 0.0
    w = {d.id: (axis_w or {}).get(d.id, 1.0) for d in sel}
    wsum = sum(w.values())
    if wsum <= 0:
        return None, 0.0
    return sum(d.score * w[d.id] for d in sel) / wsum, wsum


def _coverage(dims: list[Dimension]) -> float:
    total = sum(d.weight for d in dims)
    got = sum(d.weight for d in dims if d.score is not None and d.freshness != "missing")
    return got / total if total else 0.0


def _consistency(dims: list[Dimension]) -> tuple[float, float]:
    """返回 (一致性, 加权离散度)。离散度越大，状态越不稳。"""
    sel = [d for d in dims if d.score is not None]
    if len(sel) < 2:
        return 0.0, 1.0
    wsum = sum(d.weight for d in sel)
    mean = sum(d.score * d.weight for d in sel) / wsum
    mad = sum(abs(d.score - mean) * d.weight for d in sel) / wsum
    # 经验标定：mad=0 → 1.0；mad=0.6 以上视为完全不一致
    return ind.clamp(1.0 - mad / 0.6, 0.0, 1.0), mad


def _freshness(dims: list[Dimension]) -> float:
    return sum(d.weight for d in dims if d.freshness == "fresh") / sum(d.weight for d in dims)


def _axis_label(v: float | None) -> str:
    """把轴的读数翻译成人话。读数是产品对外的主要数字之一，
    不能只有一个浮点数——用户看的是"偏强/中性"，不是 +0.1372。"""
    if v is None:
        return "数据不足"
    if v >= 0.5:
        return "明显偏强"
    if v >= NEUTRAL_BAND:
        return "偏强"
    if v > -NEUTRAL_BAND:
        return "中性"
    if v > -0.5:
        return "偏弱"
    return "明显偏弱"


def classify(dims: list[Dimension], vol_pct: float | None = None) -> dict:
    trend, _ = _weighted(dims, TREND_IDS, TREND_AXIS_W)
    risk, _ = _weighted(dims, RISK_IDS, RISK_AXIS_W)
    coverage = _coverage(dims)
    consistency, mad = _consistency(dims)
    fresh = _freshness(dims)

    # ---- 状态判定 ----
    code = "range_split"
    if vol_pct is not None and vol_pct >= 0.85 and consistency < 0.6:
        code = "disorder"
    elif trend is not None and risk is not None:
        hi_t, lo_t = trend > NEUTRAL_BAND, trend < -NEUTRAL_BAND
        hi_r, lo_r = risk > NEUTRAL_BAND, risk < -NEUTRAL_BAND
        if hi_t and hi_r:
            code = "resonance_up"
        elif hi_t and lo_r:
            code = "structural"
        elif lo_t and hi_r:
            code = "bottom_probe"
        elif lo_t and lo_r:
            code = "resonance_down"
    elif trend is not None and risk is None:
        code = "range_split"

    if consistency < 0.3 and vol_pct is not None and vol_pct >= 0.8:
        code = "disorder"

    state = STATES[code]

    # ---- 子状态：在状态内部再做一次「同向但有条件」的细化 ----
    # 例：趋势轴近中性，但结构极弱而宽度极强 —— 这不是普通分化，而是修复初期。
    by_id = {d.id: d for d in dims}
    sub = ""
    st_s = (by_id.get("structure") or type("X", (), {"score": None})).score
    br_s = (by_id.get("breadth") or type("X", (), {"score": None})).score
    se_s = (by_id.get("sentiment") or type("X", (), {"score": None})).score
    if st_s is not None and br_s is not None:
        if st_s <= -0.4 and br_s >= 0.5:
            sub = "超跌修复初期（宽度先修复、结构未修复）"
        elif st_s >= 0.4 and br_s <= -0.5:
            sub = "指数强撑、个股走弱（结构未破、内部先坏）"
        elif st_s <= -0.4 and br_s <= -0.5:
            sub = "普跌加速（价格与宽度同步恶化）"
        elif st_s >= 0.4 and br_s >= 0.5 and (se_s or 0) > 0.3:
            sub = "多点共振（结构、宽度、情绪同向向上）"

    # ---- 置信度三因子 ----
    conf = 100.0 * (0.35 * coverage + 0.35 * consistency + 0.30 * fresh)
    if code == "disorder":
        conf *= 0.85

    return {
        "code": code,
        "name": state["name"],
        "quadrant": state["quadrant"],
        "meaning": state["meaning"],
        "watch": state["watch"],
        "risk": state["risk"],
        "substate": sub,
        "trend_axis": round(trend, 4) if trend is not None else None,
        "risk_axis": round(risk, 4) if risk is not None else None,
        "neutral_band": NEUTRAL_BAND,
        "trend_label": _axis_label(trend),
        "risk_label": _axis_label(risk),
        "confidence": round(conf, 1),
        "confidence_parts": {
            "coverage": round(coverage, 3),
            "consistency": round(consistency, 3),
            "freshness": round(fresh, 3),
            "dispersion": round(mad, 3),
            "formula": "100 × (0.35×coverage + 0.35×consistency + 0.30×freshness)",
        },
        "vol_percentile": round(vol_pct, 3) if vol_pct is not None else None,
    }


# --------------------------------------------------------------------------
# 主要矛盾
# --------------------------------------------------------------------------
CONTRADICTION_LIBRARY: list[dict] = [
    {
        "pair": ("structure", "breadth"),
        "title": "指数与个股背离",
        "text": "价格结构读数 {a}、市场宽度读数 {b}，两者方向相反。"
                "指数层面的信息量低于个股层面：当这两项打架时，"
                "通常意味着上涨（或下跌）的载体在收窄，"
                "状态能否延续取决于宽度能否向结构靠拢。",
        "check": "看上涨家数占比能否与指数同向移动，以及中位数涨跌幅是否跟随。",
    },
    {
        "pair": ("valuation", "sentiment"),
        "title": "估值与情绪错位",
        "text": "估值端读数 {a}、情绪端读数 {b}，方向相反。"
                "估值只回答「跌到多深有安全垫」，情绪回答「现在有没有人愿意接」。"
                "两者错位时，安全垫存在但不构成转折条件。",
        "check": "看涨停家数与连板高度是否止跌回升——那是情绪转向的先行读数。",
    },
    {
        "pair": ("liquidity", "sentiment"),
        "title": "情绪与资金错位",
        "text": "流动性读数 {a}、情绪读数 {b}，方向相反。"
                "情绪与资金谁先动，决定了修复是「存量博弈」还是「增量入场」。",
        "check": "看成交活跃度能否回到 1 倍以上，以及两融余额是否止跌。",
    },
    {
        "pair": ("structure", "liquidity"),
        "title": "价格与量能不同步",
        "text": "价格结构读数 {a}、流动性读数 {b}，方向相反。"
                "价格在走而量能没跟上（或量能放大而价格不动），"
                "说明推动力来自存量资金的内部腾挪。",
        "check": "对照成交活跃度与指数同期的方向，确认量价是否重新匹配。",
    },
    {
        "pair": ("rotation", "breadth"),
        "title": "风格极致与整体赚钱效应背离",
        "text": "轮动端读数 {a}、宽度读数 {b}，方向相反。"
                "资金向特定风格集中而整体宽度走弱，"
                "是结构性行情进入后期的典型组合。",
        "check": "看领涨方向的赚钱效应能否外溢到其他行业。",
    },
]


def contradictions(dims: list[Dimension]) -> list[dict]:
    by_id = {d.id: d for d in dims}
    out: list[dict] = []

    for lib in CONTRADICTION_LIBRARY:
        a, b = lib["pair"]
        da, db = by_id.get(a), by_id.get(b)
        if not da or not db or da.score is None or db.score is None:
            continue
        if da.score * db.score >= 0:          # 同向，不构成矛盾
            continue
        strength = (abs(da.score - db.score) / 2.0) * (da.weight + db.weight) / 0.4
        out.append({
            "id": f"{a}_{b}",
            "title": lib["title"],
            "text": lib["text"].format(a=f"{da.score:+.2f}", b=f"{db.score:+.2f}"),
            "check": lib["check"],
            "dims": [a, b],
            "strength": round(strength, 3),
            "detail": [
                {"dim": da.name, "score": round(da.score, 3), "status": da.status,
                 "headline": da.headline},
                {"dim": db.name, "score": round(db.score, 3), "status": db.status,
                 "headline": db.headline},
            ],
        })

    # 兜底：任意方向相反且权重靠前的组合
    if not out:
        sel = [d for d in dims if d.score is not None]
        sel.sort(key=lambda d: -abs(d.score) * d.weight)
        for i in range(len(sel) - 1):
            for j in range(i + 1, len(sel)):
                if sel[i].score * sel[j].score < 0:
                    out.append({
                        "id": f"{sel[i].id}_{sel[j].id}",
                        "title": f"{sel[i].name} 与 {sel[j].name} 方向相反",
                        "text": f"{sel[i].name}（{sel[i].score:+.2f}）与 "
                                f"{sel[j].name}（{sel[j].score:+.2f}）给出的方向不一致，"
                                f"当前证据尚未形成一致结论。",
                        "check": "等待其中一维先转向，再确认方向。",
                        "dims": [sel[i].id, sel[j].id],
                        "strength": round(abs(sel[i].score - sel[j].score) / 2, 3),
                        "detail": [
                            {"dim": sel[i].name, "score": round(sel[i].score, 3),
                             "status": sel[i].status, "headline": sel[i].headline},
                            {"dim": sel[j].name, "score": round(sel[j].score, 3),
                             "status": sel[j].status, "headline": sel[j].headline},
                        ],
                    })
                    break
            if out:
                break

    out.sort(key=lambda x: -x["strength"])
    return out[:2]


# --------------------------------------------------------------------------
# 证伪条件
# --------------------------------------------------------------------------
def _cond(cid: str, text: str, current, threshold, unit: str,
          comparator: str, source: str, why: str) -> dict:
    """构造一条可监控条件，并计算它离触发还有多远。"""
    distance = None
    distance_is_pp = False
    try:
        if current is not None and threshold is not None:
            gap = abs(current - threshold)
            # 单位本身就是百分数（家数占比、分位、收益率）时，相对距离会产生
            # 「距离 60.6%」这种看不懂的数；口沿上大家说的是「差 30.3 个百分点」。
            if threshold and unit != "%":
                distance = gap / abs(threshold) * 100
            else:
                # 阈值本身是 0（例如「相对 MA60 转负」）时，百分比分母没有意义，
                # 直接用百分点差表示距离——这是同义但不会除零的写法。
                distance = gap
                distance_is_pp = True
    except Exception:  # noqa: BLE001
        distance = None
    triggered = False
    if current is not None and threshold is not None:
        triggered = current >= threshold if comparator in (">=", ">") else current <= threshold
    return {
        "id": cid, "text": text, "current": current, "threshold": threshold,
        "unit": unit, "comparator": comparator, "source": source, "why": why,
        "distance_pct": None if distance is None else round(distance, 2),
        "distance_is_pp": distance_is_pp,
        "triggered": triggered,
    }


def switch_conditions(regime: dict, dims: list[Dimension], snap) -> list[dict]:
    by_id = {d.id: d for d in dims}
    out: list[dict] = []

    # 1) 上证 vs MA60 —— 最经典的结构性分水岭
    sec = snap.indices.get("1.000001", {})
    c = ind.closes(sec.get("kline") or [])
    if len(c) >= 60:
        m60, m20 = ind.ma(c, 60), ind.ma(c, 20)
        out.append(_cond(
            "sh_ma60", "上证指数相对 MA60", round(ind.pct(c[-1] / m60 - 1), 2),
            0.0, "%", "<=", "index_kline",
            "MA60 是中期趋势分界。跌破且不能快速收回，"
            "「趋势轴为正」的前提就不再成立。"))
        out.append(_cond(
            "sh_ma20", "上证指数相对 MA20", round(ind.pct(c[-1] / m20 - 1), 2),
            0.0, "%", "<=", "index_kline",
            "MA20 是短期成本线，跌破意味着短线资金整体亏损。"))

    # 2) 宽度
    b = by_id.get("breadth")
    if b and b.score is not None:
        up_ratio = (b.raw or {}).get("up_ratio")
        if up_ratio is not None:
            out.append(_cond(
                "breadth_50", "上涨家数占比", round(up_ratio * 100, 1), 50.0, "%", "<=",
                "market_snapshot", "宽度跌破 50% 意味着多数个股在跌，"
                                   "指数的强势将失去个股基础。"))

    # 3) 量能
    liq = by_id.get("liquidity")
    if liq:
        act = (liq.raw or {}).get("activity")
        if act is not None:
            out.append(_cond(
                "activity", "成交活跃度（当日/20日均量）", round(act, 2), 0.85, "倍", "<=",
                "index_kline", "量能持续低于 20 日均值的 85%，说明增量资金在撤。"))
        rz = (liq.raw or {}).get("rzye_pct")
        if rz is not None:
            out.append(_cond(
                "margin_5d", "两融余额 5 日变化", round(rz, 2), 0.0, "%", "<=",
                "margin", "杠杆资金连续流出会放大下行斜率。"))

    # 4) 情绪
    se = by_id.get("sentiment")
    if se and (se.raw or {}).get("zt") is not None:
        out.append(_cond(
            "zt_count", "涨停家数", se.raw["zt"], 40, "家", "<=", "limit_up",
            "涨停家数跌破 40 家，短线赚钱效应基本消失。"))
        out.append(_cond(
            "max_board", "最高连板高度", se.raw.get("max_board"), 3, "板", "<=", "limit_up",
            "连板高度是接力意愿的直接读数。"))

    # 5) 波动率
    st = by_id.get("structure")
    if st:
        vp = (st.raw or {}).get("vol_pct") or {}
        if vp:
            worst = max(vp.values())
            out.append(_cond(
                "vol_pct", "20 日波动率近一年分位", round(worst * 100, 1), 85.0, "%", ">=",
                "index_kline", "波动率进入前 15% 分位时，价格信号的噪音大于信号。"))

    # 6) 状态专属条件
    if regime["code"] in ("resonance_down", "bottom_probe"):
        out.append(_cond(
            "breadth_recover", "上涨家数占比（修复确认）", None, 55.0, "%", ">=",
            "market_snapshot", "从弱转强的第一确认通常来自宽度回到 55% 以上。"))
    if regime["code"] in ("resonance_up", "structural"):
        out.append(_cond(
            "breadth_deteriorate", "上涨家数占比（恶化确认）", None, 40.0, "%", "<=",
            "market_snapshot", "强势状态下宽度跌破 40%，是「内部先坏」的早期证据。"))

    for x in out:
        x["why"] = x["why"]
    return out


# --------------------------------------------------------------------------
def state_triggers(regime: dict) -> list[str]:
    """当前状态「被推翻」需要同时满足什么——用一句话写清楚。"""
    code = regime["code"]
    return {
        "resonance_up": [
            "趋势轴回落至 0.15 以下（结构、宽度、轮动中至少两维转中性或转负）",
            "或波动率进入近一年前 15% 分位，同时证据离散度上升",
        ],
        "structural": [
            "趋势轴跌破 0.15（价格结构失效）",
            "或风险偏好回升至 0.15 以上（情绪与资金重新配合，状态上移为共振偏强）",
        ],
        "bottom_probe": [
            "风险偏好回落至 0.15 以下（情绪修复失败，回到共振偏弱）",
            "或趋势轴回升至 0.15 以上（结构确认修复，状态上移为共振偏强）",
        ],
        "resonance_down": [
            "风险偏好回升至 0.15 以上（情绪先转向，进入底部博弈）",
            "或趋势轴回升至 0.15 以上（结构先修复）",
        ],
        "range_split": [
            "趋势轴与风险偏好同时越过 ±0.15，进入某个象限",
        ],
        "disorder": [
            "波动率回落到 80% 分位以下，且证据离散度下降到 0.45 以内",
        ],
    }.get(code, [])
