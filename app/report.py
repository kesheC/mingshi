# -*- coding: utf-8 -*-
"""把一次研判渲染成 Markdown，用于导出与归档。

报告顺序刻意与产品叙事一致：先状态，再证据，再矛盾，最后是可证伪条件——
读者看完会先知道「现在是什么」，再知道「依据是什么」，最后知道「什么情况下要改」。
"""

from __future__ import annotations

from .compliance import LEGAL_FOOTER, audit_payload          # noqa: F401
from .compliance import scrub                               # noqa: F401



def _dist(c: dict) -> str:
    """条件距离的显示。

    阈值本身为 0 时（如「涨跌家数差 >= 0」），相对距离没有定义，改用**百分点差**；
    两者混用会把 4.68pp 写成 4.7%，是这类报告里最常见的口径错误。
    """
    d = c.get("distance_pct")
    if d is None:
        return "—"
    unit = "pp" if c.get("distance_is_pp") else "%"
    return f"{abs(d):.1f}{unit}"


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
    elif isinstance(v, int):
        s = f"{v:,}"
    else:
        s = str(v)
    if unit == "%":
        return f"{s}%"
    if unit == "亿元":
        return f"{s} 亿元"
    if unit == "只":
        return f"{s} 只"
    return f"{s}{(' ' + unit) if unit else ''}"


def render_markdown(payload: dict) -> str:
    reg = payload.get("regime") or {}
    nar = payload.get("narrative") or {}
    dims = payload.get("dimensions") or []
    L: list[str] = []

    L.append(f"# 市场大势研判 · {payload.get('as_of', '')}")
    L.append("")
    L.append(f"> 状态：**{reg.get('name', '')}**"
             + (f"（{reg.get('substate')}）" if reg.get("substate") else "")
             + f"　｜　置信度 {reg.get('confidence', '—')}/100"
             + f"　｜　生成于 {payload.get('generated_at', '')}")
    L.append("")
    L.append("## 一、一句话结论")
    L.append("")
    L.append(nar.get("summary") or "（暂无）")
    L.append("")

    L.append("## 二、状态坐标")
    L.append("")
    L.append("| 轴 | 读数 | 含义 |")
    L.append("| --- | --- | --- |")
    L.append(f"| 趋势强度 | {_fmt(reg.get('trend_axis'))} | "
             f"{reg.get('trend_label', '—')} |")
    L.append(f"| 风险偏好 | {_fmt(reg.get('risk_axis'))} | "
             f"{reg.get('risk_label', '—')} |")
    L.append("")
    if nar.get("state_read"):
        L.append(nar["state_read"])
        L.append("")

    L.append("## 三、七维证据")
    L.append("")
    L.append("| 维度 | 读数 | 状态 | 置信度 | 一句话 |")
    L.append("| --- | ---: | --- | ---: | --- |")
    for d in dims:
        L.append(f"| {d['name']} | {_fmt(d.get('score'))} | {d.get('status', '')} | "
                 f"{round((d.get('confidence') or 0) * 100)}% | {d.get('headline', '')} |")
    L.append("")

    for d in dims:
        L.append(f"### {d['name']}　`{d.get('status', '')}`")
        L.append("")
        if d.get("headline"):
            L.append(f"**{d['headline']}**")
            L.append("")
        facts = d.get("facts") or []
        if facts:
            L.append("| 指标 | 读数 | 说明 | 来源 |")
            L.append("| --- | ---: | --- | --- |")
            for f in facts:
                L.append(f"| {f['label']} | {_fmt(f.get('value'), f.get('unit') or '')} | "
                         f"{f.get('note') or ''} | {f.get('source') or ''} |")
            L.append("")
        for s in d.get("signals") or []:
            L.append(f"- {s['text']}")
        if d.get("signals"):
            L.append("")
        if d.get("caliber"):
            L.append(f"> 口径：{d['caliber']}")
            L.append("")
        if d.get("uncertainty"):
            L.append("> 局限：" + "；".join(d["uncertainty"]))
            L.append("")

    L.append("## 四、主要矛盾")
    L.append("")
    cs = payload.get("contradictions") or []
    if not cs:
        L.append("七维方向一致，未检测到显著背离。")
    for c in cs:
        L.append(f"### {c['title']}")
        L.append("")
        L.append(c["text"])
        L.append("")
        L.append(f"- 强度：{_fmt(c.get('strength'))}")
        L.append(f"- 检验方式：{c.get('check', '')}")
        L.append("")

    L.append("## 五、判断可能改变的条件")
    L.append("")
    L.append("| 条件 | 当前 | 阈值 | 距离 | 状态 |")
    L.append("| --- | ---: | --- | ---: | --- |")
    for c in payload.get("conditions") or []:
        d = c.get("distance_pct")
        L.append(f"| {c['text']} | {_fmt(c.get('current'), c.get('unit') or '')} | "
                 f"{c.get('comparator', '')} {_fmt(c.get('threshold'), c.get('unit') or '')} | "
                 f"{_dist(c)} | "
                 f"{'已触发' if c.get('triggered') else '未触发'} |")
    L.append("")

    L.append("## 六、数据与口径")
    L.append("")
    dh = payload.get("data_health") or {}
    L.append(f"- 覆盖标的：{dh.get('market_rows', '—')} 只")
    L.append(f"- 取数成功率：{round((dh.get('ok_ratio') or 0) * 100)}%")
    prov = dh.get("provenance") or {}
    if prov:
        L.append("- 数据源：" + "、".join(f"{k}={v}" for k, v in prov.items()))
    errs = dh.get("errors") or []
    if errs:
        L.append(f"- 取数异常：{len(errs)} 条")
        for e in errs[:6]:
            L.append(f"  - {e}")
    L.append("")
    L.append("---")
    L.append("")
    L.append(LEGAL_FOOTER)
    # 导出物会被转发出去，是合规风险最高的一份文本，必须与页面走同一道审查。
    md, _removed = scrub("\n".join(L))
    return md + "\n"
