/* 明势 Míngshì — 前端渲染
   纯手写 SVG，无第三方库：打包体积小、离线可用、图表语义可控。 */

'use strict';

const $ = (s) => document.querySelector(s);
const NS = 'http://www.w3.org/2000/svg';

let PAYLOAD = null;
let SECTOR_MODE = 'r5';

/* ── 工具 ──────────────────────────────────────────────────────────── */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
function svg(tag, attrs = {}) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}
function num(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}
function signed(v, digits = 2, suffix = '') {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return (v > 0 ? '+' : '') + Number(v).toFixed(digits) + suffix;
}
function cls(v) {
  if (v === null || v === undefined) return 'flat';
  return v > 0.12 ? 'up' : v < -0.12 ? 'down' : 'flat';
}
function fmtValue(v, unit) {
  if (v === null || v === undefined) return '—';
  let s;
  if (typeof v === 'number') {
    s = Math.abs(v) >= 1000 ? v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
                            : (Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2));
  } else s = String(v);
  if (!unit) return s;
  if (unit === '%' || unit === '倍') return s + unit;
  return s + ' ' + unit;
}
function colors(mode) {
  // 红涨绿跌（A 股习惯）
  return mode === 'up' ? 'var(--up)' : mode === 'down' ? 'var(--down)' : 'var(--warn)';
}
function scoreColor(s) {
  if (s === null || s === undefined) return 'var(--text-3)';
  return s > 0.12 ? 'var(--up)' : s < -0.12 ? 'var(--down)' : 'var(--warn)';
}
function heatColor(v) {
  if (v === null || v === undefined) return 'rgba(255,255,255,.04)';
  const a = Math.min(Math.abs(v) / 5.5, 1);
  return v >= 0 ? `rgba(255,80,100,${0.10 + a * 0.62})`
                : `rgba(22,199,132,${0.10 + a * 0.62})`;
}

/* ── 载入 ──────────────────────────────────────────────────────────── */
async function load(force = false) {
  const url = '/api/dashboard' + (force ? '?refresh=1' : '');
  try {
    const r = await fetch(url);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    PAYLOAD = data;
    render(data);
  } catch (e) {
    $('#loading').hidden = true;
    $('#content').hidden = false;
    $('#content').insertAdjacentHTML('afterbegin',
      `<div class="err">研判失败：${String(e.message || e)}<br>可能是数据源临时不可达，请稍后点击「刷新」。</div>`);
  }
}

/* ── 总装 ──────────────────────────────────────────────────────────── */
function render(p) {
  $('#loading').hidden = true;
  $('#content').hidden = false;
  $('#asOf').textContent = p.as_of || '—';
  $('#footMeta').textContent =
    `v${p.version} · 生成 ${p.generated_at} · ${p.elapsed_ms}ms · ${p.data_health.market_rows} 只标的`;
  if (p.disclaimer) $('#footLegal').textContent = p.disclaimer;

  renderHero(p);
  renderMap(p);
  renderRadar(p);
  renderDims(p);
  renderContra(p);
  renderConds(p);
  renderRotation(p);
  renderHealth(p);
  setupAsk(p);
}

/* ── 状态总览 ──────────────────────────────────────────────────────── */
function renderHero(p) {
  const reg = p.regime, nar = p.narrative || {};
  $('#stateName').textContent = reg.name || '—';

  const sub = reg.substate || nar.substate;
  const badge = $('#substateBadge');
  if (sub) { badge.hidden = false; badge.textContent = sub; } else badge.hidden = true;

  $('#stateSummary').textContent = nar.summary || '';
  $('#stateNote').textContent = nar.state_read || '';

  const conf = reg.confidence ?? 0;
  $('#confNum').textContent = conf;
  const C = 2 * Math.PI * 50;
  const fill = $('#gaugeFill');
  fill.style.strokeDasharray = C;
  fill.style.strokeDashoffset = C * (1 - conf / 100);
  fill.style.stroke = conf >= 70 ? 'var(--accent)' : conf >= 45 ? 'var(--warn)' : 'var(--alert)';

  const setAxis = (barId, valId, v) => {
    const bar = $(barId), val = $(valId);
    const x = Math.max(-1, Math.min(1, v ?? 0));
    bar.style.left = (x >= 0 ? 50 : 50 + x * 50) + '%';
    bar.style.width = Math.abs(x) * 50 + '%';
    bar.style.background = scoreColor(x);
    val.textContent = num(v, 2);
    val.className = 'axis-val ' + cls(x);
  };
  setAxis('#barTrend', '#valTrend', reg.trend_axis);
  setAxis('#barRisk', '#valRisk', reg.risk_axis);
}

