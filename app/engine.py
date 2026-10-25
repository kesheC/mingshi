# -*- coding: utf-8 -*-
"""编排层：取数 → 证据 → 状态 → 矛盾 → 条件 → 叙述 → 合规审计 → 落盘。

这是唯一被 HTTP 层调用的入口。分开的原因是：测试可以直接调 ``analyze``，
不依赖任何服务器，也不需要网络（可以喂离线快照）。
"""

from __future__ import annotations

import time
from typing import Any

from . import store
from .compliance import audit, audit_payload, scrub_tree
from .llm import enhance_narrative
from .evidence import build_all
from .fetch import collect
from .narrate import build_narrative
from .regime import classify, contradictions, state_triggers, switch_conditions

VERSION = "1.0.0"


def _vol_percentile(dims) -> float | None:
    for d in dims:
        if d.id == "structure":
            vp = (d.raw or {}).get("vol_pct") or {}
            if vp:
                return max(vp.values())
    return None


def analyze(snap=None, *, save: bool = True, prefer: str | None = None) -> dict:
    """跑一次完整研判，返回可直接序列化给前端的 payload。"""
    t0 = time.time()
    if snap is None:
        snap = collect(prefer=prefer)

    hist = store.history()
    dims = build_all(snap, history=hist)
    vol_pct = _vol_percentile(dims)

    regime = classify(dims, vol_pct)
    contra = contradictions(dims)
    conds = switch_conditions(regime, dims, snap)
    triggers = state_triggers(regime)

    narrative = build_narrative(regime, dims, contra, conds, snap)

    # 合规审查覆盖面：叙述、矛盾、条件、状态提示全部要过。
    # 只审 summary 曾经漏掉 conditions 与 contradictions 里的文本，而那两处
    # 恰恰是最容易被写成"预测"的地方。
    guard = audit_payload({
        "narrative": narrative,
        "contradictions": contra,
        "conditions": conds,
        "triggers": triggers,
        "regime": {k: v for k, v in regime.items() if isinstance(v, str)},
    })
    narrative, scrubbed_n = scrub_tree(narrative)
    contra, scrubbed_c = scrub_tree(contra)
    conds, scrubbed_d = scrub_tree(conds)

    # LLM 只重写一句话结论；模板永远是底座（无 key 时连调用都不会发生）。
    narrative = enhance_narrative(narrative, {
        "regime": regime,
        "dimensions": [d.to_dict() for d in dims],
        "contradictions": contra,
        "conditions": conds,
    })
    if scrubbed_n or scrubbed_c or scrubbed_d:
        guard["scrubbed"] = (scrubbed_n + scrubbed_c + scrubbed_d)[:10]
        guard["note"] = "命中内容已替换为中性表述，原文不对外输出"

    payload = {
        "version": VERSION,
        "as_of": snap.as_of,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "regime": regime,
        "dimensions": [d.to_dict() for d in dims],
        "contradictions": contra,
        "conditions": conds,
        "triggers": triggers,
        "narrative": narrative,
        "compliance": guard,
        "data_health": {
            "catalog": snap.providers,
            "provenance": {k: v for k, v in (snap.provenance or {}).items()
                           if k not in ("fallbacks", "market_rows")},
            "fallbacks": (snap.provenance or {}).get("fallbacks") or [],
            "ok_ratio": round(snap.ok_ratio, 3),
            "errors": snap.errors,
            "fetch_log": snap.fetch_log,
            "market_rows": len(snap.market),
        },
        "history": store.history(),
        "series": _series(snap),
        "events": (snap.news or [])[:20],
        "disclaimer": ("本报告是基于公开行情数据的市场状态描述与条件推演，"
                       "不构成任何投资建议，不预测指数涨跌，不提供仓位与买卖建议。"),
    }

    if save:
        try:
            store.save_payload(payload, snap.as_of)
            store.save_raw(snap.to_dict(), snap.as_of)
        except Exception as exc:  # noqa: BLE001 - 归档失败不该拖垮研判
            payload.setdefault("warnings", []).append(f"归档失败：{exc}")

    return payload


def _series(snap) -> dict:
    """给前端画图用的降采样序列。"""
    out: dict[str, Any] = {"indices": {}, "sectors": []}
    for secid, item in (snap.indices or {}).items():
        kl = item.get("kline") or []
        if not kl:
            continue
        tail = kl[-120:]
        out["indices"][secid] = {
            "name": item.get("name"), "role": item.get("role"),
            "dates": [r["date"] for r in tail],
            "close": [r["close"] for r in tail],
        }
    for s in snap.sectors or []:
        kl = s.get("kline") or []
        if len(kl) < 21:
            continue
        out["sectors"].append({
            "name": s["name"],
            "r5": round((kl[-1]["close"] / kl[-6]["close"] - 1) * 100, 2) if len(kl) >= 6 else None,
            "r20": round((kl[-1]["close"] / kl[-21]["close"] - 1) * 100, 2),
            "series": [r["close"] for r in kl[-60:]],
        })
    if snap.margin:
        mg = snap.margin[-60:]
        out["margin"] = {"dates": [m["date"] for m in mg],
                         "rzye": [round((m.get("rzye") or 0) / 1e8, 0) for m in mg]}
    return out
