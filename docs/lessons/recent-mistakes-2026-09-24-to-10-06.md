# 近期错误与教训（2026-09-24 → 2026-10-06）

来源：`HANDOFF.md` 顶部追加记录、`docs/` 下的实测文档、源码与执行账本。
每个数字都有「证据」列或文末指针可以复跑。**只记录已经发生并核对过的事**；
推测一律标「待确认」。

---

## 0. 30 秒结论

| 结论 | 一句话 |
|---|---|
| 最贵的错不是市场，是**模型** | 三次把成本/盈亏算错：转账费错 2 个数量级、现货成本只取 1 笔、平仓价显示错方向 |
| 报价真假不是问题，**出口**才是 | 15 个 Solana 机会逐个链上模拟，真实到手与报价只差 0–0.15%；但 12/15 因往返损耗或深度不可做 |
| 规模是硬约束 | $10/笔时固定成本 ≈ $0.38，大于毛价差 $0.26 → 保本要 $15，净利 $1 要 $50+ |
| **拆腿 = 裸腿** | 现货腿被赎出并桥走，永续剩 11 天裸空，最后实亏 −8.03 USD |
| 第一次结论经常是错的 | 同一件事改口 ≥ 2 次的有 6 处，见 §8 |

---

## 1. 错误总览（43 条）

| 类别 | 条数 | 最贵的一条 | 状态 |
|---|---|---|---|
| §2 计算 / 口径 | 9 | 现货成本只取最新一笔（差 6.5 倍） | **4 条未修** |
| §3 执行 / 订单 | 10 | ordermin 挡住卖出；尘埃被当成新入账 | 已修 4 条（含脚本）、其余写进口径 |
| §4 跨链 / 链上 | 8 | 0.02 枚卡在失败桥消息里（−$3.81） | 部分只能靠手工/前置检查 |
| §5 估计 / 规模 | 8 | $10 规模不成立 | 已写进口径，未改闸门 |
| §6 环境 / 运维 | 8 | 监控静默停止 6 小时 | 1 条未修、1 条待确认 |

---

## 2. 计算 / 口径错误

### 2.1 看板现货成本只取「最新一笔」→ 差 6.5 倍

| 项 | 内容 |
|---|---|
| 症状 | 看板 CRWV 显示 `spot_open_cost 0.763063`、浮盈 `+2.745` |
| 真实 | 成本 **2.985028**（09-24 四笔相加）、浮盈 **+0.42** |
| 根因 | `apps/account_dashboard.py:602 spot_open_costs()` 遍历 `spot_filled` 时用 `if asset not in costs`，只留每个资产**最新一条**，不是把在仓各组相加 |
| 影响 | 把「赚了很多」读成结论。差 6.5 倍 |
| 正确口径 | 凡涉及成本/浮盈，先用 `data/execution.db` 的 group 原值核对；不要用看板派生数 |
| 状态 | **未修**（2026-10-06 00:35 只记录，没改代码） |

### 2.2 转账费估成 0.000015 SOL → 错两个数量级

| 项 | 内容 |
|---|---|
| 症状 | 看板「净利」列把亏损单显示成盈利单 |
| 真实 | 一笔 SPL 转账给 Kraken **0.003134641 SOL ≈ $0.378**（含替 Kraken 建账户 0.00155956） |
| 根因 | `scripts/xstocks_gap_live.py:95 TRANSFER_FEE_SOL = Decimal("0.000015")` |
| 正确口径 | 首次转账含对方账户租金；同名代币建过账户后再次转账 ≈ 0.001575024 SOL ≈ $0.19 |
| 状态 | 常量仍在代码里，未改 |

### 2.3 `[costs]` 的参数不是「每笔成本」

| 项 | 内容 |
|---|---|
| 症状 | 用 `[costs]` 的 5+5+5 当单笔成本，机会数量被高估 |
| 根因 | `RoundTripCosts.total` 把滑点/失败摊到**四条腿总流水**、手续费摊到**两条永续腿** |
| 正确口径 | 实际往返 ≈ **50 bps of spot_open**。证据：`HANDOFF.md` §8.2 |

### 2.4 资金费从来没进成本模型

