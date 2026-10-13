# arb v2：xStocks any-to-any 套利升级计划

日期：2026-10-07。审查修订同日写入。

这份文档是 v2 的编码规格。原文在第 14 节，原样保留。
第 1–12 节覆盖原文里已经改掉的决定。两边冲突时，按第 1–12 节写代码。

本轮只写文档。没有改配置，没有动 `mode` / `auto_orders` / `live_ack`，
没有动 `data/KILL_SWITCH` 或 `data/AUTO_HALT`。

## 0. 写代码时先看这里

| 规则 | 内容 |
|---|---|
| 读哪一节 | 实现只读第 1–12 节。第 13 节是审查对照，用来核对没有漏项。第 14 节是原文 |
| 第一批代码 | 阶段 0：只读扫描。不签名，不广播，不建执行器 |
| 不许改的文件 | `ops/xstocks_policy.toml`、`ops/xstocks_eth_policy.toml`、`strategy/hk_basis.py` 的 `STRATEGY_PHASES` |
| 不许动的开关 | v1 与 v2 的 `mode` / `auto_orders` / `live_ack`。未经 operator 明确指示不改 |
| 停机文件 | 不创建、不删除、不绕过 `KILL_SWITCH` / `AUTO_HALT` |
| 不进 v2 的腿 | 港股永续对冲（8765）保持暂停 |
| 金额类型 | 全部 `Decimal`。禁止用 `float` 算交易结果 |
| 策略边界 | 策略只产出声明式意图。策略代码不调用交易所、RPC、钱包、飞书 |

v1 现在是武装状态，不是「默认关闭」：

| 文件 | 2026-10-07 读到的值 |
|---|---|
| `ops/xstocks_policy.toml` | `mode=live`，`auto_orders=live`，`live_ack=operator-2026-10-06`，单笔 25 |
| `ops/xstocks_eth_policy.toml` | `mode=live`，`auto_orders=live`，`live_ack=operator-2026-10-07`，单笔 25 |

所以 v2 的独立目录只隔离代码和数据库。它隔离不了 Kraken nonce，也隔离不了链上钱包 nonce。

## 1. 目标

把「链上买入 → Kraken 卖出」升级成「任意买入场所 → 任意卖出场所」的**监控**。
执行仍按白名单，一次只放开一条已经实测过的路径族。

四类路径保留，用来给监控分类：

| 族 | 含义 | 第一版执行 |
|---|---|---|
| ① 同链 DEX↔DEX | 同一条链、两口不同的池 | 不做。阶段 2 才做，并且必须是一笔原子交易 |
| ② 链 → Kraken | 链上买入，充进 Kraken 卖出 | 阶段 1 纸面。三条腿都写：Solana、X Layer 原生币、Ethereum 直充。见第 3.1 节 |
| ③ 链 A → 链 B | CCIP | 只进目录。没有「对面余额增加」的样本之前，`executable=false` |
| ④ Kraken → 链 | Kraken 买入，提到链上卖出 | 只进目录。地址簿没有该币，或提币费未知，就不可执行 |

④ 是 ② 的反方向，不是第四种桥。CCIP 才要拆成三种方言，见第 6 节。

## 2. 阶段

按这个顺序写。上一阶段的验收没过，不写下一阶段的执行器。

| 阶段 | 交付 | 明确不做 |
|---|---|---|
| 0 只读扫描 | 双边报价、入账单位、时间闸门、有向路线目录、路径记分板 | 不签名。不把 t0 价差标成可执行 |
| 1 纸面：链→Kraken | Solana、X Layer、Ethereum 三条腿。卖出腿下单前再复价 | 不桥。不提币。不改单笔上限。ETH 余额不够只拒绝 ETH，不停另外两条 |
| 2 同链原子 | 两口不同的池放进一笔交易。失败则整笔回滚 | 不做「先买后卖」两笔 |
| 3 CCIP | 每个 `(源链, 目的链, mint)` 一次探针。对面余额增加之后，只打开这一条边 | 不把探针放进每一笔交易。不把 772 用到 EVM→EVM 或 Solana 源 |
| 4 Kraken→链 | 地址簿已有该 `(币, 网络)`，并且 `WithdrawInfo` 费用小于边缘 | 不在套利循环里自动补货 |

阶段写在 v2 自己的 policy 里，例如 `phase = "scan"`。代码按 phase 拒绝更后面的动作。

### 2.1 工程载体

1. 先写 `.gitignore`，再 `git init` 做基线。忽略 `config/*.env`、`data/`、`.venv/`、密钥和账本。
2. 然后 `git worktree ../arb-v2`，分支 `feat/any-to-any`。
3. v1 继续在 `/home/operator/Documents/arb` 跑。v2 用自己的目录、自己的 `data/`、自己的 `PYTHONPATH`。
4. 配置路径用相对路径。`data/xstocks_book.sqlite` 在 v2 里只读打开，或复制一份快照。v2 不写 v1 的库。
5. 两个目录各写各的 `HANDOFF.md`。
6. 看板端口 8771。阶段 0 可以先只写 `data/xstocks_v2_scan.json`，等扫描口径正确再上页面。

### 2.2 编码顺序

代码写在当前仓库的新包 `src/arb_monitor/xstocks_v2/`。不改 v1 的 policy，不停 v1 的进程。

| 步骤 | 做什么 | 不做 |
|---|---|---|
| 1 | 阶段 0：单位、报价闸门、漏斗、有向路线、成本、日历、policy。hermetic 测试 | 不签名，不扫全网，不跟 v1 抢代理 |
| 2 | 阶段 1：链→Kraken 的判定、纸面、持久 HALT、Ethereum 腿的 calldata。三条腿都在 | 不广播。`auto_orders` 保持 off |
| 3 | 测试全过之后，由 operator 停 v1 的 Solana 自动链和 ETH 自动链 | 写代码的人不要在这一步停进程 |
| 4 | v2 用钱包里已有的钱做实盘测试 | 不从 Kraken 补 ETH USDC。单笔仍是 25，实际尺寸还要过余额和最小量 |

v2 只要看到 v1 的锁或 `xstocks_auto_live.py` / `xstocks_eth_auto_live.py` 还在，就拒绝广播。

## 3. 修订后的决策

| 决策项 | 写代码时用这个 | 原文里的说法 |
|---|---|---|
| 扫描 | 有池子、深度过闸、Kraken 在线的名字。177 个名字仍在宇宙里，空链不每轮全扫 | 177 个全量扫双边 |
| 执行 | 白名单，并且路径 `measured=true` | 白名单单独维护 |
| 单笔上限 | **25 USDT**。100 不写进第一版配置 | 25 → 100 |
| 净利门槛 | 净价差 **≥ 20 bps**，并且净利 **≥ 0.1 USDT**。两个都要过 | 闭市 10 / 开市 20，再和 0.1 取严 |
| 开闭市 | 日历写入每轮快照。第一版**不用**它放宽门槛 | 闭市 10，正式盘 20 |
| 并发 | v2 **全局一笔**。跨链同时占用源链、目的链和 Kraken | 每链一笔 |
| 失败 | 见第 8 节。未知停机。已知的 approve 抢跑可以原样重试一次。原子回滚不算停机 | 不重试、不回滚 |
| CEX | 先只 Kraken。接口按场所写，不把 Kraken 写死在策略里 | 同原文 |
| 单位 | Kraken 入账单位。见第 4 节 | underlying share |

