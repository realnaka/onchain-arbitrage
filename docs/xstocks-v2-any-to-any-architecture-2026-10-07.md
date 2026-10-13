# xStocks v2：any-to-any 路径架构

日期：2026-10-07。目标：**任意买入场所 → 任意卖出场所**。这份文档写清
「有哪些路径」「怎么选」「每一步会怎么坏」「坏了怎么办」「怎么加新场所」。

代码：`src/arb_monitor/xstocks_v2/paths.py`（图 + 排序）、
`src/arb_monitor/xstocks_v2/executor.py`（状态机）、
`scripts/xstocks_v2_paths.py`（穷举打印）、
`tests/test_xstocks_v2_paths.py`（21 个 hermetic 用例）。

## 1. 节点不是一个场所，是「仓位可能在哪」

```text
node = (venue, chain, form)
```

| 字段 | 取值 | 为什么必须分开 |
|---|---|---|
| `venue` | `kraken` 或链名（`solana` / `x-layer` / `eth` / …） | Kraken 的余额不在任何链上 |
| `chain` | 链名；Kraken 是 `None` | 同一个地址在不同链上余额不互通 |
| `form` | `cash` / `native` / `wrapper` | **wrapper 不能充 Kraken；X Layer 的深池报的是 wrapper，桥给的是 native** |

`cash` 是钱（Kraken 的 USD、链上的 USDG/USDT），`buy` 是
`cash → native/wrapper`，`sell` 是反向。没有 `cash` 节点，现货腿会变成同场所自环，图就走不出去。

## 2. 五种边，穷举就靠这五种

| 边 | 从 | 到 | 跨越 | 约束 |
|---|---|---|---|---|
| `buy` / `sell` | cash | token | 同场所 | 盘口深度、costmin/ordermin、hook 分级 |
| `wrap` / `redeem` | native | wrapper | **只在同一条链** | ERC-4626；非 EVM 链没有 wrapper |
| `bridge` | native | native | 链 A → 链 B | CCIP；三种方言（`evm_to_solana` / `evm_to_evm` / `solana_source`） |
| `deposit` | 链 native | Kraken | 链 → CEX | 该币该网络必须有 Kraken 充值方法 |
| `withdraw` | Kraken | 链 native | CEX → 链 | 地址簿必须有 key，且有最小额和费 |

**图的边不是手写的**：`bridge` 边的存在性来自 `routes.has_bridge()`，也就是
`data/xstocks_book.sqlite` 的 `bridges` 登记表。登记表里缺的边在这里自动缺，
不存在「文档说有、代码里能走」的情况。

## 3. 现在的真实边集（2026-10-07，可复跑）

```bash
PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_paths.py SPYx --notional 200
```

CCIP 登记里**缺**的边（29 条，`routes.MISSING_EDGES`）：Solana↔Mantle、
X Layer↔HyperEVM/Optimism、Ink↔HyperEVM/Optimism、Mantle↔Optimism、
Optimism↔X Layer，等。TON **不在这座桥里**。

跑出来的几个真实结论：

| 观察 | 说明 |
|---|---|
| `solana → x-layer` 是唯一 `executable=YES` | 因为它是唯一有 `arrived:true` 样本的边（2026-10-07 探针） |
| `solana → mantle` 走**两段桥**（经 X Layer） | 登记表没有直连，图会自动拼出中转；每多一段桥就多一份费和一次裸腿窗口 |
| `x-layer → *` 必须先 `redeem` | X Layer 没有原生池，census 里只有 wrapper 池 |
| `eth → *` 同样先 `redeem` | 同上 |
| `* → x-layer` 必须 `wrap` | 桥上拿到的是原生币，深池收的是 wrapper |
| Kraken 出现在路径里只有两种位置 | 起点（买）或终点（卖）；**默认不允许当中转**（`allow_cex_hub=False`），因为那是两次提币两次吃单 |

## 4. 多条路径怎么选

```text
第一关键字：总成本（bps + 固定费摊到票面）
第二关键字：总时长
第三关键字：腿数
```

**先便宜的，不先快的。** 原因：能不能做由「复价后净利 ≥ 门槛」决定，成本越低
留的余量越大，同样的行情下更容易过门槛；快只影响持有风险，不影响是否过门槛。
时长仍然进排序（每多一秒都是裸腿窗口），并有一条硬上限
`max_total_duration_s`（默认 1800 s）。