| 项 | 内容 |
|---|---|
| 症状 | `conditional_profit_usdt` 里没有资金费 |
| 根因 | `financing_quote` / `funding_cashflow_quote` 恒为 0 |
| 后果 | 持仓跨结算周期时利润估计**偏乐观** |
| 相关 | `funding_exit` / `basis_exit` 连续 4 天是 `null`（09-28 → 10-02），资金费止损没有任何评估结果。原因待确认 |

### 2.5 平仓时人类可读输出打的是「卖出方向」的价

| 项 | 内容 |
|---|---|
| 症状 | 2026-10-06 平 CRWV，输出打印 `taker IOC 限价 87.69/87.67`，比买一还低 |
| 真实 | 实际下单走 `_fresh_limit_price(side="buy")`，限价 **88.05**，**订单本身是对的** |
| 根因 | `execution/full_chain.py:145 Plan.summary()` 恒用 `perp_limit_price`（它按 `side="sell"` 算）；`perp_limit_price_for_close()` 虽然存在，但 `summary()` 不调它 |
| 后果 | 只有显示错。但很容易被读成「平不掉」，从而重发或改价 |
| 状态 | **未修** |

### 2.6 「1 股」列其实是「每枚代币」价

| 项 | 内容 |
|---|---|
| 症状 | 09-28 / 09-29 报告把 MRK 当 1:1 读，一股写成 152.31 |
| 真实 | MRK `shares_per_token ≈ 1.0259`，一股 = **148.47** |
| 根因 | `spot_open_usdt ÷ spot_quantity` 是**每枚代币**价；每股要再除 `contracts` |
| 影响范围 | 1:1 的名字（MRNA / NBIS / CRWV / WMT / POPMART / SHEIN / XIAOMI）不受影响 |
| 状态 | 09-30 已在报告里更正；`scripts/venue-candidate-by-asset.py` 的列名仍是代币口径 |

### 2.7 钱包空读被当成「钱包 = 0」

| 项 | 内容 |
|---|---|
| 症状 | 2026-10-01 与 10-05 各出现一次：`wallet.status = ok`、`wallet.error = None`、但 `balances: []`、`wallet_quote_balance: null` |
| 真实 | 10-02 查明是**读取失败**（`all_routes_failed:local_http_proxy:timeout,...`），同一分钟重跑就正常（14.223073） |
| 危险点 | 状态写 `ok` 且没有错误，下游会把「读失败」当「钱包 = 0」；若同时有开仓逻辑，闸门会误判 |
| 正确口径 | **`None` 必须显式当「未知」**，不能当 0；未知就停手 |
| 状态 | 只记录，未改 |

### 2.8 误读 `quote_max_age_vs_budget_ms`

它只是 `[批次耗时, 预算]`，**不是**「最旧报价的年龄」。
真正的年龄 = 该行 `evaluated_at_ms` 与报价 `request_ts_ms` 之差。
另外「报价缺失」与「报价陈旧」共用同一个 reason 串 `missing_or_stale_*`。

### 2.9 `venue_scoreboard` 文字输出有两个错标签

| 标签 | 实际内容 |
|---|---|
| 「过门槛集中在」 | 印的是**有机会**的按标的计数（`venue_scoreboard.py:471` 只在 `gross>=20` 时累加，标签在 `:810`） |
| 「最高净」 | `max(gross) − 15 bps` 常数（`:571`），而计数表用每行约 50 bps 的记录成本 → **被高估约 35 bps** |

数字对、标签错。引用这两个字段时**不能照抄标签**。状态：未修（`HANDOFF.md` §11 第 9 项仍挂着）。

---

## 3. 执行 / 订单错误

### 3.1 Kraken 签名三个坑 + 一个会误导的报错

| 坑 | 现象 | 正解 |
|---|---|---|
| nonce 单位 | 毫秒/微秒一律 `EAPI:Invalid nonce` | **必须用纳秒** |
| tokenized 交易对写法 | 写 `MRNAXUSD` → `EGeneral:Internal error` | 必须 `pair=MRNAx/USD` **加** `asset_class=tokenized_asset` |
| `post_only` | `EGeneral:Invalid arguments:oflags` | xStocks **不支持** `oflags=post_only`，只能用普通限价单 |
| **误导性报错** | 签名写错时报的是 **`EAPI:Invalid key`** | 我据此误判「密钥被删」，12:0x 更正为「key 一直有效」 |