实际下单金额：

```text
size = min(单笔上限 25, 可用报价币 × 0.999, 出口深度能接住的最大档)
```

按交易场所的数量步长向下取整。钱包、深度、最小下单量经常比 25 更紧。

当前余额下，100 不会生效：

| 场所 | 现金 | 燃料 | 第一版 |
|---|---|---|---|
| Kraken | 46.95 USD | — | 卖出腿。到不了 100 |
| Solana | 44.54 USDT + 0.4 USDC | 0.0136 SOL。按实测 0.00313 SOL/笔，大约 4 笔转账；新 mint 还要租金 | 可纸面 |
| X Layer | 38.62 USDG | 0.00177 OKB。只有下一笔仍收 0.000791 OKB 时大约还能付 2 次桥费 | 可纸面。桥先不做 |
| Ethereum | 0.24 USDC | 0.00167 ETH，高于 gas floor 0.0005 | 代码和 Solana 一样进阶段 1。USDC 低于 floor 5 时这一条不开仓 |

空链（BSC、Arbitrum、Optimism、Monad、HyperEVM、Ink）的 gas 和稳定币都是 0。第一版不充，也不进阶段 2 的报价热路径。

门槛在不同尺寸上谁更严：

| 尺寸 | 10 bps | 20 bps | 0.1 USDT |
|---|---:|---:|---|
| 25 USDT | 0.025 | 0.05 | 0.1 更严 |
| 约 40 USDT（当前钱包） | 0.04 | 0.08 | 0.1 更严 |
| 100 USDT | 0.10 | 0.20 | 与 10 bps 相同，松于 20 bps |

Solana 转 Kraken 的转账费实测约 0.38 USDT（`buy_fee` 另计，见 `ops/xstocks_policy.toml` 的 `[costs]`）。
在 40 USDT 上大约 95 bps。这笔费用进净利，不留在门槛外面。

开闭市先不拆门槛。教训 5.7：冻结的链上价会造出 1000 bps 以上的假价差，这种价更多出现在不动的时段。
闭市用 10 bps 会在假价差更多的时候放宽。纸面跑完、复价后的分布出来之前，维持单一 20 bps。
`core/us_calendar.py` 仍然要写，见第 9 节。它只负责给快照打标签。

### 3.1 Ethereum 是阶段 1 的一条腿

v1 已经有以太坊自动链，不是「以后再看」：

| v1 已有 | 作用 |
|---|---|
| `src/arb_monitor/execution/xstocks_eth_auto.py` | 判定：green、Kraken 直充、入账单位已验证、复价、gas、USDC floor |
| `scripts/xstocks_eth_auto_live.py` | 常驻。policy 当前是 live |
| `scripts/xstocks_eth_live.py` | 报价、exact approve、v4 买入、充值、卖出 |
| `ops/xstocks_eth_policy.toml` | v1 阈值。v2 **不改这个文件** |
| `data/xstocks_eth_cycle.lock` | v1 的在飞锁 |

v2 要支持同一条路径：Ethereum 买入 → `符号 - Ethereum` 充进 Kraken → Kraken 卖出。
不要在 v2 里再写一条独立的 ETH 常驻，否则和 v1 抢同一把钥匙。
阶段 1 的执行器只有一个「链 → Kraken」状态机，Ethereum 是其中一条腿。

这条腿沿用 v1 已经证实的闸门，不放宽：

| 闸门 | 值 | 原因 |
|---|---|---|
| 池 | `chain=eth`，tier 必须是 green | amber / red 只监控 |
| 出口 | Kraken 有该币的 Ethereum 充值方法 | 方法名每个币单独查 |
| 单位 | EVM 按链规则 1:1，不再按币乘 multiplier | 充值样本不是白名单。见第 3.2 节 D3 |
| 报价币 | 用这口池的对手方。STRCx 是 USDC，不是 USDT | 不能假设所有 ETH 池都是 USDC |
| 授权 | exact allowance，按这笔数量 | 不做无限授权 |
| Gas | approve + swap + 充值都进净利 | 2026-10-07 STRCx 实测合计 0.000327429 ETH。这是样本，不是写死的常数。报价没有 gas 就当成本未知 |
| 余额 | 不要求 USDC ≥ 5。ETH gas ≥ 0.0005 | 报价币够不够看最小量。读失败当未知，不开仓 |
| 尺寸 | min(25, 余额×0.999, 深度)，并且 ≥ 最小量 | 0.24 USDC 低于 STRCx 最小量时拒因是 `below_min_size`，不是「先去补钱」 |

余额和「支不支持」分开：

| 层 | 现在的 0.24 USDC |
|---|---|
| 扫描 | 做。ETH 行照常出现 |
| 开仓 | 最小量用这 0.24 盖得住的币可以做。STRCx 盖不住（约 0.0546 枚，上一笔用了 5.2 USDC），拒因 `below_min_size` |
| 其他链 | Solana、X Layer 不受 ETH 余额影响 |
| live | 代码写完、测试过、v1 两条自动链停掉之后才广播。写代码期间 v1 继续跑 |

成本里不要把「从 Kraken 提 USDC 到 ETH」算进套利净利。那是补货。
2026-10-07 那次 6.30 USDC 提到账 5.4398，亏的大头是提币费和 gas，不是买卖价差。
补货仍走第 8.4 节：地址簿 key `ethusdc1` 已经用于 USDC，但提不提、提多少由 operator 决定。

### 3.2 已经定下来的事

operator 在 2026-10-07 定了这些。代码按这张表，不再留成待选。

| 编号 | 决定 |
|---|---|
| D1 | 先写完 v2 并测过。写的时候 v1 继续跑，v2 看到 v1 进程或锁就拒绝广播。测试通过之后再停 Solana 自动链和 ETH 自动链，然后 v2 用现有余额做实盘。停机不是写代码这一步 |
| D2 | 现在不补 ETH USDC。用钱包里已有的钱跑。不把「USDC ≥ 5」当开仓条件 |
| D3 | 实盘不锁死 STRCx。任何过了经济、充值方法、green、最小量的 xStock 都可以做 |
| D4 | 单笔上限保持 25 |
| D5 | 净利门槛保持单一 20 bps，并且 ≥ 0.1 USDT |

D3 不是漏做。两层验证不是一件事：

| 层 | 现状 |
|---|---|
| 订单单位和最小量 | 177/177 已由 `AddOrder validate=true` 写入 `config/kraken_xstocks_units.toml` |
| 真实充值比例 | 只有 STRCx 有样本，值是 1。链上 0.05184276，Kraken 0.051843 |
| Solana 自动链 | 没有 STRCx 白名单。过了经济和充值方法就可以做 |
| ETH 自动链 | `xstocks_eth_auto.py` 要求 `deposit_credit_verified`。这是 EVM 误乘 1.0918 之后的 fail-closed |

EVM 的链级规则用 STRCx 这次样本：`balanceOf()` 已经是入账单位，别的 EVM 币也不乘 multiplier。
Solana / TON 仍是 raw × multiplier。

