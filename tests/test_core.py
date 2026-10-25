# -*- coding: utf-8 -*-
"""核心单元测试：计算基元、状态机、合规、追问、数字校验、报告渲染。

运行：
    python -m unittest discover tests -v
离线、无网络、无第三方依赖——这是本产品可被复核的前提。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import indicators as ind          # noqa: E402
from app import compliance                 # noqa: E402
from app.evidence import Dimension, Fact   # noqa: E402
from app.llm import verify_numbers         # noqa: E402
from app.qa import answer                  # noqa: E402
from app.regime import (                   # noqa: E402
    classify, contradictions, state_triggers, switch_conditions, _cond,
)
from app.report import render_markdown     # noqa: E402


# --------------------------------------------------------------------------
class TestIndicators(unittest.TestCase):
    def test_ma_basic_and_edge(self):
        self.assertEqual(ind.ma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(ind.ma([1, 2], 3))       # 样本不足
        self.assertIsNone(ind.ma([], 5))
        self.assertIsNone(ind.ma([1, 2, 3], 0))    # n 非法不能除零

    def test_ret_needs_enough_points(self):
        self.assertAlmostEqual(ind.ret([100, 110], 1), 0.1)
        self.assertIsNone(ind.ret([100], 1))
        self.assertIsNone(ind.ret([0, 10], 1))     # 基数为 0 不计算

    def test_percentile_rank(self):
        self.assertEqual(ind.percentile_rank([1, 2, 3, 4], 4), 0.75)
        self.assertIsNone(ind.percentile_rank([], 1))

    def test_std_single_value(self):
        self.assertEqual(ind.std([5]), 0.0)

    def test_squash_is_bounded_and_monotonic(self):
        self.assertLess(ind.squash(-50), -0.99)
        self.assertGreater(ind.squash(50), 0.99)
        self.assertLess(ind.squash(1), ind.squash(2))

    def test_safe_div(self):
        self.assertEqual(ind.safe_div(1, 2), 0.5)
        self.assertIsNone(ind.safe_div(1, 0))
        self.assertIsNone(ind.safe_div(None, 2))

    def test_spearman_stability(self):
        self.assertAlmostEqual(ind.spearman([1, 2, 3, 4], [1, 2, 3, 4]), 1.0)
        self.assertAlmostEqual(ind.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(ind.spearman([1, 2], [1, 2]))   # 样本太少

    def test_median(self):
        self.assertEqual(ind.median([3, 1, 2]), 2)
        self.assertEqual(ind.median([4, 1, 3, 2]), 2.5)
        self.assertIsNone(ind.median([]))


# --------------------------------------------------------------------------
NAMES = {"structure": "行情结构", "breadth": "市场宽度", "rotation": "风格轮动",
         "valuation": "估值", "liquidity": "流动性", "sentiment": "情绪",
         "events": "事件"}


def _dim(id_, axis, score, weight=0.15, raw=None):
    return Dimension(id=id_, name=NAMES[id_], question="", axis=axis, weight=weight,
                     score=score, raw=raw or {})


def _book(**scores):
    """构造一组维度：默认全部中性，只覆盖传入的。"""
    axes = {"structure": "trend", "breadth": "trend", "rotation": "trend",
            "valuation": "risk", "liquidity": "risk", "sentiment": "risk",
            "events": "risk"}
    return [_dim(k, axes[k], scores.get(k, 0.0)) for k in axes]


class TestRegime(unittest.TestCase):
    def test_resonance_up(self):
        book = _book(structure=0.8, breadth=0.7, rotation=0.6,
                     valuation=-0.5, liquidity=0.6, sentiment=0.7)
        reg = classify(book, vol_pct=0.3)
        self.assertEqual(reg["code"], "resonance_up")
        self.assertGreater(reg["trend_axis"], 0.15)
        self.assertGreater(reg["risk_axis"], 0.15)

    def test_resonance_down(self):
        book = _book(structure=-0.8, breadth=-0.7, rotation=-0.6,
                     valuation=0.4, liquidity=-0.6, sentiment=-0.7)
        reg = classify(book, vol_pct=0.4)
        self.assertEqual(reg["code"], "resonance_down")

    def test_range_split_when_both_axes_neutral(self):
        """趋势轴与风险轴都落在中性带内 —— 共识缺失，就是震荡分化。"""
        book = _book(structure=0.05, breadth=-0.04, rotation=0.02,
                     valuation=0.03, liquidity=-0.05, sentiment=0.04)
        reg = classify(book, vol_pct=0.45)
        self.assertEqual(reg["code"], "range_split")

    def test_bottom_probe_when_trend_weak_risk_strong(self):
        """趋势轴弱、风险偏好强：不是普跌，是超跌后的试探性修复。"""
        book = _book(structure=-0.6, breadth=-0.5, rotation=-0.4,
                     liquidity=0.4, sentiment=0.3, valuation=0.2)
        reg = classify(book, vol_pct=0.5)
        self.assertEqual(reg["code"], "bottom_probe")

    def test_disorder_on_extreme_volatility(self):
        book = _book(structure=0.90, breadth=-0.90, rotation=0.60,
                     valuation=-0.80, liquidity=0.85, sentiment=-0.75)
        reg = classify(book, vol_pct=0.95)
        self.assertEqual(reg["code"], "disorder")
        # 离开该状态需要看到的���件必须与状态本身匹配（此处指向波动率回落）
        self.assertTrue(any("波动率" in t for t in state_triggers(reg)))

    def test_confidence_bounded(self):
        reg = classify(_book(structure=0.3, breadth=0.3), vol_pct=0.5)
        self.assertGreaterEqual(reg["confidence"], 0)
        self.assertLessEqual(reg["confidence"], 100)

    def test_missing_scores_do_not_crash(self):
        book = [_dim("structure", "trend", None), _dim("breadth", "trend", None),
                _dim("valuation", "risk", None)]
        reg = classify(book, vol_pct=None)
        self.assertIn("name", reg)
        self.assertIn("code", reg)

    def test_contradictions_detect_opposite_axes(self):
        book = _book(structure=-0.8, breadth=0.8)
        cs = contradictions(book)
        self.assertTrue(cs)
        self.assertLessEqual(len(cs), 2)
        for c in cs:
            self.assertIn("title", c)
            self.assertIn("text", c)
            self.assertIn("check", c)

    def test_no_contradiction_when_aligned(self):
        self.assertEqual(contradictions(_book(structure=0.5, breadth=0.5)), [])


class TestConditions(unittest.TestCase):
    def test_zero_threshold_uses_percentage_points(self):
        """阈值本身为 0 时不能做除法——曾出现 distance=None 的显示空洞。"""
        c = _cond("t", "测试", -4.68, 0.0, "%", "<=", "src", "why")
        self.assertEqual(c["distance_pct"], 4.68)
        self.assertTrue(c["distance_is_pp"])
        self.assertTrue(c["triggered"])

    def test_percent_unit_uses_percentage_points(self):
        """单位是 % 的条件，距离一律用 pp：「差 30.3pp」读得懂，「差 60.6%」会被误读。"""
        c = _cond("t", "测试", 80.3, 50.0, "%", "<=", "src", "why")
        self.assertAlmostEqual(c["distance_pct"], 30.3)
        self.assertTrue(c["distance_is_pp"])

    def test_non_percent_unit_keeps_relative_distance(self):
        c = _cond("t", "测试", 2.0, 1.0, "倍", "<=", "src", "why")
        self.assertAlmostEqual(c["distance_pct"], 100.0)
        self.assertFalse(c["distance_is_pp"])
        self.assertFalse(c["triggered"])

    def test_missing_values_yield_none(self):
        c = _cond("t", "测试", None, 55.0, "%", ">=", "src", "why")
        self.assertIsNone(c["distance_pct"])
        self.assertFalse(c["triggered"])

    def test_switch_conditions_survive_empty_snapshot(self):
        class Snap:
            indices = {}
            market = []
            margin = []
        out = switch_conditions({"code": "range_split"}, _book(), Snap())
        self.assertIsInstance(out, list)


# --------------------------------------------------------------------------
class TestCompliance(unittest.TestCase):
    def test_detects_direction_prediction(self):
        r = compliance.audit("预计下周将上涨 5%")
        self.assertFalse(r["ok"])
        self.assertTrue(r["violations"])

    def test_detects_position_advice(self):
        r = compliance.audit("建议仓位提升至 7 成")
        self.assertFalse(r["ok"])

    def test_clean_text_passes(self):
        r = compliance.audit("当前市场处于震荡分化，宽度强于结构。")
        self.assertTrue(r["ok"])

    def test_scrub_neutralizes(self):
        text, removed = compliance.scrub("预计下周上涨")
        self.assertTrue(removed)
        self.assertTrue(compliance.audit(text)["ok"])

    def test_legal_footer_present(self):
        self.assertIn("不构成投资建议", compliance.LEGAL_FOOTER)


# --------------------------------------------------------------------------
def _payload():
    book = _book(structure=-0.84, breadth=0.90, rotation=0.08,
                 valuation=0.11, liquidity=-0.02, sentiment=0.08)
    for d in book:
        d.headline = f"{d.name} 头条"
        d.status = "中性"
        d.confidence = 0.9
        d.facts = [Fact("示例指标", 12.3, "%", "test")]
        d.signals = [{"text": "示例信号", "direction": "up", "strength": 0.5}]
        d.caliber = "示例口径"
        d.uncertainty = ["示例局限"]
    return {
        "version": "1.0.0", "as_of": "2026-10-23", "generated_at": "2026-10-23 18:00:00",
        "elapsed_ms": 1,
        "regime": {"code": "range_split", "name": "震荡分化", "substate": "超跌修复初期",
                   "trend_axis": -0.05, "risk_axis": 0.04, "confidence": 76.7,
                   "trend_label": "中性", "risk_label": "中性"},
        "dimensions": [d.to_dict() for d in book],
        "contradictions": [{"title": "结构偏弱 × 宽度偏强", "text": "说明文字",
                            "strength": 0.86, "check": "检查方式",
                            "dims": ["structure", "breadth"],
                            "detail": [{"dim": "行情结构", "score": -0.84, "status": "明显偏弱", "text": "a"},
                                       {"dim": "市场宽度", "score": 0.90, "status": "明显偏强", "text": "b"}]}],
        "conditions": [_cond("breadth_50", "上涨家数占比", 80.3, 50.0, "%", "<=",
                             "market_snapshot", "宽度跌破 50% 意味着多数个股在跌")],
        "triggers": ["趋势轴与风险偏好同时越过 ±0.15"],
        "narrative": {"headline": "震荡分化", "summary": "摘要", "state_read": "解读",
                      "contradiction": "矛盾", "conditions": "条件", "watch": [],
                      "caveats": [], "dimension_notes": {}},
        "compliance": {"ok": True, "violations": []},
        "data_health": {"catalog": [], "provenance": {"index_kline": "tencent"},
                        "fallbacks": [], "ok_ratio": 0.9, "errors": [],
                        "fetch_log": [], "market_rows": 5223},
        "history": [], "series": {"indices": {}, "sectors": []}, "events": [],
        "disclaimer": compliance.LEGAL_FOOTER,
    }


class TestReport(unittest.TestCase):
    def test_render_markdown_contains_key_sections(self):
        md = render_markdown(_payload())
        for kw in ("市场大势研判", "七维证据", "主要矛盾", "判断可能改变的条件",
                   "数据与口径", "不构成投资建议"):
            self.assertIn(kw, md)

    def test_render_survives_empty_payload(self):
        md = render_markdown({"regime": {}, "dimensions": [], "data_health": {}})
        self.assertIn("市场大势研判", md)


# --------------------------------------------------------------------------
class TestQA(unittest.TestCase):
    def setUp(self):
        self.p = _payload()

    def test_refuses_trade_question(self):
        for q in ("现在能买吗", "推荐买什么", "仓位该怎么调"):
            r = answer(q, self.p)
            self.assertTrue(r.get("refused"), q)
            self.assertIn("不", r["answer"])

    def test_answers_conditions_question(self):
        r = answer("什么条件下需要改变判断？", self.p)
        self.assertFalse(r.get("refused"))
        self.assertIn("震荡分化", r["answer"])
        self.assertTrue(r["citations"])

    def test_answers_dimension_question_with_citations(self):
        r = answer("市场宽度现在是什么水平？", self.p)
        self.assertIn("市场宽度", r["answer"])
        self.assertEqual(r["citations"][0]["dim"], "市场宽度")

    def test_answers_contradiction_question(self):
        r = answer("当前的主要矛盾是什么？", self.p)
        self.assertIn("结构偏弱", r["answer"])

    def test_unknown_question_falls_back_to_overview(self):
        r = answer("随便说点什么", self.p)
        self.assertIn("answer", r)
        self.assertTrue(r["answer"])


# --------------------------------------------------------------------------
class TestVerifyNumbers(unittest.TestCase):
    def setUp(self):
        self.p = _payload()

    def test_accepts_numbers_present_in_evidence(self):
        self.assertEqual(verify_numbers("上涨家数占比 80.3%", self.p), [])

    def test_flags_fabricated_number(self):
        bad = verify_numbers("上证指数将到达 4200 点", self.p)
        self.assertTrue(bad)
        self.assertIn("4200", bad)

    def test_tolerance_absorbs_rounding(self):
        self.assertEqual(verify_numbers("约 80 个百分点", self.p), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
