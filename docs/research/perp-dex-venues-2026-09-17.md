# 新增三个只读场所：Aster / Hyperliquid / Lighter（2026-09-17）

> 触发（operator）："Aster / Lighter / edgeX / HL 要接成只读行情"。
>
> 结果：**Aster、Hyperliquid、Lighter 已接入只读监控**（行情+核验，无执行适配器）；
> **edgeX 探测后没有接** —— 它的公开元数据里 293 个币种**一个都不是我们的港股**。

## 1. 逐家探测结果（都先读真实接口，再写代码）

| 场所 | 接口 | 我们 6 只港股里覆盖到 | 场所时钟 |
|---|---|---|---|
| **Aster** | `fapi.asterdex.com/fapi/v1/exchangeInfo`（Binance 同形） | POPMART / XIAOMI / TENCENT / MEITUAN / KUAISHOU（5） | ✅ `E`/`T` |
| **Hyperliquid** | `api.hyperliquid.xyz/info` `{"type":"metaAndAssetCtxs","dex":"xyz"}` | **只有 SHEIN**（122 个标的里） | ✅ l2Book 的 `time` |
| **Lighter** | `mainnet.zklighter.elliot.ai/api/v1/orderBooks` | XIAOMI(203) / TENCENT(201) / POPMART(204) / SHEIN(230)（4） | ⚠️ 见 §3 |
| **edgeX** | `pro.edgex.exchange/api/v1/public/meta/getMetaData` | **0** —— coinList 里只有美股与加密（AAPL/AMD/AMZN/…/TSLA + 加密），没有任何港股 | 不适用 |

edgeX 仍然可以随时复查：`scripts/list_hk_universe.py` 与监控每 30 分钟的
`token_scan` 会在它上币时发现，但**现在没有可读的东西**，所以没有为它写一个
无法用真实数据验证的解析器（那正是本仓库不接受的"半成品当成完成"）。

顺带纠正两处 operator 清单里的偏差（都用交易所自己的字段核过）：
OKX **没有**腾讯永续（只有 XIAOMI/POPMART/SHEIN）；binance **有**腾讯 USD 合约
（`TENCENTUSDT`，你清单里没提）。

## 2. 每张合约是多少股 —— 三家都从交易所自己的字段读

| 场所 | 每张规模 | 依据 | 独立交叉核对 |
|---|---|---|---|
| Aster | **1 股/张** | 线性永续，数量单位即 base coin | 24h `quoteVolume/(volume×last)` = 0.9988(XIAOMI) / 1.0018(MEITUAN) / 1.0183(TENCENT) → ≈1 |
| Hyperliquid | **1 币/张** | 主 universe 无股票；`xyz:` dex 里 `szDecimals` 决定步长 | 同刻 `markPx=5.0434`、`oraclePx=5.0386`，与链上代币价（≈5.0）同量级 |
| Lighter | **交易所自己公布 `multiplier=1.000000000000000000`** | 市场行直接给出 | 不假设 1；如果一个场所改了它，适配器跟着变（有测试钉住） |

Aster 的写死规则里有一条要特别记：它和 binance 一样是"名称型 ticker"，
所以**沿用了 binance 的收紧规则** —— `HK0700USDT` 这种代码型（HKD 计价）一律排除。

Hyperliquid 的最小名义金额**没有**在 meta 里公布，所以 `min_notional` 记为 **0（未知）**
而不是猜一个数：猜错会把真实存在的深度误判成"低于最小下单量"。

## 3. Lighter 的时间戳问题（唯一一个需要打折的场所）

> **2026-09-22 更新：已修。** 这一节描述的"用最新成交时间当场所时钟"曾是唯一手段；
> 现在改为订阅 Lighter 自己的订单簿 WebSocket（每帧带 `timestamp`，第一帧是完整
> 快照、之后是增量），场所时钟由场所自己给出，档位仍取 REST 全量快照。实测
> 201/203/230 的行 100% 落在 15 秒预算内（改造前 `venue_ts` 比 `recv` 早 1 小时以上）。
> 见 `docs/research/lighter-ws-clock-2026-09-22.md`。下面保留当时的读数作为背景。

Lighter 的 REST 盘口**没有自带时间戳**（`orderBookOrders` 只给价格与剩余量，
`transaction_time` 恒为 0）。本仓库的新鲜度规则要求 venue 时间戳非空，所以这里
**用该市场最新一笔成交的时间**作为场所时钟，而不是用我们自己收到的时间：

* 好处：这是交易所自己的时钟，不是我们的；
* 代价：**失败关闭** —— 没有近期成交的市场会被判为陈旧、不产出可评估轮次。

