# -*- coding: utf-8 -*-
"""可选的 LLM 层。

产品的主线是**确定性研判引擎**：七维读数、状态、阈值、矛盾全部由可复现的规则
计算得到，不依赖任何模型。LLM 只做两件锦上添花的事：

1. 把结构化的矛盾/条件组织成更自然的叙述（`engine.narrate` 的 ``engine="llm"`` 分支）；
2. 追问时用更像是研究员的语气组织已经给到的证据。

两条硬约束：
* **无 key 自动降级**——没有配置就静默走模板，绝不因为缺少模型而报错或留空；
* **数字必须可溯源**——LLM 回复里出现的每个数字都要能在证据包里找到，
  找不到就整段丢弃回退到检索式回答（`verify_numbers`）。

配置方式（任一）：
    export LLM_API_KEY=sk-xxx
    export LLM_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容端点
    export LLM_MODEL=deepseek-chat
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


class LLMClient:
    """极简 OpenAI 兼容 chat completions 客户端。"""

    def __init__(self, timeout: float = 25.0) -> None:
        self.api_key = _env("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
        self.base = (_env("LLM_BASE_URL", "OPENAI_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.model = _env("LLM_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    @classmethod
    def available(cls) -> bool:
        return bool(_env("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"))

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, temperature: float = 0.2,
             max_tokens: int = 700) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 LLM_API_KEY")
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"响应格式异常: {data!r}"[:300]) from exc


# ----------------------------------------------------------------------
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _evidence_numbers(payload: dict) -> set[str]:
    nums: set[str] = set()

    def harvest(v) -> None:
        if isinstance(v, (int, float)):
            nums.add(f"{v:g}")
            nums.add(f"{abs(v):g}")
        elif isinstance(v, str):
            nums.update(_NUM.findall(v))

    def walk(node, depth=0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("facts", "value", "headline", "text", "note", "summary",
                         "current", "threshold", "conditions", "contradictions",
                         "dimensions", "narrative", "regime", "breadth", "valuation",
                         "flows", "sentiment"):
                    harvest(v) if not isinstance(v, (dict, list)) else walk(v, depth + 1)
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node[:40]:
                walk(item, depth + 1)

    walk(payload)
    return nums


NARRATE_SYSTEM = (
    "你是 A 股市场状态研判的写作助手。你会收到一份已经算好的结构化研判（状态、证据读数、"
    "矛盾、条件）。你的任务**只是把它写成 120~200 字的中文段落**，要求：\n"
    "1. 只能使用给定证据中的数字，禁止引入任何新数字（包括点数、目标位、百分比）；\n"
    "2. 禁止预测涨跌、禁止给出买卖/仓位建议、禁止使用「必涨/必跌/目标位/建议仓位」等词；\n"
    "3. 必须点名至少一个维度的具体读数，并说明它与另一维度的关系；\n"
    "4. 结尾用一句话说明「什么条件下这个判断会变」；\n"
    "5. 不要写标题、不要分点，只输出正文段落。"
)


def enhance_narrative(narrative: dict, payload: dict) -> dict:
    """用 LLM 重写「一句话结论」。失败/越界/无 key 时静默返回模板版本。

    注意：这里只重写 summary 一个字段。状态、读数、条件、矛盾都不交给模型——
    它们必须可复现。模型的职责是"把已经确定的东西写顺"，不是"决定它是什么"。
    """
    if not LLMClient.available():
        return narrative
    client = LLMClient()
    evidence = {
        "状态": payload.get("regime", {}).get("name"),
        "子状态": payload.get("regime", {}).get("substate"),
        "置信度": payload.get("regime", {}).get("confidence"),
        "维度": [{"名称": d.get("name"), "读数": d.get("score"),
                  "状态": d.get("status"), "要点": d.get("headline"),
                  "数据": [{f.get("label"): f.get("value")}
                           for f in (d.get("facts") or [])[:3]]}
                 for d in payload.get("dimensions", [])],
        "矛盾": [c.get("title") for c in payload.get("contradictions", [])],
        "条件": [{"条件": c.get("text"), "当前": c.get("current"),
                  "阈值": c.get("threshold"), "单位": c.get("unit")}
                 for c in payload.get("conditions", [])[:6]],
        "模板草稿": narrative.get("summary", ""),
    }
    try:
        text = client.chat(NARRATE_SYSTEM,
                           json.dumps(evidence, ensure_ascii=False),
                           temperature=0.3, max_tokens=500)
    except Exception as exc:  # noqa: BLE001
        out = dict(narrative)
        out.setdefault("notes", []).append(f"LLM 叙述失败，已回退模板：{exc}")
        return out

    bad = verify_numbers(text, payload)
    if bad:
        out = dict(narrative)
        out.setdefault("notes", []).append(
            "LLM 叙述出现证据中不存在的数字（" + "、".join(bad[:3]) + "），已回退模板。")
        return out
    out = dict(narrative)
    out["summary"] = text
    out["engine"] = f"llm:{client.model}"
    out.setdefault("notes", []).append("一句话结论由模型改写，数字已通过存在性校验。")
    return out


def verify_numbers(text: str, payload: dict, tol: float = 0.51) -> list[str]:
    """返回 LLM 文本里「证据中找不到」的数字片段。

    只做**存在性**校验（不要求语义一致）：模型可以引用 65.8 或 65.8%，但不能凭空
    说出 3800 点这种证据里没有的数字。容差用来吸收四舍五入差异。
    """
    known_raw = _evidence_numbers(payload)
    known = {s.lstrip("+-") for s in known_raw}
    known_f = []
    for s in known:
        try:
            known_f.append(float(s))
        except ValueError:
            continue

    bad: list[str] = []
    for m in _NUM.finditer(text):
        tok = m.group(0)
        if tok.startswith("-") and len(tok) == 2:  # 列表符号，不是数字
            continue
        if tok in known or tok.lstrip("+-") in known:
            continue
        try:
            val = float(tok)
        except ValueError:
            continue
        if 0 <= val <= 12 and tok.isdigit():  # 序号、条数、维度下标
            continue
        if any(abs(val - k) <= tol for k in known_f):
            continue
        bad.append(tok)
    return bad[:5]
