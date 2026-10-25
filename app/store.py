# -*- coding: utf-8 -*-
"""本地快照仓库：让"今天的结论"在明天仍然可复现。

三个目录：
  data/cache/      接口原始响应（带 TTL，自动过期）
  data/snapshots/  每日研判结果全文（payload.json），用于归档与历史分位
  data/raw/        每日精简原始数据（不含全市场逐股明细，控制体积）

为什么估值维度需要它：全市场 PE 的历史分位无法从单个接口直接拿到，
只能靠「每个交易日存一份」自己攒。首日运行时没有分位，产品会明确说"样本不足"，
而不是编一个十年分位出来。
"""

from __future__ import annotations

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")
RAW_DIR = os.path.join(ROOT, "data", "raw")


def _ensure(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def save_payload(payload: dict, date: str) -> str:
    _ensure(SNAP_DIR)
    path = os.path.join(SNAP_DIR, f"{date}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def save_raw(snap_dict: dict, date: str) -> str:
    """精简快照：去掉逐股明细，只保留聚合与图表需要的序列。"""
    _ensure(RAW_DIR)
    slim = dict(snap_dict)
    slim["market_summary"] = {
        "count": len(slim.get("market") or []),
        "note": "逐股明细未归档，需要时请重新取数",
    }
    slim.pop("market", None)
    path = os.path.join(RAW_DIR, f"{date}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(slim, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def history(limit: int = 250) -> list[dict]:
    """按日期升序返回历史快照的估值与宽度摘要。"""
    _ensure(SNAP_DIR)
    rows = []
    for name in sorted(os.listdir(SNAP_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(SNAP_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception:
            continue
        dims = {d["id"]: d for d in blob.get("dimensions") or []}
        val = (dims.get("valuation") or {}).get("raw") or {}
        br = (dims.get("breadth") or {}).get("raw") or {}
        reg = blob.get("regime") or {}
        rows.append({
            "date": blob.get("as_of") or name[:10],
            "median_pe": val.get("median_pe"),
            "median_pb": val.get("median_pb"),
            "pb_below_1": val.get("pb_below_1"),
            "up_ratio": br.get("up_ratio"),
            "regime": reg.get("name"),
            "trend_axis": reg.get("trend_axis"),
            "risk_axis": reg.get("risk_axis"),
            "confidence": reg.get("confidence"),
        })
    rows.sort(key=lambda x: x["date"])
    return rows[-limit:]


def list_snapshots() -> list[str]:
    _ensure(SNAP_DIR)
    return sorted(n[:-5] for n in os.listdir(SNAP_DIR) if n.endswith(".json"))


def load_payload(date: str) -> dict | None:
    path = os.path.join(SNAP_DIR, f"{date}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def latest_payload() -> dict | None:
    dates = list_snapshots()
    return load_payload(dates[-1]) if dates else None