没充值样本的币可以做，但第一笔 live 尺寸封顶在刚好过最小量，不是 25。
入账后把实际比例写回 units 文件。和链规则相差超过 1%：只停这个币，不把整条 v2 停掉。
STRCx 已经验证，可以用满 `min(25, 余额, 深度)`。
广播后结果未知，仍是全局 HALT。

## 4. 单位

规范单位叫 **Kraken 入账单位**。不要用 underlying、share、SPV 当规范名。

Kraken 订单说明里的 `STRCSPV` 是底层股，和入账单位不是同一个数。
STRCx 的例子：`0.0546 STRCx = 0.050009 STRCSPV`。入账对的是 STRCx 这一侧。

| 链 | 换算 | 证据 |
|---|---|---|
| EVM | `balanceOf()` 已经是入账单位。**禁止再乘 multiplier** | 2026-10-07 STRCx：链上 0.05184276，Kraken 入账 0.051843，比例 1:1。再乘 1.0918128111453242 会造出假的 +645 到 +773 bps |
| Solana / TON | 入账单位 = raw UI 数量 × multiplier | 官方定义 scaled = raw × multiplier。Solana 自动链现在就是这样算的 |
| 未验证 | 不是拒绝交易 | 第一笔 live 只下最小量。入账比例和链规则差超过 1% 就只停这个币 |

实现：

- 读 `config/kraken_xstocks_units.toml`。最小下单量用 `effective_min_units`，不用公开 `ordermin`。STRCx 的有效最小量是 0.05459063574，公开 ordermin 0.05 会让卖单被拒。
- 换算函数按链分支。EVM 和 Solana 不共用一个「总是乘 multiplier」的函数。
- 测试同时锁两件事：EVM 输入 1 个 balanceOf 单位，输出 1；Solana raw 1、multiplier 1.0918，输出 1.0918。
- 终点是 Kraken 的路径：没有充值样本仍然可执行，尺寸用 `probe_only` 封顶在最小量。空的充值样本不是 1，也不是拒绝。监控照常报价。
- 终点是链上池的路径：不需要 Kraken 充值样本。两边都换成入账单位之后才能比价。小数位按链读取，不假设都是 18 或都是 8。
- 同一串合约地址在不同链上各算各的。比较前对 `chainId`。

## 5. 报价

### 5.1 记录

每一条报价保存：

| 字段 | 要求 |
|---|---|
| 边 | `buy` 或 `sell` |
| 数量与单位 | 入账单位，`Decimal` |
| 场所时间 | 有则记。Kraken Ticker 没有可靠的逐档时间 |
| 本地接收时间 | 必有。同时性用这个比 |
| 序列号、来源 | 必有 |
| 深度、费用档 | 阶段 2 必有 |

新鲜度沿用现有监控口径，不要新造一套：

| 闸门 | 值 | 来源 |
|---|---|---|
| 单条最老 | `max_age_ms = 15000` | `config/hk_monitor.toml` |
| 两侧接收时间差 | `max_pair_skew_ms = 10000` | 同上 |
| 超限 | 进「不可信」，不给净利 | 教训库 §C |

`quote_max_age_vs_budget_ms` 是批次耗时对预算，不是报价年龄。不要拿它当新鲜度（教训 2.8）。

这只解决「两份报价不是同一时刻」。它不解决路径要花 25 分钟。见 5.4。

### 5.2 两阶段漏斗

阶段 1 的宇宙：

- Kraken `status=online` 的 xStock。
- 链上只扫普查里深度过闸的池，加上 v4 watch 里要展示的池。
- 不把 177 × 全部链 × 双边放进每轮热路径。Optimism、Monad、Arbitrum、HyperEVM 在 2026-10-03 普查里过闸池子是 0。
- 空链当轮可以不写行。取失败的行要写，并带原因。「没采样」和「没数据」必须能分开。

阶段 1 必须是双边：

| 路径 | 阶段 1 要看的价 |
|---|---|
| 链 → Kraken | 链上买入价，Kraken **买一**（我们卖给 Kraken） |
| Kraken → 链 | Kraken **卖一**（我们向 Kraken 买），链上卖出价 |
| 同链两池 | 池 A 的买入价，池 B 的卖出价 |
| 链 A → 链 B | 链 A 买入价，链 B 卖出价。桥费未知时标 `cost_unknown`，不把桥费当 0 |

Kraken 公开 Ticker 一次已经返回买一 `b` 和卖一 `a`。阶段 1 两个都用。
阶段 2 改用 Depth，按逗号分批。不要一次塞 177 个。X Layer 的 JSON-RPC 批次上限是 **10**（再多会 `-32014`）。

阶段 2：

- 只对阶段 1 里「距 20 bps 门槛一个缓冲以内」的少数标的做真实阶梯。
- 缓冲写进 policy，初值 `stage2_buffer_bps = 30`。中价和可成交价可以差很远，缓冲太小会漏；深报上限防止缓冲太大。
- 深报每轮最多 20 个。
- 一轮目标 ≤ 60 秒。超了先砍宇宙，不砍新鲜度。
- Solana 卖出 = Jupiter `quote` 反向（token → USDC/USDT）。EVM 卖出 = QuoterV2 / V4Quoter 反向。
- 参考价、池子中价、DexScreener `priceUsd` 只用于排序。**禁止进入净利。**
- 同一 `pool id` 禁止组成一对。同池往返在 15 个 Solana 样本里有 12 个损耗 32–594 bps（教训 5.5）。

坏行不能打掉整个场所。Multicall 用 `tryAggregate` / `allowFailure`。一个池报价失败就丢这一行并上报，其他池继续。0 不是「没有最小量」。

限速：

- 所有 Jupiter 调用共用一把限速器。429 时全局暂停，不按线程重试。
- v2 和 v1 共用 `127.0.0.1:4780`。v2 扫描让路给 v1，不在 v1 采样周期里打满额度。
- Solana 公共 RPC 不连发。沿用「逐条 + 间隔」，不假设能 batch。

### 5.3 卖出前再复价

阶段 1/2 的价只用来发现候选。
纸面和 live 在真正标记成交之前，再取一次卖出腿的可成交价。
第二次不过门槛：这条记为 `false_t0`，不算机会，不下单。

### 5.4 路径时长

| 边 | 时长 | 能不能当可执行 |
|---|---|---|
| Kraken 吃单 | 实测约 1 秒 | 可以，仍要复价 |
| Solana 转入 Kraken | 实测约 1 分钟 | 阶段 1 可以纸面 |
| USDG 提币到 X Layer | 实测约 15 秒 | 这是回款，不在套利循环里 |
| X Layer → Solana CCIP | 4 次实测 22.0 / 25.0 / 27.4 / 23.4 分钟 | 没有新的对面到账样本之前，不可执行 |
| 其他桥 | 未知 | 不可执行。禁止写成「几分钟」 |

每条候选记两个数：

| 字段 | 含义 | 谁能用 |
|---|---|---|
| `edge_t0_bps` | 扫描时刻两侧的净价差 | 只进监控 |
| `edge_after_requote_bps` | 卖出腿复价之后的净价差 | 纸面和 live 的门槛只看这个 |