实测后果（符合预期）：XIAOMI 在 Lighter 上 24h 只有 14 笔成交，所以
`lighter` 的 4 个合约里通常只有 2 个能评估（SHEIN/TENCENT 有成交，
XIAOMI/POPMART 经常没有）。**这不是 bug，是"这个场所太不活跃"的结论本身。**

## 4. 接入后立刻发现的一件事：Aster 的 XIAOMI 是真的贵

同一时刻（2026-09-17 14:20）：

| 场所 | XIAOMI 永续 bid | 相对链上 ask（3.332241） |
|---|---|---|
| aster | 3.35700 | **+74.3 bps** |
| bybit | 3.33500 | +8.3 bps |
| gate | 3.33700 | +14.3 bps |
| bitget | 3.32650 | −17.2 bps |
| okx | 3.32700 | −15.7 bps |

薄盘口本身会制造假机会，所以又按真实尺寸走了盘（Aster 的买卖档）：

```text
aster  XIAOMI 买一到买五: 3.357×2, 3.349×192, 3.341×2, 3.327×266, 3.326×2
   1 股  → 均价 3.35700  = +74.3 bps
   3 股  → 均价 3.35433  = +66.3 bps
  10 股  → 均价 3.35060  = +55.1 bps      ← 深度撑得住
bybit  XIAOMI 买一到买三: 3.335×26, 3.333×11.9, 3.332×10
   10 股 → 均价 3.33500  = +8.3 bps
```

**所以 Aster 的价差不是 2 股假深度造出来的**：到 10 股仍有 +55 bps。
它是目前 XIAOMI 上最好的场所（bybit 只有 +8 bps）—— 这正是"接更多场所"
想要看到的东西。注意它的代价：Aster 24h 成交只有 958 股，属于极薄市场，
能否成交、成交后能否平掉都要靠持续记录来判断。

## 5. 一个需要盯的开销问题

接入 3 家之后每轮评估从 29 个变成 39 个合约。同一时间观测到：

```text
quote_batch_ms: 10100 ~ 26868    （预算 15000，来自 max_age_ms）
quote_max_age_vs_budget_ms: [26868, 15000]   ← 最旧的一条探针已经超预算
quote_rate_limited: 1                        ← 出现了一次限频
```

探针批次耗时**不能稳定维持在 15 秒预算内**，一旦超出，配对会被判
`non_simultaneous_quotes`/陈旧而减少可评估轮次。三个新场所与现货探针共用同一个
本地代理，彼此争抢带宽是最可能的原因（本轮 14:05 的 24s 出现在接入之前，
说明也有代理本身的波动成分）。

### 5.1 隔轮采样做了，但**没有**解决这个问题（实测，2026-09-17 15:00）

operator 让先做隔轮采样，于是新场所改成每 2 轮采一次（`alternate_venues` /
`alternate_every_cycles=2`，跳过的那一轮**完全不评估**，不留"没数据"的行）。
实现是好的，但它**不是这个问题的解**，数据说得很清楚：

| 轮次类型 | `quote_batch_ms` |
|---|---|
| full（8 场所） | 6029 / 7364 / 11595 |
| skip（5 场所） | 6091 / 6176 / 10820 / **31456** |

**采得更少的那一轮反而最慢。** 批次耗时与场所数量无关。

再看真正付了什么代价（阻断原因逐条统计）：

```text
慢轮（qb=11595）：missing_or_stale_spot_buy_quote 28 / spot_sell 15 / non_simultaneous 6
                  missing_or_stale_perp_book 5   ← 与快轮完全一样，是固定基线
快轮（qb= 6176）：以上三条**全部为 0**
```

**坏死的是现货报价，不是永续盘口。** 那 39 行里有 28 行走在
`missing_or_stale_spot_buy_quote` 上，而永续盘口的陈旧数一直是 5
（就是那些没有映射的 HKD 兄弟合约）。

所以瓶颈是**现货报价网格本身**：16 次 OnchainOS CLI 探针（8 个标的 × 买卖两侧）
÷ 4 个 worker = **4 个串行波次**，每一波的延迟在 1.5～7 秒之间摆动，乘起来就是
6～31 秒。配对预算 `max_age_ms=15000` 比这个批次的波动范围还窄 —— **批次自己
就可能比预算还长**，那它产出的报价天然有一半配不上。

隔轮采样保留的理由：它确实降低了总请求量（不加它时出现过
`quote_rate_limited=1`），代价只是三个"日成交几百股"的场所样本减半。
**真正的修法必须针对报价网格**，可选：

