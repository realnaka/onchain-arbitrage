# 套利监控框架实施计划 v0.3

> **2026-09-22：美股代币 × OKX 永续扩展（计划来源
> `codex://threads/01a0c7a4-1521-7c82-a36c-554c6b35dc17`）。**
> 落地情况与证据见 [`docs/research/us-mapping-2026-09-22.md`](research/us-mapping-2026-09-22.md)。
>
> - 港股基差策略已提炼为**通用** `TokenPerpBasisStrategy`
>   （`strategy/token_perp_basis.py`）；`strategy/hk_basis.py` 保留旧类名与 HK 默认值，
>   HK 行为零回归。策略只做"链上代币多 + USD 线性永续空"，只接受正基差，
>   **不拿正股交易日历当闸门**（`calendar.phase()` 只是报表分组）。
> - 自动发现分**三层**：发行方宇宙 → 可配对候选 → 可执行白名单；
>   配置用 `token_scan_countries`（能看哪些市场）与 `token_adopt_markets`
>   （能热加入价格监控的市场）**分开**。当前是 `["HK","US"]` / `["HK","US"]`：
>   **美股扫描 + 采纳（监控）**。同日 operator 指示"现在开始对这114个公司也进行
>   监控"、"监控结果也要展示在看板上"。采纳不等于可执行：被采纳的美股
>   `mapping_verified = false`、不在执行白名单，`execution_blockers()` 写
>   "份额映射未核验"。
> - **看板按上市地平铺**（`market_coverage_html`）：港股/美股各自的"监控中标的 /
>   本周期有报价 / 本轮候选"，外加一行**发现漏斗**（发行方宇宙 → 无 OKX 股票永续
>   未探针 → 交集内 / 本轮探针 → 本轮新增有池候选 → 执行白名单）。
>   「交集内」是市场级总量上限，「本轮探针」（`layers.probed_this_round`）才是
>   这一轮真的量过的名字数；「本轮新增有池候选」只数刚刚进监控的，
>   早先几轮进的不会重复出现（否则会读成"零候选"）。美股新标的**轮转报价**
>   （`quote_slice_markets=["US"]`、每轮 3 个），避免一次性涌入把采样节奏拖坏。
> - **OKX 最小规模已按文档确认**（`us-okx-overlap-2026-09-22.md` §5.1）：
>   `min_notional` 不是 OKX 发布的字段；官方文档对 SWAP 只定义 `sz ≥ minSz`
>   与 `sz` 为 `lotSz` 整数倍（违规 `51020`/`51121`），181/181 股票永续
>   `lotSz == minSz`（AAPL `0.01 张 ≈ 3.40 USDT`）。**待确认**：没有真实下单
>   走通过"刚好 `minSz`"的对照；`order-precheck` 对本账户返回 `51010`（账户模式，
>   不能当规模证据）。另发现 `config/okx-demo.env` 里的 key 实为**实盘** key。
> - **探测前先取交集**（`token_scan_perp_gate_markets = ["US"]`）：发行方 754 个
>   美股代币 ∩ OKX 181 个股票永续（`instCategory="3"`）= **114** 个，闸门挡掉
>   640 个不可能对冲的名字，一遍全宇宙从约 19 小时降到约 5 小时（193 个 =
>   US 114 + HK 79）。名单读不到时**不放行**。两张名单与交集表见
>   [`docs/research/us-okx-overlap-2026-09-22.md`](research/us-okx-overlap-2026-09-22.md)。
> - US 映射证据链（`arb_monitor mapping-verify`）**7/7 通过 6/6 项检查**。
>   早先"`multiplier() 5/7 ≠ 1` 所以不能 verify"的判断**已更正**：配置原先指错
>   （指了池子为空的 native 代币，深度在 wrapper 池里），且用的不变量
>   `totalSupply == balanceOf` 从一开始就是错的（它只在 `multiplier == 1` 时成立）。
>   正确的是 `totalAssets() == native.balanceOf(wrapper)`，而
>   `convertToAssets(1e18) == multiplier()` 就是 corporate-action 因子：
>   **原生代币 = 1 股，wrapper = `multiplier()` 股**，对冲股数照乘即可。
>   `verified` / `valid_until_ms` 仍留空等 operator 签。
> - 新增 `core/markets.py`（上市地词表）、`core/liquidity_state.py`（池子五状态）、
>   `core/sizing.py`（Decimal 数量模型）、`core/cost_table.py`（per asset/venue 成本覆盖）。
> - **发现层的两个看板口径 bug 已修**：① 链上池读取是异步的（~3 分钟一批），
>   而"算数窗口"是 2 分钟，中间轮次被误印成 `no_pool`；新增 `pool_read_stale`
>   （本轮未采样）区分"没采样"与"没池"。② 后台发现扫描跑完后要等下一次 30 分钟
>   due 才被读走，`layers` / `report_only` 因此空转一整个周期；改为**每轮先收件、
>   到点才开新扫**（`_token_scan(..., due=)`），扫描频率不变。
> - **执行白名单 / 额度 / 开关 / 密钥本轮一字未改**；"发现 ≠ 执行授权"在代码里有硬约束。