时长未知的路径可以有 `edge_t0_bps`，`executable` 仍是 false。

## 6. 路线

`RouteGraph` 是过滤器，不是通用步行执行器。
一个步行器容易把 Solana 的 extraArgs 送到 EVM。

| 对象 | 职责 |
|---|---|
| 图 | 有向边。回答能不能走、成本、时长、不可执行原因 |
| 执行器 | 按路径族写死。阶段 1 只有「买 → 充 Kraken → 卖」，腿含 Solana、X Layer、Ethereum。阶段 2 只有「同链一笔原子交换」 |
| CCIP、Kraken 提币 | 阶段 3、4 之前只有目录，没有执行器 |

### 6.1 腿，不是一个节点

一条路径是有序的腿，不是一条边：

```text
买入 → （可选）同链赎回 wrapper → （可选）桥 → （可选）充值 → 卖出
```

节点至少要能区分：

| 维度 | 原因 |
|---|---|
| 链 | 同一地址在不同链上余额不互通 |
| 场所 / 池 | 同链多池。同一 pool id 不能成对 |
| 原生还是 wrapper | wrapper 不能充 Kraken。赎回只在同一条链 |
| 报价币 | USDG、USDT、USDC、ZUSD 不是同一种美元 |

净利先把报价币按**可成交价**换成 USDT。换算报价缺失时，这条路径不可定价，不把脱锚当 0。

X Layer 的买入经常落到 wrapper。充 Kraken 或再桥之前，先同链 `redeem`。
现有 `scripts/xstocks_route_matrix.py` 的 `load_bridge_index()` 只标记「这条链能不能桥到 Solana」。不要把它当成 any-to-any 的图。

### 6.2 桥表是有向的，而且缺边

来源：`data/xstocks_book.sqlite` 的 `bridges`，2026-10-07 只读。
品种数 1370–1472 与原文一致。那是发行登记，不是有池子的数量。

| 源 | 目的地数 | 登记品种 | 相对另外 9 条链缺的边 |
|---|---:|---:|---|
| BNB Smart Chain | 9 | 1472 | 无 |
| Ethereum | 9 | 1451 | 无 |
| Arbitrum | 9 | 1443 | 无 |
| Solana | 8 | 1467 | Mantle |
| Monad | 8 | 1370 | HyperEVM |
| XLayer | 7 | 1472 | HyperEVM、Optimism |
| Ink | 7 | 1472 | HyperEVM、Optimism |
| Mantle | 6 | 1464 | Solana、HyperEVM、Optimism |
| Optimism | 5 | 1471 | Mantle、HyperEVM、Ink、XLayer |
| HyperEVM | 4 | 1440 | Mantle、Ink、XLayer、Optimism、Monad |

阶段 1 的三角在登记里是齐的：X Layer、Ethereum、Solana 互相都有目的地。
实测成功的只有 **X Layer → Solana**。Ethereum→Solana、Solana→Ethereum、Solana→X Layer、X Layer→Ethereum、Ethereum→X Layer 都还没有到账样本。

图的键是 `(source_chain, dest_chain, symbol)`。链对通了，不代表这个币在这条边上，也不代表目的链有池子。

### 6.3 不可执行原因

每条候选带 `executable` 和原因。监控照常展示。原因用稳定字符串，方便记分板聚合。至少包括：

| 原因 | 含义 |
|---|---|
| `same_pool` | 买卖是同一口池 |
| `no_dest_pool` | 目的侧没有过闸的池 |
| `stale_or_skew` | 超过 15 s 或两侧差超过 10 s |
| `cost_unknown` | 桥费、提币费或时长没有实测 |
| `probe_only` | 还没有充值样本。可以做，尺寸封顶在最小量。不是拒绝 |
| `below_min_size` | 低于充值最小量、`effective_min_units` 或池子最小量 |
| `below_floor` | gas 或报价币低于 floor |
| `hook_not_green` | v4 不是 green |
| `unmeasured_route` | 这条桥或提币路线没有对面到账样本 |
| `v1_live_busy` | v1 的锁还在，或 v1 自动链进程还在 |
| `halt` | 上一笔未知或留下了库存 |

`outside_strategy_window` 不使用。v2 不按美股开市拒绝。
`below_min_notional` 和经济门槛不够，不放进「不可信」列。数据质量（陈旧、非同时、成本未知）才进「不可信」。

### 6.4 执行资格是 AND

green 只表示 hook 没有改 swap 的特殊逻辑。它不是执行授权。
STRCx 两口 green 池曾经显示 +38.65 / +32.27 USDT，修正单位之后候选是 0。

一条路径要同时满足才 `executable=true`：

1. 阶段允许这条路径族。
2. hook 分级是 green。amber 只监控。red 不进净利。动态费率、能识别模拟的 hook，即使报价好看也不可执行。
3. 单位规则和这条路径匹配。
4. 终点是 Kraken 时，充值方法名里有这条网络。方法名每个币单独查，MRNAx 曾经只有 Solana。没有充值样本时第一笔只下最小量。
5. 卖出腿复价仍过门槛。
6. 出口深度接得住这个数量。折价大不等于能做（教训 5.3、5.4）。
7. gas 高于 floor。报价币读失败是未知，不是 0（教训 2.7）。ETH 不再用 USDC ≥ 5 当门槛，不够最小量就 `below_min_size`。
8. 不是同一口池。
9. 桥或提币路线已经 measured，或者这条路径根本不桥、不提币。

## 7. CCIP

三种方言，三个构造器。禁止共用一套 extraArgs。

| 方言 | 构造 | 实测 | 第一版 |
|---|---|---|---|
| EVM → Solana | `abi.encode(uint64 bitmap, bytes32[] accounts)`，bitmap **772**，11 个账户。第 3 槽是**源链选择子**。真正收款人在消息 data 和第 10 号账户里，不是通用 SVM 结构里那个全零 `tokenReceiver` | X Layer→Solana：有成功的 `0xde280184…`，也有永久锁死的 0.02 枚 | 可做一次性探针。不进自动循环 |
| EVM → EVM | 标准 CCIP extraArgs。不是 772，没有 11 个 Solana 账户 | 无 | 只监控 |
| Solana → 其他链 | Solana 程序指令。不是 EVM 的 `send` | 无。桥表里 Solana 有 8 个目的地 | 只监控 |

实现约束：

- 账户表按 `(源链, 目的链, mint)` 生成。禁止把 X Layer 那 11 个账户拿去发 Ethereum、BNB 或 Arbitrum。
- 广播前解码即将送出的 calldata。核对：账户长度 11、含这个 mint、含我们的 Token-2022 关联账户、收款公钥不是全零。
- 源链 `eth_call` 成功只说明源链 `send` 能锁币。它不执行对面的接收程序。模拟通过不等于到账。
- 目的侧关联账户先建好。SOL 只打进钱包 `AmkjszArk…`。不要往关联账户地址转 SOL，那个地址一旦变成普通账户，这个 mint 的关联账户就建不出来。
- 桥费逐笔从这笔交易自己的报价或回执读取。X Layer 上实测过 `msg.value = 0.000791242725185203 OKB`。value=0 会在源链回滚。这个数只属于那一次 X Layer→Solana。其他边没有样本时费用是未知，不是 0，也不是 0.000791。
- 探针每个三元组做**一次**，记在路线账 `data/xstocks_v2_route_samples.jsonl`。对面余额增加之后，这一条边才 `measured=true`。
- 探针留在自己的目的钱包。不要把低于 Kraken 最小充值量的探针转进 Kraken。低于最小量会永久不到账，后面补一笔也找不回。
- 交易本体一次发够。数量取「桥最小额、卖出最小额、充值最小额」里的较大者。不要每笔都先发最小额再发余量：桥费按消息收，两笔就是两倍费用和两倍等待。