### 3.2 Kraken API 不能新增提币地址

`/0/private/AddWithdrawAddress` 返回 **404**；`Withdraw` 的 `key` 必须是**地址簿里已保存**的条目
（`WithdrawAddresses` 当时是空）。所以提币需要一个**手工步骤**：
operator 在 Kraken 界面加地址簿条目（X Layer 网络）。

**2026-10-07 复测（operator 问「权限是不是全打勾了」）**：不是权限问题，是接口不存在。

| 通道 | 试过的名字 | 结果 |
|---|---|---|
| REST | `AddWithdrawAddress`、`WithdrawAddresses/Add`、`AddAddress`、`WithdrawAddress`、`WithdrawAddressAdd` | **全 404** |
| WebSocket v2 私有 | `add_withdraw_address`、`add_address`、`withdraw_addresses_add`、`withdraw_address_add` | **全 `Unsupported event`** |
| 同一把 key 的读权限 | `WithdrawMethods` / `WithdrawAddresses` | **正常**（23 条 / 5 条） |

复跑：`scripts/kraken_address_probe.py`、`scripts/kraken_ws_address_probe.py`（都只发空 payload）。
所以地址簿只能手工加，一次一个 `(币种, 网络)`。**推论：能不加地址就不加。**
「链上买 → 充 Kraken 卖」不需要提币地址（充值地址由 `DepositAddresses` 当次返回）；
只有「Kraken 买 → 链上卖」才需要，所以那条方向的白名单要尽量短。

### 3.3 ordermin 会直接卡死卖出

`CRWVx/USD` 的 `ordermin = 0.06`，钱包只有 `0.03990766620831878`。
低于最小量**必被拒**。要卖就得先在 Kraken 买 0.06 把余额顶上去。
本例选了**链上直卖**（3.395385 USDG，1 笔 swap，gas 0），不走 Kraken。

### 3.4 入账判定被「尘埃」骗过

Kraken 账上原有 `0.000001 XOPx`。脚本把「余额非零」当成「新充值已到账」，
于是拿尘埃去下单，低于 `ordermin` 被拒。
**正解：入账判定要用数量阈值（`> ordermin`），不能只看非零。** 已修。

### 3.5 「下单后立刻查」会把已成交读成未成交

第 3 轮 XOPx 下单后 5 秒查还是挂着，实际已经成交 → 被误判为「未成交」。
**正解：改成最多 2 分钟轮询，真没成交才撤单等买盘。** 已修。

### 3.6 `swap execute` 与自己的 approve 抢跑

两条一起发时第一笔 swap 必失败（链上 `txStatus: ERROR`，USDG 不动、gas 0）。
**原样重试一次就成功（3/3 复现）。**

### 3.7 XOPx 一挂 maker 卖单，买盘就消失

| 时刻 | 事件 |
|---|---|
| 22:55:47 | 我们挂 186.46 的 maker 卖单 |
| 22:57 | 真实买盘（186.44 × 7.47、50 bps 内 $20k）**全撤**，只剩碎单 |
| 同期 | SLVx / MRNAx / NVDAx / SPYx / AAPLx / CRCLx / VIDAx 盘口全部正常 → **只针对 XOPx** |
| 23:17 | 我们撤单 |
| +45 秒 | 买盘回来（187.15563 × 3.04），立刻吃单成交 |

**结论：XOPx 只吃买一，绝不挂 maker 单。**
推论：看板给 XOPx 标「风险=高」是对的 —— 风险不在买入侧 10% 冲击，
而在**出口侧流动性是间歇的**。

### 3.8 Kraken maker 卖单 3/3 没成交

挂 `bid+1 tick`，10–13 分钟无人吃，期间买一还往下走约 0.2%。
按 `maker_close_timeout_seconds=600` 撤单后吃买一，**每次都瞬间成交**。
taker 实际费率 **8.21 bps**，比模型里的 10 bps 便宜。

### 3.9 Kraken 按「UI 数量」记账

Solana 上的 xStock mint 带 `scaledUiAmountConfig` 时，Kraken 按乘数后的数量记。
XOPx 乘数 1.0063282603：转出原始 `0.07456205`，Kraken 入账 `0.075034`。
同一个 mint 还带 `permanentDelegate` / `pausableConfig`（发行方可暂停或移动代币）→ 持有期风险。

