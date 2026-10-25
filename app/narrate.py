# -*- coding: utf-8 -*-
"""叙述生成：把结构化的证据矩阵，翻译成研究员能直接读的文字。

两条生成路径，**输出 schema 完全一致**，前端无需区分：

* ``template``（默认）：确定性模板。速度毫秒级、零成本、可复现，
  同一个快照永远生成同一段话——这对"研判"这种要复盘的产品是硬要求。
* ``llm``：调用任意 OpenAI 兼容端点（见 llm.py），在模板之上做更自然的表达
  与更灵活的矛盾归因。**LLM 输出会被逐句核对数字**，凡出现证据里没有的数字，
  该句直接被丢弃并回退到模板（见 llm.py 的 ``verify_numbers``）。

写这套文字时守住一条底线：**只描述状态与条件，不给方向结论。**
"结构已破位"是可以说的（它是对已发生事实的描述），
"预计继续下跌"是不能说的（它是对未来的断言）。
"""

from __future__ import annotations

import statistics
from typing import Any

from .regime import STATES


def _n(v, unit="", digits=1, sign=False) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{f:+.{digits}f}" if sign else f"{f:.{digits}f}"
    return s + unit


def _by_id(dims) -> dict:
    return {d.id: d for d in dims}


def _pick_tag(dims) -> str:
    """取两个「最显著」的维度拼成状态标签。

    只有 1 个维度可用时（离线、或数据源大面积失败、只剩事件维度）不能拼「A 与 B」，
    否则 names[1] 越界直接让整次研判 500——降级路径上崩溃是最不该发生的事。
    """
    sel = [d for d in dims if d.score is not None]
    if not sel:
        return "证据不足"
    sel.sort(key=lambda d: -abs(d.score) * d.weight)
    names = [d.name for d in sel[:2]]
    if len(names) == 1:
        return f"仅{names[0]}可读"
    same = sel[0].score * sel[1].score > 0
    return f"{names[0]}与{names[1]}同向" if same else f"{names[0]}与{names[1]}背离"


# --------------------------------------------------------------------------
def _structure_sentence(d: dict) -> str:
    raw = d.get("raw") or {}
    ratio = raw.get("above_ratio")
    slopes = raw.get("slopes") or {}
    dds = raw.get("drawdown") or {}
    if ratio is None:
        return "主要指数日线样本不足，价格结构未能完整判定。"
    total = len(raw.get("dists") or {}) * 3
    k = round(ratio * total)
    s = f"价格层面，{total} 个「指数×均线」组合中有 {k} 个站上均线"
    if slopes:
        avg = sum(slopes.values()) / len(slopes)
        s += f"，20 日均线斜率均值 {avg:+.2f}%"
    if dds:
        worst = min(dds.values())
        s += f"，最深回撤 {worst:.1f}%"
    return s + "。"


def _breadth_sentence(d: dict) -> str:
    raw = d.get("raw") or {}
    n = raw.get("n")
    if not n:
        return "全市场快照缺失，宽度无法计算。"
    up_ratio = raw.get("up_ratio") or 0
    up = round(up_ratio * n)
    med = raw.get("median")
    s = (f"宽度层面，{n} 只有效样本中上涨 {up} 只（{up_ratio*100:.1f}%），"
         f"个股涨跌中位数 {med:+.2f}%" if med is not None else
         f"宽度层面，{n} 只有效样本中上涨 {up} 只（{up_ratio*100:.1f}%）")
    su, sd = raw.get("strong_up"), raw.get("strong_dn")
    if su is not None and sd is not None:
        s += f"，涨超 5% 的 {round(su*n)} 只、跌超 5% 的 {round(sd*n)} 只"
    return s + "。"


def _liquidity_sentence(d: dict) -> str:
    raw = d.get("raw") or {}
    seg = []
    if raw.get("amount"):
        seg.append(f"两市成交 {raw['amount']/1e8:.0f} 亿元")
    if raw.get("activity"):
        seg.append(f"活跃度 {raw['activity']:.2f} 倍于 20 日均量")
    if raw.get("rzye"):
        seg.append(f"两融余额 {raw['rzye']/1e8:.0f} 亿元")
    if raw.get("rzye_pct") is not None:
        seg.append(f"5 日变化 {raw['rzye_pct']:+.2f}%")
    if raw.get("net_inflow") is not None:
        seg.append(f"主力资金净{'流入' if raw['net_inflow'] > 0 else '流出'} "
                   f"{abs(raw['net_inflow'])/1e8:.0f} 亿元")
    return ("流动性层面，" + "，".join(seg) + "。") if seg else "流动性数据不完整。"