## 8. 状态机

复用 `execution/xstocks_cycle.py` 的思路：append-only JSONL，每步一个明确结果。
不要原样复用它的锁。`CycleLock.__exit__` 现在无论成败都 `unlink`。
Solana 自动链结束后把 `phase` 写回 `idle`，unknown 只发飞书。
ETH 的 `open_gate` 不读 `phase`。所以「未知即停」在现码里停不住下一轮。

### 8.1 状态

| 状态 | 币在哪 | 下一轮能不能开新仓 |
|---|---|---|
| `refused` | 没动 | 能 |
| `reverted` | 原子交易整笔回滚，没有仓 | 能 |
| `sold` | 两边都完成 | 能，仍受日限额和熔断约束 |
| `stopped` | 币在钱包、桥上或卖出场所，位置写在 `held_at` | 不能。写持久 HALT |
| `unknown` | 动作已发出，结果不明 | 不能。写持久 HALT |

`held_at` 用 `wallet` / `in_bridge` / `dest_wallet` / `kraken` / `unknown`。禁止写「大概没了」。

### 8.2 锁和 HALT

- v2 第一版一把全局锁：`data/xstocks_v2_cycle.lock`。
- 进入 `unknown` 或带着库存的 `stopped`：把锁保留，并写 `data/xstocks_v2_HALT`。`__exit__` 在这两种状态下不许删锁。
- `open_gate` 看到 HALT 或锁就拒绝新开仓。只有 operator 对账之后删除 HALT，才恢复。
- `KILL_SWITCH` 仍是开仓和平仓都停。`AUTO_HALT` 仍是只挡开仓。v2 用自己 `data/` 里的这两个文件，语义与 v1 相同。
- 纸面不广播，所以纸面可以和 v1 并行。
- **任何广播之前**再查 v1：
  - `../arb/data/xstocks_cycle.lock`
  - `../arb/data/xstocks_eth_cycle.lock`
  - 进程 `xstocks_auto_live.py`、`xstocks_eth_auto_live.py`
  - 任一存在就拒绝，原因 `v1_live_busy`。
- Ethereum 这条腿还要单独满足第 3.1 节的 floor、green、直充和入账样本。
- 第一笔 v2 live 之前，先停 v1 的 Solana 自动链和 ETH 自动链。这一点写进启动脚本的 live 子命令，不靠人记得。什么时候停，见第 3.2 节 D1。

跨链如果将来执行，一笔交易同时占用源链、目的链和 Kraken。在有余额预留账之前，不要改成「每链一笔」。

### 8.3 重试

| 情况 | 动作 |
|---|---|
| 已广播，没有确认 | 不重试，不重播。HALT |
| 模拟失败，还没广播 | 没有仓。记录原因，下一轮可以再看 |
| 同链原子交易回滚 | 没有仓。不 HALT |
| `swap execute` 和自己的 approve 抢跑 | 原样重试一次。这是教训 3.6，禁止被「一律不重试」盖掉 |
| Kraken 入账未确认 | 不第二次转账。HALT |
| 卖出没成，币已在 Kraken | HALT。不自动反向再买 |

下单后回读账户。不信任返回码。入账看数量增量，阈值用 `effective_min_units`。尘埃余额不算入账（教训 3.4）。卖出后轮询最多 2 分钟，5 秒看到「还挂着」不等于没成交（教训 3.5）。

Kraken 下单：`pair=<符号>/USD`，`asset_class=tokenized_asset`，nonce 用纳秒且严格递增。签名错误时 Kraken 会报 `Invalid key`，先查自己的签名。

阶段 1 的卖出用复价后的吃单，走现有出口闸门（`kraken_exit_gate`）的买盘深度。
不把 v1 的 `maker_first` 搬进 v2。xStocks 不支持 `post_only`。XOPx 挂 maker 卖单会让买盘消失（教训 3.7）。maker 卖单有 3/3 没成交的记录（教训 3.8）。

链上卖出若将来单独存在，滑点容忍和防夹小费要盖住卖出腿。阶段 2 不走两腿小费，整笔一起保护。

### 8.4 回款

套利循环不提币，不桥稳定币。

| 条件 | 动作 |
|---|---|
| 链上报价币 ≥ floor | 可以开仓 |
| 低于 floor | 拒绝开仓，飞书告警 |
| 读余额失败 | 未知。拒绝开仓。禁止当成 0 |
| Kraken 上堆了 USD | 留在 Kraken。补货是另一张工单 |

补货工单的条件：地址簿里已有 key，`WithdrawInfo` 的费用已经读到，费用相对这一批金额可接受，并且达到批量阈值。
现有 Solana 策略是 USDG/USDT 攒够再提（`withdraw_threshold_usdg = 100`，Solana USDT 提币费实测 1.2345）。
ETH 上 6.30 USDC 提出去只到账 5.4398。不要把某一次提币费写成所有网络的费。

库存目标可以沿用 v1 的 floor/target 作监控。v2 第一版不自动执行补货。

## 9. 日历

新增 `core/us_calendar.py`，时区 `America/New_York`。

| 项 | 规格 |
|---|---|
| 正式盘 | 09:30–16:00 ET |
| 半日市 | 13:00 ET 收盘。13:00 之后不是正式盘 |
| 盘前、盘后、夜盘 | 标签可以记，门槛与闭市相同 |
| 休市表 | 只覆盖 2026。2027 或表里没有的日期 → `calendar_unknown`，快照照写，**不放宽门槛** |
| 与港股策略的关系 | 不写入 `hk_basis.STRATEGY_PHASES`。全天候原则继续约束港股永续那条腿 |

测试锁：夏令时切换、秋令时切换、半日市的 12:59 与 13:00、假日、2027-01-01 必须是 `calendar_unknown`。

第一版净利门槛不读这个日历。

## 10. 成本

> **2026-10-07 复测补充（地址簿）**：Kraken **没有任何编程接口**能新增提币地址。
> REST 五个候选名全 404，WebSocket v2 私有通道四个候选名全是 `Unsupported event`，
> 而同一把 key 的读取权限正常（`WithdrawMethods` 23 条、`WithdrawAddresses` 5 条）。
> 复跑脚本：`scripts/kraken_address_probe.py`、`scripts/kraken_ws_address_probe.py`。
> 因此方向④（Kraken → 链）的白名单**每加一个币就是一次人工操作**，必须尽量短；
> 能不加地址的路径优先（方向②不需要提币地址，只需要充值）。
> 详见 `docs/lessons/recent-mistakes-2026-09-24-to-10-06.md` §3.2。

