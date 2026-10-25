# 数据说明 · 明势 Míngshì

本文记录产品实际使用的数据源、取数路径、字段口径、缓存与快照策略，以及已知的数据缺陷与处理方式。**所有公式以 `app/` 下的实现为准**；文中给出的阈值、权重、样本量都可以直接在代码中检索到。

## 1. 数据流

```text
公开行情接口
   │  app/net.py         IPv4 强制、超时、重试、磁盘缓存（data/cache）
   ▼
app/source.py            Provider 适配：东方财富 / 新浪 / 腾讯 / 扶摇
   │
   ▼
app/fetch.py             能力调度 C1–C8，多源降级，产出 Snapshot
   │                     （provenance / fallbacks / errors / fetch_log）
   ▼
app/evidence.py          七维证据：facts / signals / raw / confidence
   │
   ▼
app/regime.py            两轴、状态、矛盾、证伪条件
   │
   ▼
app/engine.py            payload（含 data_health、series、history）
   │
   ├── app/store.py       data/snapshots/<as_of>.json 与 data/raw/<as_of>.json
   └── app/report.py      Markdown 导出
```

`Snapshot` 是本产品唯一的取数中间态，字段为：`as_of / generated_at / providers / provenance / indices / sectors / market / boards / limit_up / limit_down / margin / news / fetch_log / errors`。

## 2. 数据源清单

### 2.1 东方财富（主源，字段最全）

| 能力 | 接口 | 关键参数 | TTL | 说明 |
| --- | --- | --- | ---: | --- |
| `index_quotes` | `push2.eastmoney.com/api/qt/ulist.np/get` | `secids` 为 10 个指数；`fields=f1,f2,f3,f4,f6,f12,f14` | 180s | 指数实时快照；可能被 `push2` 限流 |
| `index_kline` | `push2his.eastmoney.com/api/qt/stock/kline/get` | `klt=101`（日线）、`fqt=1`（前复权）、`lmt=260`、`end=20500101` | 600s | `fields2=f51..f57` |
| `sector_kline` | 同上（`board_kline`） | `klt=101`、`fqt=1`、`lmt=120` | 900s | 11 个中证全指一级行业 |
| `market_snapshot` | `push2.eastmoney.com/api/qt/clist/get` | `pn` 页码、`pz=100`、`po=1`、`np=1`、`fltt=2`、`invt=2`、`fid=f3`、`fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23` | 300s | 逐页最多 60 页，页间并行 4 |
| `industry_boards` | `push2.eastmoney.com/api/qt/clist/get` | `pz=200`、`fs=m:90+t:2`、`fields=f2,f3,f12,f14,f62,f104,f105,f128,f136,f20` | 300s | 行业板块广度与领涨 |
| `limit_up` | `push2ex.eastmoney.com/getTopicZTPool` | `pagesize=300`、`sort=fbt:asc`、`date=YYYYMMDD` | 180s | Referer 为 `quote.eastmoney.com/ztb/` |
| `limit_down` | `push2ex.eastmoney.com/getTopicDTPool` | `pagesize=300`、`sort=fund:asc`、`date=YYYYMMDD` | 180s | 同上 |
| `margin` | `datacenter-web.eastmoney.com/api/data/v1/get` | `reportName=RPTA_RZRQ_LSHJ`、`columns=ALL`、`pageSize=25`、`sortColumns=DIM_DATE`、`sortTypes=-1` | 3600s | 沪深合计两融历史 |
| `news` | `np-listapi.eastmoney.com/comm/web/getNewsByColumns` | `column=350`、`page_size=40` | 900s | 财经资讯标题，供事件词典 |

个股快照字段映射（`_QUOTE_FIELDS`）：