> **2026-09-16 晚：项目已进入 live 灰度。** 本次会话的完整记录（决定、改动、
> 错误与教训、运营手册、当前状态）见
> [`docs/session-2026-09-16-live-and-funding-stop.md`](session-2026-09-16-live-and-funding-stop.md)；
> 看板已合并为一个页面两个标签：`scripts/board-ctl.sh start 8765` →
> http://127.0.0.1:8765/ 。

## 2026-09-16 进展：maker 平仓（10 分钟 taker 兜底）+ 自动执行链（先 paper）

按交接文档 `docs/new-conversation-handoff-2026-09-16.md` 落地两件事：

1. **平仓也走 maker**（任务 A）。`full-chain --close` 现在与开仓同源：
   `[orders] style = "maker_first"` 时，合约侧在 **bid1 挂 post-only 买单**
   （`reduceOnly`），成交后才卖出**实际买回的张数**对应的现货；这样两腿在每一刻
   都保持对冲（部分成交→部分卖出）。等待上限 `maker_close_timeout_seconds = 600`
   （10 分钟，独立于开仓的 120 秒）、`maker_close_max_reprices = 0`（不平仓追价）、
   超时按 `maker_close_fallback = "taker"` 兜底：撤单 → taker 买回剩余 → 卖现货。
   合约已平但现货卖出失败时**重试 5 次**（每次先读钱包余额、只卖仍然持有的数量，
   所以是幂等重试而不是重放未知写）；5 次都不成功才记 `manual_intervention_required`
   并通知人工审核。实盘验证：3 张挂 3.341 成交耗时 147 秒、费率 1.60 bps（M），
   随后链上卖出 3 枚（gas=0），两腿归零。用这笔真实成交重算成本：
   maker 双腿往返 ≈ 14 bps → **出手门槛 ≈ 39 bps**（taker 双腿 27 → 52 bps），
   拆解见 `docs/research/cost-calibration-2026-09-16-maker.md`，
   各标的实时差距用 `scripts/threshold_report.py` 复跑。
2. **自动执行链**（任务 B）：`监控候选 → 下单前重取价 → RiskGate → maker-first →
   账本 + 飞书`。两条独立开关（`[execution] mode = "preauthorized_program"` 且
   `auto_orders = "paper"|"live"`），当日额度改为读 `data/execution.db` 的真实成交额，
   执行期持有 flock 单实例锁，任何未知状态写 `data/AUTO_HALT` 并停止自动执行。
   状态沿革：当日先以 **paper** 运行（真实报价、真实成本、真实风控，不产生成交），
   当晚由 operator 落 `live_ack` 后进入 **live 灰度**，并在 2026-09-21 扩到自动平仓
   （基差回到 3 bps + 资金费止损）。**当前生效状态看
   `config/automation_policy.toml` 与最近一条 `run_summary.mode`，不要凭这一行推断。**

同时修正两个当天暴露的真实缺陷（见 `docs/execution-lessons-2026-09-15.md` 的新增行）：