排序前先过闸门，任何一条不满足就 `executable=false` 并写明原因：

| 原因 | 触发 |
|---|---|
| `duration_unknown:<leg>` | 这条腿没有实测时长。**未知不是快** |
| `unmeasured_route` | 桥/提币没有「对面到账」样本 |
| `below_min:<leg>` / `min_unknown:<leg>` | 低于最小额，或最小额未知 |
| `above_depth:<leg>` | 超过 `池深 × max_depth_share`（默认 0.2） |
| `too_slow:<sec>` | 总时长超预算 |

固定费的量级（实测）：$7.80 票面摩擦 604 bps，$200 票面只剩 ~40 bps。
**这就是为什么小票面根本不该盯 20 bps 门槛。**

## 5. 每一步会怎么坏，坏了怎么办

### 5.1 按腿分

| 腿 | 失败 | 看到什么 | 归类 | 处理 |
|---|---|---|---|---|
| `buy` (CEX) | 低于 ordermin/costmin | 报错 | refused | 金额取严；无仓，下一轮可再试 |
| | 部分成交 | 余额 < 计划 | refused | **用实际成交量继续**，不得假设全额 |
| | 广播后无回执 | 无 txid 状态 | **unknown** | HALT + `held_at=kraken`，人工对账 |
| `withdraw` | 参数写错 | `EFunding:Unknown asset` | refused | xStock 必须 `aclass=tokenized_asset`（实测） |
| | 低于 minimum | 报错 | refused | 事前用 `WithdrawMethods.minimum` 卡 |
| | refid 有了但链上没到 | 对面余额不变 | **unknown** | HALT。**绝不重复提**，重提=双倍出金 |
| `bridge` | 模拟失败 | `eth_call` 回滚 | refused | 无仓，下一轮再看 |
| | CU 不足 | `Computational budget exceeded` | refused | Solana 源必须带 ComputeBudget 400k |
| | 收款全零 | 合约 `TOKEN_RECEIVER_INVALID` | refused | 发前解码 payload 校验非零 |
| | 源链成功、对面没到 | 源链扣币、对面 0 | **stopped/unknown** | `held_at=in_bridge`，HALT，人工重放或等 |
| `wrap`/`redeem` | revert | 回执 status=0 | reverted | 无仓 |
| `sell` (DEX) | 返回 txHash 但没广播 | `wallet history: ERROR / in-flight limit` | retry → unknown | 重试一次（allowance 已在）；再不行 HALT |
| | 滑点超限 | 回执 status=0 | reverted | 无仓，可重试或换路 |
| | wrap 成功、swap 失败 | 手里是 wrapper | **stopped** | `held_at=dest_wallet`，不是 unknown（知道币在哪） |
| `deposit` | 低于充值最小额 | 币**永久不到账** | refused | 金额取严；这类错误补一笔也找不回 |

### 5.2 按场景（状态机）

| 状态 | 币在哪 | 锁 | HALT | 下一轮 |
|---|---|---|---|---|
| `refused` | 没动 | 释放 | 不写 | 可以开新仓 |
| `reverted` | 原子回滚，没仓 | 释放 | 不写 | 可以开新仓 |
| `sold` | 两边都完成 | 释放 | 不写 | 可以开新仓 |
| `stopped` | 有库存，位置写在 `held_at` | **保留** | **写** | 不能；人工对账后删 HALT |
| `unknown` | 不知道 | **保留** | **写** | 不能；人工对账 |

三条不可协商的规则：

1. **每一步都要过闸门**，不是每轮过一次。`kill` / `auto_halt` / `halt` /
   `lock` / `v1_live_busy` 任一成立就拒绝，且拒绝发生在调用 runner **之前**。
2. **返回成功不算成功**。有 `verify` 的步骤要回读世界；2026-10-07 实测
   `swap execute` 回 `ok:true` + txHash，实际是后端在飞上限挡掉、交易没上链。
3. **重试只给它列出来的原因**（approve 抢跑、在飞上限）。第二次仍失败即 unknown。

## 6. 怎么加一个新场所

