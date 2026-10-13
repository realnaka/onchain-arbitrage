# 执行层（研究/纸面阶段）

`arb_monitor.execution` 是执行层的安全骨架，将策略输出的
`ExecutionIntent` 与交易所、钱包适配器分离，并默认 fail-closed。

当前版本只适合单元测试、paper trade 和 demo/testnet 适配器开发。它不会读取或
保存私钥、调用 Binance/OKX 私有 API、生成签名、广播 X Layer 交易，或把
`conditional_open` 转换成实盘订单。

## 组件

- `ExecutionIntent`：链、代币、稳定币、永续合约、数量、价格保护、费用和过期时间。
- `ReadOnlyCEXAdapter`：Binance/OKX 的安全默认实现，交易权限关闭时拒绝下单。
- `SafeXLayerWallet`：不接收私钥的 X Layer 钱包 façade，默认拒绝签名。
- `PreTradeRisk`：信号新鲜度、额度、滑点、执行开关与 kill switch。
- `ExecutionCoordinator`：永续 IOC 先开，再链上 exact-in 的状态机骨架。
- `reconciliation.reconcile()`：两腿成交数量配平检查。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 接入真实适配器前的硬性要求

- CEX 使用独立子账户；API key 仅保留 Read/Trade，禁止 Withdraw，并绑定固定出口 IP。
- 乘数、步长、结算币、持仓模式来自交易所元数据；订单使用 client order id。
- 钱包使用 KMS/HSM 或 MPC signer，私钥不得进入本进程。
- token、router、chain_id 采用白名单；发送前执行 `eth_call` 模拟。
- 交易必须设置 `amountOutMinimum` 与 deadline，nonce 由单一服务串行管理。
- 签名服务设置单笔/日累计额度，kill switch 可冻结签名。
- 每条 Intent、订单、回执、成交、手续费、Gas 和余额变化都必须持久化。

## 接入顺序

1. Binance Futures Testnet 与 OKX Demo；
2. 只读 X Layer RPC 与模拟 swap；
3. 纸面成交、延迟、部分成交回放；
4. 小额单腿和对账；
5. 人工批准的双腿执行；
6. 最后才开放带额度、白名单和 kill switch 的自动执行。

## 下单风格：maker-first，开仓与平仓各有一套参数（2026-09-16）

2026-09-16 起 `[orders] style = "maker_first"`：永续腿**挂 post-only 等成交**，
成交后立刻做现货腿。开仓和平仓是同一套机制的镜像，但参数与风险方向不同：

| | 开仓（open） | 平仓（close） |
|---|---|---|
| 永续腿 | 在 **ask1** 挂 post-only **卖** | 在 **bid1** 挂 post-only **买**（`reduceOnly`） |
| 成交后 | 买入**实际成交张数**对应的现货 | 卖出**实际买回张数**对应的现货 |
| 等待上限 | `maker_timeout_seconds = 120` | `maker_close_timeout_seconds = 600` |
| 追价 | 允许 `maker_max_reprices = 3`（抢时间） | `maker_close_max_reprices = 0`（不急，追价更差） |
| 超时后 | 撤单，不买现货 | `maker_close_fallback = "taker"`：撤单 → taker 买回 → 卖现货 |
| 中途敞口 | 裸**空**（合约先成交） | 裸**多**（合约先平掉、现货还在） |
| 失败处置 | 现货腿失败 → 立即 taker 回补（`maker_unwind_on_spot_failure`） | 卖现货失败 → **重试 5 次**，5 次都失败才 `manual_intervention_required` + 通知人工 |

共同的不变量（由代码保证，不靠操作纪律）：

- **只做实际成交的数量**：部分成交就只做那一部分，两腿每一刻都保持对冲；
- **现货腿失败要重试，但重试必须是幂等的**：合约已经平掉时这笔现货没有任何对冲，
  所以网络类失败（报价/广播失败、代理抖动）会重试 `close_spot_retry_attempts = 5` 次，
  间隔 `close_spot_retry_backoff_seconds × 次数`；每次重试前先读钱包余额、
  只卖仍然持有的数量，所以「上一次其实已经成功但结果丢了」不会被卖第二次。
  5 次都不成功才记 `manual_intervention_required` 并通知人工审核；
- **撤单必须被确认**：只要读不到 `filled/canceled/mmp_canceled`，状态就是
  `unknown`，绝不当成“未成交”（2026-09-16 事故的直接教训）；
- **价格在提交那一刻读**：计划里的价格可能已越价，post-only 会（也应该）撤掉它；
- **永不追价平仓**：等待窗口结束就按兜底执行。

实盘记录（2026-09-16，3 股 ≈ 10 USDG）：平仓挂 3 张买单在 bid1 3.341，
147 秒后全部成交（`execType=M`，1.60 bps），随后链上卖出 3 枚（gas=0），
两腿归零；同日开仓侧的三次 3 股往返用于成本校准。据此把
`[costs] perp_maker_fee_bps` 从实测 1.6 提到模型 2 bps，并在
`docs/research/cost-calibration-2026-09-16-maker.md` 里给出完整拆解：
maker 双腿往返成本 ≈ 14.1 bps、出手门槛 ≈ 39 bps（taker 双腿是 27 → 52 bps）。
看「每个标的离触发还有多远」：