按路径相加，全部 `Decimal`。缺一项就 `cost_unknown`，不要补 0。

| 项 | 怎么取 |
|---|---|
| 池费和滑点 | 阶段 2 的真实报价，不用中价 |
| Gas | 报价附带的 gas，乘当次 gas 价。ETH 路径还要记得 approve |
| CCIP | 这一条边的实测值。没有就是未知 |
| 转账到 Kraken | Solana 首次 0.00313 SOL，重复 0.000015 SOL。不要用 0.000015 当全部 |
| Kraken 手续费 | taker 10 bps，到有新的成交样本再改 |
| 滑点缓冲 | 沿用现有 10 bps 缓冲，写在 v2 policy，不散落在代码里 |
| 提币费 | 只在补货工单里出现。按资产和网络读 `WithdrawInfo` |
| 报价币换算 | 可成交价。没有就不定价 |
| 被夹暴露 | 滑点容忍 × 名义。阶段 2 用原子交易把「卖出时已经拿着货」去掉 |

同池往返损耗不是一项可以加回去的成本。同池直接 `same_pool` 拒绝。

## 11. 配置与代码边界

v2 阈值只放 `ops/xstocks_v2_policy.toml`。不改 v1 的两个 policy。

第一版内容：

```toml
[trade]
per_trade_quote_usdt = "25"
min_net_profit_usdt  = "0.1"
min_net_edge_bps     = "20"
session_split        = false
stage2_buffer_bps    = "30"
stage2_max           = 20
max_slippage_bps     = 10

[limits]
one_cycle_at_a_time = true
daily_open_quote_usdt = "500"
daily_trades_max = 20
loss_alert_trades = 1
loss_pause_trades = 2

[watch]
max_age_ms = 15000
max_pair_skew_ms = 10000
round_budget_s = 60

[execution]
phase = "scan"          # scan | paper_kraken | atomic | ccip | kraken_withdraw
mode = "off"
auto_orders = "off"
live_ack = ""

[capital]
# 报价币够不够看最小量。ETH 不设 USDC floor 5。gas 仍然要有。
solana_floor_usdt = "25"
xlayer_floor_usdg = "20"
eth_usdc_floor_usdt = "0"
eth_gas_floor_eth = "0.0005"
sol_min_alert = "0.01"
credit_mismatch_ratio = "0.01"
```

`mode` / `auto_orders` / `live_ack` 仍是 operator 专属。阶段 0 和阶段 1 的纸面都不需要把它们改成 live。

建议的代码位置。阶段 0 只建前四行和测试：

| 模块 | 职责 |
|---|---|
| `src/arb_monitor/xstocks_v2/units.py` | 入账单位。EVM 不乘，Solana/TON 乘 |
| `src/arb_monitor/xstocks_v2/quotes.py` | 报价记录、15 s / 10 s 闸门 |
| `src/arb_monitor/xstocks_v2/funnel.py` | 阶段 1 双边粗筛，阶段 2 最多 20 个深报 |
| `src/arb_monitor/xstocks_v2/routes.py` | 有向图、方言、`executable` 与原因 |
| `src/arb_monitor/xstocks_v2/costs.py` | 路径成本。缺项就是未知 |
| `src/arb_monitor/xstocks_v2/calendar.py` | 2026 NYSE。不参与第一版门槛 |
| `src/arb_monitor/xstocks_v2/cycle.py` | 阶段 1 才写。持久 HALT。Ethereum 腿复用第 3.1 节的闸门，不另起常驻 |
| `src/arb_monitor/xstocks_v2/legs_eth.py` | exact approve、按池子报价币买入、`符号 - Ethereum` 充值。阶段 1 才接进状态机 |
| `ops/xstocks_v2_policy.toml` | 上面的阈值 |
| `scripts/xstocks_v2_scan.py` | 阶段 0 入口。只读 |

`SpotVenue` 只有 `quote_buy` / `quote_sell`。返回数量、单位、场所时间、接收时间、来源、深度、费用档。
策略不直接调 Kraken、Jupiter 或 RPC。

统计：

- 记分板的一行是「买场所 → 卖场所」，不是单个场所。
- 分母是这条路径这一轮两侧都有新鲜可成交报价的次数。
- 数据质量行单独进「不可信」。
- 分开报「下得了手」和「过了 20 bps 且过了 0.1 USDT」。
- 阶段 1 落紧凑行。阶梯报价单独落，并接入现有 `[retention]`：原始行 48 小时，更早的只留信号行。

## 12. 测试与验收

测试必须 hermetic：不碰真实网络、真实 CLI、真实停机文件。断言打在不变量上，不打在耗时上。

| 测试 | 锁住的不变量 |
|---|---|
| 单位 | EVM 不乘；Solana/TON raw×multiplier；空的充值样本不是 1 |
| 最小量 | 用 `effective_min_units`。STRCx 拒绝 0.05、接受 ≥ 0.05459063574 |
| 时间 | 接收时间差 > 10000 ms，或年龄 > 15000 ms → 不可信，净利为空 |
| 漏斗 | 只有 Kraken 买一、没有卖一的行，不能生成 Kraken→链候选 |
| 同池 | 同一 pool id 的一对被拒绝 |
| 图 | Solana→Mantle 不存在。X Layer→HyperEVM 不存在。边是有向的 |
| CCIP | EVM→Solana 才检查 bitmap 772 和 11 账户。EVM→EVM 拒绝 772。Solana 源拒绝 EVM `send`。换源链则第 3 槽必须变 |
| 日历 | DST、半日市 13:00、假日、2027 为 `calendar_unknown` |
| 门槛 | 20 bps 和 0.1 USDT 都要过。`session_split=false` 时闭市不放宽到 10 |
| 锁 | `unknown` 之后锁还在，HALT 文件在，下一轮 `open_gate` 失败 |
| 重试 | 已广播未知不会第二次调用发送函数。approve 抢跑会第二次调用，且参数相同 |
| 原子回滚 | 状态是 `reverted`，没有 HALT |
| v1 | v1 锁存在时，广播函数不被调用 |
| 余额 | `balance is None` 不开仓 |
| ETH 腿 | 非 green、无 Ethereum 充值方法、gas 成本缺失、gas 低于 floor，都不可执行。没有充值样本时尺寸是最小量，不是拒绝。EVM 数量不再乘 multiplier |
| 小余额 | 0.24 USDC 对 STRCx 是 `below_min_size`。对最小量低于 0.24 的币可以开。Solana 纸面不受影响 |
| 记分板 | 分母不是监控轮数。陈旧行不进机会 |

从 `docs/lessons/recent-mistakes-2026-09-24-to-10-06.md` 反推的回归，优先这些节：

| 节 | 在 v2 里的落点 |
|---|---|
| 2.1 / 2.2 / 2.6 / 2.7 | 成本按路径加总；转账费用实测档；报每股前先换算；空读不是 0 |
| 3.3 / 3.4 / 3.5 | `effective_min_units`；增量入账；卖出后轮询 |
| 3.6 | approve 抢跑重试一次 |
| 4.1 / 4.2 | 模拟通过不等于到账；772 不能换链照抄 |
| 4.5 / 4.7 | 探针不进 Kraken；时长未知就不可执行 |
| 5.1 / 5.3 / 5.5 / 5.6 | 固定费进净利；深度是上限；同池拒绝；滑点×名义要看得见 |

