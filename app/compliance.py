# -*- coding: utf-8 -*-
"""合规审查：把「不得预测涨跌 / 不得给仓位建议」从口头约定变成代码约束。

产品输出里所有对外文本（叙述、追问回答、导出的 Markdown）在离开服务端前
都会过一遍 ``audit()``。命中禁止表达时：

* 记录违规项（含命中片段与所在字段），供前端在「合规」面板展示；
* ``scrub()`` 会把命中片段替换为中性表述，保证没有任何违规文本被交付出去。

这不是"提示词里写一句不要预测"，而是可测试的硬约束（见 tests/test_core.py）。
"""

from __future__ import annotations

import re
from typing import Iterable

#: (正则, 违规类型, 说明)
RULES: list[tuple[str, str, str]] = [
    # 方向预测
    # 「预计…上涨」中间常夹时间状语（预计下周上涨），因此允许至多 8 个非标点字符；
    # 同时用负向先行断言排除「上涨家数 / 上涨占比」这类合法的市场宽度术语。
    (r"(预计|预期|预测|将会|即将|有望|大概率|很可能)(?:会)?"
     r"[^。！？；，\r\n]{0,8}?"
     r"(上涨|下跌|反弹|回调|见顶|见底|突破|跌破|走牛|走熊|创新高|创新低)"
     r"(?!家数|数量|比例|占比|空间)",
     "方向预测", "题目明确禁止直接预测涨跌"),
    (r"(明天|明日|下周|下个月|后市)(会|将|有望)", "方向预测", "禁止对未来方向的断言"),
    # 点位预测
    (r"(目标位|目标价|看到|上看|下看|支撑位在|阻力位在)\s*\d+", "点位预测", "禁止给出目标点位"),
    (r"(上涨|下跌)空间(还)?有\s*\d+", "点位预测", "禁止量化涨跌空间"),
    # 仓位与交易建议
    (r"(建议|应当|应该|可以考虑)(买入|卖出|加仓|减仓|清仓|建仓|补仓|止盈|止损)",
     "交易建议", "禁止给出买卖建议"),
    (r"(满仓|半仓|空仓|重仓|轻仓|加仓|减仓|清仓|建仓)\b", "仓位建议", "禁止给出仓位建议"),
    (r"(建议配置|建议关注|可以布局|逢低吸纳|逢高减持)", "交易建议", "禁止给出操作建议"),
    # 「建议仓位提升至 7 成」这类表述没有买卖动词，但同样是仓位建议。
    (r"(建议|应当|应该|可以考虑|可以把|可将)[^。！？；]{0,6}(仓位|持仓)",
     "仓位建议", "禁止给出仓位建议"),
    (r"(仓位|持仓)[^。！？；]{0,10}\d+\s*成", "仓位建议", "禁止给出仓位建议"),
    # 绝对化表达
    (r"(必涨|必跌|稳赚|稳赢|一定会上涨|一定会下跌|抄底机会|绝佳买点|最佳买点)",
     "绝对化表述", "禁止绝对化承诺"),
    # 推荐评级
    (r"(强烈推荐|买入评级|增持评级|卖出评级|推荐买入)", "评级表述", "禁止给个股/市场评级"),
]

_COMPILED = [(re.compile(p), kind, why) for p, kind, why in RULES]

#: 命中后替换成的中性表述
_NEUTRAL = {
    "方向预测": "方向尚待确认",
    "点位预测": "具体点位不予给出",
    "交易建议": "请自行决策",
    "仓位建议": "请自行决策",
    "绝对化表述": "存在不确定性",
    "评级表述": "不作评级",
}


def audit(text: str) -> dict:
    """检查文本是否触发合规红线。"""
    hits = []
    for rx, kind, why in _COMPILED:
        for m in rx.finditer(text or ""):
            hits.append({"kind": kind, "match": m.group(0), "why": why,
                         "start": m.start()})
    return {"ok": not hits, "violations": hits,
            "checked_chars": len(text or ""),
            "rules": len(RULES)}


def audit_fields(fields: dict) -> dict:
    """批量检查多个字段，返回汇总。"""
    all_hits = []
    for name, val in fields.items():
        if not isinstance(val, str):
            continue
        r = audit(val)
        for h in r["violations"]:
            all_hits.append({**h, "field": name})
    return {"ok": not all_hits, "violations": all_hits}


def scrub(text: str) -> tuple[str, list[dict]]:
    """把所有违规片段替换为中性表述，返回 (净化文本, 被替换项)。"""
    removed = []
    out = text or ""
    for rx, kind, _why in _COMPILED:
        def _repl(m):
            removed.append({"kind": kind, "match": m.group(0)})
            return _NEUTRAL.get(kind, "（已按合规要求略去）")
        out = rx.sub(_repl, out)
    return out, removed


def _walk(node, prefix: str = ""):
    """遍历任意嵌套结构，产出 (字段路径, 字符串)。"""
    if isinstance(node, str):
        yield prefix, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk(v, f"{prefix}[{i}]")


def scrub_tree(node):
    """深拷贝并净化其中所有字符串，返回 (新结构, 被替换项列表)。

    为什么需要它：叙述是嵌套结构（list 里套 dict 再套 str），逐字段手写 audit 调用
    迟早会漏掉一个出口——而漏掉的那一个就是合规事故。这里只保留一个遍历实现。
    """
    removed: list[dict] = []

    def rec(v):
        if isinstance(v, str):
            out, hit = scrub(v)
            removed.extend(hit)
            return out
        if isinstance(v, dict):
            return {k: rec(x) for k, x in v.items()}
        if isinstance(v, list):
            return [rec(x) for x in v]
        if isinstance(v, tuple):
            return tuple(rec(x) for x in v)
        return v

    return rec(node), removed


def audit_payload(sections: dict) -> dict:
    """审计一组对外字段（叙述、条件、矛盾、追问答案……），一次给出全部违规项。"""
    hits: list[dict] = []
    checked = 0
    for name, sec in sections.items():
        for path, text in _walk(sec):
            checked += len(text)
            for h in audit(text)["violations"]:
                hits.append({**h, "field": f"{name}.{path}" if path else name})
    return {"ok": not hits, "violations": hits, "checked_chars": checked,
            "rules": len(RULES)}


def preview_requires_disclaimer(text: str) -> bool:
    return len(text or "") > 120


LEGAL_FOOTER = (
    "本内容为市场状态描述与条件推演，基于公开行情数据，"
    "不构成投资建议，不预测涨跌，不提供买卖与仓位建议。"
)
