# -*- coding: utf-8 -*-
"""追问层：让「研判」可以被继续问下去。

设计要点
--------
1. **回答必须有引用**：每条回答都返回 ``citations``，指向具体的维度与事实行
   （维度 id + 事实 label + 数值 + 数据源）。前端把它渲染成可点击的证据条，
   用户能一路回溯到原始数字。
2. **拒绝回答交易问题**：问「能不能买」「买什么」时，不做任何方向性回答，
   而是把问题重新框定为「当前状态是什么、什么条件下会变」。
   这不是能力不足，是产品的边界（见 docs/PRODUCT.md 的合规章节）。
3. **无 LLM 也能用**：内置检索式回答覆盖七维、矛盾、条件、历史、数据质量
   六类问题；配置 LLM 后同一套证据会交给模型组织语言，但数字仍需校验。
"""

from __future__ import annotations

import re
from typing import Iterable

from . import compliance
from .llm import LLMClient, verify_numbers

# --------------------------------------------------------------------------
DIM_ALIASES: dict[str, tuple[str, ...]] = {
    "structure": ("结构", "均线", "趋势", "价格", "指数位置", "技术面", "破位", "回撤"),
    "breadth": ("宽度", "涨跌家数", "赚钱效应", "普涨", "普跌", "多少只", "个股"),
    "rotation": ("轮动", "风格", "大小盘", "成长", "价值", "板块", "行业", "主线"),
    "valuation": ("估值", "市盈率", "pe", "市净率", "pb", "破净", "便宜", "贵"),
    "liquidity": ("流动性", "成交", "量能", "两融", "资金", "杠杆", "缩量", "放量"),
    "sentiment": ("情绪", "涨停", "跌停", "连板", "封板", "炸板", "人气", "投机"),
    "events": ("事件", "消息", "新闻", "政策", "利好", "利空", "发生了什么"),
}

TRADE_WORDS = ("能买", "可以买", "该买", "买什么", "推荐", "仓位", "加仓", "减仓",
               "清仓", "止损", "止盈", "抄底", "入场", "出局", "目标价", "看多", "看空")


def _dim_of(question: str) -> str | None:
    q = question.lower()
    best, best_hits = None, 0
    for dim, words in DIM_ALIASES.items():
        hits = sum(1 for w in words if w in q)
        if hits > best_hits:
            best, best_hits = dim, hits
    return best


def _dim_payload(payload: dict, dim_id: str) -> dict | None:
    for d in payload.get("dimensions") or []:
        if d["id"] == dim_id:
            return d
    return None


def _cites_from(dim: dict, limit: int = 4) -> list[dict]:
    out = []
    for f in (dim.get("facts") or [])[:limit]:
        out.append({"dim": dim["name"], "dim_id": dim["id"], "label": f["label"],
                    "value": f["value"], "unit": f.get("unit") or "",
                    "source": f.get("source") or ""})
    return out


# --------------------------------------------------------------------------
def _refuse_trade(payload: dict) -> dict:
    reg = payload["regime"]
    return {
        "engine": "guardrail",
        "refused": True,
        "answer": (
            "这个问题我不会回答，也不应该回答——本产品的定位是「描述市场状态、"
            "标出可能改变判断的条件」，不预测指数涨跌、不判断某只标的该不该买、"
            "不给仓位建议。硬要回答只会给你一个看起来专业但并不负责任的结论。\n\n"
            f"我能提供的是：当前市场状态是「{reg['name']}」"
            f"（趋势轴 {reg.get('trend_axis')}、风险偏好轴 {reg.get('risk_axis')}，"
            f"置信度 {reg.get('confidence')}/100）。"
            "你可以继续问我：当前最主要的矛盾是什么、哪些阈值一旦触发就需要重估、"
            "或者某一维证据的具体读数。"
        ),
        "citations": [],
        "followups": ["当前的主要矛盾是什么？", "什么条件下需要改变判断？", "市场宽度现在是什么水平？"],
    }