验收：

| 阶段 | 过关条件 |
|---|---|
| 0 | 单位测试全过。扫描能标出不可执行原因。没有任何签名 |
| 1 | 纸面 ≥ 3 天。每条路径族有候选数、复价后净利分布、拒因分布。`false_t0` 单独计数。纸面期间 v1 仍可 live |
| 1 的「无假信号」 | 复价后不过门槛的行没有被标成可执行 |
| live | 每条放开的路径族先一笔最小额闭环。最小额仍要 ≥ 充值最小量和卖出最小量 |
| 对账 | 预估和实现的符号相同。`abs(实现 - 预估) / 名义金额` ≤ 15 bps。另记绝对差额。不要用净利的 20%：0.1 USDT 的 20% 只有 0.02，小于 gas 噪声 |
| 收尾 | 没有未清除的 HALT，没有 `held_at=unknown` |

单笔从 25 提到 100 是另一次 operator 决定。纸面证明固定费吃掉了真实边缘之后再改 `ops/xstocks_v2_policy.toml`。不在阶段 0 改。

## 13. 审查对照

实现时按编号核对。规格正文已经吸收了「改成」这一列。

### 13.1 阻断

| 编号 | 问题 | 证据 | 改成 |
|---|---|---|---|
| B1 | 用 bitmap 772 泛化所有桥会锁币 | 772 和 11 账户只属于 EVM→Solana。第 3 槽是源链选择子。换链照抄，源链仍成功，对面永久失败（教训 4.1、4.2）。EVM→EVM 和 Solana 源是另外两套。桥费不能外推（教训 4.3） | 第 6、7 节。三种构造器。未 measured 的边不可执行。探针每个三元组一次 |
| B2 | 独立 `data/` 隔离不了同一把钥匙 | 两个 v1 policy 现在都是 live，见第 0 节。Kraken nonce 必须严格递增。两条进程同时签名会制造未知状态 | 第 8.2 节。纸面可并行。广播前看 v1 的锁和进程。live 子命令先要求 v1 自动链已停 |
| B3 | 现成状态机不会在未知状态停住 | `CycleLock.__exit__` 总是删锁。Solana 自动链把 phase 写回 idle。ETH `open_gate` 不读 phase | 第 8 节。unknown 和带库存的 stopped 保留锁并写 HALT |
| B4 | 同时刻价差不是路径净利 | 时间差闸门只比较两份报价。X Layer→Solana 实测 22–27 分钟（教训 4.7） | 第 5.3、5.4 节。门槛看复价后的边缘。时长未知则不可执行 |
| B5 | 「underlying share」会把 1.0918 乘回去 | STRCx 实盘 1:1。SPV 单位是另一列。只有 STRCx 有充值样本 | 第 4 节。规范名是 Kraken 入账单位 |
| B6 | 阶段 1 只看 Kraken 买一，看不到另外三条路 | 买一只服务「链上买、Kraken 卖」。同池往返是亏的（教训 5.5） | 第 5.2 节。阶段 1 双边。同池拒绝。中价不进净利 |

### 13.2 逻辑

| 编号 | 问题 | 证据 | 改成 |
|---|---|---|---|
| L1 | 闭市 10 bps、开市 20 bps，和冻结报价的证据相反 | 教训 5.7。半日市若写死 16:00，13:00 之后仍会被当成正式盘 | 第 3、9 节。第一版单一 20 bps。日历只打标签。2027 fail closed |
| L2 | 上限改 100 现在不生效 | 实际金额还要过钱包和深度。三边现金都小于 50。历史上上限被改到 100 后又要求改回 | 第 3 节。第一版保持 25 |
| L3 | 「三条链可直接开工」和余额不符 | ETH USDC 0.24。STRCx 最小量大约要 5 USDC。OKB 只在旧费率下够约 2 次 | 第 3.1、3.2 节。不补 USDC。不够最小量就拒绝这一笔 |
| L4 | 每笔都先发最小额 | 桥费按消息收。探针低于 Kraken 最小充值量会永久丢失（教训 4.5）。MRNAx 桥最小 0.0001，充值最小 0.0164 | 第 7 节。探针一生一次，留在自己钱包 |
| L5 | 「不重试」盖掉了该重试的失败 | approve 抢跑要原样重试一次（教训 3.6）。原子回滚没有仓 | 第 8.3 节 |
| L6 | 只有 green 就能执行 | v4 文档：green 不是执行授权。STRCx 的正净利是单位错误 | 第 6.4 节。资格是 AND |
| L7 | 验收用净利的 20% | 0.1 的 20% 是 0.02，小于 ETH gas。符号相反时，用名义金额当分母会看起来很小 | 第 12 节。符号相同，且相对名义 ≤ 15 bps |
| L8 | 「10 条链近全互联」过满 | 第 6.2 节的缺边。1370–1472 是登记不是池子。现有路线索引只认到 Solana | 有向图，键含品种 |
| L9 | 缺 wrapper 和报价币 | wrapper 不能充 Kraken。USDG / USDT / USDC / ZUSD 要换算 | 第 6.1、10 节 |
| L10 | 回款写进套利循环会把利润送掉 | 提币费随网络和金额变。ETH USDC 6.30 → 5.44。旧规则是攒到阈值再提（教训 5.2） | 第 8.4 节。低于 floor 只拒绝开仓 |

### 13.3 优化

| 编号 | 改成 | 落点 |
|---|---|---|
| O1 | 图只过滤。执行器按路径族写死 | 第 6 节 |
| O2 | 同链两腿做成一笔交易 | 阶段 2。失败为 `reverted` |
| O3 | 宇宙用普查结果。Jupiter 共用限速器。批次 ≤ 10。坏池不拖垮全场 | 第 5.2 节 |
| O4 | 先 `.gitignore` 再 `git init`。v2 自己的 `PYTHONPATH`。sqlite 只读 | 第 2.1 节 |
| O5 | 记分板分母是两侧都新鲜的轮数。阶段 1 紧凑留存 | 第 11 节 |

### 13.4 原文第 9 条补充怎么处理

| 原文补充 | 处理 |
|---|---|
| 1 卖出回款去哪 | 采纳，但补货离开套利循环。见 L10、第 8.4 节 |
| 2 同一币跨链单位不同 | 采纳。用词改为 Kraken 入账单位。见 B5 |
| 3 链上卖出也有夹子 | 采纳，并再进一步：同链必须原子。见 O2 |
| 4 非同时刻报价 | 采纳一半。时间差闸门保留。再加复价。见 B4 |
| 5 库存与裸腿窗口 | 采纳。动作是持久 HALT，不自动止损。见 B3 |
| 6 统计按路径 | 采纳。见 O5 |
| 7 数据留存 | 采纳。见第 11 节 |
| 8 只有 green 进执行链 | 必要，不够。见 L6 |
| 9 Jupiter 额度 | 采纳。宇宙缩小，并让路给 v1。见 O3 |

## 14. 原文（2026-10-07，已被第 1–12 节取代）

下面是审查前的计划，保留作对照。不要按这一节实现。