### 3.10 平腿顺序：先平永续，后卖现货

反了就会变成裸多代币。反过来，卖出之后平空失败就变成裸空 —— 后者就是 §4.1 的事故。

---

## 4. 跨链 / 链上错误

> 详细流程与账户表见 `docs/xstocks-kraken-exit-lessons-2026-09-27.md`
> 与 `docs/execution-lessons-xstocks-bridge-2026-09-26.md`。这里只记教训。

### 4.1 源链模拟通过 ≠ 对面接住了币

两次失败都是**源链成功、Solana 失败**。源链 `eth_call` 只跑桥的 `send`，
**不执行 Solana 上的接收程序**。
后果示例：0.02 枚 MRNAx 扣币成功、Solana 报 `AccountNotEnoughKeys`，
消息内容不能改、重放也加不进账户 → **永久锁在桥里**。

### 4.2 11 个账户表 + bitmap 772，换链就要换登记账户

`send` 的附加数据是 `abi.encode(uint64 bitmap, bytes32[] accounts)`，**11 个账户**。
第 3 号槽位写的是**源链选择子**（X Layer = `3016212468291539606`）。
把 X Layer 那 11 个账户原样拿去发 Ethereum / BNB / Arbitrum → Solana 执行失败，且消息改不了。
**发布前必须解码 calldata 逐字核对。**

### 4.3 桥要交付费，不是 0；同一件事改口三次

| 版本 | 结论 |
|---|---|
| 09-24 旧记录 | 「三笔桥交易 value 都是 0，桥费为 0」 |
| 10-05 13:0x | 回执核对：MRNAx 那三笔与 redeem 的 `value`、`gasPrice` 都是 0 → 修正为 0 |
| 10-05 17:50 | **X Layer 桥实测要交 0.000791242725185203 OKB ≈ $0.10，与数量无关**；value=0 时源链 `eth_call` 直接回滚 |

**教训：某一次免费 ≠ 下一次免费。桥费逐笔按回执核，不要外推。**

### 4.4 SOL 打进还不存在的关联账户 → 账户永远建不出来

该地址会变成普通 SOL 账户，这个 mint 的 Token-2022 账户再也建不出来，桥会一直失败。
**SOL 只进钱包 `AmkjszArk…`；建账户用 Token-2022 的关联账户指令。**

### 4.5 充值低于最小量 → 永久不到账

低于 `DepositMethods` 的最小量，充值失败**而且永久不到账**；后面再补一笔达标的也找不回第一笔。
**探针留在自己钱包，只有余额 ≥ 最小量才转到充值地址。**

### 4.6 「同一串合约地址」在不同链各算各的

EVM 上原生与 wrapper 的地址经常各链相同，余额不互通。
充值前必须对 `chainId`；方法名必须写清网络（例：`{符号} - Ethereum` 才只发到 chainId 1）。

### 4.7 「几分钟完成」当时是假设，不是结论

| 环节 | 实测 |
|---|---|
| Kraken 卖出（挂单→成交） | **1.00 秒** |
| USDG 提币 → X Layer 链上到账 | **15 秒** |
| X Layer → Solana 的 CCIP 桥 | **约 22–27 分钟**（4 次：22.0 / 25.0 / 27.4 / 23.4） |
| Solana 转账 → Kraken 入账 | ≈ 1 分钟（4/4） |

所以：**Solana 直买**这条大概率几分钟；**X Layer 出发**必然 25 分钟起，必须接受几十分钟的持仓暴露。

### 4.8 一笔失败桥的账面成本

买的时候按 0.56 付钱，最后只卖出 0.54。
那 0.02 枚按卖出价 190.56 计 = **$3.81**，现在价值 0。
如果 0.56 都能卖，现货腿会是 +11.28，不是 +7.47。

---

## 5. 估计 / 规模错误

### 5.1 $10 规模不成立

| 项 | 数字 |
|---|---|
| 单笔本金 | $10 |
| 固定成本 | ≈ $0.38（转账费 + 账户租金） |
| 毛价差 | 2.6% = $0.26 |
| **结果** | **净 −$0.117**（VIDAx 首个闭环） |
| 保本 | ≈ **$15** |
| 净利 $1 | 需要 **$50+** |