def _answer_conditions(payload: dict) -> dict:
    conds = [c for c in payload.get("conditions") or [] if c.get("current") is not None]
    conds.sort(key=lambda c: abs(c.get("distance_pct") or 999))
    lines = []
    for c in conds[:6]:
        cmp = "跌破" if c["comparator"] in ("<=", "<") else "升破"
        d = c.get("distance_pct")
        dist = f"，距离约 {abs(d):.1f}%" if d is not None else ""
        flag = "【已触发】" if c.get("triggered") else ""
        lines.append(f"· {flag}{c['text']}：当前 {c['current']}{c['unit']}，"
                     f"{cmp} {c['threshold']}{c['unit']} 触发{dist}")
    body = "\n".join(lines) or "当前缺少可用阈值。"
    return {
        "engine": "retrieval",
        "answer": (
            f"当前状态是「{payload['regime']['name']}」。"
            "判断是否需要修正，看这几条阈值（按距离当前值排序）：\n\n" + body +
            "\n\n这些阈值的作用是让判断可被证伪——先写下「什么情况下我错了」，"
            "比事后找理由更可靠。"
        ),
        "citations": [{"dim": "状态机", "dim_id": "conditions", "label": c["text"],
                       "value": c["current"], "unit": c.get("unit") or "",
                       "source": c.get("source", "")} for c in conds[:4]],
        "followups": ["当前的主要矛盾是什么？", "七维里哪一维最弱？"],
    }


def _answer_contradiction(payload: dict) -> dict:
    cs = payload.get("contradictions") or []
    if not cs:
        return {
            "engine": "retrieval",
            "answer": "七维证据方向高度一致，没有出现显著的两两背离。"
                      "这通常意味着当前状态的延续性较强，但也意味着单一维度转弱时缺少对冲提示。",
            "citations": [],
            "followups": ["七维各自的读数是多少？"],
        }
    c = cs[0]
    detail = "\n".join(f"· {x['dim']}：{x['score']:+.2f}（{x['status']}）"
                       for x in c["detail"])
    out = {
        "engine": "retrieval",
        "answer": f"当前最主要的矛盾是「{c['title']}」。\n\n{c['text']}\n\n"
                  f"两维具体读数：\n{detail}\n\n检验方式：{c['check']}",
        "citations": [{"dim": x["dim"], "dim_id": d, "label": "维度读数",
                       "value": x["score"], "unit": "", "source": "evidence"}
                      for x, d in zip(c["detail"], c["dims"])],
        "followups": ["什么条件下需要改变判断？", "市场宽度现在是什么水平？"],
    }
    if len(cs) > 1:
        out["answer"] += f"\n\n次要矛盾是「{cs[1]['title']}」，强度相对更弱。"
    return out


def _answer_dimension(payload: dict, dim_id: str) -> dict:
    dim = _dim_payload(payload, dim_id)
    if not dim:
        return {"engine": "retrieval", "answer": "没有找到这一维的数据。",
                "citations": [], "followups": []}
    facts = "\n".join(
        f"· {f['label']}：{f['value']}{f.get('unit') or ''}"
        + (f"（{f['note']}）" if f.get("note") else "")
        for f in (dim.get("facts") or [])[:7])
    sig = "\n".join(f"· {s['text']}" for s in (dim.get("signals") or [])[:4])
    ans = (f"【{dim['name']}】{dim['headline']}\n\n"
           f"读数 {dim['score']:+.2f}（{dim['status']}），置信度 {dim['confidence']*100:.0f}%。\n\n"
           f"关键数据：\n{facts}\n")
    if sig:
        ans += f"\n判读：\n{sig}\n"
    if dim.get("caliber"):
        ans += f"\n口径：{dim['caliber']}"
    if dim.get("uncertainty"):
        ans += "\n\n局限：" + "；".join(dim["uncertainty"])
    return {
        "engine": "retrieval",
        "answer": ans,
        "citations": _cites_from(dim, 5),
        "followups": ["当前的主要矛盾是什么？", "什么条件下需要改变判断？"],
    }