/* ── 状态地图 ──────────────────────────────────────────────────────── */
function renderMap(p) {
  const root = $('#stateMap');
  root.innerHTML = '';
  const W = 320, H = 300, PAD = 34;
  const x0 = PAD, x1 = W - PAD, y0 = PAD, y1 = H - PAD;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const reg = p.regime;
  const tx = Math.max(-1, Math.min(1, reg.trend_axis ?? 0));
  const ry = Math.max(-1, Math.min(1, reg.risk_axis ?? 0));
  const px = cx + tx * (x1 - cx);
  const py = cy - ry * (y1 - cy);

  // 四象限底色
  const quads = [
    [x0, y0, cx - x0, cy - y0, 'rgba(255,80,100,.055)', '强趋势 · 高风险偏好'],
    [cx, y0, x1 - cx, cy - y0, 'rgba(255,200,87,.05)', '强趋势 · 低风险偏好'],
    [x0, cy, cx - x0, y1 - cy, 'rgba(22,199,132,.045)', '弱趋势 · 高风险偏好'],
    [cx, cy, x1 - cx, y1 - cy, 'rgba(79,157,255,.05)', '弱趋势 · 低风险偏好'],
  ];
  for (const [x, y, w, h, fill, label] of quads) {
    root.appendChild(svg('rect', { x, y, width: w, height: h, fill, rx: 6 }));
    const t = svg('text', {
      x: x + w / 2, y: y + h / 2, 'text-anchor': 'middle', 'dominant-baseline': 'middle',
      'font-size': 10, fill: 'rgba(170,184,205,.34)',
    });
    label.split(' · ').forEach((part, i) => {
      const ts = svg('tspan', { x: x + w / 2, dy: i === 0 ? 0 : 13 });
      ts.textContent = part;
      t.appendChild(ts);
    });
    root.appendChild(t);
  }

  // 网格与零轴
  [0.5, 1].forEach(() => {});
  root.appendChild(svg('line', { x1: x0, y1: cy, x2: x1, y2: cy, stroke: '#33445e', 'stroke-width': 1, 'stroke-dasharray': '3 4' }));
  root.appendChild(svg('line', { x1: cx, y1: y0, x2: cx, y2: y1, stroke: '#33445e', 'stroke-width': 1, 'stroke-dasharray': '3 4' }));
  root.appendChild(svg('rect', { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: 'none', stroke: '#1c2739', rx: 6 }));

  // 轴标签
  const lab = (x, y, text, anchor = 'middle') => {
    const t = svg('text', { x, y, 'text-anchor': anchor, 'font-size': 10.5, fill: '#6d7f99' });
    t.textContent = text; return t;
  };
  root.appendChild(lab(W / 2, H - 12, '趋势强度 →'));
  const yl = lab(12, H / 2, '风险偏好 →', 'middle');
  yl.setAttribute('transform', `rotate(-90 12 ${H / 2})`);
  root.appendChild(yl);

  // 当前点：外光晕 + 实心
  root.appendChild(svg('circle', { cx: px, cy: py, r: 16, fill: 'rgba(79,157,255,.14)' }));
  root.appendChild(svg('circle', { cx: px, cy: py, r: 8, fill: 'rgba(79,157,255,.3)' }));
  root.appendChild(svg('circle', { cx: px, cy: py, r: 4.5, fill: '#4f9dff', stroke: '#fff', 'stroke-width': 1.5 }));
  const tag = svg('text', {
    x: px + (tx >= 0 ? -12 : 12), y: py - 12, 'text-anchor': tx >= 0 ? 'end' : 'start',
    'font-size': 11, fill: '#e8eefa', 'font-weight': 600,
  });
  tag.textContent = reg.name || '';
  root.appendChild(tag);
}