### 5.2 提币费 1 USDG 是随规模变化的成本

| 本金 | 1 USDG 占比 |
|---|---|
| $10 | **10%** |
| $94 | 1.06% |
| $102.7 | ≈1% |

所以 operator 定的规则是「**Kraken 侧 USDG 累计到 100 再提一次**」。

### 5.3 出口深度才是上限

主流名字：50 bps 内买盘 $40k–133k、价差 0–2 bps。
看板「折价最大」的长尾名字：NBISx $2.7k、XOPx $1.4k、AMBRx $53，价差 184–511 bps 但**深度不够**。
**折价大 ≠ 能做。出口能接多少，决定了这笔能做多大。**

### 5.4 「便宜」不等于机会

XOPx「便宜 23–30%」是真的：$1 真能买到 129.73/枚（正股 184.93）。
但池子只有 1 天、总深 **$360**、24 小时 5 笔共 $38.89。
$20 档往返损耗 **19.74%**。价是真的，池子是新的、薄的。

### 5.5 真正的杀手是同池往返损耗

15 个 Solana 机会逐个真实模拟：真实到手比报价只差 0–0.15%（15/15 无转账税、无 transfer hook）。
但 12/15 的往返损耗是 32–594 bps，而价差只有 10–120 bps。
只有 XOPx / ASMLx / CRMx 还剩正边际。

### 5.6 被夹暴露 = 滑点容忍度 × 名义

50 bps 容忍度 × $857 名义 = **$4.29**，是当时估算利润的 **42%**。
10 bps 只有 $0.86。
另外 **Jupiter Ultra 对 xStock 薄池不可用**（RFQ 回答
`Quote not available from market maker`）；`dynamicSlippage` 与 Jito 小费可用。

### 5.7 冻结报价不是机会

MRNA 链上价**连续 5 个交易日**冻结在 169.75–169.82，NBIS 在 237.4–237.6，
永续却在动 → 算出来的基差 1141–1915 bps。
这 96% 的「候选」是坏/冻结报价，不是机会。
**单值不变的链上价 = 先去查报价管链，不要先算利润。**
（另：`outside_strategy_window` 与「美股开市」不是这件事的原因，见「全天候原则」。）

### 5.8 「下得了手」不等于「值得下手」

2026-10-02 第一次出现可行动 ≠ 0：okx 14 次，全在 SHEIN，利润合计 0.41 USD。
但「够最小利润」仍是 0（门槛 1 USD，这批只有 0.016–0.050）。
**监控自己判 `watch_only` + `conditional_profit_below_threshold` —— 不下单是对的。**

---

## 6. 环境 / 运维坑

### 6.1 代理与请求头

| 现象 | 正解 |
|---|---|
| `onchainos` 报 `Network unavailable` / `tcp connect error: Host is down` | 必须显式 `HTTPS_PROXY=http://127.0.0.1:4780` |
| `eth_call` 不带 User-Agent 被以太坊 publicnode 断流 | 加 User-Agent |
| `ethereum.publicnode.com` 经代理大量 SSL 断流 | 换 `ethereum-rpc.publicnode.com` |
| `mainnet.optimism.io` 经代理握手失败 | 换 publicnode |
| Toncenter 请求 | 必须带 User-Agent |

### 6.2 限频：往往是自己打满的

| 通道 | 现象 | 正解 |
|---|---|---|
| Jupiter `lite-api` | 看板自己 4 路并发、429 后 1.5 s 重试 ≈ 5 次/秒 → 每轮 177/177 失败；停发约 60 秒恢复 | 所有询价线程**共用一个限速器**，429 时全局暂停降速 |
| OKX `depth` | `source_error` 33 → 76 → **274 条/小时** | 待处理 |
| X Layer RPC 批次 | `too many RPC calls in batch request`（-32014） | 改成 **10 条一批** |
| Solana 公共 RPC | 连发直接 429（批式） | 逐条调用 + 中间停 0.6 s |

### 6.3 监控静默停止 6 小时，没人知道是谁停的