| 字段 | 含义 | 归一化键 |
| --- | --- | --- |
| `f2` | 最新价 | `price` |
| `f3` | 涨跌幅（%） | `pct` |
| `f5` | 成交量（手） | `volume` |
| `f6` | 成交额（元） | `amount` |
| `f8` | 换手率（%） | `turnover` |
| `f9` | 市盈率（动态） | `pe` |
| `f115` | 市盈率 TTM | `pe_ttm` |
| `f23` | 市净率 | `pb` |
| `f20` / `f21` | 总市值 / 流通市值（元） | `mcap` / `float_mcap` |
| `f62` | 主力净流入（元） | `net_inflow` |
| `f100` | 所属行业 | `industry` |
| `f12` / `f14` | 代码 / 名称 | `code` / `name` |

日线映射（`parse_em_kline`）：`f51..f57` → `date, open, close, high, low, volume, amount`，只保留 `close` 非空的行。

涨跌停池映射（`normalize_limit`）：`c/n/zdp/p/amount/ltsz/hs/pe/lbc/zbc/fund/hybk` → `code/name/pct/price/amount/float_mcap/turnover/pe/boards/broken/seal_amount/industry`；其中 `price` 由 `p/1000` 得到（厘 → 元），`boards` 取 `lbc`，缺失时取 `zttj.days`，再缺省为 1。

### 2.2 新浪财经（备源，稳定性优先）

| 能力 | 接口 | 关键参数 | TTL | 说明 |
| --- | --- | --- | ---: | --- |
| `market_snapshot` | `vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData` | `page`、`num=100`、`sort=symbol`、`asc=1`、`node=hs_a`、`_s_r_a=page` | 300s | 最多 60 页；某页不足 100 条即停止 |
| `industry_boards` | `vip.stock.finance.sina.com.cn/q/view/newSinaHy.php` | — | 300s | GBK 文本；解析 `var S_Finance_bankuai_sinaindustry = {...}` |
| `index_quotes` | `hq.sinajs.cn/list=` | `sh000001,sz399001,...` | 120s | GBK 文本；按 `hq_str_x="..."` 解析 |

新浪个股字段映射：`code/name/trade/changepercent/volume/amount/turnoverratio/per/pb/mktcap/nmc` → `code/name/price/pct/volume/amount/turnover/pe/pe_ttm/pb/mcap/float_mcap`。**新浪没有 PE(TTM)，归一化时 `pe` 与 `pe_ttm` 都取 `per`；也没有主力净流入与行业归属**（`net_inflow=None`、`industry=None`）。市值字段单位为万元，归一化时乘 `1e4` 转为元。

### 2.3 腾讯证券（K 线备源）

| 能力 | 接口 | 关键参数 | TTL |
| --- | --- | --- | ---: |
| `index_kline` / `sector_kline` | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | `param=<sh/sz+code>,day,,,<lmt>,` | 600s |

返回 `data.<code>.day` 或 `data.<code>.qfqday`，每行 `[date, open, close, high, low, volume]`；**没有成交额字段**（`amount=None`），成交额由 `market_snapshot` 汇总。

### 2.4 同花顺扶摇（可选优先源）

- 环境变量 `FUYAO_API_KEY`；基址 `FUYAO_BASE_URL`，默认 `https://fuyao.aiclick.ai/api/v1`。
- 适配的路径：`POST /market/index/quotes`、`POST /market/index/kline`、`POST /market/limit/pool`（`type=up|down`）。
- 无 key 时 `available()=False`：不发起请求、不返回占位数据。代码按公开约定编写，**本机没有凭据，未做端到端验证**。

## 3. 能力路由与降级

能力 → 源优先级（`app/fetch.py` 的 `CAPS`）：

| 能力 | 优先级 |
| --- | --- |
| `index_kline` | 扶摇 → 东财 → 腾讯 |
| `sector_kline` | 东财 → 腾讯 |
| `index_quotes` | 扶摇 → 东财 → 新浪 |
| `market_snapshot` | 东财 → 新浪 |
| `industry_boards` | 东财 → 新浪 |
| `limit_up` / `limit_down` | 扶摇 → 东财 |
| `margin` | 东财 |
| `news` | 东财 |

调度规则：