1. **把没有永续腿的标的踢出报价网格**（MIXUx / HKEXCx 各占 2 次探针，占
   16 次的 25%，却永远不可能配成对）—— 最直接；
2. `quote_workers` 4 → 6/8，把 4 个波次压到 2～3 个（配置里"6 并发会让本地代理
   报 Network unavailable"是 2026-09-15 测的，需要重测而不是照抄）；
3. 报价按 2 轮一批（与永续盘口解耦），代价是报价年龄上限变大。

### 5.2 最终解：流式评估（operator 2026-09-17 指定，实测有效）

operator 的判断是："每条配对在自己两条腿都到位时就立刻评估…我们现在做的是检测，
需要实时的数据来统计，如果不准确的话，没有意义。"

这句话直接推翻了前几版的编排方式。原来的结构是**先取完一整轮、再统一评估**，
于是 `evaluated_at` 的定义变成了"本轮最慢的那条探针返回的时刻"——一条 20 秒的
探针会把本轮**所有**盘口和报价一起拖过 15 秒预算，39 行一次性全部作废。这就是
为什么"被全灭的轮"里包含作业更少的跳过轮：它跟调用量无关，取决于当轮最慢的是谁。

改成流式之后：每条腿是独立作业，**每对在它自己的盘口与自己的买卖报价都到位的那一刻**
就被判定，并写下它自己的 `evaluated_at_ms`。一条慢探针如今只作废它自己那一对。

实测（同一套上游、同一时间段）：

| | 改动前 | 流式之后 |
|---|---|---|
| 每轮陈旧现货报价行数 | 21～29（整轮作废） | **8/9 轮为 0**，最差 5 |
| 轮内判定时刻的跨度 | 0 ms（全部同一时刻） | **4.5～7.5 秒**（各对各自判定） |
| `quote_batch_ms` | 5.4～25.9 秒 | 4.5～7.5 秒 |

实施中测试抓到的一个真 bug：第一版用 `book = None` 同时表示"还没回来"和"取失败"，
于是**取失败的那一对永远不会被判定、也不留行**——"我们试过但失败了"被静默地
变成了"这条不存在"。现在用显式的 `book_done` / `quotes_done` 区分，取失败的行会
照常记录（`missing_or_stale_perp_book` + 无基差），与"本轮没采样"区分开。

残留：`stale_perp` 每轮还有 1～9 行，那是没有映射的 HKD 兄弟合约与真正过期的盘口，
是固定基线，不再随上游抖动放大。

在解决之前，"60 秒一轮"是**目标值**，实际节奏见 §6。

## 6. 实际扫描频率（operator 问"多久扫一次"）

| 口径 | 值 |
|---|---|
| 配置 `interval_seconds` | 60 秒（目标节奏，也是下限） |
| 安静时段实测（02:00–08:00，5 场所） | 331 轮，**中位 62.7 秒**，均值 65.1 秒 |
| 接入 8 场所后的实测 | 中位 ~82 秒、均值 ~116 秒（含我本机压测争抢代理的时段，待安静后重新测） |
| 其它周期 | 池子状态 60s／现货探针 300s／合约元数据 1800s／新币发现 1800s |

循环是"从本轮开始算 60 秒"，所以**一轮超过 60 秒时会立刻开始下一轮**——
配置值是下限，不是保证。

## 7. 复现

```bash
# 四家场所的覆盖情况（只读）
PYTHONPATH=src .venv/bin/python scripts/list_hk_universe.py --proxy http://127.0.0.1:4780
# 某个新场所的合约与盘口
PYTHONPATH=src .venv/bin/python - <<'PY'
from arb_monitor.data.hk_cex import CexPublicAdapter
for v in ("aster","hyperliquid","lighter"):
    cs,_,_ = CexPublicAdapter(v, proxy="http://127.0.0.1:4780").instruments()
    print(v, [(c.instrument_id, c.asset, str(c.multiplier)) for c in cs][:6])
PY
# 全量映射核验（现在 34 条，8 个场所）
ARB_HTTP_PROXY=http://127.0.0.1:4780 PYTHONPATH=src .venv/bin/python -m arb_monitor mapping-verify
```

测试：`tests/test_perp_dex_venues.py`（20 项）钉住这三家的解析规则 ——
包括"Aster 的 HK 代码型必须被拒""Hyperliquid 的 `xyz:` 前缀与 ticker 都要对"
"Lighter 的 multiplier 是读出来的不是假设的""Lighter 没有时间戳时必须判为不新鲜"。