/* ── 七维雷达 ──────────────────────────────────────────────────────── */
function renderRadar(p) {
  const root = $('#radar');
  root.innerHTML = '';
  const dims = (p.dimensions || []).filter(d => d.score !== null && d.score !== undefined);
  if (!dims.length) return;

  const W = 340, H = 300, cx = W / 2, cy = H / 2 - 4, R = 96;
  const n = dims.length;
  const pt = (i, r) => {
    const a = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
  };

  // 同心环
  for (const frac of [0.25, 0.5, 0.75, 1]) {
    const pts = dims.map((_, i) => pt(i, R * frac).join(',')).join(' ');
    root.appendChild(svg('polygon', {
      points: pts, fill: 'none', stroke: '#1c2739',
      'stroke-width': frac === 1 ? 1.2 : 0.8,
    }));
  }
  // 轴与标签
  dims.forEach((d, i) => {
    const [ex, ey] = pt(i, R);
    root.appendChild(svg('line', { x1: cx, y1: cy, x2: ex, y2: ey, stroke: '#1c2739', 'stroke-width': 0.8 }));
    const [lx, ly] = pt(i, R + 22);
    const t = svg('text', {
      x: lx, y: ly + 3, 'text-anchor': Math.abs(lx - cx) < 12 ? 'middle' : (lx > cx ? 'start' : 'end'),
      'font-size': 10.5, fill: '#aab8cd',
    });
    t.textContent = d.name;
    root.appendChild(t);
    const s = svg('text', {
      x: lx, y: ly + 15, 'text-anchor': Math.abs(lx - cx) < 12 ? 'middle' : (lx > cx ? 'start' : 'end'),
      'font-size': 9.5, fill: scoreColor(d.score), 'font-family': 'var(--mono)',
    });
    s.textContent = signed(d.score);
    root.appendChild(s);
  });

  // 数据多边形：把 [-1,1] 映射到 [0.18R, R]
  const pts = dims.map((d, i) => {
    const v = Math.max(-1, Math.min(1, d.score));
    return pt(i, R * (0.16 + ((v + 1) / 2) * 0.84)).join(',');
  }).join(' ');
  root.appendChild(svg('polygon', {
    points: pts, fill: 'rgba(79,157,255,.20)', stroke: '#4f9dff', 'stroke-width': 1.8,
    'stroke-linejoin': 'round',
  }));
  dims.forEach((d, i) => {
    const v = Math.max(-1, Math.min(1, d.score));
    const [x, y] = pt(i, R * (0.16 + ((v + 1) / 2) * 0.84));
    root.appendChild(svg('circle', { cx: x, cy: y, r: 3.2, fill: scoreColor(d.score), stroke: '#0b0f17', 'stroke-width': 1.2 }));
  });
}

/* ── 七维卡片 ──────────────────────────────────────────────────────── */
function renderDims(p) {
  const wrap = $('#dims');
  wrap.innerHTML = '';
  for (const d of p.dimensions || []) {
    const card = el('div', 'dim');

    const head = el('div', 'dim-head');
    const nameWrap = el('div', 'dim-name');
    nameWrap.appendChild(el('span', null, d.name));
    const tag = el('span', 'dim-tag', `权重 ${((d.weight ?? 0) * 100).toFixed(0)}%`);
    nameWrap.appendChild(tag);

    const score = el('div', 'dim-score ' + cls(d.score), signed(d.score));
    const headline = el('div', 'dim-headline', d.headline || '');
    const caret = el('div', 'dim-caret', '▶');
    head.append(nameWrap, score, headline, caret);
    head.addEventListener('click', () => card.classList.toggle('open'));

    const body = el('div', 'dim-body');

    if (d.facts && d.facts.length) {
      const tb = el('table', 'facts');
      tb.innerHTML = '<thead><tr><th>指标</th><th style="text-align:right">读数</th>'
                   + '<th>说明</th><th>来源</th></tr></thead>';
      const tbody = el('tbody');
      for (const f of d.facts) {
        const tr = el('tr');
        tr.appendChild(el('td', null, f.label));
        tr.appendChild(el('td', 'num', fmtValue(f.value, f.unit)));
        tr.appendChild(el('td', null, f.note || ''));
        tr.appendChild(el('td', 'src', f.source || ''));
        tbody.appendChild(tr);
      }
      tb.appendChild(tbody);
      body.appendChild(tb);
    }

    if (d.signals && d.signals.length) {
      const ul = el('ul', 'signals');
      for (const s of d.signals) ul.appendChild(el('li', null, s.text));
      body.appendChild(ul);
    }

    const meta = el('div', 'meta-row');
    if (d.caliber) meta.appendChild(el('span', 'meta', '口径：' + d.caliber));
    for (const u of d.uncertainty || []) meta.appendChild(el('span', 'meta', '局限：' + u));
    if (meta.children.length) body.appendChild(meta);

    card.append(head, body);
    wrap.appendChild(card);
  }
  // 默认展开第一张，给用户一个“可交互”的暗示
  const first = wrap.querySelector('.dim');
  if (first) first.classList.add('open');
}