1. **顺序尝试**：按上表顺序取 provider，跳过 `available()=False` 与被熔断的组合，第一个满足 `_has_payload` 的结果即采用。
2. **成功判定**（`_has_payload`）不是只看 HTTP 200：
   - `index_kline` / `sector_kline`：能解析出非空行；
   - `market_snapshot`：返回的 rows 非空；
   - `limit_up` / `limit_down`：`data` 是对象；
   - `margin`：`result.data` 非空；
   - `news`：`data` 非空；
   - `index_quotes`：**恒为 True**（即使没有任何 quote，也记为该能力成功）。
3. **熔断**：`source.mark(provider, cap, ok)` 以 `provider:capability` 为键累计失败；连续失败 `_DEAD_AFTER=2` 次后 `is_dead()` 为真，本轮取数跳过该组合。每次 `collect()` 开始会 `reset_health()`，因此熔断只在单次取数内生效。
4. **记录**：
   - `provenance[cap] = provider`：最终供数方；全失败写 `"failed"`；
   - `provenance["fallbacks"]`：`[{cap, used, skipped}]`，记录降级链；
   - `provenance["hot_quote_derived"] = "kline_tail"`：指数实时快照失败、由 K 线末根回推的标记；
   - `provenance["market_rows"] = "<N>"`：个股覆盖数（字符串）；
   - `errors`：每次失败一条 `{cap, provider, error}`，全失败额外追加一条 `"全部数据源均失败"`；
   - `fetch_log`：每次请求的 `to_meta()`（含 key / url / ok / from_cache / elapsed_ms / attempts / fetched_at）。
5. **`ok_ratio`**：`provenance` 中值不为 `"failed"` 且不为空的条目占比。注意它的分母也包含 `fallbacks`、`market_rows`、`hot_quote_derived` 这类非能力条目，因此它是**能力级近似值**，不是 HTTP 成功率；实际运行中可能出现 `ok_ratio=1.0` 同时 `errors` 有十余条（单页失败但被其它源补齐）。
6. `engine` 输出 `data_health` 时会剔除 `provenance.fallbacks` 与 `provenance.market_rows`，保留其余（含 `hot_quote_derived`）。

## 4. 字段口径与公式

量纲统一：维度读数 `score ∈ [-1, 1]`；`clamp(x)` 默认截断到 `[-1,1]`；`squash(x, s)=tanh(x/s)` 再截断；`pct(x)` 为 `round(x×100, 2)`。

### 4.1 行情结构（structure，趋势轴，权重 0.22）

探测指数：上证指数、沪深300、创业板指；每个指数需要 **≥130 根**日线，否则该指数不进入结构判定并写入 uncertainty。

| 量 | 公式 |
| --- | --- |
| 均线上方 | 对 MA20、MA60、MA120 各判断 `last > MA`，得 1/0；三个指数合计最多 9 个组合 |
| 均线斜率 | `ma_series(c,20)` 的 `ms[-1]/ms[-6]−1`，再 `pct`，即 20 日均线的 5 日变化率（%） |
| 回撤 | `last / max(c[-250:]) − 1`，再 `pct`（样本不足 250 时取全样本；代码要求 `len(c)>=60`） |
| 波动率 | `rolling_vol(c,20,252)[-1] × 100`：20 日收益率样本标准差 × √252 |
| 波动率分位 | `percentile_rank(vs[-250:], vs[-1])`，分母为序列长度（严格小于当前值的占比） |
| 活跃度 | `(v_sh[-1]+v_sz[-1]) / mean((v_sh+v_sz)[-21:-1])`，即当日两市成交量 / 前 20 日均量 |

打分（只纳入可用的分量，权重按可用分量归一）：

```text
score = clamp(
    0.45 × (above_ratio×2 − 1)
  + 0.30 × squash(mean(斜率) × 100, 3.0)
  + 0.15 × clamp((worst_drawdown + 25) / 25)
  + 0.10 × clamp((mean(vol_pct) − 0.5) × −2)
) / Σ可用权重
```

- `worst_drawdown` 为三个指数回撤的最小值（最深，负数），`(worst+25)/25` 将 −25% 映射为 0、0% 映射为 1；
- 波动率项等价于 `clamp((0.5 − vol_pct)×2)`：分位越低，得分越高；
- `confidence = min(1.0, 0.5 + 0.1 × n_above)`（`n_above` 为均线组合数）；无任何均线组合时 `0.15`；
- `raw` 输出 `above_ratio / dists / slopes / drawdown / vol / vol_pct / activity`。