### 14.1 Summary

把「链上买入 → Kraken 卖出」的单出口结构，升级为「任意买入场所 → 任意卖出场所」。路径族四类：①同链 DEX↔DEX；②链↔Kraken 双向；③链A买→链B卖（Chainlink CCIP）；④Kraken买→链上卖。

| 决策项 | 原文定稿 | 取代它的章节 |
|---|---|---|
| 工程载体 | `git init` 建基线 + `git worktree ../arb-v2` 分支 `feat/any-to-any` | 第 2.1 节。先 gitignore |
| 新旧关系 | v1 原地照常跑（含 live 自动链）；v2 独立目录、独立 `data/`、独立端口 8771，只读起步 | 第 0、8.2 节。独立目录不够 |
| 扫描范围 | 177 个 xStocks 全量扫双边报价；执行白名单单独维护 | 第 5.2 节 |
| 门槛 | 净价差 bps，闭市 10 / 美股正式开盘 20，且净利 ≥ 0.1 USDT，两者取严 | 第 3、9 节 |
| 规模 | 单笔上限 25 → 100 USDT；资金验收后再充 | 第 3 节。保持 25 |
| 并发 | 每链一笔在飞（同链不并发） | 第 8.2 节。全局一笔 |
| 失败 | 未知状态即停机 + 飞书，不重试、不回滚 | 第 8.3 节 |
| CEX | 先只 Kraken，接口做成场所无关 | 仍有效 |

### 14.2 原文的关键改动

**报价层（双边 + 多链）**

- Kraken 侧几乎零成本：公开 `Ticker` 一次调用已同时返回买一 `b` 与卖一 `a`（现只用 `b`）；`Depth` 改成逗号多对批量。
- Solana 卖出价 = Jupiter `quote` 反向 mint（token→USDC/USDT）；EVM 卖出价 = QuoterV2/V4Quoter 反向。同链多池走 JSON-RPC batch / Multicall，一次 HTTP 往返。
- 两阶段漏斗：阶段 1 全量粗筛（参考价 + 池子中价 + Kraken 买一），阶段 2 只对「距门槛 < 缓冲」的少数标的做真实阶梯与反向深报，目标一轮 ≤ 60 s、深报 ≤ 20 个。
- 单位归一：统一到 underlying share 口径。EVM `balanceOf()` 已是 scaled，Solana/TON 要 ×multiplier——这条必须有测试锁死（STRCx 1.0918 的错刚犯过）。
- 时间差是闸门：每条报价记场所时间、本地接收时间、序列号；两侧时间差超限、或报价陈旧，进「不可信」列，不给净利。

**路径可行性层**

- RouteGraph：节点 = (chain, venue)，边 = 可执行动作 + 成本 + 时长。每条候选带 `executable` 与不可执行原因。
- 跨链只用 CCIP（`data/xstocks_book.sqlite` 的 `bridges` 表驱动：10 条链近全互联，1370–1472 品种，X Layer→Solana 实测费 0.000791 OKB ≈ $0.10）。
- 复用 X Layer→Solana 的已实测样本并泛化：extraArgs 按源/目的链选择子构造，目的侧关联账户先建，发前解码 CCIP 消息校验 11 账户与 bitmap 772，先发最小额再发余量。
- Kraken 提币路线只在「该 (币种, 网络) 已在地址簿」时可用。Kraken API 不能新增地址。白名单只列真正要用的标的。
- 成本模型按路径独立：池费/滑点/gas/CCIP 费/提币费/费率/价差缓冲，全部 `Decimal`。

**状态机与执行**

- 复用 `execution/xstocks_cycle.py` 的 append-only JSONL 账本，`CycleLock` 从全局锁改为按链锁；`data/KILL_SWITCH` 与 `data/AUTO_HALT` 语义不变。
- 下单前强制复价；提交后回读账户核对，不信任返回码。
- 链上卖出同样加滑点容忍 + 防夹小费（现在这套只用在买入侧）。

**美股开市判定**

- 新增 `core/us_calendar.py`，与 `core/hk_calendar.py` 同构：`America/New_York` + 2026 NYSE 休市表 + 半日市（13:00 ET 收盘）。
- 只认正式盘 09:30–16:00 ET；盘前、盘后、夜盘一律按「闭市」→ 10 bps。判定结果写入每轮快照，阈值单点化配置。

### 14.3 原文的接口、账户、测试、假设

接口：

| 原文 | 内容 |
|---|---|
| `SpotVenue` | `quote_buy()` / `quote_sell()` |
| `RouteGraph` / `RouteEdge` | 买场所→卖场所的可执行性、成本、时长、不可执行原因 |
| `ops/xstocks_policy.toml` | 原文要改成单笔 100，闭市 10 / 开市 20。**不要改这个文件。** v2 用 `ops/xstocks_v2_policy.toml` |
| 开关 | `mode` / `auto_orders` / `live_ack` 仍是 operator 专属 |
| 端口 | v2 看板 8771。v1 的 8770、v4 watch、自动链不动 |

账户原文：

| 项 | 原文结论 | 规格里的用法 |
|---|---|---|
| Kraken 46.95 USD；Solana 44.54 USDT + 0.4 USDC + 0.0136 SOL；X Layer 38.62 USDG + 0.00177 OKB；ETH 0.00167 ETH | 三条链可直接开工 | 三条链都进代码。ETH 不补 USDC，开仓看最小量，见第 3.2 节 |
| BSC / Arbitrum / Optimism / Monad / HyperEVM / Ink 为 0 | 等验收后再充 | 仍有效 |
| Kraken 地址簿 5 条：`hkzabank`、`xlayertee1`、`soltee1`、`ethusdt1`、`ethusdc1` | ④ 要按白名单逐币加地址 | 仍有效。阶段 4 之前不执行 |
| Ethereum USDC 0.24 | 要跑 ETH 需先补 | D2：现在不补。够最小量就做，不够就 `below_min_size` |

原文测试计划：

1. hermetic 单测：单位归一、双边聚合与时间差闸门、RouteGraph、CCIP extraArgs、US 日历、门槛取严、每链一笔与停机语义。
2. 从教训库 43 条反推，重点覆盖 §2.1/2.2/2.6/2.7、§3.3/3.4/3.5、§4.1/4.2/4.5/4.7、§5.1/5.3/5.5/5.6。
3. 纸面 ≥ 3 天。
4. 实盘先每族一笔最小额，再放到 100 USDT。
5. 验收：paper 无假信号；实盘对账差 ≤ 20%；无未知状态遗留。

这些测试意图保留。断言改成第 12 节的不变量。100 USDT 和「净利的 20%」不再使用。

原文假设：

- 只做 xStocks 现货。港股永续腿不进 v2。仍有效。
- 跨链只走 CCIP，不走 Kraken 中转。仍有效。Kraken 提币是路径 ④ 和补货，不是跨链捷径。
- US 日历 2026 年有效。仍有效，并且 2027 要 fail closed。
- v2 第一版启用 Solana / X Layer / Ethereum。三条都进「链 → Kraken」。X Layer 先不桥。ETH 不补 USDC，开仓看最小量，见第 3.2 节。

原文自己补的 9 条见第 13.4 节。
