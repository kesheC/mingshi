/*!
 * web/research.js — 静态快照的本地追问引擎（零依赖、零网络、零模型）
 *
 * 用途：静态导出站（site/）没有后端，追问不能发请求，也不能"编"一个答案。
 * 这里用**模板 + 快照数据**做证据推演：
 *   - 每个回答里的数字、名称、时点都从 payload 里取，绝不硬编码当期数值；
 *   - 每条回答附带 citations，与页面上的证据条目一一对应，点击可高亮；
 *   - 交易类问题直接拒答；超出快照能力的问题明确说"不知道"，并给出能力范围。
 *
 * 该文件同时被浏览器（window.MINGSHI_RESEARCH）与 Node 校验脚本
 * （scripts/check_static.mjs，vm 沙箱）加载，因此不触碰 DOM、不读全局配置。
 */
(function (global) {
  "use strict";

  var ENGINE = "static";
  var NOTE_STATIC = "本地证据推演：仅使用静态快照内的数据，未联网、未调用模型。";

  /* ---------------------------------------------------------------- 工具 */

  function isNum(v) {
    return typeof v === "number" && isFinite(v);
  }

  function num(v, digits) {
    if (typeof v === "string" && v.trim() !== "" && isFinite(Number(v))) v = Number(v);
    if (!isNum(v)) return "—";
    var d = typeof digits === "number" ? digits : 2;
    return v.toFixed(d).replace(/\.?0+$/, function (m) {
      return m.indexOf(".") === 0 ? "" : m;
    });
  }

  function signed(v, digits, unit) {
    if (typeof v === "string" && v.trim() !== "" && isFinite(Number(v))) v = Number(v);
    if (!isNum(v)) return "—";
    var d = typeof digits === "number" ? digits : 2;
    return (v >= 0 ? "+" : "") + num(v, d) + (unit || "");
  }

  function pct(v, digits) {
    if (!isNum(v)) return "—";
    return (v * 100).toFixed(typeof digits === "number" ? digits : 0) + "%";
  }

  function fmt(v, unit) {
    if (v === null || v === undefined || v === "") return "—";
    var s;
    if (isNum(v)) s = Math.abs(v) >= 1000 ? v.toLocaleString("zh-CN") : num(v);
    else s = String(v);
    return unit ? s + (unit === "%" ? "%" : " " + unit) : s;
  }

  function arr(v) {
    return Object.prototype.toString.call(v) === "[object Array]" ? v : [];
  }

  function obj(v) {
    return v && typeof v === "object" && Object.prototype.toString.call(v) !== "[object Array]" ? v : {};
  }

  function str(v) {
    return typeof v === "string" ? v : "";
  }

  function first(list, n) {
    return arr(list).slice(0, typeof n === "number" ? n : 5);
  }

  function uniq(list) {
    var seen = {}, out = [];
    for (var i = 0; i < list.length; i++) {
      if (!seen[list[i]]) { seen[list[i]] = 1; out.push(list[i]); }
    }
    return out;
  }

  function lower(s) {
    return str(s).toLowerCase();
  }

  function any(text, words) {
    for (var i = 0; i < words.length; i++) {
      if (text.indexOf(words[i]) >= 0) return words[i];
    }
    return null;
  }

  function cite(dim, dimId, label, value, unit, source, asOf) {
    return {
      dim: dim || "",
      dim_id: dimId || "",
      label: label || "",
      value: value === undefined ? null : value,
      unit: unit || "",
      source: source || "",
      as_of: asOf || null
    };
  }

  function regimeOf(p) {
    return obj(p && p.regime);
  }

  function dimsOf(p) {
    return arr(p && p.dimensions);
  }

  function planOf(p) {
    return obj(p && p.plan);
  }

  function gateOf(p) {
    var g = obj(planOf(p).gate);
    if (g.level) return g;
    var rg = obj(regimeOf(p).gate);
    return rg.level ? rg : g;
  }

  function asOfOf(p) {
    return str(p && p.as_of) || "";
  }

  function dimsById(p) {
    var map = {}, dims = dimsOf(p);
    for (var i = 0; i < dims.length; i++) {
      var d = obj(dims[i]);
      if (d.id) map[d.id] = d;
    }
    return map;
  }

  function factCitations(p, dim, limit) {
    var out = [], facts = arr(dim.facts);
    var asOf = asOfOf(p);
    for (var i = 0; i < facts.length && out.length < (limit || 5); i++) {
      var f = obj(facts[i]);
      if (!f.label) continue;
      out.push(cite(dim.name || dim.id || "证据", dim.id || "", f.label,
        f.value, f.unit || "", f.source || "", f.as_of || dim.as_of || asOf));
    }
    return out;
  }

  function dimCitations(p, dims, limit) {
    var out = [];
    for (var i = 0; i < dims.length && out.length < (limit || 4); i++) {
      var d = obj(dims[i]);
      if (!d.id && !d.name) continue;
      out.push(cite(d.name || d.id, d.id || "", (d.name || d.id) + " 读数",
        isNum(d.score) ? d.score : null, "", "七维证据合成", d.as_of || asOfOf(p)));
    }
    return out;
  }

  /* ------------------------------------------------------------ 意图识别 */

  var TRADE_WORDS = [
    "能买", "可以买", "该买", "买什么", "买哪", "卖不卖", "要卖", "推荐", "仓位",
    "加仓", "减仓", "清仓", "建仓", "止损", "止盈", "抄底", "入场", "出局",
    "目标价", "看多", "看空", "买入", "卖出", "持有", "梭哈", "满仓", "空仓"
  ];

  var INTENT_WORDS = {
    conditions: ["条件", "什么情况", "会变", "改变判断", "什么时候", "触发", "证伪", "重估", "失效"],
    contradiction: ["矛盾", "背离", "冲突", "不一致", "打架"],
    epistemic: ["认识论", "事实", "归纳", "判断", "口径", "方法论", "怎么算", "可信", "证据", "知道", "凭什么", "三层"],
    quality: ["数据质量", "质量", "数据来源", "来源", "可靠", "缺失", "置信度", "闸门", "降级", "备源", "覆盖率", "新鲜度"],
    history: ["历史", "过去", "阶段", "似曾相识", "以前", "回溯", "时间轴", "曾经"],
    style: ["风格", "大小盘", "大盘", "小盘", "价值", "成长", "红利", "沪深300", "中证1000"],
    risk: ["风险", "脆弱", "波动", "回撤", "隐患", "担心"],
    industry: ["行业", "板块", "赛道", "领涨", "领跌", "轮动", "景气"]
  };

  var DIM_ALIASES = {
    structure: ["结构", "趋势", "均线", "价格", "形态", "破位", "技术面"],
    breadth: ["宽度", "广度", "赚钱效应", "涨跌家数", "个股", "中位数", "普涨", "普跌"],
    rotation: ["轮动", "风格", "切换", "主线", "持续性"],
    valuation: ["估值", "pe", "pb", "市盈率", "市净率", "破净", "便宜", "贵"],
    liquidity: ["流动性", "成交", "量能", "两融", "资金", "杠杆", "缩量", "放量"],
    sentiment: ["情绪", "涨停", "跌停", "连板", "封板", "热度", "人气"],
    events: ["事件", "新闻", "消息", "公告", "政策", "题材"]
  };

  var OVERVIEW_WORDS = ["什么状态", "怎么样", "如何", "市场", "大势", "研判", "总结", "概况", "今天", "当前", "现在", "情况", "一句话"];

  function classify(question, payload) {
    var q = lower(question);
    if (!q.trim()) return "unknown";
    if (any(q, TRADE_WORDS)) return "refuse";

    var dims = dimsOf(payload), i;
    // 直接点名维度（如「行情结构」「市场宽度」）优先于泛化的「市场怎么样」
    for (i = 0; i < dims.length; i++) {
      var name = str(obj(dims[i]).name);
      if (name && q.indexOf(lower(name)) >= 0) return "dimension";
    }
    var order = ["conditions", "contradiction", "epistemic", "quality", "history", "style", "risk", "industry"];
    for (i = 0; i < order.length; i++) {
      var key = order[i];
      if (any(q, INTENT_WORDS[key])) {
        if (key === "history" && any(q, ["类似", "有没有", "阶段"])) return "history";
        return key;
      }
    }
    var ids = Object.keys(DIM_ALIASES);
    for (i = 0; i < ids.length; i++) {
      if (any(q, DIM_ALIASES[ids[i]])) {
        var hit = findDim(payload, ids[i]);
        if (hit) return "dimension";
      }
    }
    if (any(q, OVERVIEW_WORDS)) return "overview";
    return "unknown";
  }

  function findDim(payload, id) {
    var dims = dimsOf(payload);
    for (var i = 0; i < dims.length; i++) {
      if (obj(dims[i]).id === id) return obj(dims[i]);
    }
    return null;
  }

  function matchDimByQuestion(payload, question) {
    var q = lower(question), dims = dimsOf(payload);
    var best = null, bestLen = 0, i;
    for (i = 0; i < dims.length; i++) {
      var d = obj(dims[i]), name = str(d.name);
      if (name && q.indexOf(lower(name)) >= 0 && name.length > bestLen) {
        best = d; bestLen = name.length;
      }
    }
    if (best) return best;
    var ids = Object.keys(DIM_ALIASES);
    for (i = 0; i < ids.length; i++) {
      if (any(q, DIM_ALIASES[ids[i]])) {
        var hit = findDim(payload, ids[i]);
        if (hit) return hit;
      }
    }
    return null;
  }

  /* -------------------------------------------------------------- 模板 */

  function base(payload) {
    return {
      ok: true,
      engine: ENGINE,
      intent: "overview",
      answer: "",
      citations: [],
      evidence_used: [],
      followups: [],
      confidence: isNum(regimeOf(payload).confidence) ? regimeOf(payload).confidence : null,
      as_of: asOfOf(payload),
      refused: false,
      notes: [NOTE_STATIC],
      compliance: { ok: true, violations: [], checked_chars: 0, rules: 0, scrubbed: 0 },
      mode: "static"
    };
  }

  function withFollowups(res, payload, question) {
    var sugg = suggestions(payload), out = [];
    for (var i = 0; i < sugg.length && out.length < 4; i++) {
      if (lower(sugg[i]) !== lower(question)) out.push(sugg[i]);
    }
    res.followups = out;
    return res;
  }

  function suggestions(payload) {
    var s = arr(obj(obj(payload).research).suggested);
    var out = [];
    for (var i = 0; i < s.length; i++) {
      if (typeof s[i] === "string" && s[i].trim()) out.push(s[i].trim());
      else if (s[i] && typeof s[i] === "object" && s[i].question) out.push(String(s[i].question));
    }
    if (out.length) return out;
    return [
      "当前市场处于什么状态？",
      "现在的主要矛盾是什么？",
      "什么条件会改变当前判断？",
      "哪些行业在领涨？",
      "大小盘风格谁占优？",
      "历史上有没有类似阶段？",
      "现在的主要风险在哪里？",
      "这次研判的数据质量如何？"
    ];
  }

  /* -- 概览 ------------------------------------------------------------ */
  function answerOverview(payload, question) {
    var res = base(payload), reg = regimeOf(payload), dims = dimsOf(payload), g = gateOf(payload);
    res.intent = "overview";
    var parts = [];
    if (g.level === "insufficient") {
      parts.push("当前研判受限：数据不足（" + (arr(g.missing).join("、") || "关键能力缺失") +
        "），置信度上限 " + fmt(g.confidence_cap) + "% 。");
    }
    var asOf = asOfOf(payload);
    parts.push("截至 " + (asOf || "快照日") + "，七维证据合成后的市场状态是「" + (reg.name || "未知") + "」" +
      (reg.substate ? "（" + reg.substate + "）" : "") + "。" +
      "趋势轴 " + signed(reg.trend_axis) + "（" + (reg.trend_label || "—") + "），" +
      "风险偏好轴 " + signed(reg.risk_axis) + "（" + (reg.risk_label || "—") + "），" +
      "合成置信度 " + fmt(reg.confidence) + "/100。");
    if (reg.meaning) parts.push(str(reg.meaning));
    var narr = obj(payload.narrative);
    if (narr.summary) parts.push("一句话结论：" + str(narr.summary));

    var weak = [], strong = [];
    for (var i = 0; i < dims.length; i++) {
      var d = obj(dims[i]);
      var s = isNum(d.score) ? d.score : null;
      if (s === null) continue;
      if (s <= -0.35) weak.push(d.name + "（" + d.status + "）");
      else if (s >= 0.35) strong.push(d.name + "（" + d.status + "）");
    }
    if (strong.length) parts.push("偏强的维度：" + strong.join("、") + "。");
    if (weak.length) parts.push("偏弱的维度：" + weak.join("、") + "。");
    var cs = arr(payload.contradictions);
    if (cs.length) {
      var c0 = obj(cs[0]);
      parts.push("最需要留意的矛盾是「" + (c0.title || c0.id || "—") + "」" +
        (c0.check ? "；检验方式：" + c0.check : "") + "。");
    }
    var cond = nearestCondition(payload);
    if (cond) {
      parts.push("离当前值最近的条件是「" + cond.text + "」：当前 " +
        fmt(cond.current, cond.unit) + "，阈值 " + (cond.comparator || "") + " " +
        fmt(cond.threshold, cond.unit) + "。");
    }
    if (reg.watch) parts.push(str(reg.watch));
    res.answer = parts.join("\n\n");
    var pick = [dimsById(payload).structure, dimsById(payload).breadth].filter(Boolean);
    res.citations = dimCitations(payload, pick.length ? pick : first(dims, 2), 3);
    res.evidence_used = ["市场状态", "七维证据"].concat(weak.length ? ["偏弱维度"] : []);
    return withFollowups(res, payload, question);
  }

  /* -- 条件 ------------------------------------------------------------ */
  function nearestCondition(payload) {
    var cs = arr(payload.conditions), best = null, bestD = Infinity;
    for (var i = 0; i < cs.length; i++) {
      var c = obj(cs[i]);
      var d = isNum(c.distance_pct) ? Math.abs(c.distance_pct) : null;
      if (d !== null && d < bestD) { bestD = d; best = c; }
    }
    if (!best && cs.length) best = obj(cs[0]);
    return best;
  }

  function answerConditions(payload, question) {
    var res = base(payload), reg = regimeOf(payload), cs = arr(payload.conditions);
    res.intent = "conditions";
    if (!cs.length) {
      res.answer = "快照里没有可用的判断条件数据，因此无法给出「什么情况下改变判断」。可以查看取数计划面板确认缺了哪一类数据。";
      return withFollowups(res, payload, question);
    }
    var sorted = cs.slice().sort(function (a, b) {
      var da = isNum(obj(a).distance_pct) ? Math.abs(obj(a).distance_pct) : 1e9;
      var db = isNum(obj(b).distance_pct) ? Math.abs(obj(b).distance_pct) : 1e9;
      return da - db;
    });
    var lines = [], cites = [];
    lines.push("当前状态是「" + (reg.name || "未知") + "」。判断是否需要修正，看下面几条阈值（按距离当前值由近到远）：");
    for (var i = 0; i < sorted.length; i++) {
      var c = obj(sorted[i]);
      var triggered = c.triggered ? "【已触发】" : "【未触发】";
      lines.push("- " + triggered + c.text + "：当前 " + fmt(c.current, c.unit) +
        "，阈值 " + (c.comparator || "") + " " + fmt(c.threshold, c.unit) +
        "，距离 " + (isNum(c.distance_pct) ? num(Math.abs(c.distance_pct), 1) +
          (c.distance_is_pp ? "pp" : "%") : "—") + "。");
      if (c.why) lines.push("  - 为什么重要：" + c.why);
      cites.push(cite("判断条件", "conditions", c.text, c.current, c.unit || "",
        c.source || "", asOfOf(payload)));
    }
    var trig = arr(payload.triggers);
    if (trig.length) {
      lines.push("");
      lines.push("已触发条件的组合：" + first(trig, 4).map(function (t) {
        return str(obj(t).text || obj(t).label || t);
      }).join("；") + "。");
    }
    lines.push("");
    lines.push("这些是状态条件，不是涨跌预测：条件满足时该做的是重新评估，而不是照着做交易。");
    res.answer = lines.join("\n");
    res.citations = cites;
    res.evidence_used = ["判断条件"];
    return withFollowups(res, payload, question);
  }

  /* -- 矛盾 ------------------------------------------------------------ */
  function answerContradiction(payload, question) {
    var res = base(payload), cs = arr(payload.contradictions), map = dimsById(payload);
    res.intent = "contradiction";
    if (!cs.length) {
      res.answer = "七维证据当前方向一致，快照里没有检测到显著背离。这不等于没有风险，只说明各维度暂时没有互相打架。";
      return withFollowups(res, payload, question);
    }
    var lines = [], cites = [];
    lines.push("当前列出 " + cs.length + " 组主要矛盾（按强度排序）：");
    for (var i = 0; i < cs.length; i++) {
      var c = obj(cs[i]);
      lines.push("");
      lines.push("**" + (c.title || c.id || "矛盾") + "**");
      if (c.text) lines.push(str(c.text));
      if (isNum(c.strength)) lines.push("- 强度：" + num(c.strength, 2));
      var detail = arr(c.detail);
      for (var j = 0; j < detail.length; j++) {
        var d = obj(detail[j]);
        if (!d.dim) continue;
        lines.push("- " + d.dim + "：" + fmt(d.score) + "（" + (d.status || "—") + "）" +
          (d.headline ? " — " + d.headline : ""));
      }
      if (c.check) lines.push("- 怎么检验：" + c.check);
      var ids = arr(c.dims);
      for (var k = 0; k < ids.length; k++) {
        var dim = map[ids[k]];
        if (dim && dim.id) {
          cites.push(cite(dim.name || dim.id, dim.id, (dim.name || dim.id) + " 读数",
            isNum(dim.score) ? dim.score : null, "", "七维证据合成",
            dim.as_of || asOfOf(payload)));
        }
      }
    }
    lines.push("");
    lines.push("矛盾本身不是结论：它说明当前状态的解释不唯一，需要看哪一维度先向另一维度收敛。");
    res.answer = lines.join("\n");
    res.citations = cites.length ? uniqCites(cites) :
      dimCitations(payload, [map.structure, map.breadth].filter(Boolean), 2);
    res.evidence_used = ["主要矛盾", "七维证据"];
    return withFollowups(res, payload, question);
  }

  function uniqCites(list) {
    var seen = {}, out = [];
    for (var i = 0; i < list.length; i++) {
      var c = list[i], key = (c.dim_id || "") + "|" + (c.label || "");
      if (seen[key]) continue;
      seen[key] = 1;
      out.push(c);
    }
    return out;
  }

  /* -- 维度 ------------------------------------------------------------ */
  function answerDimension(payload, question) {
    var res = base(payload), dim = matchDimByQuestion(payload, question);
    res.intent = "dimension";
    if (!dim) {
      res.intent = "unknown";
      return answerUnknown(payload, question);
    }
    var lines = [];
    lines.push("【" + (dim.name || dim.id) + "】" + (dim.headline || ""));
    lines.push("读数 " + fmt(dim.score) + "（" + (dim.status || "—") + "），置信度 " +
      (isNum(dim.confidence) ? num(dim.confidence * 100, 0) + "%" : "—") + "。");
    var facts = arr(dim.facts);
    if (facts.length) {
      lines.push("");
      lines.push("关键数据：");
      var top = facts.slice(0, 6);
      for (var i = 0; i < top.length; i++) {
        var f = obj(top[i]);
        lines.push("- " + f.label + "：" + fmt(f.value, f.unit) +
          (f.source ? "（来源 " + f.source + "）" : "") +
          (f.note ? " — " + f.note : ""));
      }
    }
    var sig = arr(dim.signals);
    if (sig.length) {
      lines.push("");
      lines.push("信号：");
      for (var j = 0; j < sig.length && j < 5; j++) {
        lines.push("- " + (obj(sig[j]).text || String(sig[j])));
      }
    }
    if (dim.caliber) { lines.push(""); lines.push("口径：" + dim.caliber); }
    if (arr(dim.uncertainty).length) {
      lines.push(""); lines.push("局限：" + arr(dim.uncertainty).join("；"));
    }
    if (arr(dim.gaps).length) {
      lines.push(""); lines.push("数据缺口：" + arr(dim.gaps).join("；"));
    }
    res.answer = lines.join("\n");
    res.citations = factCitations(payload, dim, 6);
    res.evidence_used = [dim.name || dim.id];
    return withFollowups(res, payload, question);
  }

  /* -- 行业 ------------------------------------------------------------ */
  function industryRows(payload) {
    var ind = obj(payload.industry);
    return arr(ind.rows).filter(function (r) { return r && (r.name || r.r20 !== undefined); });
  }

  function focusIndustry(payload, question) {
    var q = lower(question), rows = industryRows(payload);
    var best = null, bestLen = 0;
    for (var i = 0; i < rows.length; i++) {
      var name = str(obj(rows[i]).name);
      if (name && q.indexOf(lower(name)) >= 0 && name.length > bestLen) {
        best = obj(rows[i]); bestLen = name.length;
      }
    }
    return best;
  }

  function answerIndustry(payload, question) {
    var res = base(payload), ind = obj(payload.industry), rows = industryRows(payload);
    res.intent = "industry";
    if (!rows.length) {
      res.answer = "快照里没有行业指数数据（取数计划中行业日线能力可能失败），因此无法给出行业排行。可以查看取数计划面板与数据质量面板确认。";
      return withFollowups(res, payload, question);
    }
    var focus = focusIndustry(payload, question);
    if (focus) {
      var peer = rows.filter(function (r) { return obj(r).rank_20 !== null; })
        .sort(function (a, b) { return (obj(a).rank_20 || 999) - (obj(b).rank_20 || 999); });
      var lines = [];
      lines.push("【" + focus.name + "】近 5 日 " + signed(focus.r5, 2, "%") +
        "，近 20 日 " + signed(focus.r20, 2, "%") + "，20 日排名 " +
        (focus.rank_20 ? focus.rank_20 + " / " + peer.length : "—") + "。");
      lines.push("是否站上 20 日均线：" +
        (focus.above_ma20 === true ? "是" : focus.above_ma20 === false ? "否" : "快照未提供") + "。");
      if (isNum(focus.up_ratio)) lines.push("行业内上涨占比：" + pct(focus.up_ratio) + "。");
      var neighbors = peer.filter(function (r) { return r.name !== focus.name; }).slice(0, 4);
      if (neighbors.length) {
        lines.push("");
        lines.push("同期排名靠前的行业：" + neighbors.map(function (r) {
          return obj(r).name + "（" + signed(obj(r).r20, 2, "%") + "）";
        }).join("、") + "。");
      }
      lines.push("");
      lines.push("口径：" + (ind.caliber || "行业指数为公开行业指数代理。"));
      res.answer = lines.join("\n");
      res.citations = [cite("行业", "industry", focus.name, focus.r20, "%",
        ind.source || "", ind.as_of || asOfOf(payload))];
      var cand = neighbors.concat([focus]);
      for (var i = 0; i < cand.length && res.citations.length < 5; i++) {
        var r = obj(cand[i]);
        if (!r.name) continue;
        res.citations.push(cite("行业", "industry", r.name, r.r20, "%",
          ind.source || "", ind.as_of || asOfOf(payload)));
      }
      res.citations = uniqCites(res.citations);
      res.evidence_used = ["行业排行"];
      return withFollowups(res, payload, question);
    }
    var ranked = rows.filter(function (r) { return isNum(obj(r).r20); })
      .sort(function (a, b) { return obj(b).r20 - obj(a).r20; });
    var lines2 = [];
    lines2.push("截至 " + (ind.as_of || asOfOf(payload)) + "，共 " + (ind.count || rows.length) +
      " 个行业（来源 " + (ind.source || "—") + "）。按近 20 日涨跌幅排序：");
    lines2.push("");
    lines2.push("领涨：" + first(ranked, 5).map(function (r) {
      return obj(r).name + "（" + signed(obj(r).r20, 2, "%") + "）";
    }).join("、") + "。");
    var lag = ranked.slice(-3).reverse();
    if (lag.length) {
      lines2.push("落后：" + lag.map(function (r) {
        return obj(r).name + "（" + signed(obj(r).r20, 2, "%") + "）";
      }).join("、") + "。");
    }
    var above = rows.filter(function (r) { return obj(r).above_ma20 === true; }).length;
    lines2.push("站上 20 日均线的行业：" + above + " / " + rows.length + "。");
    lines2.push("");
    lines2.push("这是对行业相对强弱的横截面描述，不是对后续轮动的预测。");
    lines2.push("口径：" + (ind.caliber || "行业指数为公开行业指数代理。"));
    res.answer = lines2.join("\n");
    var cites = [];
    for (var k = 0; k < ranked.length && cites.length < 5; k++) {
      var rr = obj(ranked[k]);
      cites.push(cite("行业", "industry", rr.name, rr.r20, "%",
        ind.source || "", ind.as_of || asOfOf(payload)));
    }
    res.citations = cites;
    res.evidence_used = ["行业排行"];
    return withFollowups(res, payload, question);
  }

  /* -- 风格 ------------------------------------------------------------ */
  function answerStyle(payload, question) {
    var res = base(payload), styles = arr(payload.styles);
    res.intent = "style";
    if (!styles.length) {
      res.answer = "快照里没有风格对比数据（需要宽基指数日线），无法判断大小盘/价值成长的相对强弱。";
      return withFollowups(res, payload, question);
    }
    var lines = [], cites = [];
    lines.push("风格对比（近 20 个交易日）：");
    for (var i = 0; i < styles.length; i++) {
      var s = obj(styles[i]);
      if (!s.label) continue;
      lines.push("");
      lines.push("**" + s.label + "**");
      lines.push("- 占优：" + (s.winner || "—") + "；20 日差 " +
        (isNum(s.spread_20d) ? signed(s.spread_20d, 2, s.spread_unit === "pct" ? "pp" : "") : "—"));
      if (s.leader || s.lagger) {
        lines.push("- 领先：" + (s.leader || "—") +
          (isNum(s.leader_r20) ? "（" + signed(s.leader_r20, 2, "%") + "）" : "") +
          "；落后：" + (s.lagger || "—") +
          (isNum(s.lagger_r20) ? "（" + signed(s.lagger_r20, 2, "%") + "）" : ""));
      }
      if (s.note) lines.push("- 口径：" + s.note);
      cites.push(cite("风格", "style", s.label, s.spread_20d, "pp",
        "宽基指数日线", asOfOf(payload)));
    }
    if (isNum(regimeOf(payload).confidence)) {
      lines.push("");
      lines.push("风格与状态的关系：当风格差走阔时，七维中的「风格轮动」读数往往同步变化，可以对照轮动维度一起看。");
    }
    res.answer = lines.join("\n");
    res.citations = uniqCites(cites);
    res.evidence_used = ["风格对比"];
    var seg = arr(obj(obj(payload.series).sectors));
    if (!seg.length) res.notes.push("快照未包含板块序列，风格结论只基于宽基指数。");
    return withFollowups(res, payload, question);
  }

  /* -- 历史 ------------------------------------------------------------ */
  function answerHistory(payload, question) {
    var res = base(payload), hp = obj(payload.history_phases), phases = arr(hp.phases);
    res.intent = "history";
    if (!phases.length) {
      res.answer = "快照里没有历史阶段数据（需要指数日线），无法做历史对比。";
      return withFollowups(res, payload, question);
    }
    var lines = [], cites = [];
    lines.push("历史阶段划分方法：" + (hp.method || "proxy") +
      "；窗口 " + fmt(hp.window_days) + " 个交易日。");
    lines.push(str(hp.caveat || "历史阶段为代理口径，不是真实状态模型的历史回溯。"));
    lines.push("");
    for (var i = 0; i < phases.length; i++) {
      var p = obj(phases[i]);
      var cur = p.id === hp.current_phase ? "（当前）" : "";
      lines.push("- " + (p.start || "—") + " → " + (p.end || "—") + cur +
        "：" + (p.regime || "—") +
        "，涨跌 " + signed(p.trend, 2, "%") +
        (isNum(p.vol_pct) ? "，波动分位 " + num(p.vol_pct, 2) : "") +
        (isNum(p.breadth_proxy) ? "，行业广度 " + pct(p.breadth_proxy) : "") +
        "。" + (p.diff_vs_today ? "与今天相比：" + p.diff_vs_today + "。" : ""));
      cites.push(cite("历史阶段", "history", (p.start || "") + " → " + (p.end || ""),
        p.trend, "%", "proxy", p.end || asOfOf(payload)));
    }
    lines.push("");
    lines.push("提示：这里的「类似」只是涨跌幅与波动分位的形态相似，不构成对未来的类比推理。");
    res.answer = lines.join("\n");
    res.citations = cites;
    res.evidence_used = ["历史阶段"];
    return withFollowups(res, payload, question);
  }

  /* -- 风险 ------------------------------------------------------------ */
  function answerRisk(payload, question) {
    var res = base(payload), map = dimsById(payload), reg = regimeOf(payload);
    res.intent = "risk";
    var want = ["valuation", "liquidity", "sentiment", "events"], picked = [], lines = [], cites = [];
    for (var i = 0; i < want.length; i++) {
      var d = map[want[i]];
      if (d) picked.push(d);
    }
    if (!picked.length) {
      res.answer = "快照里缺少估值/流动性/情绪等风险相关维度，无法给出风险读数。";
      return withFollowups(res, payload, question);
    }
    lines.push("风险侧读数（来自 " + picked.length + " 个维度，不是涨跌预测）：");
    for (var j = 0; j < picked.length; j++) {
      var dim = picked[j];
      lines.push("- " + dim.name + "：" + fmt(dim.score) + "（" + (dim.status || "—") + "）" +
        (dim.headline ? " — " + dim.headline : ""));
      var facts = first(arr(dim.facts), 2);
      cites = cites.concat(factCitations(payload, { id: dim.id, name: dim.name, facts: facts }, 2));
    }
    var cp = obj(reg.confidence_parts);
    lines.push("");
    lines.push("置信度构成：" + (cp.formula ? str(cp.formula) : "由覆盖、一致性、新鲜度合成") +
      "；当前覆盖 " + fmt(cp.coverage) + "、一致性 " + fmt(cp.consistency) +
      "、新鲜度 " + fmt(cp.freshness) + "。");
    var g = gateOf(payload);
    if (g.level && g.level !== "full") {
      lines.push("质量闸门为 " + g.level + "：" + (g.reason || "") +
        (isNum(g.confidence_cap) ? "；置信度上限 " + fmt(g.confidence_cap) + "%" : "") + "。");
    }
    var cs = arr(payload.contradictions);
    if (cs.length) {
      lines.push("");
      lines.push("当前最需要盯的矛盾：" + (obj(cs[0]).title || "—") + "。");
    }
    lines.push("");
    lines.push("以上是「当前状态的脆弱点」，不是风险预报；任何一条都可能因为数据更新而改变。");
    res.answer = lines.join("\n");
    res.citations = uniqCites(cites);
    res.evidence_used = picked.map(function (d) { return d.name; });
    return withFollowups(res, payload, question);
  }

  /* -- 数据质量 -------------------------------------------------------- */
  function answerQuality(payload, question) {
    var res = base(payload), g = gateOf(payload), plan = planOf(payload);
    var health = obj(payload.data_health), quality = obj(health.quality);
    res.intent = "quality";
    var lines = [], cites = [];
    lines.push("质量闸门：" + (g.level || "未知") + "。" + (g.reason ? str(g.reason) : ""));
    if (isNum(g.confidence_cap)) lines.push("置信度上限：" + fmt(g.confidence_cap) + "% 。");
    if (arr(g.missing).length) lines.push("缺失能力：" + arr(g.missing).join("、") + "。");
    if (plan.summary) lines.push("取数计划：" + str(plan.summary) + "。");
    if (quality.score !== undefined) lines.push("数据质量分：" + fmt(quality.score) + "。");
    if (quality.coverage !== undefined) lines.push("覆盖率：" + fmt(quality.coverage) + "。");
    if (quality.freshness !== undefined) lines.push("新鲜度：" + fmt(quality.freshness) + "。");
    var conflicts = arr(quality.conflicts);
    if (conflicts.length) {
      lines.push("");
      lines.push("数据冲突（" + conflicts.length + " 处）：");
      for (var i = 0; i < conflicts.length && i < 5; i++) {
        var c = obj(conflicts[i]);
        var a = obj(c.a), b = obj(c.b);
        lines.push("- " + (c.field || "字段") + "：" +
          (a.source || "源A") + "=" + fmt(a.value) + " vs " +
          (b.source || "源B") + "=" + fmt(b.value) +
          (isNum(c.delta_pct) ? "，差异 " + num(c.delta_pct, 2) + "%" : "") +
          (c.resolution ? "；处理：" + c.resolution : ""));
      }
    }
    if (arr(quality.missing).length) lines.push("缺失字段：" + arr(quality.missing).join("、") + "。");
    var steps = arr(plan.steps), bad = [];
    for (var j = 0; j < steps.length; j++) {
      var s = obj(steps[j]);
      if (s.status && s.status !== "ok") bad.push(s);
    }
    if (bad.length) {
      lines.push("");
      lines.push("降级/失败的能力：");
      for (var k = 0; k < bad.length && k < 6; k++) {
        var st = obj(bad[k]);
        lines.push("- " + (st.name || st.id) + "：" + st.status +
          (st.used ? "（实际源 " + st.used + "）" : "") +
          (st.error ? "；错误 " + st.error : ""));
        cites.push(cite("取数计划", "quality", st.name || st.id, st.rows, "行",
          st.used || "", st.as_of || asOfOf(payload)));
      }
    }
    if (!cites.length) {
      cites.push(cite("质量闸门", "quality", "gate=" + (g.level || "unknown"),
        isNum(g.confidence_cap) ? g.confidence_cap : null, "%", "plan.gate", asOfOf(payload)));
    }
    lines.push("");
    lines.push("静态快照的数据截止于 " + (asOfOf(payload) || "快照日") +
      "，页面上的所有数字都来自这一次取数，不会自动更新。");
    res.answer = lines.join("\n");
    res.citations = cites;
    res.evidence_used = ["质量闸门", "取数计划"];
    return withFollowups(res, payload, question);
  }

  /* -- 认识论 ---------------------------------------------------------- */
  function answerEpistemic(payload, question) {
    var res = base(payload), epi = obj(payload.epistemics);
    var legend = arr(obj(obj(payload.meta).epistemic_legend));
    var dims = dimsOf(payload);
    var factsCount = 0, factsAll = [];
    for (var i = 0; i < dims.length; i++) {
      factsAll = factsAll.concat(arr(obj(dims[i]).facts));
    }
    factsCount = obj(epi.facts).count;
    if (!isNum(factsCount)) factsCount = factsAll.length;
    res.intent = "epistemic";
    var lines = [];
    lines.push("本产品把输出拆成三层，每一条都标了层：");
    lines.push("");
    var labels = { fact: "事实", induction: "归纳", judgment: "判断" };
    for (var j = 0; j < legend.length; j++) {
      var item = obj(legend[j]);
      lines.push("- **" + (item.label || labels[item.key] || item.key) + "**：" +
        str(item.desc || ""));
    }
    if (!legend.length) {
      lines.push("- **事实**：可复核的原始读数，带来源与时点。");
      lines.push("- **归纳**：由多个事实合成的维度读数，不是唯一解释。");
      lines.push("- **判断**：在事实与归纳之上的研判，附置信度与证伪条件。");
    }
    lines.push("");
    lines.push("这份快照里：事实 " + fmt(factsCount) + " 条（带来源 " +
      fmt(obj(epi.facts).with_source) + " 条）、归纳 " +
      fmt(obj(epi.inductions).count || dims.length) + " 个维度、判断 " +
      fmt(arr(epi.judgments).length) + " 条。");
    if (factsAll.length) {
      var f0 = obj(factsAll[0]);
      lines.push("");
      lines.push("举例（事实层）：「" + (f0.label || "—") + "」= " + fmt(f0.value, f0.unit) +
        "，来源 " + (f0.source || "—") + "，时点 " + (f0.as_of || asOfOf(payload) || "—") + "。");
    }
    var d0 = obj(dims[0]);
    if (d0.name) {
      lines.push("举例（归纳层）：「" + d0.name + "」= " + fmt(d0.score) +
        "（" + (d0.status || "—") + "），由 " + arr(d0.facts).length + " 条事实合成。");
    }
    var judgments = arr(epi.judgments);
    if (judgments.length) {
      var j0 = obj(judgments[0]);
      lines.push("举例（判断层）：" + str(j0.statement || "") +
        (isNum(j0.confidence) ? "（置信度 " + fmt(j0.confidence) + "）" : "") + "。");
    }
    lines.push("");
    lines.push("静态站不联网：以上全部来自这份快照，追问由本地模板从快照取数，未调用模型。");
    res.answer = lines.join("\n");
    res.citations = factsAll.length ? factCitations(payload, { id: d0.id || "", name: d0.name || "事实", facts: first(factsAll, 4) }, 4) : [];
    res.evidence_used = ["三层认识论"];
    return withFollowups(res, payload, question);
  }

  /* -- 拒答 / 未知 ------------------------------------------------------ */
  function answerRefuse(payload, question) {
    var res = base(payload), reg = regimeOf(payload);
    res.intent = "refuse";
    res.refused = true;
    res.citations = [];
    res.evidence_used = [];
    res.answer = "这个问题我不会回答，也不应该回答。\n\n" +
      "本产品的定位是描述市场状态、列出证据与可能改变判断的条件；" +
      "不预测涨跌、不给买卖或仓位建议，也不判断任何个股该不该买。\n\n" +
      "如果你关心的是「现在处于什么状态」，当前快照的判断是「" + (reg.name || "未知") +
      "」，置信度 " + fmt(reg.confidence) + "/100；" +
      "可以继续问「当前市场处于什么状态？」或「什么条件会改变当前判断？」。";
    res.followups = ["当前市场处于什么状态？", "什么条件会改变当前判断？",
      "现在的主要风险在哪里？", "这次研判的数据质量如何？"];
    res.notes.push("交易类问题按合规红线拒答（与后端同一套规则）。");
    return res;
  }

  function answerUnknown(payload, question) {
    var res = base(payload);
    res.intent = "unknown";
    res.answer = "这个问题超出了当前快照能回答的范围。\n\n" +
      "静态站没有联网、没有模型，只能基于快照里的证据做推演，所以：\n" +
      "- 不认识的个股、基金、期货、海外市场：没有数据，不猜；\n" +
      "- 未来走势、目标价、买卖建议：产品定位不允许回答；\n" +
      "- 快照之外的新事件：数据截至 " + (asOfOf(payload) || "快照日") + "，之后的消息看不到。\n\n" +
      "我能回答的是这几类：市场状态概览、主要矛盾、判断条件、七个证据维度、行业排行、风格对比、历史阶段、风险侧读数、数据质量与三层认识论。";
    var sugg = suggestions(payload);
    res.followups = first(sugg, 4);
    res.notes.push("未知问题：给出能力范围而不是编造答案。");
    return res;
  }

  /* ------------------------------------------------------------ 入口 */

  function answer(question, payload, history) {
    var p = obj(payload);
    var q = str(question).trim();
    if (!q) {
      var empty = base(p);
      empty.intent = "unknown";
      empty.answer = "请先输入一个问题。";
      empty.followups = first(suggestions(p), 4);
      return empty;
    }
    var intent = classify(q, p);
    var res;
    switch (intent) {
      case "refuse": res = answerRefuse(p, q); break;
      case "conditions": res = answerConditions(p, q); break;
      case "contradiction": res = answerContradiction(p, q); break;
      case "dimension": res = answerDimension(p, q); break;
      case "industry": res = answerIndustry(p, q); break;
      case "style": res = answerStyle(p, q); break;
      case "history": res = answerHistory(p, q); break;
      case "risk": res = answerRisk(p, q); break;
      case "quality": res = answerQuality(p, q); break;
      case "epistemic": res = answerEpistemic(p, q); break;
      case "overview": res = answerOverview(p, q); break;
      default: res = answerUnknown(p, q);
    }
    res.compliance.checked_chars = res.answer.length;
    var prior = arr(history);
    if (prior.length) {
      var last = obj(prior[prior.length - 1]);
      if (last.question && lower(last.question) === lower(q)) {
        res.notes.push("与上一轮问题相同，答案基于同一份快照。");
      }
    }
    return res;
  }

  var api = {
    answer: answer,
    classify: classify,
    suggestions: suggestions,
    intents: ["overview", "conditions", "contradiction", "dimension", "industry",
      "style", "history", "risk", "quality", "epistemic", "refuse", "unknown"],
    engine: ENGINE
  };

  global.MINGSHI_RESEARCH = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : this);