def _answer_overview(payload: dict) -> dict:
    n = payload.get("narrative") or {}
    reg = payload["regime"]
    dims = payload.get("dimensions") or []
    strongest = max(dims, key=lambda d: d["score"] if d["score"] is not None else -9)
    weakest = min(dims, key=lambda d: d["score"] if d["score"] is not None else 9)
    return {
        "engine": "retrieval",
        "answer": (f"{n.get('summary', '')}\n\n"
                   f"最强的一维是「{strongest['name']}」（{strongest['score']:+.2f}），"
                   f"最弱的一维是「{weakest['name']}」（{weakest['score']:+.2f}）。\n\n"
                   f"{n.get('contradiction', '')}"),
        "citations": _cites_from(strongest, 2) + _cites_from(weakest, 2),
        "followups": ["当前的主要矛盾是什么？", "什么条件下需要改变判断？"],
    }


# --------------------------------------------------------------------------
def answer(question: str, payload: dict) -> dict:
    """对追问返回带引用的回答。"""
    q = (question or "").strip()
    low = q.lower()

    # 1) 交易类问题：硬拒绝 + 重框定
    if any(w in low for w in TRADE_WORDS):
        return _refuse_trade(payload)

    # 2) 条件类
    if any(w in low for w in ("条件", "什么情况", "会变", "改变判断", "什么时候", "触发", "证伪")):
        return _answer_conditions(payload)

    # 3) 矛盾类
    if any(w in low for w in ("矛盾", "背离", "冲突", "打架", "不一致", "为什么")):
        return _answer_contradiction(payload)

    # 4) 维度类
    dim = _dim_of(q)
    if dim:
        res = _answer_dimension(payload, dim)
        if LLMClient.available():
            res = _llm_enhance(question, payload, res)
        return res

    # 5) 默认为总览
    res = _answer_overview(payload)
    if LLMClient.available():
        res = _llm_enhance(question, payload, res)
    return res


# --------------------------------------------------------------------------
def _digest(payload: dict) -> str:
    lines = [f"日期={payload.get('as_of')}", f"状态={payload['regime']['name']}",
             f"子状态={payload['regime'].get('substate') or '无'}",
             f"趋势轴={payload['regime'].get('trend_axis')}",
             f"风险偏好轴={payload['regime'].get('risk_axis')}",
             f"置信度={payload['regime'].get('confidence')}"]
    for d in payload.get("dimensions") or []:
        lines.append(f"[{d['id']}] {d['name']}: score={d['score']} status={d['status']}")
        for f in (d.get("facts") or [])[:6]:
            lines.append(f"    - {f['label']}={f['value']}{f.get('unit') or ''}")
    for c in (payload.get("contradictions") or [])[:1]:
        lines.append(f"主要矛盾={c['title']}: {c['text'][:120]}")
    return "\n".join(lines)


def _llm_enhance(question: str, payload: dict, fallback: dict) -> dict:
    client = LLMClient()
    system = (
        "你是 A 股市场状态研判助手。你只能使用用户提供的证据作答，"
        "禁止预测涨跌、禁止给出买卖与仓位建议、禁止编造证据中不存在的数字。"
        "回答用中文，先给结论再给依据，控制在 300 字以内。"
        "末尾用「依据：」列出你引用的维度名。"
    )
    user = f"证据：\n{_digest(payload)}\n\n问题：{question}"
    try:
        text = client.chat(system, user)
    except Exception as exc:  # noqa: BLE001
        fallback.setdefault("notes", []).append(f"LLM 调用失败，已回退到检索式回答：{exc}")
        return fallback

    clean, removed = compliance.scrub(text)
    bad = verify_numbers(clean, payload)
    if bad:
        fallback.setdefault("notes", []).append(
            "LLM 回答中出现了证据里不存在的数字（" + "、".join(bad[:3]) + "），已回退到检索式回答。")
        return fallback
    return {
        "engine": f"llm:{client.model}",
        "answer": clean,
        "citations": fallback.get("citations", []),
        "followups": fallback.get("followups", []),
        "scrubbed": removed,
    }