- `hk_monitor.cycle()` 里 `strategy` 变量被策略对象覆盖，导致第一个真实候选出现时
  监控进程崩溃（11:21–11:25 崩溃循环）；
- `maker_first.place_and_wait()` 把“撤单未被确认”当成“未成交”，遗留的 post-only
  卖单 16 分钟后成交，形成 3 张裸空（已买回并如实补记账本）。

## 2026-09-15 进展：全链路运行器、账户看板、成本校准、双腿 taker

四件事在本轮落地（详细证据见对应文档）：

1. **全链路**（对应 M5/M6 之间的实盘前置）：
   `arb_monitor full-chain` 把 行情 → Intent → RiskGate → 链上现货 → receipt →
   永续对冲 → 回读 → 账本 串成一条不可换序的链路，`--mode dry|paper|live`。
   现货优先由代码保证：receipt 未确认不提交合约腿，合约张数按实际到账数量重算，
   不足最小合约就不下单（`src/arb_monitor/execution/full_chain.py`、
   `tests/test_full_chain_live.py`）。
2. **账户与风控看板**：`scripts/account-board-ctl.sh start 8766` →
   `http://127.0.0.1:8766/`，显示 OKX 余额/持仓/挂单/真实费率、X Layer 钱包余额、
   按标的折算的净敞口、当前生效风控参数与执行账本；数据源失败显示 UNKNOWN，
   不做旧值回退（`src/arb_monitor/apps/account_dashboard.py`）。
3. **成本校准**：用 6 笔真实 taker 成交（4.5 bps）与 1 笔链上成交（模型比实际贵
   0.61 bps、gas=0）把 `[costs]` 从 18 bps 改到 15 bps，并新增只读校准脚本
   `scripts/cost_calibration.py` 与 `docs/research/cost-calibration-2026-09-15.md`。
4. **双腿 taker**：`[orders] style = "taker_both_legs"`，链上 exact-in swap +
   永续 IOC 限价（最优 Bid 让价 20 bps），禁止 maker 与挂单留存，
   `ExecutionLeg.liquidity_role = TAKER`。
5. **份额映射核验**：把 `1 wXIAOx = 1 股 1810.HK = 1 张 XIAOMI-USDT-SWAP`
   从“默认假设”变成可复现的证据链——发行方文档（1:1 抵押 + 乘数机制）、
   发行方资产表（代币 ISIN ↔ 正股 ISIN、官方 wrapper 地址）、链上储备
   （当时写的是 `wrapper.totalSupply() == native.balanceOf(wrapper)`、
   `multiplier()=1.0`；**2026-09-22 更正**：正确的不变量是
   `totalAssets() == native.balanceOf(wrapper)`，前者只在 `multiplier == 1`
   时成立，港股恰好都等于 1 所以当时没暴露）、
   交易所合约规格与 OKX 指数成分。新增 `arb_monitor mapping-verify` 随时重跑，
   配置带 30 天有效期；核验后结论是**基差真实存在、方向不利**，
   见 `docs/research/mapping-xiaomi-2026-09-15.md`。

飞书告警正文新增**链上代币合约地址（CA）**（代币/支付币/池子/链）与开仓成本逐项
构成，避免用 ticker 认资产、也避免看不到成本来自哪里。

## 2026-09-12 策略接入进展

已在既有框架接入“X Layer 港股现货多 + 永续空”的只读研究监控：

- 首批小米、腾讯、泡泡玛特、美团，以及 X Layer xStocks 的 AAPLx/NVDAx/TSLAx/MSFTx/AMZNx/METAx/GOOGLx；按用户要求发现 Gate / OKX / Binance 三家合约，持续轮询；
- CCXT 公共行情传输，补充原生股票分类、面值与 HKD 数值型语义，保留时间戳、序列、来源、原始响应；
- 指定金额/方向 OnchainOS 只读探针、基差/双腿同时退出模型、成本/份额/时序/资金闸门；
- 09:00 通道可用性与昨收锚定分别评估，不把 AMM、周末或鉴权失败当作漏洞失效；
- SQLite 行情与证据录制、命令行、本机只读看板、工作日盘前采样心跳；
- 不建纸面仓位、不启用自动执行、不自动发送飞书。
- 链上现货公共池/参考价与永续盘口已增加同轮筛选字段；筛选价差不是可执行净价差，仍需 exact-in 报价、Gas 与映射验证。