def _sentiment_sentence(d: dict) -> str:
    raw = d.get("raw") or {}
    if raw.get("zt") is None:
        return "涨跌停池缺失，情绪无法度量。"
    s = (f"情绪层面，涨停 {raw['zt']} 家、跌停 {raw['dt']} 家，"
         f"最高 {raw['max_board']} 连板")
    if raw.get("seal_rate") is not None:
        s += f"，封板率 {raw['seal_rate']*100:.0f}%"
    if raw.get("multi") is not None:
        s += f"，连板股 {raw['multi']} 家"
    return s + "。"


def _rotation_sentence(d: dict) -> str:
    raw = d.get("raw") or {}
    seg = []
    if raw.get("size20") is not None:
        who = "大盘" if raw["size20"] > 0 else "小盘"
        seg.append(f"近 20 日{who}占优 {abs(raw['size20']):.1f}pct")
    if raw.get("style20") is not None:
        who = "成长" if raw["style20"] > 0 else "价值"
        seg.append(f"近 20 日{who}风格占优 {abs(raw['style20']):.1f}pct")
    if raw.get("rank_stability") is not None:
        rho = raw["rank_stability"]
        seg.append("行业主线较稳定" if rho > 0.5 else
                   "行业轮动很快、主线不稳" if rho < 0.2 else "行业主线半稳定")
    return ("轮动层面，" + "，".join(seg) + "。") if seg else "行业指数数据不足，轮动未能判定。"


def _valuation_sentence(d: dict) -> str:
    raw = d.get("raw") or {}
    seg = []
    if raw.get("median_pe"):
        seg.append(f"全 A 中位 PE {raw['median_pe']:.1f} 倍")
    if raw.get("median_pb"):
        seg.append(f"中位 PB {raw['median_pb']:.2f} 倍")
    if raw.get("pb_below_1") is not None:
        seg.append(f"破净占比 {raw['pb_below_1']*100:.1f}%")
    return ("估值层面，" + "，".join(seg) + "。") if seg else "估值数据不足。"


def _events_sentence(d: dict) -> str:
    raw = d.get("raw") or {}
    evs = raw.get("events") or []
    if not evs:
        return "事件面没有命中预设词典的显著变化。"
    top = evs[0]
    cats = "、".join(top.get("categories") or [])
    direction = top.get("direction")
    if direction is None:
        return "事件面命中了关键词，但方向无法判定，仅作为题材线索。"
    if abs(direction) < 0.35:
        return (f"事件面缺乏方向性：命中词典的最强一条是「{top['title'][:32]}」"
                f"（{cats or '未归类'}），方向读数仅 {direction:+.2f}，"
                f"更接近题材噪音而非宏观驱动。")
    return (f"事件面，最具影响的一条是「{top['title'][:38]}」"
            f"（类别：{cats or '未归类'}），方向读数 {direction:+.2f}。")