```bash
PYTHONPATH=src .venv/bin/python scripts/threshold_report.py --refresh
```

## 自动执行链（2026-09-16，默认 paper）

```text
监控候选（已过出手门槛）
  → 下单前重新取价（信号只是提名，价格必须重读）
  → RiskGate（白名单/额度/TTL/kill switch/未对冲/开放合约/当日额度）
  → maker-first 执行器（与 CLI 同一个 order_flows）
  → 账本 + 飞书结果播报
```

- **两道开关**：`[execution] mode = "preauthorized_program"` 且
  `auto_orders = "paper" | "live"`；`"paper"` 用真实报价与真实风控但不产生真实成交，
  `"live"` 才是真金白银。默认值 `"off"` 让监控保持只读。
- **出手门槛与告警同源**：用候选自己带的 `roundtrip.breakeven_gross_bps`
  （往返成本 + `edge_buffer_bps` + `expected_profit_bps`），告警说该做才可能被执行；
  重新取价后**再比一次**，信号过期就不做。
- **当日额度是真的**：`[limits] daily_quote_usdt` 从 `data/execution.db` 当天的
  买入成交额累计（含 paper），不是记忆中的数字。
- **单实例**：执行期持有 `data/auto_execute.lock`（flock），进程异常退出会自动释放。
- **一个信号只做一次**：`auto_cooldown_seconds`（默认 900）内同一标的不再执行。
  这条是 2026-09-16 12:00 的实测逼出来的：SHEIN 信号在 12:00:25 与 12:00:42
  连续两轮各执行一次纸面交易，而 live 模式虽然会被 `max_open_contracts = 1`
  挡住，那也是执行之后的事。冷却按标的记忆，重启后从账本恢复。
- **未知即停**：任何 `maker_unknown` / `manual_intervention_required` /
  `spot_not_confirmed` / `unhedged_spot_only` / `close_perp_underfilled` 会写
  `data/AUTO_HALT`，之后每一轮都拒绝执行，直到人工对账后删除该文件。
- **结果播报与告警分开**：告警是「有机会」，自动执行消息是「机器做了什么」，
  前缀 `【自动执行·纸面/实盘】`，正文含重取价、挂单成交、两条腿与账本组。

冒烟验证（市场不提供信号时也可用）：

```bash
# 只做决策，不写账本、不发飞书、不下单
PYTHONPATH=src .venv/bin/python scripts/auto_execute_smoke.py --dry
# 跑完整纸面链（真实报价；门槛会被人为下调并在输出里注明）
PYTHONPATH=src .venv/bin/python scripts/auto_execute_smoke.py --asset SHEIN --mode paper
```

## 历史：第一版下单风格：双腿 taker（2026-09-15 确定）

（已被上面的 maker-first 取代，保留作为成本对比与回退参考。）

第一版不追求 maker 返佣、不挂单、不做阶梯，只做最可能成交的两笔 taker：

| 腿 | 形态 | 价格保护 | 不会发生的事 |
|---|---|---|---|
| 链上现货 | exact-in swap（`USDG → 代币`） | `amountOutMinimum` + 自动滑点上限 0.8% | 不会用市价没有下限的路径；approve 成功不算成交 |
| 永续 | IOC 限价，价格 = 当时最优 Bid 让价 20 bps | 限价即上限，超出则不成交 | 不会转成 maker 挂单，不会留在盘口 |

依据与约束：

- 实测 6 笔真实成交全部是 taker（`execType=T`），费率 4.5 bps（VIP1），
  见 `docs/research/cost-calibration-2026-09-15.md`；
- `config/automation_policy.toml` 的 `[orders]` 明确 `style = "taker_both_legs"`、
  `perp_order_type = "ioc"`、`perp_price_source = "best_bid"`、
  `allow_maker_orders = false`、`allow_resting_orders = false`；
- 代码层由 `ExecutionLeg.liquidity_role = TAKER` 与
  `intent.taker_limit_price()` 表达，IOC 单必须带限价，否则拒绝提交；
- 账户看板把这几项直接显示在“风控参数（当前生效值）”里，改配置立刻可见。

## 全链路运行器

`arb_monitor.execution.full_chain.FullChainRunner` 是唯一把监控信号接到真实双腿的
入口，`arb_monitor full-chain` 是它的命令行外壳：

```text
build_plan()            报价 + 最优 Bid + 成本模型 + 风控输入（不写任何东西）
  → execute_open(live)  precheck → 现货 swap → receipt → 合约 IOC → 回读 → 账本
  → execute_close(live) 合约 reduce-only 买回 → 现货卖回 → 账本
```

关键不变式（`tests/test_full_chain_live.py` 逐条覆盖）：

1. 计划阶段不调用交易所下单、不签名；
2. 现货 receipt 未确认时合约腿永不提交（revert/零成交/低于最小合约都不下单）；
3. 合约张数由**实际到账数量**重新计算，向上取整被禁止；
4. 未对冲数量与强平相关字段都进账本，部分成交记为 `perp_underfilled` 而不是
   “已对冲”；
5. 平仓先 reduce-only 买回合约，再卖现货。

`--forced` 跳过净基差阈值，只用于验证链路，会在计划与账本中标注
`forced_plumbing_test=true`，其余门禁一律照旧。