2026-10-05 最后一轮 `run_summary` = 10:47（`continuous_am`），进程与 wrapper 都不在。
排查结果：日志 0 条 Traceback/CRITICAL；LaunchAgent 未加载；
`data/logs/hk-monitor.pid` **不存在** —— 该文件只在 `stop` 成功路径里删
（`scripts/hk-monitor-ctl.sh:86`），所以「进程自己死掉」这条分支**不会删 pid 文件**。

| 判读 | 内容 |
|---|---|
| 最可能 | 10:47–10:48 有人显式跑了 `hk-monitor-ctl.sh stop` |
| 不能排除 | `scripts/db-maintain.sh --vacuum` 会先停再拉起，而它的 `start` 是**静默的**（失败不报错） |
| 结论 | **谁在何时停的，待确认** |

### 6.4 日志只在 supervisor 启动时轮转

`run_hk_monitor.sh` 开头判断轮转 → 监控不重启就**永不轮转**，实测约 40 MB/天。

### 6.5 磁盘：一个 17 天没回滚的备份占了 4.14 GiB

`data/hk_monitor.db.pre-prune-20260917-151812`（4.14 GiB）+
`hk-monitor.log.1`（204 MB）+ 237 MB 活动日志。
2026-10-04 已清理：`data/` 7.2 GB → 2.6 GB，数据卷 15 GiB → 19 GiB 可用。

### 6.6 沙箱内的误报

沙箱内 `ps` / `pgrep` / `lsof` / `bind()` / 外网都不可用 →
`board-ctl.sh status` 误报 `not running`，`account-status` 误报「未验证」。
核对进程必须加权限执行。

### 6.7 场馆目录里的坏行不能打掉整个场馆

Gate 合约表里 `SOXS_USDT` 带 `order_size_min = 0` → 整场解析失败、静默退出监控，
而其它场馆照常出数、`summary` 看起来毫无异常。
**必须丢弃坏行并上报（`source_error` → `contract_spec_skipped:<instId>`）；0 永远不是「没有最小量」。**

### 6.8 本仓库不是 git 仓库

没有提交历史、无法 diff、无法回滚。这是当前最大的流程风险。
改动前先想清楚，重大改动必须在 `HANDOFF.md` 与 `docs/` 留书面记录。

---

## 7. 统计口径（涉及「哪个场所机会最多」时）

| 规则 | 错误做法 | 正确做法 |
|---|---|---|
| 分母 | 用**监控轮数** | 用该场所该时段**能定价的轮数**（否则「监控得久」伪装成「机会多」） |
| 时段 | 只印交易时段 + 盘前盘后 | 按**全部已记录时段**排序，把「其他/休市」也印出来 |
| 数据质量 | 把带质量标记的行算进机会 | 陈旧盘口/报价/非同时刻/成本过期 → 单独计「不可信」；`outside_strategy_window`、`below_min_notional` 不在「不可信」里 |
| 没采样 vs 没数据 | 混为一谈 | 隔轮采样的场所当轮**不写行**；取失败的行**照常写**并带原因 |
| 结论 | 用「有 X 条候选」下结论 | 分开报「下得了手（可行动）」与「值得下手（够最小利润）」 |

---

## 8. 自我更正清单（同一件事改口 ≥ 2 次）

| 日期 | 第一版（错的） | 更正后 | 教训 |
|---|---|---|---|
| 10-05 11:5x → 12:0x | Kraken「key 被删」 | **key 一直有效**；是签名写错，报错伪装成 `Invalid key` | 报错文本不等于根因；先看自己的签名代码 |
| 10-05 13:0x → 17:50 | 「桥费 ≈$0.10 + redeem 费」→「都是 0」 | 再更正：**X Layer 桥要交 0.000791 OKB** | 逐笔按回执核，不外推 |
| 10-05 13:0x | 现货腿 +7.37、整笔 −0.70 | **+7.47 / −0.60** | 费用写错就重算整笔 |
| 10-05 13:0x | 卖出成交 12:4x | **11:59:41** | 时间取订单回报，不取记忆 |
| 10-05 12:3x | Kraken 177 →「354 / 344 online」 | 那是含重复行的**原始行数**；去重后 **177 标的 / 172 online**；板上「12 online」是 10-03 旧快照 | 报数前先去重、先看快照日期 |
| 09-29 → 09-30 | MRK 按 1:1 读（一股 152.31） | `shares_per_token ≈ 1.0259`，一股 **148.47** | 报「每股」前先查乘数 |
| 10-01 → 10-02 | 空钱包读数「待确认」 | 查明是**读取失败**，重试即好 | 未知状态要标未知，不要猜 |