# --------------------------------------------------------------------------
def build_narrative(regime: dict, dims, contra, conds, snap) -> dict:
    d = _by_id(dims)
    tag = _pick_tag(dims)
    state = STATES.get(regime["code"], {})

    headline = f"{regime['name']} · {tag}"

    # ---- 导语 ----
    t_ax, r_ax = regime.get("trend_axis"), regime.get("risk_axis")
    as_of = getattr(snap, "as_of", "") or ""
    lede = (f"截至 {as_of}，七维证据合成后的市场状态是「{regime['name']}」："
            f"趋势轴 {t_ax:+.2f}、风险偏好轴 {r_ax:+.2f}"
            if t_ax is not None and r_ax is not None else
            f"截至 {as_of}，七维证据合成后的市场状态是「{regime['name']}」")
    if t_ax is not None and r_ax is not None:
        both = "同处正区" if t_ax > 0 and r_ax > 0 else (
            "同处负区" if t_ax < 0 and r_ax < 0 else "分处两侧")
        lede += f"，两项{both}。"

    # ---- 主段落 ----
    parts = []
    for bid, fn in (("structure", _structure_sentence),
                    ("breadth", _breadth_sentence),
                    ("rotation", _rotation_sentence),
                    ("liquidity", _liquidity_sentence),
                    ("sentiment", _sentiment_sentence),
                    ("valuation", _valuation_sentence),
                    ("events", _events_sentence)):
        if bid in d:
            sent = fn(d[bid].to_dict())
            if sent:
                parts.append(sent)

    summary = lede + "".join(parts)

    # ---- 状态解读 ----
    sub = regime.get("substate") or ""
    state_read = (
        (f"更具体的形态是「{sub}」。" if sub else "")
        + f"{state.get('meaning', '')}"
        f"这一判定的依据是：趋势轴由{d['structure'].status if 'structure' in d else '结构'}的结构、"
        f"{d['breadth'].status if 'breadth' in d else '宽度'}的宽度、"
        f"{d['rotation'].status if 'rotation' in d else '轮动'}的轮动共同构成；"
        f"风险偏好轴由估值、流动性、情绪与事件四项构成。"
        f"合成置信度 {regime['confidence']:.0f}/100，"
        f"其中数据覆盖 {regime['confidence_parts']['coverage']*100:.0f}%、"
        f"证据一致性 {regime['confidence_parts']['consistency']*100:.0f}%、"
        f"数据新鲜度 {regime['confidence_parts']['freshness']*100:.0f}%。"
        f"需要留意的是：{state.get('risk', '')}"
    ).strip()

    # ---- 主要矛盾 ----
    if contra:
        c0 = contra[0]
        contradiction_text = (
            f"当前最需要解释的矛盾是「{c0['title']}」。{c0['text']}"
            f"检验方式：{c0['check']}"
        )
        if len(contra) > 1:
            contradiction_text += f" 次要矛盾是「{contra[1]['title']}」，强度相对更弱。"
    else:
        contradiction_text = ("七维证据方向基本一致，没有出现显著的两两背离；"
                              "这通常意味着当前状态的延续性较强，但也意味着单一维度转弱时"
                              "缺少对冲提示。")

    # ---- 什么会改变判断 ----
    live = [c for c in conds if c.get("current") is not None and not c.get("triggered")]
    live.sort(key=lambda c: abs(c.get("distance_pct") or 999))
    near = live[:4]
    if near:
        pieces = []
        for c in near:
            cmp = "跌破" if c["comparator"] in ("<=", "<") else "升破"
            pieces.append(f"{c['text']}（当前 {c['current']}{c['unit']}，"
                          f"{cmp} {c['threshold']}{c['unit']} 触发）")
        conditions_text = ("判断是否需要修正，主要盯这几条阈值：" + "；".join(pieces) + "。"
                           "它们被选中的原因是距离当前值最近，也最容易被单日数据打到。")
    else:
        conditions_text = "当前可用阈值条件不足，待下一交易日数据补齐后重新校准。"

    # ---- 观察清单 ----
    watch = []
    if "breadth" in d:
        watch.append(f"宽度能否回到 50% 以上（当前"
                     f"{(d['breadth'].raw or {}).get('up_ratio', 0)*100:.1f}%）")
    if "liquidity" in d:
        act = (d["liquidity"].raw or {}).get("activity")
        if act:
            watch.append(f"成交活跃度能否回到 1 倍以上（当前 {act:.2f} 倍）")
    if "sentiment" in d:
        w = d["sentiment"].raw or {}
        watch.append(f"涨停家数与连板高度能否回升（当前 {w.get('zt')} 家 / {w.get('max_board')} 板）")
    if "rotation" in d:
        rho = (d["rotation"].raw or {}).get("rank_stability")
        if rho is not None:
            watch.append("行业主线是否形成（5 日与 20 日排名秩相关当前 "
                         f"{rho:.2f}，越接近 1 越稳定）")
    if "valuation" in d:
        watch.append("破净率与中位 PE 是否继续走高（反映杀估值是否在延续）")

    # ---- 免责与不确定性 ----
    caveats = []
    for dim in dims:
        for u in dim.uncertainty:
            caveats.append(f"【{dim.name}】{u}")
    if regime["code"] == "disorder":
        caveats.insert(0, "当前处于高波动/证据冲突状态，任何单一维度的读数都不应被单独使用。")

    return {
        "engine": "template",
        "substate": sub,
        "headline": headline,
        "lede": lede,
        "summary": summary,
        "state_read": state_read,
        "contradiction": contradiction_text,
        "conditions": conditions_text,
        "watch": watch,
        "caveats": caveats,
        "dimension_notes": {k: v.headline for k, v in d.items()},
    }