/* ── 矛盾 ──────────────────────────────────────────────────────────── */
function renderContra(p) {
  const wrap = $('#contra');
  wrap.innerHTML = '';
  const cs = p.contradictions || [];
  if (!cs.length) {
    wrap.appendChild(el('div', 'card', '七维证据方向一致，未检测到显著背离。'));
    return;
  }
  cs.forEach((c, idx) => {
    const card = el('div', 'contra-card');
    const top = el('div', 'contra-top');
    top.appendChild(el('div', 'contra-idx', String(idx + 1).padStart(2, '0')));
    top.appendChild(el('h3', 'contra-title', c.title));
    top.appendChild(el('div', 'contra-strength', `强度 ${num(c.strength, 2)}`));
    card.appendChild(top);
    card.appendChild(el('p', 'contra-text', c.text));

    const detail = el('div', 'contra-detail');
    for (const d of c.detail || []) {
      const box = el('div', 'vs');
      const nm = el('div', 'vs-name');
      nm.appendChild(el('span', null, d.dim));
      const b = el('b', cls(d.score), signed(d.score));
      nm.appendChild(b);
      box.appendChild(nm);
      box.appendChild(el('div', 'vs-text', d.text || ''));
      detail.appendChild(box);
    }
    if (detail.children.length) card.appendChild(detail);

    if (c.check) {
      const ck = el('div', 'contra-check');
      ck.innerHTML = '<b>如何检验：</b>';
      ck.appendChild(document.createTextNode(c.check));
      card.appendChild(ck);
    }
    wrap.appendChild(card);
  });
}

/* ── 条件 ──────────────────────────────────────────────────────────── */
function renderConds(p) {
  const wrap = $('#conds');
  wrap.innerHTML = '';
  const conds = (p.conditions || []).slice().sort(
    (a, b) => Math.abs(a.distance_pct ?? 999) - Math.abs(b.distance_pct ?? 999));

  for (const c of conds) {
    const row = el('div', 'cond' + (c.triggered ? ' trig' : ''));
    const left = el('div');
    left.appendChild(el('div', 'cond-text',
      `${c.text}　${c.comparator === '>=' || c.comparator === '>' ? '升破' : '跌破'} ${fmtValue(c.threshold, c.unit)}`));
    const cmp = c.comparator === '>=' || c.comparator === '>' ? '高于' : '低于';
    const state = c.triggered ? '已成立' : '待观察';
    left.appendChild(el('div', 'cond-sub',
      `当前 ${fmtValue(c.current, c.unit)} · ${cmp}阈值 → ${state}`));
    if (c.why) left.appendChild(el('div', 'cond-why', c.why));

    const track = el('div', 'cond-track');
    const bar = el('i');
    const dist = Math.abs(c.distance_pct ?? 100);
    bar.style.width = Math.max(2, Math.min(100, dist * 4)) + '%';
    track.appendChild(bar);

    const right = el('div', 'cond-dist');
    const unit = c.distance_is_pp ? 'pp' : '%';
    right.appendChild(document.createTextNode(dist >= 100 ? '—' : dist.toFixed(1) + unit));
    right.appendChild(el('small', null, c.distance_is_pp ? '百分点' : '距离阈值'));

    row.append(left, track, right);
    wrap.appendChild(row);
  }

  const tr = $('#triggers');
  tr.innerHTML = '';
  const tg = p.triggers || [];
  if (tg.length) {
    for (const t of tg) {
      const chip = el('div', 'trig-chip');
      // 不用 innerHTML 拼接：哪怕当前文本来自服务端常量，也不给未来留一个注入点。
      chip.appendChild(el('b', null, t.name || t.dim || '触发'));
      chip.appendChild(document.createTextNode(' ' + (t.text || '')));
      tr.appendChild(chip);
    }
  }
}