### 4.2 市场宽度（breadth，趋势轴，权重 0.20）

样本：`market_snapshot` 中 `pct` 非空的全部个股（`n`）。涨跌停来自涨跌停池。

| 量 | 公式 |
| --- | --- |
| 上涨 / 下跌 / 平盘 | `pct>0` / `pct<0` / 其余（平盘含 `pct=0`） |
| 上涨占比 | `up / n` |
| 极端分布 | `strong_up = count(pct≥5)/n`，`strong_dn = count(pct≤−5)/n` |
| 中位数 | 全部 `pct` 的中位数（偶数个取中间两值均值） |
| 横盘占比 | `cross = count(|pct|<1)/n` |
| 涨跌停 | `zt = len(limit_up)`，`dt = len(limit_down)` |

```text
s1 = clamp((up_ratio − 0.5) × 4)
s2 = clamp((strong_up − strong_dn) × 8)
s3 = clamp((zt − dt) / 60)
score = clamp(0.5×s1 + 0.3×s2 + 0.2×s3 − 0.15×max(0, cross − 0.4)×2)
confidence = 0.92（有涨跌停池）否则 0.72
```

横盘占比超过 40% 后开始扣分：横盘越多，说明越缺乏主线。`raw` 输出 `up_ratio / median / strong_up / strong_dn / zt / dt / n / flat_band`。

### 4.3 风格轮动（rotation，趋势轴，权重 0.15）

```text
size_n  = pct(ret(沪深300, n) − ret(中证1000, n))     n = 5, 20
style_n = pct(ret(创业板指, n) − ret(中证红利, n))    n = 5, 20
```

- 行业样本为 11 个中证全指一级行业；每个行业需 ≥21 根日线，计算 `r5`、`r20`；
- `rho = spearman(r5, r20)`（行业数 ≥5 且全部可用时）；
- `dispersion = mean(r20 前 3) − mean(r20 后 3)`（行业数 ≥6）。

```text
score = clamp(
    0.35 × squash(size20, 4.0)
  + 0.25 × squash(style20, 6.0)
  + 0.20 × clamp(dispersion / 20)
  + 0.20 × clamp((rho − 0.3) / 0.7)
) / Σ可用权重
confidence = min(1.0, 0.4 + 0.1 × 行业数)
```

正值读数的方向约定：`size20>0` 表示大盘占优，`style20>0` 表示成长占优；`dispersion` 越大表示行业分化越极端；`rho` 越接近 1 表示 5 日与 20 日排名越稳定（主线持续）。`raw` 输出 `size5/size20/style5/style20/sectors/rank_stability/dispersion`。

### 4.4 估值（valuation，风险轴，权重 0.12）

| 量 | 公式 |
| --- | --- |
| 中位 PE | `median([pe_ttm for pe_ttm>0])`（剔除亏损，避免负值污染中位数） |
| 中位 PB | `median([pb for pb>0])` |
| 破净率 | `count(pb<1)/count(pb>0)` |
| 高估值占比 | `count(pe_ttm>80)/count(pe_ttm>0)` |
| 亏损占比 | `count(pe_ttm≤0)/n`（`n` 为全市场有效样本） |
| 历史分位 | 仅当本地 `history` 中有效 PE 样本 ≥5 且当前中位 PE 非空：`percentile_rank(h_pe, med_pe)`；PB 同理 |

```text
score = clamp(
    0.4 × clamp((35 − med_pe) / 25)
  + 0.3 × clamp((3.2 − med_pb) / 2.0)
  + 0.3 × clamp((pb_below_1 − 0.06) × 6)
) / Σ可用权重
confidence = 0.85（有 PE 历史分位）否则 0.6
```

方向约定：「估值低」得分高，但它描述的是**安全垫厚度**，不是「要涨」。在风险轴里它只做阻尼项。`raw` 输出 `median_pe / median_pb / pb_below_1 / pe_above_80 / loss_ratio / pe_percentile / pb_percentile`。