| 加什么 | 改哪里 | 不用改 |
|---|---|---|
| 新链（已有 CCIP 登记） | 登记表进 `xstocks_book.sqlite` 后自动出现 | 图、排序、脚本 |
| 新链（要新建边） | `paths.py` 加一个 builder（照 `bridge_legs` 写） | executor、脚本 |
| 新 CEX | `paths.py` 加 `deposit`/`withdraw`/`buy`/`sell` 四个 builder | 图算法、状态机 |
| 新桥（非 CCIP） | 新增一种 `kind` + 它的方言校验 | 其余全部 |
| 新池子/新 form | `dex_trade_legs(form=...)` 传新 form，`wrap_legs` 加对应边 | 排序规则 |
| 新现货场所（同一条链上的另一种成交，例如 xChange） | 新的 `Quote` 来源（`feeds/xchange.py`），扫描把它并进 `chain_quotes` | 图算法、桥、三把钥匙 |

约束：**同一场所买、同一场所卖（同链两池）必须是一笔原子交易**，那是阶段 2，
不走这套状态机。跨链走 CCIP，不走 Kraken 中转。

### 阶段 2 的现状（2026-10-07 晚更新）

代码在 `src/arb_monitor/xstocks_v2/atomic.py`，只读计划脚本
`scripts/xstocks_v2_atomic.py`。规则已经落地成可测的判据：

| 判据 | 函数 | 结果 |
|---|---|---|
| 同链、两个不同池 | `atomic.same_chain_two_pool` | 否则 `not_same_chain` / `same_pool` |
| **一次调用**的证明 | `atomic.route_is_single_call` | 只认**环形**路线（同进同出代币）且两个池 id 都在 route 里；缺证明即拒绝 |
| 净利 | `atomic.net_usdt` | `名义×edge_bps/1e4 − gas − 名义×(slippage+池费)/1e4`，缺项即未知 |

扫描侧：同链两池的 `dex_to_dex` 行现在带 `atomic_required: true` 和
`atomic_route_unproven`，`executable=false`——**任何消费者都不能把它当两条腿执行**。

**卡点已重新实测（2026-10-07 晚，证据见
[xstocks-v2-atomic-router-proof-2026-10-07.md](xstocks-v2-atomic-router-proof-2026-10-07.md)）**：

| 旧结论 | 实测结果 |
|---|---|
| 聚合器拒绝环形报价 → 拿不到「一次调用走两口池」的证明 | ✅ 报价确实被拒（`Cannot swap a token to itself.`） |
| 所以要自建 router 合约 | ❌ **错**。报价工具的限制 ≠ 链上能力 |

用 X Layer 上**现成的** Uniswap v3 `SwapRouter02`
（`0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca`，已逐项链上核对）做
`multicall([exactInput(path), sweepToken(...)])` 的真实 `eth_call`：**两个池
（`USDG →100→ WOKB →500→ USDG`）与三个池都成功**，QuoterV2 预测值与返回逐位相等。
全程只读（allowance 用 state override 伪造，没有签名/approve/广播）。

**所以阶段 2 的真实边界不是「缺合约」，是「缺第二个有流动性的池子」**：
X Layer 上每个 xStock 只有一个有流动性的池（SPYx 的第二个池 `wSPYx/USDC` 5 bps
`liquidity == 0`），`dex_to_dex` 因此**没有可交易对象**（扫描快照 0 行候选，
与链上一致）。自建合约不会造出流动性。

calldata 生成已进 `atomic.py`（`ring_calldata` 等），探针脚本
`scripts/xstocks_v2_atomic_probe.py` 的 `discover` / `pools` / `ladder` / `simulate`
全部只读可复跑。

### 桥费表与阶梯报价（同日补充）

桥费表的边界条件写在这里，免得下次再推一遍：

* 桥费是**按消息**收、付在**源链 gas 代币**上，所以「费用」和「gas 代币美元价」
  是两个独立的输入；**任缺一个都是 `cost_unknown`**（`bridge_fees.fee_usd`）。
* 已定价的边：`x-layer->solana` 0.000791242725185203 OKB ≈ 0.105259 USD；
  `solana->x-layer` 0.000852448 SOL ≈ 0.100306 USD（各 2 个样本）。
* 有了费用，行上会写 `bridge_fee_usd` 和 `net_usdt`；**净利闸门在选择器里**
  （`live.bridge_net_ok`），不在扫描的行标记里。
* 卖出腿在 25/50/75/100% 四个尺寸上做**阶梯确认**（`ladder.py`）：每档都要能报价、
  尺寸递增、最大档相对最小档的均价衰减 ≤ `slippage_buffer_bps`、更大档不能更便宜。
  全尺寸档的报价同时是对账闸门的预测值。