/* ── 轮动 ──────────────────────────────────────────────────────────── */
function sparkline(values, { w = 220, h = 34, color = '#4f9dff' } = {}) {
  const s = svg('svg', { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none' });
  if (!values || values.length < 2) return s;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - min) / span) * (h - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  s.appendChild(svg('polyline', {
    points: pts.join(' '), fill: 'none', stroke: color,
    'stroke-width': 1.4, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    'vector-effect': 'non-scaling-stroke',
  }));
  return s;
}

function renderRotation(p) {
  const box = $('#indexLines');
  box.innerHTML = '';
  const idx = p.series?.indices || {};
  for (const [secid, it] of Object.entries(idx)) {
    const closes = it.close || [];
    if (closes.length < 2) continue;
    const chg = (closes[closes.length - 1] / closes[closes.length - 2] - 1) * 100;
    const row = el('div', 'line-row');
    row.appendChild(el('div', 'line-name', it.name || secid));
    row.appendChild(sparkline(closes, { color: chg >= 0 ? '#ff5064' : '#16c784' }));
    row.appendChild(el('div', 'line-val ' + (chg >= 0 ? 'up' : 'down'), signed(chg, 2, '%')));
    box.appendChild(row);
  }

  const mg = p.series?.margin;
  const mbox = $('#marginLine');
  mbox.innerHTML = '';
  if (mg && mg.rzye && mg.rzye.length > 1) {
    const row = el('div', 'line-row');
    const last = mg.rzye[mg.rzye.length - 1];
    const prev = mg.rzye[mg.rzye.length - 2];
    row.appendChild(el('div', 'line-name', '两融余额'));
    row.appendChild(sparkline(mg.rzye, { color: '#4f9dff' }));
    row.appendChild(el('div', 'line-val ' + (last >= prev ? 'up' : 'down'),
      (last / 10000).toFixed(2) + '万亿'));
    mbox.appendChild(row);
  } else {
    mbox.appendChild(el('div', 'card-hint', '未取到两融数据'));
  }

  const heat = $('#heat');
  heat.innerHTML = '';
  const sectors = (p.series?.sectors || []).slice();
  const key = SECTOR_MODE;
  sectors.sort((a, b) => (b[key] ?? -99) - (a[key] ?? -99));
  for (const s of sectors) {
    const v = s[key];
    const cell = el('div', 'cell');
    cell.style.background = heatColor(v);
    cell.appendChild(el('div', 'cell-name', s.name));
    cell.appendChild(el('div', 'cell-val', signed(v, 2, '%')));
    cell.style.color = v === null || v === undefined ? '#6d7f99' : '#0a0d13';
    if (v >= -1.2 && v <= 1.2) cell.style.color = '#e8eefa';
    heat.appendChild(cell);
  }
}

/* ── 数据健康 ──────────────────────────────────────────────────────── */
function renderHealth(p) {
  const dh = p.data_health || {};
  const stats = $('#healthStats');
  stats.innerHTML = '';
  const mk = (num, cap, color) => {
    const d = el('div', 'stat');
    const n = el('div', 'stat-num', num);
    if (color) n.style.color = color;
    d.append(n, el('div', 'stat-cap', cap));
    return d;
  };
  const okPct = Math.round((dh.ok_ratio || 0) * 100);
  stats.append(
    mk(String(dh.market_rows ?? '—'), '覆盖标的（只）'),
    mk(okPct + '%', '取数成功率', okPct >= 80 ? 'var(--up)' : okPct >= 50 ? 'var(--warn)' : 'var(--down)'),
    mk(String((dh.errors || []).length), '异常项'),
    mk(String((p.events || []).length), '事件池'),
  );

  const pv = $('#providers');
  pv.innerHTML = '';
  const prov = dh.provenance || {};
  const NICE = {
    index_kline: '指数日线', index_quotes: '指数快照', market_snapshot: '全市场快照',
    sector_kline: '行业日线', industry_boards: '行业板块', limit_up: '涨停池',
    limit_down: '跌停池', margin: '两融余额', news: '资讯', hot_quote: '热点报价',
  };
  if (!Object.keys(prov).length) pv.appendChild(el('div', 'card-hint', '无'));
  for (const [k, v] of Object.entries(prov)) {
    if (k === 'hot_quote_derived') continue;
    const row = el('div', 'prov');
    row.appendChild(el('div', 'prov-name', NICE[k] || k));
    const val = el('div', 'prov-val', String(v));
    val.style.color = v === 'failed' ? 'var(--alert)' : 'var(--text-3)';
    row.appendChild(val);
    pv.appendChild(row);
  }

  const log = $('#fetchLog');
  log.innerHTML = '';
  const entries = (dh.fetch_log || []).slice(-40);
  if (!entries.length) log.appendChild(el('div', 'card-hint', '无记录'));
  for (const e of entries) {
    const line = el('div');
    const ok = e.ok !== false;
    line.className = ok ? 'ok' : 'bad';
    line.textContent = `${ok ? '✓' : '✗'} ${e.name || e.api || ''} ${e.ok ? '' : (e.error || '')}`;
    log.appendChild(line);
  }

  const ev = $('#events');
  ev.innerHTML = '';
  const evs = p.events || [];
  if (!evs.length) ev.appendChild(el('div', 'card-hint', '未取到事件'));
  for (const e of evs) {
    const row = el('div', 'ev');
    row.appendChild(el('div', 'ev-date', (e.date || '').slice(5)));
    row.appendChild(el('div', 'ev-title', e.title || ''));
    row.appendChild(el('div', 'ev-src', e.source || ''));
    ev.appendChild(row);
  }
}

/* ── 追问 ──────────────────────────────────────────────────────────── */
const CHIPS = [
  '当前的主要矛盾是什么？',
  '什么条件下需要改变判断？',
  '市场宽度是什么水平？',
  '情绪维度怎么看？',
  '估值贵吗？',
];

function setupAsk(p) {
  const chips = $('#askChips');
  chips.innerHTML = '';
  for (const q of CHIPS) {
    const c = el('button', 'chip', q);
    c.type = 'button';
    c.addEventListener('click', () => { $('#askInput').value = q; ask(q); });
    chips.appendChild(c);
  }
  if (p.narrative?.followups) {
    for (const q of p.narrative.followups.slice(0, 3)) {
      const c = el('button', 'chip', q);
      c.type = 'button';
      c.addEventListener('click', () => { $('#askInput').value = q; ask(q); });
      chips.appendChild(c);
    }
  }

  const form = $('#askForm');
  form.onsubmit = (e) => {
    e.preventDefault();
    const q = $('#askInput').value.trim();
    if (q) ask(q);
  };
}

async function ask(question) {
  const log = $('#askLog');
  const box = el('div', 'qa');
  box.appendChild(el('div', 'qa-q', question));
  const a = el('div', 'qa-a', '正在检索证据…');
  box.appendChild(a);
  log.prepend(box);

  try {
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'HTTP ' + r.status);
    a.textContent = data.answer || '';
    a.style.whiteSpace = 'pre-wrap';

    const cites = data.citations || [];
    if (cites.length) {
      const cw = el('div', 'qa-cite');
      for (const c of cites) {
        const tag = el('span', 'cite');
        tag.appendChild(document.createTextNode(c.dim + ' · ' + c.label + ' '));
        const b = el('b', null, fmtValue(c.value, c.unit));
        tag.appendChild(b);
        cw.appendChild(tag);
      }
      box.appendChild(cw);
    }
    for (const note of data.notes || []) {
      box.appendChild(el('div', 'qa-note', '⚑ ' + note));
    }
    if (data.engine && data.engine !== 'retrieval' && data.engine !== 'guardrail') {
      box.appendChild(el('div', 'qa-note', '⚙ ' + data.engine));
    }
  } catch (e) {
    a.textContent = '回答失败：' + (e.message || e);
    a.style.color = 'var(--alert)';
  }
}

