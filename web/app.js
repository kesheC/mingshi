/* =========================================================================
   app.js — 大势研判前端（零依赖）
   数据全部来自 payload；缺字段时静默降级，任何单块渲染失败都不会白屏。
   静态模式（site/index.html）下：读 window.__MINGSHI_DATA__，
   禁用刷新，追问交给 research.js 本地推演。
   ========================================================================= */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var MODE = "live";          // live | static
  var PAYLOAD = null;
  var MANIFEST = null;
  var HISTORY = [];           // [{question, answer}]
  var ACTIVE_PHASE = null;
  var EVIDENCE = {};          // "dimId|label" -> element
  var STATE_MAP = {};
  var RADAR = {};

  /* ------------------------------------------------------------ 小工具 */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function svg(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }

  function clear(node) { if (node) while (node.firstChild) node.removeChild(node.firstChild); }
  function show(node, on) { if (node) node.hidden = !on; }

  function safe(name, fn) {
    try { fn(); } catch (e) {
      console.warn("[render:" + name + "]", e && e.message ? e.message : e);
    }
  }

  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function num(v, d) {
    if (!isNum(v)) return "—";
    var n = typeof d === "number" ? d : 2;
    return v.toFixed(n).replace(/\.?0+$/, function (m) { return m.indexOf(".") === 0 ? "" : m; });
  }
  function signed(v, d, suffix) {
    if (!isNum(v)) return "—";
    return (v >= 0 ? "+" : "") + num(v, d) + (suffix || "");
  }
  function fmt(v, unit) {
    if (v === null || v === undefined || v === "") return "—";
    var s;
    if (isNum(v)) s = Math.abs(v) >= 1000 ? v.toLocaleString("zh-CN") : num(v);
    else s = String(v);
    return unit ? s + (unit === "%" ? "%" : " " + unit) : s;
  }
  function ratio01(v) {
    if (!isNum(v)) return null;
    return v > 1.5 ? v / 100 : v;
  }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function arr(v) { return Object.prototype.toString.call(v) === "[object Array]" ? v : []; }
  function obj(v) { return v && typeof v === "object" && Object.prototype.toString.call(v) !== "[object Array]" ? v : {}; }
  function str(v) { return typeof v === "string" ? v : ""; }
  function first(list, n) { return arr(list).slice(0, typeof n === "number" ? n : 5); }
  function uniq(list) {
    var seen = {}, out = [];
    list.forEach(function (x) { if (x && !seen[x]) { seen[x] = 1; out.push(x); } });
    return out;
  }
  function setText(id, text) { var n = document.getElementById(id); if (n) n.textContent = text === undefined ? "" : text; }
  function setHTML(id, node) { var n = document.getElementById(id); if (!n) return; clear(n); if (node) n.appendChild(node); }

  function upDown(v) { return !isNum(v) ? "flat" : (v > 0 ? "up" : v < 0 ? "down" : "flat"); }
  function heatColor(v, max) {
    if (!isNum(v)) return "rgba(120,140,170,0.12)";
    var m = isNum(max) && max > 0 ? max : 1;
    var k = clamp(Math.abs(v) / m, 0.08, 1);
    return v >= 0 ? "rgba(255,80,100," + (0.14 + 0.55 * k).toFixed(3) + ")"
      : "rgba(22,199,132," + (0.14 + 0.55 * k).toFixed(3) + ")";
  }
  function statusClass(s) {
    var t = str(s);
    if (t.indexOf("强") >= 0 || t.indexOf("改善") >= 0) return "st-strong";
    if (t.indexOf("弱") >= 0 || t.indexOf("恶化") >= 0) return "st-weak";
    return "st-neutral";
  }

  function layerMeta(layer) {
    var map = {
      fact: { label: "事实", cls: "fact", title: "事实层：可复核的原始读数，带来源与时点" },
      induction: { label: "归纳", cls: "induction", title: "归纳层：由多条事实合成的维度读数，不是唯一解释" },
      judgment: { label: "判断", cls: "judgment", title: "判断层：在事实与归纳之上的研判，附置信度与证伪条件" },
      condition: { label: "条件", cls: "condition", title: "条件层：判断可能改变的状态条件，不是买卖信号" }
    };
    var key = str(layer).toLowerCase();
    return map[key] || map.induction;
  }
  function layerBadge(layer) {
    var m = layerMeta(layer);
    var s = el("span", "lbadge " + m.cls, m.label);
    s.title = m.title;
    return s;
  }

  function regimeOf(p) { return obj(p && p.regime); }
  function dimsOf(p) { return arr(p && p.dimensions); }
  function planOf(p) { return obj(p && p.plan); }
  function gateOf(p) {
    var g = obj(planOf(p).gate);
    if (g.level) return g;
    var rg = obj(regimeOf(p).gate);
    return rg.level ? rg : g;
  }
  function asOfOf(p) { return str(p && p.as_of); }
  function legendOf(p) {
    var l = arr(obj(obj(p.meta).epistemic_legend));
    if (l.length) return l;
    return [
      { key: "fact", label: "事实", desc: "可复核的原始读数，带来源与时点" },
      { key: "induction", label: "归纳", desc: "由多条事实合成的维度读数，不是唯一解释" },
      { key: "judgment", label: "判断", desc: "在事实与归纳之上的研判，附置信度与证伪条件" }
    ];
  }

  function evidenceKey(dimId, label) { return str(dimId) + "|" + str(label); }
  function registerEvidence(dimId, label, node) {
    if (!node) return;
    var k = evidenceKey(dimId, label);
    if (!EVIDENCE[k]) EVIDENCE[k] = node;
    if (label && !EVIDENCE["*|" + label]) EVIDENCE["*|" + label] = node;
  }

  /* ------------------------------------------------------------ 启动 */
  function boot() {
    var embedded = window.__MINGSHI_DATA__;
    if (embedded && typeof embedded === "object") {
      MODE = "static";
      PAYLOAD = embedded.payload || embedded;
      MANIFEST = embedded.manifest || window.__MINGSHI_MANIFEST__ || null;
      paint();
      return;
    }
    fetch("/api/dashboard", { cache: "no-store" })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          if (j && j.ok === false) throw new Error(j.error || "接口返回失败");
          return j;
        });
      })
      .then(function (j) {
        MODE = "live";
        PAYLOAD = (j && j.payload) || j;
        paint();
      })
      .catch(function (e) {
        var why = e && e.message ? e.message : String(e);
        fetchStatic(why);
      });
  }

  function fetchStatic(why) {
    fetch("data/dashboard.json", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("静态数据缺失（HTTP " + r.status + "）");
        return r.json();
      })
      .then(function (j) {
        MODE = "static";
        PAYLOAD = (j && j.payload) || j;
        MANIFEST = null;
        paint();
      })
      .catch(function (e2) {
        showError("无法加载研判数据：后端不可用（" + why + "），且未找到静态快照（" +
          (e2 && e2.message ? e2.message : e2) + "）。",
          "live 模式请运行 python run.py --port 8791；静态模式请确认 data/dashboard.json 存在（由 scripts/export_static.py 生成）。");
      });
  }

  function showError(msg, hint) {
    show($("#loading"), false);
    show($("#content"), false);
    var box = $("#err");
    if (!box) return;
    clear(box);
    box.appendChild(el("b", null, msg));
    if (hint) {
      var p = el("p", null, hint);
      p.style.marginTop = "6px";
      box.appendChild(p);
    }
    show(box, true);
  }

  /* ------------------------------------------------------------ 总渲染 */
  function paint() {
    if (!PAYLOAD) { showError("数据为空。", "请检查取数流程或静态导出。"); return; }
    show($("#loading"), false);
    show($("#err"), false);
    show($("#content"), true);

    safe("mode", renderMode);
    safe("gate", renderGate);
    safe("hero", renderHero);
    safe("legend", renderLegend);
    safe("plan", renderPlan);
    safe("dims", renderDims);
    safe("contra", renderContra);
    safe("conds", renderConds);
    safe("industry", renderIndustry);
    safe("styles", renderStyles);
    safe("history", renderHistory);
    safe("health", renderHealth);
    safe("ask", setupAsk);
    safe("foot", renderFoot);
    safe("stateMap", renderStateMap);
    safe("radar", renderRadar);
    setupTabs();
  }

  /* ------------------------------------------------------------ 模式/页脚 */
  function renderMode() {
    var p = PAYLOAD, chip = $("#modeChip"), sb = $("#staticBadge"), btn = $("#btnRefresh");
    var asOf = p.as_of_label || asOfOf(p) || "—";
    setText("asOf", asOf);
    if (MODE === "static") {
      show(chip, true);
      chip.textContent = "静态快照";
      show(sb, true);
      clear(sb);
      var b = el("b", null, "静态快照");
      sb.appendChild(b);
      sb.appendChild(document.createTextNode(" · 数据截至 " + (asOfOf(p) || "—") +
        " · 生成于 " + (str(p.generated_at) || "—")));
      if (MANIFEST) {
        var bits = [];
        if (MANIFEST.generated_at) bits.push("导出 " + MANIFEST.generated_at);
        if (MANIFEST.files) bits.push(Object.keys(MANIFEST.files).length + " 个文件");
        if (bits.length) sb.appendChild(el("span", "hint", "（" + bits.join(" · ") + "）"));
      }
      sb.appendChild(el("span", "hint", "本页不联网、不自动更新，所有数字来自该次快照。"));
      if (btn) {
        btn.disabled = true;
        btn.textContent = "静态快照 · 不可刷新";
        btn.title = "静态快照不提供刷新；重新生成请运行 scripts/export_static.py";
        btn.setAttribute("aria-disabled", "true");
      }
    } else {
      show(chip, false);
      show(sb, false);
      if (btn) {
        btn.disabled = false;
        btn.textContent = "刷新研判";
        btn.title = "重新取数并研判";
        btn.removeAttribute("aria-disabled");
      }
    }
  }

  function renderFoot() {
    var p = PAYLOAD, g = gateOf(p), dh = obj(p.data_health);
    var bits = [
      "payload v" + (str(p.version) || "—"),
      "mode " + (str(p.mode) || MODE),
      "gate " + (g.level || "—"),
      "生成 " + (str(p.generated_at) || "—"),
      "耗时 " + (isNum(p.elapsed_ms) ? num(p.elapsed_ms, 0) + "ms" : "—"),
      "标的 " + (isNum(dh.market_rows) ? num(dh.market_rows, 0) : "—")
    ];
    setText("footMeta", bits.join(" · "));
    if (MODE === "static") {
      var fs = $("#footStatic");
      if (fs) {
        fs.hidden = false;
        fs.textContent = "静态导出时间：" + ((MANIFEST && MANIFEST.generated_at) || str(p.generated_at) || "—") +
          "；追问由本地模板从快照推演，非实时、非模型生成。";
      }
    }
    var legal = $("#footLegal");
    if (legal && p.disclaimer) legal.textContent = str(p.disclaimer);
  }

  /* ------------------------------------------------------------ 闸门 */
  function renderGate() {
    var p = PAYLOAD, g = gateOf(p), banner = $("#gateBanner");
    var level = str(g.level) || "unknown";
    var map = { full: "数据闸门：通过", partial: "数据闸门：部分能力降级", insufficient: "数据闸门：研判受限（数据不足）" };
    banner.className = "gate-banner gate-" + level;
    show(banner, true);
    setText("gateLevel", level);
    setText("gateTitle", map[level] || "数据质量闸门");
    setText("gateCap", isNum(g.confidence_cap) ? "置信度上限 " + num(g.confidence_cap, 0) + "%" : "");
    setText("gateReason", str(g.reason) || (level === "full" ? "所有关键能力均由主源或可用备源提供。" : ""));
    var miss = arr(g.missing).concat(arr(g.failed));
    var gm = $("#gateMissing");
    if (miss.length) {
      gm.hidden = false;
      gm.textContent = "缺失/失败能力：" + uniq(miss).join("、");
    } else {
      gm.hidden = true;
    }
  }

  /* ------------------------------------------------------------ 状态卡 */
  function renderHero() {
    var p = PAYLOAD, reg = regimeOf(p), g = gateOf(p), n = obj(p.narrative);
    var name = str(reg.name) || "未知状态";
    var insufficient = g.level === "insufficient";
    var nameEl = $("#stateName");
    nameEl.textContent = insufficient ? "研判受限：数据不足" : name;
    nameEl.classList.toggle("is-limited", insufficient);

    var badge = $("#stateBadge");
    badge.textContent = insufficient ? "受限" : (str(reg.substate) || str(reg.code) || "—");
    var quad = $("#stateQuadrant");
    if (str(reg.quadrant)) { quad.hidden = false; quad.textContent = "象限 " + str(reg.quadrant); }
    else quad.hidden = true;

    setText("stateAsOf", "数据截至 " + (p.as_of_label || asOfOf(p) || "—"));
    setText("stateSummary", str(n.summary) || str(reg.meaning) || "—");

    var note = str(reg.watch) || str(n.state_read) || str(reg.risk) || "";
    setText("stateNote", note);

    var limit = $("#stateLimit");
    if (insufficient) {
      limit.hidden = false;
      var miss = arr(g.missing).concat(arr(g.failed));
      limit.textContent = "缺什么：" + (uniq(miss).join("、") || "关键数据能力") +
        (isNum(g.confidence_cap) ? "；置信度上限 " + num(g.confidence_cap, 0) + "%。" : "。") +
        " 下面的维度读数与判断均按此上限降级展示，不应被当作完整研判。";
    } else {
      limit.hidden = true;
    }

    var conf = isNum(reg.confidence) ? clamp(reg.confidence, 0, 100) : null;
    setText("confNum", conf === null ? "—" : num(conf, 1));
    var C = 2 * Math.PI * 50;
    var fill = $("#gaugeFill");
    var ratio = conf === null ? 0 : conf / 100;
    fill.style.strokeDasharray = (C * ratio).toFixed(1) + " " + C.toFixed(1);
    fill.style.stroke = conf === null ? "#4f9dff" : conf >= 70 ? "#16c784" : conf >= 45 ? "#ffc857" : "#ff8a5c";
    var capMark = $("#capMark");
    if (isNum(g.confidence_cap)) {
      capMark.hidden = false;
      capMark.textContent = "cap " + num(g.confidence_cap, 0) + "%";
    } else capMark.hidden = true;

    setText("trendLabel", str(reg.trend_label) || "—");
    setText("riskLabel", str(reg.risk_label) || "—");
    var t = isNum(reg.trend_axis) ? clamp(reg.trend_axis, -1, 1) : 0;
    var r = isNum(reg.risk_axis) ? clamp(reg.risk_axis, -1, 1) : 0;
    $("#barTrend").style.left = ((t + 1) / 2 * 100).toFixed(2) + "%";
    $("#barRisk").style.left = ((r + 1) / 2 * 100).toFixed(2) + "%";
    setText("valTrend", signed(reg.trend_axis, 3) + " · " + (str(reg.trend_label) || "—"));
    setText("valRisk", signed(reg.risk_axis, 3) + " · " + (str(reg.risk_label) || "—"));
  }

  function renderLegend() {
    var p = PAYLOAD, legend = legendOf(p), epi = obj(p.epistemics);
    var box = $("#epiLegend");
    clear(box);
    legend.forEach(function (item) {
      var o = obj(item);
      var card = el("div", "epi-item");
      var h = el("h4");
      h.appendChild(layerBadge(o.key));
      h.appendChild(document.createTextNode(str(o.label) || str(o.key)));
      card.appendChild(h);
      card.appendChild(el("p", null, str(o.desc) || ""));
      box.appendChild(card);
    });

    var dims = dimsOf(p);
    var facts = 0, withSource = 0, withCaliber = 0, factList = [];
    dims.forEach(function (d) {
      arr(d.facts).forEach(function (f) {
        facts++;
        factList.push(f);
        if (str(f.source)) withSource++;
        if (str(f.caliber) || str(d.caliber)) withCaliber++;
      });
    });
    var ef = obj(epi.facts);
    var counts = {
      facts: isNum(ef.count) ? ef.count : facts,
      src: isNum(ef.with_source) ? ef.with_source : withSource,
      cal: isNum(ef.with_caliber) ? ef.with_caliber : withCaliber,
      ind: isNum(obj(epi.inductions).count) ? obj(epi.inductions).count : dims.length,
      jud: arr(epi.judgments).length || (regimeOf(p).name ? 1 + arr(p.contradictions).length : 0)
    };
    var stats = $("#epiStats");
    clear(stats);
    [
      [counts.facts, "事实条数"],
      [counts.src, "带来源（事实层）"],
      [counts.cal, "带口径（事实层）"],
      [counts.ind, "归纳维度"],
      [counts.jud, "判断条目"]
    ].forEach(function (pair) {
      var b = el("div", "epi-stat");
      b.appendChild(el("b", null, num(pair[0], 0)));
      b.appendChild(el("span", null, pair[1]));
      stats.appendChild(b);
    });
    if (arr(epi.judgments).length) {
      var list = el("div", "meta-list");
      list.style.marginTop = "12px";
      arr(epi.judgments).slice(0, 4).forEach(function (j) {
        var o = obj(j);
        var row = el("div");
        row.appendChild(layerBadge("judgment"));
        row.appendChild(document.createTextNode(" " + str(o.statement || o.kind || "")));
        if (isNum(o.confidence)) row.appendChild(el("span", "hint", "置信度 " + num(o.confidence, 0)));
        list.appendChild(row);
      });
      stats.appendChild(list);
    }
  }

  /* ------------------------------------------------------------ 地图/雷达 */
  function renderStateMap() {
    var reg = regimeOf(PAYLOAD), box = $("#stateMap");
    clear(box);
    var W = 320, H = 260, pad = 26;
    var plotW = W - pad * 2, plotH = H - pad * 2;
    var g = svg("g");
    // 象限背景
    var quads = [
      { x: pad, y: pad, w: plotW / 2, h: plotH / 2, c: "rgba(255,80,100,0.045)" },
      { x: pad + plotW / 2, y: pad, w: plotW / 2, h: plotH / 2, c: "rgba(255,200,87,0.045)" },
      { x: pad, y: pad + plotH / 2, w: plotW / 2, h: plotH / 2, c: "rgba(22,199,132,0.045)" },
      { x: pad + plotW / 2, y: pad + plotH / 2, w: plotW / 2, h: plotH / 2, c: "rgba(79,157,255,0.045)" }
    ];
    quads.forEach(function (q) { g.appendChild(svg("rect", { x: q.x, y: q.y, width: q.w, height: q.h, fill: q.c })); });
    g.appendChild(svg("rect", { x: pad, y: pad, width: plotW, height: plotH, fill: "none", stroke: "#1c2739", "stroke-width": 1, rx: 4 }));
    void g;
    // 中线
    g.appendChild(svg("line", { x1: pad + plotW / 2, y1: pad, x2: pad + plotW / 2, y2: pad + plotH, stroke: "#22304a", "stroke-width": 1, "stroke-dasharray": "3 4" }));
    g.appendChild(svg("line", { x1: pad, y1: pad + plotH / 2, x2: pad + plotW, y2: pad + plotH / 2, stroke: "#22304a", "stroke-width": 1, "stroke-dasharray": "3 4" }));

    function label(text, x, y, fill, size, anchor) {
      var t = svg("text", { x: x, y: y, fill: fill || "#6d7f99", "font-size": size || 10, "text-anchor": anchor || "middle" });
      t.textContent = text;
      g.appendChild(t);
      return t;
    }
    label("强势·高偏好", pad + 4, pad + 12, "#7d8ca3", 9, "start");
    label("弱势·高偏好", W - pad - 4, pad + 12, "#7d8ca3", 9, "end");
    label("弱势·低偏好", pad + 4, H - pad - 5, "#7d8ca3", 9, "start");
    label("强势·低偏好", W - pad - 4, H - pad - 5, "#7d8ca3", 9, "end");
    label("趋势 →", W - pad, H - 6, "#55657d", 9, "end");
    var yl = label("风险偏好 →", 6, H / 2, "#55657d", 9, "middle");
    yl.setAttribute("transform", "rotate(-90 10 " + (H / 2) + ")");

    var t = isNum(reg.trend_axis) ? clamp(reg.trend_axis, -1, 1) : 0;
    var r = isNum(reg.risk_axis) ? clamp(reg.risk_axis, -1, 1) : 0;
    var cx = pad + (t + 1) / 2 * plotW;
    var cy = pad + (1 - (r + 1) / 2) * plotH;
    g.appendChild(svg("circle", { cx: cx, cy: cy, r: 11, fill: "rgba(79,157,255,0.18)" }));
    g.appendChild(svg("circle", { cx: cx, cy: cy, r: 5, fill: "#4f9dff", stroke: "#081018", "stroke-width": 2 }));
    var nameEl = label(str(reg.name) || "当前", cx, cy - 15, "#dbe9ff", 11, "middle");
    nameEl.setAttribute("font-weight", "600");
    box.appendChild(g);
    registerEvidence("regime", str(reg.name), nameEl);
  }

  function renderRadar() {
    var dims = dimsOf(PAYLOAD), box = $("#radar");
    clear(box);
    box.setAttribute("viewBox", "0 0 320 300");
    if (!dims.length) return;
    var cx = 160, cy = 148, R = 92;
    var n = dims.length;
    var g = svg("g");
    for (var ring = 1; ring <= 4; ring++) {
      var rr = R * ring / 4;
      var pts = [];
      for (var i = 0; i < n; i++) {
        var a = -Math.PI / 2 + i * 2 * Math.PI / n;
        pts.push((cx + Math.cos(a) * rr).toFixed(1) + "," + (cy + Math.sin(a) * rr).toFixed(1));
      }
      g.appendChild(svg("polygon", {
        points: pts.join(" "), fill: ring % 2 ? "rgba(79,157,255,0.02)" : "none",
        stroke: "#1c2739", "stroke-width": 1
      }));
    }
    for (var k = 0; k < n; k++) {
      var a2 = -Math.PI / 2 + k * 2 * Math.PI / n;
      g.appendChild(svg("line", {
        x1: cx, y1: cy, x2: cx + Math.cos(a2) * R, y2: cy + Math.sin(a2) * R,
        stroke: "#1c2739", "stroke-width": 1
      }));
    }
    var outer = [], inner = [];
    for (var j = 0; j < n; j++) {
      var d = obj(dims[j]);
      var ang = -Math.PI / 2 + j * 2 * Math.PI / n;
      // 圆心 = -1（弱），外圈 = +1（强）
      var v = isNum(d.score) ? clamp(d.score, -1, 1) : 0;
      var rad = R * (v + 1) / 2;
      outer.push((cx + Math.cos(ang) * R).toFixed(1) + "," + (cy + Math.sin(ang) * R).toFixed(1));
      inner.push((cx + Math.cos(ang) * rad).toFixed(1) + "," + (cy + Math.sin(ang) * rad).toFixed(1));
      var lx = cx + Math.cos(ang) * (R + 18);
      var ly = cy + Math.sin(ang) * (R + 18);
      var txt = svg("text", {
        x: lx, y: ly, fill: "#8fa2b8", "font-size": 9.5,
        "text-anchor": Math.abs(Math.cos(ang)) < 0.3 ? "middle" : (Math.cos(ang) > 0 ? "start" : "end"),
        "dominant-baseline": "middle"
      });
      txt.textContent = str(d.name).slice(0, 4);
      txt.title = str(d.name) + " " + fmt(d.score);
      g.appendChild(txt);
    }
    g.appendChild(svg("polygon", {
      points: inner.join(" "), fill: "rgba(79,157,255,0.16)",
      stroke: "#4f9dff", "stroke-width": 1.6, "stroke-linejoin": "round"
    }));
    var center = svg("circle", { cx: cx, cy: cy, r: 2, fill: "#2d3d57" });
    g.appendChild(center);
    var hint = svg("text", { x: 160, y: 288, fill: "#55657d", "font-size": 9, "text-anchor": "middle" });
    hint.textContent = "圆心 = 弱(-1)，外圈 = 强(+1)；每轴由多条事实合成";
    g.appendChild(hint);
    box.appendChild(g);
  }

  /* ------------------------------------------------------------ 取数计划 */
  function renderPlan() {
    var p = PAYLOAD, plan = planOf(p), gate = gateOf(p), health = obj(p.data_health);
    setText("planObjective", str(plan.objective) || "取到研判所需的最小数据集合，并记录主备源与降级。");
    setText("planSummary", str(plan.summary) || "—");
    var note = $("#planNote");
    if (plan.note) { note.hidden = false; note.textContent = str(plan.note); } else note.hidden = true;

    var tbody = $("#planRows");
    clear(tbody);
    var steps = arr(plan.steps);
    if (!steps.length) {
      var tr0 = el("tr");
      var td0 = el("td", "empty", "暂无取数计划（后端未输出 plan，且无法从 data_health 重建）");
      td0.colSpan = 9;
      tr0.appendChild(td0);
      tbody.appendChild(tr0);
    }
    steps.forEach(function (s) {
      var o = obj(s);
      var tr = el("tr");
      var tdName = el("td");
      tdName.appendChild(el("div", "plan-name", str(o.name) || str(o.id) || "—"));
      tr.appendChild(tdName);
      tr.appendChild(el("td", "plan-need", str(o.need) || "—"));
      tr.appendChild(el("td", "src-chain", arr(o.providers).join(" → ") || "—"));
      var usedTd = el("td");
      usedTd.appendChild(el("b", null, str(o.used) || "—"));
      tr.appendChild(usedTd);
      var stTd = el("td");
      var st = str(o.status) || "unknown";
      stTd.appendChild(el("span", "pill " + st, st));
      tr.appendChild(stTd);
      tr.appendChild(el("td", "num", isNum(o.rows) ? num(o.rows, 0) : "—"));
      tr.appendChild(el("td", "num", isNum(o.elapsed_ms) ? num(o.elapsed_ms, 0) + "ms" : "—"));
      tr.appendChild(el("td", "num", str(o.as_of) || "—"));
      var chain = el("td", "src-chain");
      var bits = [];
      if (o.fallback_used) bits.push("备源：" + (str(o.fallback_from) || "已切换"));
      if (arr(o.skipped).length) bits.push("跳过 " + arr(o.skipped).length);
      if (str(o.error)) bits.push(str(o.error));
      chain.textContent = bits.join(" · ") || "—";
      if (str(o.error)) chain.title = str(o.error);
      tr.appendChild(chain);
      tbody.appendChild(tr);
      registerEvidence("quality", str(o.name) || str(o.id), tr);
    });

    var gateBox = $("#planGate");
    clear(gateBox);
    var boxes = [
      ["闸门等级", str(gate.level) || "—", str(gate.reason) || ""],
      ["置信度上限", isNum(gate.confidence_cap) ? num(gate.confidence_cap, 0) + "%" : "无", "数据不足时强制封顶"],
      ["覆盖", isNum(gate.coverage) ? num(ratio01(gate.coverage) * 100, 1) + "%"
        : (isNum(health.ok_ratio) ? num(ratio01(health.ok_ratio) * 100, 1) + "%" : "—"), "关键能力满足比例"],
      ["缺失 / 失败", String(uniq(arr(gate.missing).concat(arr(gate.failed))).length) + " 项",
        uniq(arr(gate.missing).concat(arr(gate.failed))).slice(0, 6).join("、") || "无"]
    ];
    boxes.forEach(function (b) {
      var box = el("div", "gate-box");
      box.appendChild(el("div", "k", b[0]));
      box.appendChild(el("div", "v", b[1]));
      if (b[2]) box.appendChild(el("p", null, b[2]));
      gateBox.appendChild(box);
    });
  }

  /* ------------------------------------------------------------ 维度 */
  function renderDims() {
    var p = PAYLOAD, dims = dimsOf(p), wrap = $("#dims");
    clear(wrap);
    if (!dims.length) { wrap.appendChild(el("p", "hint", "暂无维度数据。")); return; }
    dims.forEach(function (raw, idx) {
      var d = obj(raw);
      var card = el("div", "dim" + (idx === 0 ? " open" : ""));
      var head = el("div", "dim-head");
      head.tabIndex = 0;
      head.setAttribute("role", "button");
      head.setAttribute("aria-expanded", idx === 0 ? "true" : "false");
      head.setAttribute("aria-controls", "dim-body-" + idx);
      var title = el("div", "dim-title");
      title.appendChild(layerBadge(d.layer || "induction"));
      title.appendChild(el("h3", null, str(d.name) || str(d.id) || "维度"));
      if (str(d.question)) title.appendChild(el("span", "dim-q", str(d.question)));
      head.appendChild(title);
      var score = el("div", "dim-score");
      score.appendChild(el("span", "s " + upDown(d.score), fmt(d.score)));
      var pill = el("span", "dim-status " + statusClass(d.status), str(d.status) || "—");
      score.appendChild(pill);
      score.appendChild(el("span", "dim-toggle"));
      head.appendChild(score);
      card.appendChild(head);

      var body = el("div", "dim-body");
      body.id = "dim-body-" + idx;
      if (str(d.headline)) body.appendChild(el("p", "dim-headline", str(d.headline)));

      var facts = arr(d.facts);
      if (facts.length) {
        body.appendChild(el("div", "dim-sub", "事实层 · 点开任一行查看证据链"));
        var table = el("div", "facts");
        facts.forEach(function (rf, fi) {
          var f = obj(rf);
          var row = el("button", "fact-row");
          row.type = "button";
          row.setAttribute("aria-expanded", "false");
          row.setAttribute("aria-controls", "fact-detail-" + idx + "-" + fi);
          row.appendChild(el("span", "fl", str(f.label) || "—"));
          var v = el("span", "fv " + upDown(typeof f.value === "number" ? f.value : null), fmt(f.value, f.unit));
          row.appendChild(v);
          row.appendChild(el("span", "fn", str(f.note) || ""));
          row.appendChild(el("span", "fz", str(f.source) || "无来源"));
          table.appendChild(row);

          var det = el("div", "fact-detail");
          det.id = "fact-detail-" + idx + "-" + fi;
          var dl = document.createElement("dl");
          function put(k, val) {
            dl.appendChild(el("dt", null, k));
            var dd = el("dd");
            if (val instanceof Node) dd.appendChild(val);
            else dd.textContent = val === undefined || val === null || val === "" ? "—" : String(val);
            dl.appendChild(dd);
          }
          put("层", str(f.layer) || "fact");
          put("来源", str(f.source) || "—");
          put("时点 (as_of)", str(f.as_of) || str(d.as_of) || asOfOf(p));
          put("口径", str(f.caliber) || str(d.caliber) || "—");
          if (str(f.raw_field)) {
            var code = el("code", null, str(f.raw_field));
            put("原始字段", code);
          }
          if (str(f.note)) put("说明", str(f.note));
          if (isNum(f.value)) put("读数", fmt(f.value, f.unit));
          det.appendChild(dl);
          table.appendChild(det);
          row.addEventListener("click", function () {
            var open = row.getAttribute("aria-expanded") === "true";
            row.setAttribute("aria-expanded", open ? "false" : "true");
            det.classList.toggle("is-open", !open);
          });
          registerEvidence(d.id, str(f.label), row);
        });
        body.appendChild(table);
      } else {
        body.appendChild(el("p", "hint", "该维度暂无事实数据。"));
      }

      var signals = arr(d.signals);
      if (signals.length) {
        body.appendChild(el("div", "dim-sub", "信号"));
        var ul = el("ul", "signals");
        signals.slice(0, 8).forEach(function (s) {
          ul.appendChild(el("li", null, str(obj(s).text) || String(s)));
        });
        body.appendChild(ul);
      }

      var meta = el("div", "dim-sub", "口径与不确定性");
      body.appendChild(meta);
      var mlist = el("div", "meta-list");
      function metaRow(k, val) {
        if (val === undefined || val === null || val === "" || (arr(val).length === 0)) return;
        var row = el("div");
        row.appendChild(el("b", null, k + "："));
        row.appendChild(document.createTextNode(arr(val).length ? arr(val).join("；") : String(val)));
        mlist.appendChild(row);
      }
      metaRow("口径", str(d.caliber));
      metaRow("不确定性", arr(d.uncertainty).length ? arr(d.uncertainty) : str(d.uncertainty));
      metaRow("判断依据", str(d.confidence_reason));
      metaRow("数据缺口", arr(d.gaps));
      if (isNum(d.confidence)) metaRow("置信度", num(d.confidence * (d.confidence <= 1 ? 100 : 1), 0) + "%");
      body.appendChild(mlist);
      card.appendChild(body);

      function toggle() {
        var open = card.classList.toggle("open");
        head.setAttribute("aria-expanded", open ? "true" : "false");
      }
      head.addEventListener("click", toggle);
      head.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") { e.preventDefault(); toggle(); }
      });
      wrap.appendChild(card);
      registerEvidence(d.id, str(d.name), card);
    });
  }

  /* ------------------------------------------------------------ 矛盾 */
  function renderContra() {
    var p = PAYLOAD, list = arr(p.contradictions), wrap = $("#contra");
    clear(wrap);
    if (!list.length) { wrap.appendChild(el("p", "hint", "七维证据方向一致，未检测到显著矛盾。")); return; }
    list.forEach(function (raw) {
      var c = obj(raw), card = el("div", "contra-card");
      var head = el("div", "contra-card-head");
      head.appendChild(layerBadge("judgment"));
      head.appendChild(el("h3", null, str(c.title) || str(c.id) || "矛盾"));
      if (isNum(c.strength)) head.appendChild(el("span", "contra-strength", "强度 " + num(c.strength, 2)));
      card.appendChild(head);
      if (str(c.text)) card.appendChild(el("p", "contra-text", str(c.text)));
      var detail = arr(c.detail);
      if (detail.length) {
        var vs = el("div", "vs");
        detail.slice(0, 4).forEach(function (rd) {
          var d = obj(rd), item = el("div", "vs-item");
          var dh = el("div", "vs-d");
          dh.appendChild(el("span", null, str(d.dim) || "维度"));
          dh.appendChild(el("span", "dim-status " + statusClass(d.status), str(d.status) || "—"));
          item.appendChild(dh);
          item.appendChild(el("div", "vs-s " + upDown(d.score), fmt(d.score)));
          if (str(d.headline)) item.appendChild(el("p", "hint", str(d.headline)));
          vs.appendChild(item);
        });
        card.appendChild(vs);
      }
      if (str(c.check)) {
        var check = el("div", "check");
        check.appendChild(el("b", null, "怎么检验："));
        check.appendChild(document.createTextNode(str(c.check)));
        card.appendChild(check);
      }
      wrap.appendChild(card);
      arr(c.dims).forEach(function (id) {
        registerEvidence(id, str(c.title), card);
        registerEvidence(id, str(id), card);
      });
    });
  }

  /* ------------------------------------------------------------ 条件 */
  function renderConds() {
    var p = PAYLOAD, conds = arr(p.conditions), wrap = $("#conds");
    clear(wrap);
    if (!conds.length) { wrap.appendChild(el("p", "hint", "暂无判断条件。")); }
    var sorted = conds.slice().sort(function (a, b) {
      var da = isNum(obj(a).distance_pct) ? Math.abs(obj(a).distance_pct) : 1e9;
      var db = isNum(obj(b).distance_pct) ? Math.abs(obj(b).distance_pct) : 1e9;
      return da - db;
    });
    sorted.forEach(function (raw) {
      var c = obj(raw);
      var row = el("div", "cond");
      var main = el("div", "cond-main");
      var text = el("div", "cond-text");
      text.appendChild(layerBadge("condition"));
      text.appendChild(document.createTextNode(" " + (str(c.text) || "—")));
      if (c.triggered) text.appendChild(el("span", "cond-triggered", "已触发"));
      main.appendChild(text);
      if (str(c.why)) main.appendChild(el("p", "cond-why", str(c.why)));
      row.appendChild(main);
      var right = el("div", "cond-right");
      right.appendChild(el("div", null, "当前 " + fmt(c.current, c.unit)));
      right.appendChild(el("div", null, "阈值 " + (str(c.comparator) || "") + " " + fmt(c.threshold, c.unit)));
      if (isNum(c.distance_pct)) {
        right.appendChild(el("div", "cond-dist", "距离 " + num(Math.abs(c.distance_pct), 1) + (c.distance_is_pp ? "pp" : "%")));
      }
      row.appendChild(right);
      wrap.appendChild(row);
      registerEvidence("conditions", str(c.text), row);
    });

    var trig = arr(p.triggers), box = $("#triggers");
    clear(box);
    if (!trig.length) { box.appendChild(el("p", "hint", "当前没有条件被触发。")); return; }
    trig.forEach(function (t) {
      box.appendChild(el("span", "trig-chip", str(obj(t).text) || str(obj(t).label) || String(t)));
    });
  }

  /* ------------------------------------------------------------ 行业 */
  function renderIndustry() {
    var p = PAYLOAD, ind = obj(p.industry), rows = arr(ind.rows), tbody = $("#industryRows");
    clear(tbody);
    setText("industryMeta", [
      str(ind.as_of) || asOfOf(p),
      isNum(ind.count) ? num(ind.count, 0) + " 个行业" : "",
      str(ind.source) || ""
    ].filter(Boolean).join(" · ") || "—");
    var note = $("#industryNote");
    if (str(ind.caliber) || str(ind.note)) {
      note.hidden = false;
      note.textContent = [str(ind.caliber), str(ind.note)].filter(Boolean).join("；");
    } else note.hidden = true;

    if (!rows.length) {
      var tr = el("tr");
      var td = el("td", "empty", "暂无行业数据（行业日线能力可能降级或失败，见取数计划）。");
      td.colSpan = 7;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    rows = rows.slice().sort(function (a, b) {
      var ra = obj(a).rank_20, rb = obj(b).rank_20;
      if (isNum(ra) && isNum(rb)) return ra - rb;
      if (isNum(ra)) return -1;
      if (isNum(rb)) return 1;
      return (obj(b).r20 || 0) - (obj(a).r20 || 0);
    });
    var maxAbs = 0;
    rows.forEach(function (r) { if (isNum(obj(r).r20)) maxAbs = Math.max(maxAbs, Math.abs(obj(r).r20)); });
    rows.forEach(function (raw, i) {
      var r = obj(raw), tr = el("tr");
      tr.appendChild(el("td", "num rank-no", isNum(r.rank_20) ? num(r.rank_20, 0) : String(i + 1)));
      tr.appendChild(el("td", "industry-name", str(r.name) || "—"));
      tr.appendChild(el("td", "num " + upDown(r.r5), signed(r.r5, 2, "%")));
      tr.appendChild(el("td", "num " + upDown(r.r20), signed(r.r20, 2, "%")));
      var heatTd = el("td");
      var bar = el("div", "heat-bar");
      var fill = el("i");
      var width = maxAbs > 0 && isNum(r.r20) ? clamp(Math.abs(r.r20) / maxAbs * 100, 6, 100) : 6;
      fill.style.width = width.toFixed(0) + "%";
      fill.style.background = heatColor(r.r20, maxAbs);
      bar.appendChild(fill);
      heatTd.appendChild(bar);
      tr.appendChild(heatTd);
      tr.appendChild(el("td", "", r.above_ma20 === true ? "是" : r.above_ma20 === false ? "否" : "—"));
      tr.appendChild(el("td", "num", isNum(r.up_ratio) ? num(ratio01(r.up_ratio) * 100, 0) + "%" : "—"));
      tbody.appendChild(tr);
      registerEvidence("industry", str(r.name), tr);
    });
  }

  /* ------------------------------------------------------------ 风格 */
  function renderStyles() {
    var p = PAYLOAD, styles = arr(p.styles), wrap = $("#styleCards");
    clear(wrap);
    if (!styles.length) { wrap.appendChild(el("p", "hint", "暂无风格对比数据。")); return; }
    styles.forEach(function (raw) {
      var s = obj(raw), card = el("div", "card style-card");
      var head = el("div", "style-head");
      head.appendChild(el("h3", null, str(s.label) || str(s.id) || "风格"));
      if (str(s.winner)) head.appendChild(el("span", "style-win", "占优：" + str(s.winner)));
      card.appendChild(head);
      card.appendChild(el("div", "style-spread", "20 日差 " +
        (isNum(s.spread_20d) ? signed(s.spread_20d, 2, s.spread_unit === "pct" ? "pp" : "") : "—") +
        (str(s.winner) ? "，方向：" + str(s.winner) : "")));
      var vals = [obj({ v: s.leader_r20 }), obj({ v: s.lagger_r20 })];
      void vals;
      var maxAbs = Math.max(Math.abs(isNum(s.leader_r20) ? s.leader_r20 : 0), Math.abs(isNum(s.lagger_r20) ? s.lagger_r20 : 0), 0.01);
      var bars = el("div", "style-bars");
      [["leader", s.leader, s.leader_r20], ["lagger", s.lagger, s.lagger_r20]].forEach(function (pair) {
        var row = el("div", "sbar");
        row.appendChild(el("span", null, pair[0] === "leader" ? "领先" : "落后"));
        var track = el("div", "track");
        var fill = el("i");
        fill.style.left = "0";
        fill.style.width = (clamp(Math.abs(isNum(pair[2]) ? pair[2] : 0) / maxAbs * 100, 2, 100)).toFixed(0) + "%";
        fill.style.background = pair[0] === "leader" ? "linear-gradient(90deg,#ff8a5c,#ff5064)" : "linear-gradient(90deg,#16c784,#0e9e6a)";
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(el("span", "val", (str(pair[1]) || "—") + " " + signed(pair[2], 2, "%")));
        bars.appendChild(row);
      });
      card.appendChild(bars);
      if (str(s.note)) card.appendChild(el("p", "style-note", str(s.note)));
      wrap.appendChild(card);
      registerEvidence("style", str(s.label), card);
    });
  }

  /* ------------------------------------------------------------ 历史 */
  function renderHistory() {
    var p = PAYLOAD, hp = obj(p.history_phases), phases = arr(hp.phases);
    var meta = $("#historyMeta");
    var bits = [];
    if (str(hp.method)) bits.push("方法：" + str(hp.method));
    if (isNum(hp.window_days)) bits.push("窗口 " + num(hp.window_days, 0) + " 个交易日");
    bits.push("代理口径：用指数日线切分，不是状态模型的历史回溯");
    meta.textContent = bits.join(" · ") + (str(hp.caveat) ? "。" + str(hp.caveat) : "");

    var tl = $("#historyTimeline"), detail = $("#historyDetail");
    clear(tl);
    clear(detail);
    if (!phases.length) {
      detail.appendChild(el("p", "hint", "暂无历史阶段数据（需要指数日线）。"));
      return;
    }
    var byId = {};
    phases.forEach(function (raw) {
      var ph = obj(raw);
      byId[ph.id || ""] = ph;
      var btn = el("button", "phase");
      btn.type = "button";
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", ph.id === hp.current_phase ? "true" : "false");
      btn.appendChild(el("span", "p-range", (str(ph.start).slice(5) || "—") + " → " + (str(ph.end).slice(5) || "—")));
      btn.appendChild(el("span", "p-name", str(ph.regime) || "阶段"));
      btn.appendChild(el("span", "p-trend " + upDown(ph.trend), signed(ph.trend, 1, "%")));
      if (ph.id === hp.current_phase) btn.appendChild(el("span", "p-now", "当前"));
      btn.addEventListener("click", function () { selectPhase(ph, hp, byId); });
      tl.appendChild(btn);
      registerEvidence("history", (str(ph.start) + " → " + str(ph.end)), btn);
      registerEvidence("history", str(ph.start), btn);
    });
    var current = byId[hp.current_phase] || obj(phases[phases.length - 1]);
    selectPhase(current, hp, byId);
  }

  function selectPhase(ph, hp, byId) {
    ACTIVE_PHASE = ph && ph.id;
    $$("#historyTimeline .phase").forEach(function (b, i) {
      b.setAttribute("aria-selected", (arr(hp.phases)[i] && obj(arr(hp.phases)[i]).id === ACTIVE_PHASE) ? "true" : "false");
    });
    var detail = $("#historyDetail");
    clear(detail);
    if (!ph) return;
    var head = el("h3");
    head.appendChild(el("span", null, (str(ph.start) || "—") + " → " + (str(ph.end) || "—")));
    if (ph.id === hp.current_phase) head.appendChild(el("span", "hint", " 当前阶段"));
    detail.appendChild(head);
    var grid = el("div", "hd-grid");
    [
      ["天数", isNum(ph.days) ? num(ph.days, 0) + " 天" : "—"],
      ["区间涨跌", signed(ph.trend, 2, "%")],
      ["波动分位", isNum(ph.vol_pct) ? num(ph.vol_pct, 2) : "—"],
      ["行业广度", isNum(ph.breadth_proxy) ? num(ph.breadth_proxy * 100, 0) + "%" : "—"]
    ].forEach(function (pair) {
      var box = el("div", "hd-item");
      box.appendChild(el("div", "k", pair[0]));
      box.appendChild(el("div", "v", pair[1]));
      grid.appendChild(box);
    });
    detail.appendChild(grid);
    if (str(ph.summary)) detail.appendChild(el("p", "hd-diff", "形态：" + str(ph.summary)));
    if (str(ph.diff_vs_today)) detail.appendChild(el("p", "hd-diff", "与今天相比：" + str(ph.diff_vs_today)));
    var caveat = str(hp.caveat) || "历史阶段为代理口径（proxy）：由指数日线切分，不代表真实状态模型的历史回溯，也不能用于类比外推。";
    detail.appendChild(el("p", "hd-caveat", "口径 caveat：" + caveat));
    void byId;
  }

  /* ------------------------------------------------------------ 数据健康 */
  function renderHealth() {
    var p = PAYLOAD, dh = obj(p.data_health), q = obj(dh.quality), gate = gateOf(p);
    var sources = arr(dh.sources), catalog = arr(dh.catalog);
    var srcList = sources.length ? sources : catalog;
    var available = srcList.filter(function (s) { return obj(s).available !== false; }).length;

    var stats = $("#healthStats");
    clear(stats);
    var items = [
      [isNum(dh.market_rows) ? num(dh.market_rows, 0) : "—", "市场标的数"],
      [isNum(dh.ok_ratio) ? num(ratio01(dh.ok_ratio) * 100, 1) + "%" : "—", "抓取成功率"],
      [srcList.length ? available + " / " + srcList.length : "—", "可用数据源"],
      [isNum(p.elapsed_ms) ? num(p.elapsed_ms, 0) + "ms" : "—", "研判耗时"],
      [isNum(q.score) ? num(q.score, 1) : "—", "质量分"],
      [isNum(q.coverage) ? num(ratio01(q.coverage) * 100, 0) + "%" : "—", "覆盖率"],
      [isNum(q.freshness) ? num(q.freshness, 2) : "—", "新鲜度"],
      [str(gate.level) || "—", "质量闸门"]
    ];
    items.forEach(function (pair) {
      var s = el("div", "stat");
      s.appendChild(el("b", null, pair[0]));
      s.appendChild(el("span", null, pair[1]));
      stats.appendChild(s);
    });
    setText("healthHint", (str(p.as_of_label) || asOfOf(p) || "") + " · " + (MODE === "static" ? "静态快照" : "实时取数"));

    var qp = $("#qualityPanel");
    clear(qp);
    function qbox(title, itemsList, emptyText) {
      var box = el("div", "q-box");
      box.appendChild(el("h4", null, title));
      if (!itemsList.length) {
        box.appendChild(el("p", "q-empty", emptyText || "无"));
      } else {
        var ul = el("ul");
        itemsList.forEach(function (t) { ul.appendChild(el("li", null, t)); });
        box.appendChild(ul);
      }
      return box;
    }
    var conflicts = arr(q.conflicts).map(function (raw) {
      var c = obj(raw), a = obj(c.a), b = obj(c.b);
      return str(c.field) + "：" + (str(a.source) || "A") + "=" + fmt(a.value) + " vs " +
        (str(b.source) || "B") + "=" + fmt(b.value) +
        (isNum(c.delta_pct) ? "，差 " + num(c.delta_pct, 2) + "%" : "") +
        (str(c.resolution) ? "，处理：" + str(c.resolution) : "");
    });
    var missing = arr(q.missing).map(String);
    qp.appendChild(qbox("数据冲突（" + conflicts.length + "）", conflicts, "未检测到跨源冲突"));
    qp.appendChild(qbox("缺失与陈旧", missing.concat(arr(q.stale).map(function (s) {
      var o = obj(s);
      return (str(o.field) || str(o.name) || "字段") + (isNum(o.lag_days) ? " 滞后 " + num(o.lag_days, 0) + " 天" : "");
    })), "无缺失"));
    if (isNum(q.score) && isNum(gate.confidence_cap) && q.score < gate.confidence_cap) {
      var note = qbox("闸门说明", [str(gate.reason) || "质量分低于置信度上限，判断已降级"], "");
      qp.appendChild(note);
    }
    if (arr(dh.errors).length) {
      qp.appendChild(qbox("取数错误", arr(dh.errors).map(function (e) {
        var o = obj(e);
        return (str(o.cap) || str(o.key) || "能力") + "：" + (str(o.error) || String(e));
      }), ""));
    }

    var prov = $("#providers");
    clear(prov);
    if (!srcList.length) {
      prov.appendChild(el("p", "hint", "暂无数据源信息。"));
    } else {
      srcList.forEach(function (raw) {
        var s = obj(raw);
        var row = el("div", "prov");
        var dot = el("span", "dot " + (s.available === false ? "bad" : "ok"));
        row.appendChild(dot);
        row.appendChild(el("span", "name", str(s.label) || str(s.name) || "—"));
        var bits = [];
        if (str(s.name) && str(s.label)) bits.push(str(s.name));
        if (str(s.as_of)) bits.push(str(s.as_of));
        if (isNum(s.latency_ms)) bits.push(num(s.latency_ms, 0) + "ms");
        if (s.available === false) bits.push("不可用");
        if (arr(s.dead_caps).length) bits.push("缺失 " + arr(s.dead_caps).length + " 项");
        if (str(s.error)) bits.push(str(s.error));
        row.appendChild(el("span", "prov-detail", bits.join(" · ") || "正常"));
        prov.appendChild(row);
        registerEvidence("quality", str(s.name) || str(s.label), row);
      });
    }

    var flog = $("#fetchLog");
    clear(flog);
    var logs = arr(dh.fetch_log);
    if (!logs.length) {
      flog.appendChild(el("p", "hint", "暂无抓取日志。"));
    } else {
      logs.slice(0, 40).forEach(function (raw) {
        var lg = obj(raw);
        var row = el("div", "logrow " + (lg.ok ? "ok" : "bad"));
        var label = str(lg.key) || str(lg.cap) || "fetch";
        if (lg.from_cache) label += " (cache)";
        row.appendChild(el("span", null, label));
        row.appendChild(el("span", "lg-ms", isNum(lg.elapsed_ms) ? num(lg.elapsed_ms, 0) + "ms" : "—"));
        row.appendChild(el("span", "lg-time", (str(lg.fetched_at) || "").replace("T", " ").slice(5, 19) || "—"));
        if (str(lg.error)) row.title = str(lg.error);
        flog.appendChild(row);
      });
    }

    var evs = $("#events");
    clear(evs);
    var events = arr(p.events);
    if (!events.length) evs.appendChild(el("p", "hint", "暂无事件。"));
    events.slice(0, 10).forEach(function (raw) {
      var e = obj(raw), box = el("div", "ev");
      var link = el("a", null, str(e.title) || "事件");
      link.href = str(e.url) || "#";
      if (str(e.url)) { link.target = "_blank"; link.rel = "noopener noreferrer"; }
      box.appendChild(link);
      box.appendChild(el("div", "ev-meta", [str(e.time), str(e.source)].filter(Boolean).join(" · ")));
      if (str(e.summary)) box.appendChild(el("p", null, str(e.summary)));
      evs.appendChild(box);
    });
  }

  /* ------------------------------------------------------------ 追问 */
  function suggestions() {
    var out = ["当前市场处于什么状态？", "现在的主要矛盾是什么？", "什么条件会改变当前判断？",
      "哪些行业在领涨？", "大小盘风格谁占优？", "现在的主要风险在哪里？", "这次研判的数据质量如何？"];
    var rs = arr(obj(PAYLOAD && PAYLOAD.research).suggested);
    rs.forEach(function (s) {
      if (typeof s === "string") out.push(s);
      else if (s && s.question) out.push(String(s.question));
    });
    return uniq(out).slice(0, 8);
  }

  function setupAsk() {
    var chips = $("#askChips"), form = $("#askForm"), input = $("#askInput"), log = $("#askLog");
    clear(chips);
    suggestions().forEach(function (q) {
      var b = el("button", "chip", q);
      b.type = "button";
      b.addEventListener("click", function () { ask(q); });
      chips.appendChild(b);
    });
    if (MODE === "static") {
      setText("askHint", "静态快照 · 本地证据推演（不联网、不调用模型），每条回答带引用");
    } else {
      setText("askHint", "每条回答都带引用，可点击定位证据");
    }
    if (form && !form.__bound) {
      form.__bound = true;
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var q = input.value.trim();
        if (!q) return;
        ask(q);
        input.value = "";
      });
    }
    void log;
  }

  function ask(question) {
    var log = $("#askLog"), form = $("#askForm");
    if (!log) return;
    var pending = el("div", "qa is-pending");
    pending.setAttribute("data-q", question);
    pending.appendChild(el("div", "qa-q", question));
    pending.appendChild(el("div", "qa-a", MODE === "static" ? "本地证据推演中…" : "检索证据中…"));
    log.appendChild(pending);
    log.scrollTop = log.scrollHeight;
    var submit = $("#askSubmit");
    if (submit) submit.disabled = true;

    function finish(payload) {
      pending.classList.remove("is-pending");
      renderAnswer(pending, payload);
      if (log.children.length > 12) log.removeChild(log.firstChild);
      log.scrollTop = log.scrollHeight;
      if (submit) submit.disabled = false;
    }

    if (MODE === "static") {
      finish(localAnswer(question, []));
      return;
    }
    fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ question: question, history: HISTORY.slice(-3) })
    })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          return j;
        });
      })
      .then(function (j) {
        if (!j || j.ok === false) throw new Error((j && j.error) || "追问失败");
        finish(j);
      })
      .catch(function (e) {
        if (window.MINGSHI_RESEARCH && PAYLOAD) {
          var res = localAnswer(question, ["后端追问不可用（" + (e.message || e) + "），已回退到本地证据推演。"]);
          finish(res);
        } else {
          pending.classList.remove("is-pending");
          clear(pending);
          pending.appendChild(el("div", "qa-q", question));
          pending.appendChild(el("div", "qa-a", "追问失败：" + (e.message || e)));
          if (submit) submit.disabled = false;
        }
      });
  }

  function localAnswer(question, extraNotes) {
    try {
      var res = window.MINGSHI_RESEARCH.answer(question, PAYLOAD, HISTORY.slice(-3));
      res.notes = arr(res.notes).concat(arr(extraNotes));
      return res;
    } catch (e) {
      return {
        ok: false, engine: "static", intent: "unknown",
        answer: "本地推演模块执行失败：" + (e.message || e) + "。这不代表没有数据，请刷新页面重试。",
        citations: [], notes: arr(extraNotes), followups: [], refused: false
      };
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function formatAnswer(text) {
    var html = escapeHtml(text);
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return html;
  }

  function renderAnswer(node, res) {
    var r = obj(res);
    var qText = str(r.question) || node.getAttribute("data-q") || "";
    clear(node);
    node.appendChild(el("div", "qa-q", qText || "（问题）"));
    var a = el("div", "qa-a");
    a.innerHTML = formatAnswer(str(r.answer) || "（无内容）");
    node.appendChild(a);

    if (r.refused) node.classList.add("qa-refusal");

    var cites = arr(r.citations);
    if (cites.length) {
      var cbox = el("div", "qa-cites");
      cites.forEach(function (rc) {
        var c = obj(rc);
        var label = (str(c.dim) || str(c.dim_id) || "证据") + " · " + (str(c.label) || "—");
        if (c.value !== undefined && c.value !== null) label += " " + fmt(c.value, c.unit);
        var btn = el("button", "cite", label);
        btn.type = "button";
        btn.title = "点击定位到对应证据";
        btn.addEventListener("click", function () { highlightEvidence(c); });
        cbox.appendChild(btn);
      });
      node.appendChild(cbox);
    } else if (r.refused) {
      node.appendChild(el("div", "qa-note", "合规拒答：不回答交易类问题，因此不引用任何证据。"));
    }

    var meta = el("div", "qa-meta");
    var engine = str(r.engine);
    var engineLabel = engine === "static" || MODE === "static"
      ? "本地证据推演"
      : engine.indexOf("llm") === 0 ? "模型增强 · " + engine.slice(3) : engine === "guardrail" ? "合规拦截" : "检索式研判";
    meta.appendChild(el("span", null, engineLabel));
    if (str(r.intent)) meta.appendChild(el("span", null, "intent " + str(r.intent)));
    if (isNum(r.confidence)) meta.appendChild(el("span", null, "置信度 " + num(r.confidence, 1)));
    if (str(r.as_of)) meta.appendChild(el("span", null, "as_of " + str(r.as_of)));
    if (str(r.mode) === "static" || MODE === "static") meta.appendChild(el("span", null, "不联网"));
    node.appendChild(meta);

    arr(r.notes).forEach(function (n) { if (str(n)) node.appendChild(el("div", "qa-note", str(n))); });
    var comp = obj(r.compliance);
    if (arr(comp.violations).length) {
      node.appendChild(el("div", "qa-warn", "合规检查：" + arr(comp.violations).length + " 处措辞已改写或拦截。"));
    }
    if (isNum(comp.scrubbed) && comp.scrubbed > 0) {
      node.appendChild(el("div", "qa-note", "已按合规规则改写 " + comp.scrubbed + " 处表述。"));
    }
    var fus = arr(r.followups);
    if (fus.length) {
      var box = el("div", "chips");
      box.style.marginTop = "9px";
      fus.slice(0, 4).forEach(function (q) {
        var b = el("button", "chip", str(q));
        b.type = "button";
        b.addEventListener("click", function () { ask(str(q)); });
        box.appendChild(b);
      });
      node.appendChild(box);
    }
    HISTORY.push({ question: qText, answer: str(r.answer) });
    if (HISTORY.length > 6) HISTORY.shift();
  }

  function highlightEvidence(c) {
    var cObj = obj(c);
    var key = evidenceKey(cObj.dim_id, cObj.label);
    var node = EVIDENCE[key] || EVIDENCE["*|" + str(cObj.label)];
    if (!node && str(cObj.dim_id)) node = EVIDENCE[evidenceKey(cObj.dim_id, str(cObj.dim_id))];
    if (!node && str(cObj.dim_id)) {
      var card = document.querySelector('[data-evidence-dim="' + str(cObj.dim_id) + '"]');
      node = card || null;
    }
    if (!node) {
      // 没有精确证据时，退到对应分区
      var map = { industry: "#sec-industry", history: "#sec-history", style: "#sec-style", quality: "#planRows", conditions: "#sec-cond", regime: "#sec-state" };
      var sec = document.querySelector(map[str(cObj.dim_id)] || "");
      if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    var dimCard = node.closest ? node.closest(".dim") : null;
    if (dimCard && !dimCard.classList.contains("open")) {
      dimCard.classList.add("open");
      var head = dimCard.querySelector(".dim-head");
      if (head) head.setAttribute("aria-expanded", "true");
    }
    try { node.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (e) { node.scrollIntoView(); }
    node.classList.add("evidence-hit");
    if (node.setAttribute) node.setAttribute("tabindex", "-1");
    setTimeout(function () { node.classList.remove("evidence-hit"); }, 2600);
  }

  /* ------------------------------------------------------------ 分区导航 */
  function setupTabs() {
    var tabs = $$(".tab"), secs = $$(".sec");
    if (!tabs.length) return;
    tabs.forEach(function (t) {
      if (t.__bound) return;
      t.__bound = true;
      t.addEventListener("click", function () {
        var target = document.getElementById(t.getAttribute("data-go"));
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    if (window.IntersectionObserver && !window.__mingshiObs) {
      try {
        var obs = new IntersectionObserver(function (entries) {
          entries.forEach(function (en) {
            if (!en.isIntersecting) return;
            tabs.forEach(function (t) {
              var on = t.getAttribute("data-go") === en.target.id;
              t.classList.toggle("is-on", on);
              if (on) t.setAttribute("aria-current", "true"); else t.removeAttribute("aria-current");
            });
          });
        }, { rootMargin: "-45% 0px -50% 0px" });
        secs.forEach(function (s) { obs.observe(s); });
        window.__mingshiObs = obs;
      } catch (e) { /* 忽略：导航高亮不是关键路径 */ }
    }
    var btn = $("#btnRefresh");
    if (btn && !btn.__bound) {
      btn.__bound = true;
      btn.addEventListener("click", function () {
        if (MODE === "static") return;
        load(true);
      });
    }
  }

  function load(force) {
    if (MODE === "static") return;
    show($("#loading"), true);
    show($("#err"), false);
    fetch("/api/dashboard" + (force ? "?refresh=1" : ""), { cache: "no-store" })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          return j;
        });
      })
      .then(function (j) {
        PAYLOAD = (j && j.payload) || j;
        show($("#loading"), false);
        paint();
      })
      .catch(function (e) {
        show($("#loading"), false);
        var box = $("#err");
        if (box) {
          clear(box);
          box.appendChild(el("b", null, "刷新失败：" + (e.message || e)));
          box.appendChild(el("p", null, "页面保留上一次成功的数据；可稍后重试，或检查后端日志。"));
          show(box, true);
        }
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