### 4.5 流动性（liquidity，风险轴，权重 0.13）

| 量 | 公式 |
| --- | --- |
| 成交额 | `Σ amount`（元），展示时 `/1e8` 为亿元 |
| 换手率中位数 | `median([turnover>0])`（%） |
| 成交活跃度 | 同结构维：当日两市成交量 / 前 20 日均量 |
| 两融余额 | `margin[-1].rzye`（元），展示时 `/1e8` |
| 5 日变化 | `rzye[-1] − rzye[-6]`；变化率 `pct(rzye[-1]/rzye[-6] − 1)` |
| 两融占流通市值 | `pct(rzye / ltsz)` |
| 主力净流入 | `Σ net_inflow`，仅当至少一行 `net_inflow` 非空时可用（东财口径） |

```text
score = clamp(
    0.4 × clamp((activity − 1.0) × 2.5)
  + 0.35 × squash(rzye_pct, 2.0)
  + 0.25 × squash(net_inflow / 1e10, 2.0)
) / Σ可用权重
confidence = min(1.0, 0.35 + 0.25×有活跃度 + 0.2×有两融 + 0.2×有净流入)
```

若数据源不含净流入（新浪）或全部为空，该项不参与打分；`raw.net_inflow` 写 `None`，且 uncertainty 会注明「主力资金流字段仅在东方财富源可用时提供」。

### 4.6 情绪（sentiment，风险轴，权重 0.11）

样本为当日涨停池与跌停池。

| 量 | 公式 |
| --- | --- |
| `zt` / `dt` | 涨停 / 跌停家数 |
| 最高连板 | `max(boards)`（`boards` 来自 `lbc` / `zttj.days`，缺省 1） |
| 连板股 | `count(boards ≥ 2)` |
| 炸板 | `count(broken > 0)`（`broken=zbc`） |
| 封板率 | `1 − broken/len(zt)` |
| 涨跌停比 | `len(zt)/max(len(dt),1)` |
| 封单额中位数 | `median([seal_amount])`（元） |
| 涨停股换手率中位数 | `median([turnover])`（%） |

```text
score = clamp(
    0.30 × clamp((zt − 60) / 60)
  + 0.25 × clamp((max_board − 4) / 4)
  + 0.25 × clamp((seal_rate − 0.75) × 4)
  + 0.20 × clamp((ratio − 5) / 25)
) / Σ可用权重
confidence = 0.90
```

封板率不可得时该项不参与；涨停池与跌停池同时为空则维度 `freshness="missing"`、`score=None`。`raw` 输出 `zt/dt/max_board/multi/seal_rate/ratio/avg_seal/avg_turnover`。

### 4.7 重要事件（events，风险轴，权重 0.07）

- 词典共 **16 组规则、9 个类别**：货币政策（2 组）、财政政策（1）、监管（2）、海外（3）、宏观数据（2）、资金（1）、产业（2）、公司（2）、风险（1）。每组为 `(类别, 关键词元组, 方向读数)`。
- 噪音过滤：标题命中 14 个题材词（旅游/门票/美食/招聘/展会/文旅/景区/演唱会/天气/节气/养生/楼市成交量/土拍/开盘预告）直接跳过。
- 单条标题的多组命中：`weight = mean(命中权重) × (1 + 0.15 × (类别数 − 1))`，截断到 `[-1,1]` 并保留 2 位小数。
- 去重：同一标题只保留一次；按 `|direction|` 降序。
- 方向性明确：`|direction| ≥ 0.25`。

```text
score = clamp(Σ(前 10 条方向性明确事件的方向读数) / 5.0)
confidence = 0.72（LLM 抽取通道可用）否则 0.50
若方向性明确事件 < 3 条：confidence × 0.8
```

无资讯时：`freshness="missing"`、`score=0.0`、`confidence=0.1`，并写入 uncertainty。词典无法处理未登录词与语义反转（「降准落空」会被判为正面），这是已知缺陷。