真实采样：2026-09-12 盘口/参考行情可读，但 OnchainOS 报价返回 `50114 Invalid Authority`。
当前没有完成有效现货报价验证、连续 24 小时/7–14 天录制或盈利验收。
详见 `docs/hk-basis.md` 与 `docs/research/hk-basis-2026-09-12.md`。

## 目标

在 1 万 USDT 以内的资金规模下，先构建一个稳定的监控和人工决策系统。第一版不自动下单、不自动签名、不做跨链转账。

第一阶段范围固定为：

- 2 个 CEX + 1 个 DEX，或者先用 2 个 CEX 验证数据链路；
- 5–10 个高流动性交易对；
- 1 个具体策略；
- 7–14 天原始行情录制；
- 本地看板、飞书提醒、人工执行记录。

新增的链上目标：

- 先监控多链代币现货价格和指定金额下的可成交报价；
- 使用 OKX OnchainOS 作为多链现货/DEX 的聚合行情与报价入口；
- 保留链上原生数据和 perp DEX 专用适配器的位置，不把 OnchainOS 当作唯一事实来源。

## 不变的架构边界

```text
Venue data adapter
  → normalized market event
  → strategy plugin
  → cost model
  → monitoring risk gate
  → deduplicated signal
  → dashboard / Feishu / manual execution log
```

后续实盘执行才增加：

```text
validated Intent → execution adapter → fills → ledger → reconciliation
```

DEX 和 CEX 不强行实现完全相同的底层接口。统一的是行情、报价、执行意图和结算结果；链上模拟、签名、广播、确认等能力保留在各自适配器内。

## CEX 接入取舍：CCXT 优先，原生适配器兜底

### 结论

第一版 CEX 行情建议优先使用 CCXT，原因是它已经提供统一的市场、交易对、订单簿和 REST API 结构，适合快速接入 Binance、OKX、Bybit 等多个交易所。需要注意，统一 API 只覆盖各交易所的共同子集。

实时行情可以使用 CCXT 的 WebSocket 能力（通常使用 CCXT Pro 接口）；不同交易所的深度推送能力、限制和行为仍有差异，必须在本项目适配层做统一的断线、序列和新鲜度处理。

### 推荐分层

```text
CCXT / CCXT Pro
  → CEX 统一行情适配器
  → 本项目 OrderBook / Instrument
  → 策略和净价差计算
```

CCXT 只负责“接入和翻译”，不负责：

- 数据新鲜度判断；
- 序列断裂后的业务处理；
- 机会判定；
- 统一手续费和滑点模型；
- 库存、账本和风险控制。

### 什么时候直接写原生 CEX 适配器

当出现以下情况时，再针对单一交易所写原生适配器：

1. CCXT 没有暴露该交易所需要的字段；
2. WebSocket 深度合并或重连行为不满足要求；
3. 需要交易所特有的订单类型、批量下单或低延迟路径；
4. 需要严格控制限频、连接数和私有成交回报；
5. 进入实盘双腿执行阶段。

因此不是“CCXT 或原生接口二选一”，而是：

```text
监控和研究阶段：CCXT 优先
执行和性能瓶颈阶段：关键 venue 原生适配器
```

CCXT 支持很多交易所，但统一 API 只覆盖共同子集；不要因为能用同一个方法，就假设各交易所的合约面值、保证金、订单状态和 WebSocket 语义完全相同。

### CEX 第一版建议

- 先接 2 家 CEX，不超过 10 个交易对；
- 先用公共行情，不配置私有 API key；
- REST 只用于市场元数据、快照和恢复；
- WebSocket 用于实时订单簿；
- 每个 CCXT 事件进入本项目后必须重新记录 `venue_ts / recv_ts / sequence / source`；
- 交易所特有字段放入 `metadata`，不能丢弃；
- 第一版不通过 CCXT 自动下单。

## OnchainOS 接入定位

OnchainOS 放在 `data` 层，拆成两个能力：

