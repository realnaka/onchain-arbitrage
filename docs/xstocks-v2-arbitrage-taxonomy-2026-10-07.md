# xStocks v2：套利方案的完整枚举（2026-10-07）

operator 问：是不是按「链上买/CEX 买/链A→链B/同链两池」这几种场景写的代码？
每种都跑通过吗？每种都有测试吗？这份文档就是答案，**表里的每一行都能追到代码、
账本和测试**。

## 1. 两个「端点」和六种走法

一笔套利永远由「买入口」「卖出口」和它们之间的搬运方式决定。端点是
**CEX（Kraken）** 或 **某条链上的池子**；搬运方式是 **同链换池** / **跨链桥** /
**CEX 提充**。穷举出来的走法只有六种：

| # | 场景 | family（快照里的名字） | 看板中文 |
|---:|---|---|---|
| 1 | 链上买 → Kraken 卖 | `chain_to_kraken` | 链 → Kraken |
| 2 | Kraken 买 → 链上卖 | `kraken_to_chain` | Kraken → 链 |
| 3 | 链 A 买 → **桥** → 链 B 卖 | `chain_to_chain` | 链A → 链B（跨链） |
| 4 | **同一条链**两口池买/卖 | `dex_to_dex` | 同链两口池 |
| 5 | 链 A 买 → **桥** → 链 B（Kraken 收）→ Kraken 卖 | `chain_via_bridge_to_kraken` | 链 → 桥 → Kraken |
| 6 | Kraken 买 → **提**到链 A → **桥** → 链 B 卖 | `kraken_via_bridge_to_chain` | Kraken → 桥 → 链 |

5、6 是 1、2、3 的组合：**Kraken 只对少数几条链收币**，所以链 A 的池子和 Kraken
之间常常要绕一段桥。`#3` 里的「链 B」再换成 Kraken 就是 `#5`，同理 `#6`。

## 2. 覆盖情况（代码 / 执行器 / 跑通 / 测试）

> 2026-10-09 补充：链 → Kraken 的执行器已覆盖 solana、eth、ton、x-layer
> （x-layer 没有 Kraken 充值轨，走桥到 Solana）。其余有向边的结论在
> `src/arb_monitor/xstocks_v2/capability.py`，不是第二份链名单。
> `no_lane`（含全部 TON 跨链、以及 `routes.MISSING_EDGES`）没有广播入口。
> 本轮新的源链确认：X Layer→Ink
> `0x0000000000000000000000000000000000000000000000000000000000000000`，
> X Layer→Ethereum
> `0x0000000000000000000000000000000000000000000000000000000000000000`。
> 目的链到账还要等 CCIP，不把「源链已出」写成「对面已到」。

| # | 场景 | 扫描 | 执行器 | 真跑通闭环 | 测试 |
|---:|---|---|---|---|---|
| 1 | 链 → Kraken | ✅ | v1 的执行模块（v2 只纸面） | v1：`spot_filled` 12 笔、`spot_sold` 2 笔；**v2 只到 `refused`，0 次成交** | ✅ paper / sell |
| 2 | Kraken → 链（单跳） | ✅ | 作为 `#6` 的第一段 | 见 `#6` | ✅ |
| 3 | 链A → 链B | ✅ | **两个方向都有**：`xlayer_via_bridge_to_solana`、`solana_via_bridge_to_xlayer` | x-layer→solana **已跑通**（2026-10-07 18:56 `sold`）；solana→x-layer 有执行器，**还没跑过真钱** | ✅ 选择器 + 阶梯 + 对账 |
| 4 | 同链两口池 | ✅（5 行/轮） | **无**：必须一笔原子交易，现在拿不到证明 | ❌ 没跑过 | ✅ `atomic.py` 规则 |
| 5 | 链 → 桥 → Kraken | ✅ | ✅ `xstocks_v2_solana_roundtrip.py --exit kraken`（2026-10-07 晚补上，并已接进常驻 live） | 2026-10-07 22:18–22:50 第一次真跑：买 ✅ → approve ✅ → 桥 ✅（到账 raw 1100697）→ 充值腿抛 `NameError` 停住（当晚已修）→ 币被人工补进 Kraken；**只剩「卖」这一腿没由本脚本完成** | 部分（充值/卖出腿的 hermetic 测试见 `docs/xstocks-v2-todo.md` A2） |
| 6 | Kraken → 桥 → 链 | ✅ | ✅ `xstocks_v2_xlayer_roundtrip.py` | **已跑通**（2026-10-07 17:0x `sold`） | ✅ |

账本证据（`data-v2/xstocks_v2_cycles.jsonl`）：

```text
kraken_via_bridge_to_chain   sold 1 / ok 5 / stopped 1 / unknown 1 / refused 3
xlayer_via_bridge_to_solana  sold 1 / ok 6 / stopped 2 / unknown 2 / refused 4
chain_to_kraken              refused 1016 / sold 0        <- v2 没有成交过
dex_to_dex / chain_to_chain / chain_via_bridge_to_kraken  <- 从未进入执行器
```

> 更新（2026-10-07 22:5x）：`chain_via_bridge_to_kraken`（#5）**已进入执行器**：22:23:14
> 桥广播成功（`0x46d630aa…`）、22:48:45 Solana 到账（raw 1100697），充值腿因 `LIVE` 未绑定
> 抛 `NameError` 停在 `stopped`，随后币被转进 Kraken（`SPYx.T = 0.011070`）。详见
> `HANDOFF.md` 顶部 2026-10-07 22:3x–22:5x 段。

## 3. 现在缺什么

| 缺口 | 影响 | 状态 |
|---|---|---|
| `#3` solana→x-layer 执行器 | 唯一长期正价差的方向没有执行器 | **2026-10-07 已补**（`xstocks_v2_solana_bridge_roundtrip.py`），还没跑过真钱 |
| `#4` 原子路线证明 | 同链两池完全不能做 | **2026-10-07 晚已证不需要自建 router**（X Layer 的 Uniswap v3 `SwapRouter02` + `multicall([exactInput, sweepToken])` 真 `eth_call` 通过，见 `docs/xstocks-v2-atomic-router-proof-2026-10-07.md`）；**真正的卡点是第二口池 `liquidity == 0`** |
| `#5` 链→桥→Kraken 执行器 | 只有监控 | **2026-10-07 晚已补**（`xstocks_v2_solana_roundtrip.py --exit kraken` + 常驻 live 接线）；第一次真跑走完前三腿，充值腿 `NameError` 已修，只剩卖出腿 |
| `#1` v2 成交 0 次 | 主力场景在 v2 没有实证 | 门槛是 20/4 bps + 净利 0.1；钱包与固定费经常把它压下去 |

## 4. 每个方向「值不值得做」要按钱包算，不按 200 算

扫描的净利用的是 `per_trade_quote_usdt`（200）。桥费是**固定**的，所以同一行在
不同票面下结论相反。实测（SPYx solana→x-layer，62.2 bps，桥费 0.1003 USD）：

| 买入侧可用资金 | 净利 | 结论 |
|---:|---:|---|
| 200 USDT | +0.94 | 过 |
| 50 USDT | +0.11 | 过（刚好） |
| **8.48 USDT（Solana 上真实 USDC）** | **−0.06** | **不做** |

所以 v2 的选择器现在**按钱包实际能出的钱**重算净利（`bridge_net_ok(..., available_usdt=…)`），
余额读不到就是拒绝。这一条同时解释了为什么 `#1` 在 v2 一直 0 成交。