### 4.8 状态轴、置信度与条件

```text
trend = Σ(score_i × w_i) / Σw_i        i ∈ {structure:0.45, breadth:0.35, rotation:0.20}
risk  = Σ(score_i × w_i) / Σw_i        i ∈ {valuation:0.20, liquidity:0.30, sentiment:0.30, events:0.20}
coverage    = Σ weight(有 score 且 freshness≠missing) / Σ weight(全部七维)
consistency = clamp(1 − mad/0.6, 0, 1)
   mad = Σ|score_i − 加权均值| × weight_i / Σweight_i
freshness   = Σ weight(freshness=="fresh") / Σ weight
confidence  = 100 × (0.35×coverage + 0.35×consistency + 0.30×freshness)
disorder 状态下 confidence × 0.85
```

条件距离（`switch_conditions` 的 `_cond`）：

```text
若 threshold ≠ 0：distance_pct = |current − threshold| / |threshold| × 100，distance_is_pp=False
若 threshold = 0：distance_pct = |current − threshold|（百分点），distance_is_pp=True
triggered = current ≥ threshold（比较符 ">="/">"）或 current ≤ threshold（"<="/"<"）
```

`current` 或 `threshold` 为空时 `distance_pct=None`、`triggered=False`。状态专属的 `breadth_recover` / `breadth_deteriorate` 的 `current` 恒为 `None`，只作为待监控阈值存在。

## 5. 缓存与快照归档

### 5.1 磁盘缓存（`app/net.py`）

- 路径：`data/cache/<md5(key)[:16]>.json`；内容 `{"_key": key, "_ts": unix_ts, "data": ...}`；
- 有效期：`get_json(..., ttl=...)` 传入；`ttl<=0` 表示永不过期；读取时校验 `_key` 一致且未超时；
- 写入：先写 `.tmp` 再 `os.replace`，加进程锁；
- 命中：`FetchResult.from_cache=True`，`fetched_at` 取缓存写入时间；
- 失败重试：`retries=2`，即最多 3 次；退避 `0.25 × attempt` 秒；失败返回 `ok=False`，不抛异常；
- 强制刷新：`get_json(force=True)` 可绕过缓存，但**当前产品链路没有传入 force**；因此 `/api/dashboard?refresh=1` 重新取数时，HTTP 层仍可能命中 TTL 内的磁盘缓存。

### 5.2 快照（`app/store.py`）

| 目录 | 内容 | 写入时机 |
| --- | --- | --- |
| `data/snapshots/` | 完整 payload，`<as_of>.json`，缩进 1，UTF-8 | `analyze(save=True)`（默认） |
| `data/raw/` | `Snapshot.to_dict()`，但移除逐股 `market`，写入 `market_summary={count, note}` | 同上 |

- `save_payload` / `save_raw` 使用 `.tmp` + `os.replace`，避免半写文件；
- `history(limit=250)`：按文件名排序读取全部快照，取每份的 `valuation.raw`（`median_pe / median_pb / pb_below_1`）、`breadth.raw.up_ratio`、`regime`（`name / trend_axis / risk_axis / confidence`），按 `as_of` 升序返回最后 `limit` 条；
- `list_snapshots()` 返回不含 `.json` 的日期列表；
- `load_payload(date)` 精确读某日；`latest_payload()` 读最后一日；
- 没有自动清理与保留策略；同一天重复运行会覆盖当天文件。

## 6. 时间口径