---

## 9. 未修 / 待确认（**不要当成已解决**）

| # | 事项 | 现状 |
|---|---|---|
| 1 | 看板现货成本只取最新一笔（§2.1） | 未修，仍会高估浮盈 |
| 2 | 看板转账费常量 0.000015 SOL（§2.2） | 未修，仍会把亏损显示成盈利 |
| 3 | 平仓价显示用卖出方向（§2.5） | 未修，只有显示错 |
| 4 | `venue_scoreboard` 两个错标签（§2.9） | 未修，引用时不能照抄标签 |
| 5 | `funding_exit` / `basis_exit` 连续 4 天 `null` | 原因待确认 |
| 6 | `auto_close_policy` 文档冲突 | `HANDOFF.md` §1 写 `manual_sla_plus_funding_cost_stop`，`AGENTS.md` 写 `program_close_on_basis_target_plus_funding_cost_stop`（且 §8.4 写「自动链只开不平」）。**三处不一致，待确认哪个是实况** |
| 7 | 126 条 `mrna-*` `spot_preflight_blocked` 账本噪音 | 是噪音不是敞口，但把未结组从 6 撑到 132 |
| 8 | wrapper 取池盲点 | `onchain_provenance.token_symbol = wMRNAx`，只重扫 wrapper、忽略原生池 |
| 9 | `tests/` 24 项历史失败 | maker-first / hk-monitor 路径，与 10-03 改动无关，未修 |
| 10 | Ink 上一轮测到的 QQQx $187,874 本轮读失败被整条丢 | 应标「本轮未测」而不是丢掉。未做 |
| 11 | 谁在 10-05 10:47 停了监控 | 待确认 |
| 12 | 10-05 钱包 USDG +2.000000 的来源 | 待确认 |
| 13 | `README.md` 仍写 paper，实况是 `auto_orders = "live"` | 文档不一致 |
| 14 | 日志永不轮转（§6.4） | 未修 |

---

## 10. 证据指针

| 主题 | 文件 |
|---|---|
| 逐轮时间线（最全） | `HANDOFF.md` 顶部追加段（60 条） |
| MRNAx 整笔经济账 | `docs/xstocks-mrna-roundtrip-pnl-2026-10-05.md` |
| Kraken 出口与跨链教训 | `docs/xstocks-kraken-exit-lessons-2026-09-27.md` |
| 跨链桥操作细节 | `docs/execution-lessons-xstocks-bridge-2026-09-26.md`、`docs/research/xstocks-bridge-to-solana-2026-09-26.md` |
| 双腿执行/平仓/裸腿 | `docs/execution-lessons-2026-09-15.md` |
| 全部机会的执行审计 | `docs/research/xstocks-execution-audit-2026-10-03.md` |
| XOPx 薄池复核 | `docs/research/xopx-pool-verification-2026-10-03.md` |
| Jupiter 路由费用 | `docs/research/jupiter-route-fees-2026-10-03.md` |
| 价格 API vs 报价 API 分层 | `docs/research/price-vs-quote-api-architecture-2026-10-03.md` |
| 多链监控现状 | `docs/xstocks-multichain-monitoring-2026-10-03.md` |
| 全链路推进计划 v2 | `docs/xstocks-fullchain-plan-2026-10-05.md` |
| 场馆统计（每天一份） | `docs/venue-opportunity-scoreboard-*.md` |

复跑常用命令：

```bash
# 成本/浮盈的真相（不要用看板派生数）
python - <<'PY'
import json, sqlite3
conn = sqlite3.connect("file:data/execution.db?mode=ro", uri=True)
for (p,) in conn.execute("select payload from execution_events where event_type='spot_filled' order by id"):
    print(json.loads(p))
PY

# 场馆统计（8 小时窗口，钱包口径按当前余额）
PYTHONPATH=src .venv/bin/python -m arb_monitor venue-scoreboard --hours 8 --wallet-quote-usdt 10.92

# 逐标的机会与「一股」价格（注意这是代币口径）
PYTHONPATH=src .venv/bin/python scripts/venue-candidate-by-asset.py --hours 8
```