```text
OnchainOSMarketAdapter
  - 代币价格
  - 交易/流动性数据
  - 多链市场概览

OnchainOSQuoteAdapter
  - 指定链、代币和金额的可成交报价
  - 预估滑点、Gas 和路由
  - 后续生成 Swap 交易数据
```

第一阶段只使用 Market/Quote 的只读能力。钱包签名、授权、广播和交易确认必须放入后续独立的 `wallet` / `execution` 模块，并保留审计日志。

链上机会必须同时记录：

- chain_id；
- token contract；
- quote token contract；
- quote amount；
- route；
- quote 请求和响应时间；
- 预估 Gas；
- price impact/slippage；
- 数据来源和有效期。

不能只保存一个“代币价格”，因为聚合报价随交易金额、路由和区块状态变化。

## 里程碑

### M0：策略接口确认

- 明确交易品种、两腿方向、计价资产、持仓要求；
- 明确“可执行机会”的净收益公式；
- 明确最小净利润、最大名义金额、信号有效期。

验收：给出一份可计算的 `Intent` 定义和成本参数。

### M1：行情可靠性

- 接入第一家 CEX（CCXT）；
- 接入一条链的 OnchainOS Market API；
- WebSocket 断线重连和快照重建；
- 保留 `venue_ts / recv_ts / seq`；
- 检查 stale、序列断裂、时钟偏差；
- 录制原始事件。

验收：连续运行 24 小时，断线后能恢复，异常不产生有效信号。

### M2：净价差扫描

- 接入第二家 CEX（仍优先 CCXT）；
- 接入 OnchainOS Quote API；
- 统一 symbol、精度、合约元数据；
- 按盘口深度计算可成交数量；
- 扣除手续费、滑点、Gas/资金成本（适用时）；
- 记录机会出现、持续、消失时间。

验收：回放数据能确定性重现信号。

### M3：看板与提醒

- 本地看板显示当前机会、历史机会、数据源状态、库存快照；
- 飞书只发送高价值且未过期的信号；
- 同一机会去重、冷却、恢复通知；
- 每条信号支持“已执行 / 放弃 / 误报”。

验收：连续运行 7 天，提醒量可控，能统计人工执行成功率。

看板至少包含：

- CEX 订单簿和数据源状态；
- OnchainOS 报价、路由、链和代币地址；
- 同一交易对的不同金额档报价；
- 可执行数量和净利润；
- 信号年龄和失效倒计时；
- 每个 venue 的库存/可用余额；
- “已执行 / 放弃 / 误报”按钮。

### M4：纸面交易

- 用真实行情回放；
- 模拟盘口成交、部分成交、延迟和滑点；
- 输出理论收益与实际可执行收益的差异；
- 做参数敏感性分析。

验收：回答“按真实成本后是否仍有正期望”。

### M5：小额单腿验证

- CEX 只读、交易权限分离；
- 先做单腿下单、成交回报、余额查询；
- API key 禁止提币并限制 IP；
- 完成本地账本与交易所余额对账。

验收：小额订单可恢复、可对账、可停止。

### M6：双腿自动化（以后）

2026-09-16 已实现第一版手动/自动执行器：maker-first 开仓与平仓、超时兜底、
部分成交处理（只卖实际买回的张数）、单实例锁、当日额度、未知状态停机。
自动执行按两步上线：先 `auto_orders = "paper"`（真实报价、不产生真实成交），
核对若干轮后再改为 `"live"`。

## 当前代码验收标准

- `python3 -m arb_monitor demo` 可运行；
- `python3 -m unittest discover -s tests -v` 通过；
- 断线、旧行情、深度不足、成本后无利差时不产生信号；
- 同一机会在冷却期内不重复告警；
- 所有机会和人工反馈可从 SQLite 查询；
- 后续策略只需实现 `Strategy.on_snapshot()`，不直接调用 venue API。

## 后续具体策略需要提供

1. 交易品种与 venue；
2. 两腿是现货、永续还是 DEX swap；
3. 是否预置库存；
4. 目标持仓时间；
5. 可接受单笔最大亏损；
6. 最小净利润和信号有效期。