1. **交易日 `as_of`**：由 C1 中上证指数（`1.000001`）最后一根日线的 `date` 决定；C1 全失败时退化为当天日历日期（`time.strftime("%Y-%m-%d")`）。没有节假日日历，也不判断当天是否开市。
2. **盘中最后一根 K 线可能未完成**：东财 `klt=101&fqt=1` 在交易时段会返回当日未收盘的 bar；`as_of` 与所有基于 `close` 的计算都会使用它。产品定位是「收盘后的状态卡」，不建议在盘中把结论当作收盘读数。
3. **`generated_at`** 是服务端本地时间（`time.strftime`，无时区），用于区分「数据日期」和「生成时间」。
4. **涨跌停池日期**：`date_compact = as_of.replace("-", "")`，即用 `as_of` 请求当日池；非交易日会用最近交易日日期。
5. **两融 5 日变化**：取 `margin[-1]` 与 `margin[-6]` 的差值，按返回的交易日序列对齐，不是自然日。
6. **资讯时间**：保留接口返回的字符串（`showTime` / `Art_ShowTime`），不做时区与格式归一。
7. **缓存与交易日**：缓存 TTL 为固定秒数（120–3600 秒），与收盘时点无关；收盘后不久重新请求，仍可能命中盘中写入的缓存。需要严格的收盘快照时，应先清空 `data/cache/` 或等待 TTL 过期。

## 7. 已知数据缺陷与处理

| 缺陷 | 影响 | 现有处理 |
| --- | --- | --- |
| 东财 `push2` 家族并发后限流（`RemoteDisconnected`） | 全市场快照、指数实时快照可能失败 | 降级到新浪；`errors/fetch_log` 记录；宽度/估值改用新浪口径 |
| 新浪没有 PE(TTM) | 估值口径退化为动态 PE | 代码中 `pe_ttm` 与 `pe` 同取 `per`；文档明确声明 |
| 新浪没有主力净流入、行业归属 | 流动性少一项、板块数据变薄 | 该分项不参与打分；uncertainty 提示；`raw.net_inflow=None` |
| 腾讯 K 线没有成交额 | 若用腾讯算流动性会缺 amount | 流动性成交额来自 `market_snapshot` 汇总，不用 K 线 amount |
| 涨跌停池只有扶摇/东财 | 两者都失败时情绪维度不可用 | 维度 `missing`，置信度按覆盖率下降；不伪造 0 |
| 两融、资讯只有东财 | 单源失败即缺失 | 维度缺项/降级；`errors` 可见 |
| `index_quotes` 成功判定恒为 True | 空响应也被记为成功 | 仅影响数据健康展示；指数 quote 缺失不影响 K 线口径的结构计算 |
| `ok_ratio` 含伪条目 | 可能显示 100% 而实际有分页失败 | 文档声明为能力级近似；界面同时展示 `errors` 与 `fetch_log` |
| 个股过滤只剔除 `price=None` 与 `code` 以 `"92"` 开头 | 注释称「剔除北交所」，实际未按 4/8 开头过滤 | `FS_ALL_A` 本身不含北交所；新浪 `hs_a` 也不含；影响有限，但注释与实现不一致 |
| 事件词典只有 16 组规则 | 未登录词、语义反转无法处理 | 命中不足时明确显示「无显著单一事件驱动」；不做语义猜测 |
| `_num` 对 `"-" / "" / "null"` 返回 `None` | 缺失值不会当成 0 | 维度按可用项归一，缺失写入 uncertainty |
| 同一标题在 `news` 中可能重复 | 资讯列表重复 | 事件维度按标题去重，`/api/dashboard` 的 `events` 仍展示原始前 20 条 |
| 除权与复权 | 指数点位不受影响；个股 PE/PB 为接口原值 | 指数 K 线 `fqt=1`；个股财务口径完全依赖数据源，不做二次调整 |
| 单位混用 | 成交额（元）、市值（元/万元）、封单额（元） | 归一化时统一为元；展示层按亿元换算；成交量只用于比值 |

## 8. 复核方式

```bash
python run.py doctor          # 打印交易日、覆盖个股数、ok_ratio、provenance、errors
python run.py report -o r.md  # 导出报告，检查「数据与口径」一节
```

- `GET /api/health` 返回 `data_health`（catalog / provenance / fallbacks / ok_ratio / errors / fetch_log / market_rows）；
- `GET /api/dashboard?date=2026-10-23` 读取归档快照，可在断网环境下复现当日全部读数；
- 逐项复核入口：每个 `Fact` 都带 `source`（形如 `tencent::index_kline`），七维的 `raw` 保留中间量（均线距离、波动率、两融余额、涨停池统计等），可按本文公式手算。