/* ── 交互 ──────────────────────────────────────────────────────────── */
$('#btnRefresh')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.querySelector('span:last-child').textContent = '取数中';
  await load(true);
  btn.disabled = false;
  btn.querySelector('span:last-child').textContent = '刷新';
});

document.querySelectorAll('.tab').forEach((t) => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((x) => x.classList.remove('is-on'));
    t.classList.add('is-on');
    const target = document.getElementById(t.dataset.go);
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

$('#sectorSeg')?.addEventListener('click', (e) => {
  const btn = e.target.closest('.seg-btn');
  if (!btn) return;
  document.querySelectorAll('.seg-btn').forEach((b) => b.classList.remove('is-on'));
  btn.classList.add('is-on');
  SECTOR_MODE = btn.dataset.r;
  renderRotation(PAYLOAD);
});

// 滚动高亮当前分区
const secs = ['sec-state', 'sec-dims', 'sec-contra', 'sec-cond', 'sec-rot', 'sec-health'];
const io = new IntersectionObserver((entries) => {
  for (const en of entries) {
    if (!en.isIntersecting) continue;
    document.querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('is-on', t.dataset.go === en.target.id));
  }
}, { rootMargin: '-45% 0px -50% 0px' });
secs.forEach((id) => { const n = document.getElementById(id); if (n) io.observe(n); });

load(false);
