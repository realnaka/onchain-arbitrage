# family 5 / family 6：全部路径清单（2026-10-07）

示例标的 SPYx，名义 200 USDT。枚举命令：
`scripts/xstocks_v2_paths.py SPYx --notional 200 --max-legs 7`
（本轮先修了一个枚举 bug，见第 4 节）。

读法：一行 = 一条路径。「最少额」取该路径每段最小值的**最大值**（低于任何一段
最小值都不是小交易，是丢币或被拒）。「成本」是**模型值**，不是实测——实测只有
`solana↔x-layer` 两条桥边有费用样本。

## 1. family 5：链上买 → 桥 → Kraken 卖

| # | 路径 | 买入 form | 桥 | 枢纽链 | Kraken 入金 rail | 成本 | 时长 | 最少额 | 阻塞 | 执行器 |
|---:|---|---|---|---|---|---:|---:|---|---|---|
| 1 | `solana -buy-> -deposit-> kraken -sell->`（直连 = family 1） | native | 无 | - | Solana（`SPYx` / `solanspyx1`） | 10.0 bps / $0.2000 | 123 s | 0.00055847 | 无 | 有（`scripts/xstocks_v2_live.py`，v2 内 0 成交） |
| 2 | `x-layer -buy wrapper-> -redeem-> -bridge-> solana -deposit-> kraken -sell->` | wrapper | x-layer→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095（桥下界） | 无（但本轮快照价差是 **−67.7 bps**，闸门不放行） | 有（`xstocks_v2_solana_roundtrip.py --exit kraken` ← `run_via_bridge_cycle`，未跑过） |
| 3 | `eth -…-> solana -deposit-> kraken -sell->` | wrapper | eth→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095 | `unmeasured_route` | 有（同上，同一段代码） |
| 4 | `bsc -…-> solana -deposit-> kraken -sell->` | wrapper | bsc→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095 | `unmeasured_route` | 有 |
| 5 | `arbitrum -…-> solana -deposit-> kraken -sell->` | wrapper | arbitrum→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095 | `unmeasured_route` | 有 |
| 6 | `optimism -…-> solana -deposit-> kraken -sell->` | wrapper | optimism→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095 | `unmeasured_route` | 有 |
| 7 | `monad -…-> solana -deposit-> kraken -sell->` | wrapper | monad→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095 | `unmeasured_route` | 有 |
| 8 | `hyperevm -…-> solana -deposit-> kraken -sell->` | wrapper | hyperevm→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095 | `unmeasured_route` | 有 |
| 9 | `ink -…-> solana -deposit-> kraken -sell->` | wrapper | ink→solana | solana | Solana | 15.6 / 0.3110 | 363 s | 0.0095 | `unmeasured_route` | 有 |
| 10 | `mantle -…-> x-layer -bridge-> solana -deposit-> kraken -sell->` | wrapper | mantle→x-layer→solana | x-layer | Solana | 20.6 / 0.4120 | 543 s | 0.0095 | `unmeasured_route` 两段 | 有（两段桥需 `paths.py` 支持，脚本未跑过） |

**更正（本轮实地核对后）**：family 5 **已经有执行器**，不是「无」。
`scripts/xstocks_v2_solana_roundtrip.py` 有 `--exit kraken` 分支
（`KRAKEN_EXIT`），`scripts/xstocks_v2_live.py:run_via_bridge_cycle()` 就是它的常驻入口，
`live.EXECUTABLE_FAMILIES` 也已包含 `chain_via_bridge_to_kraken`。
账本里这个 family **0 行**——不是没代码，是从来没被触发过（本轮快照里它的价差全是负的）。

去掉的：`ton -…-> kraken`。TON 不在这座 CCIP 桥里，而且 Kraken 的
`SPYx - TON` 入金通道没有地址（本项目没有 TON 地址）。

## 2. family 6：Kraken 买 → 提 → 桥 → 链上卖

| # | 路径 | 卖出 form | 桥 | 枢纽链 | Kraken 出金 rail | 成本 | 时长 | 最少额 | 阻塞 | 执行器 |
|---:|---|---|---|---|---|---:|---:|---|---|---|
| 1 | `kraken$ -buy-> -withdraw-> solana -sell->`（直连 = family 2） | native | 无 | - | Solana（`solanspyx1`） | 10.0 / 0.2000 | 128 s | 0.00055847 | 无 | 部分（无独立执行器） |
| 2 | `kraken$ -buy-> -withdraw-> solana -bridge-> x-layer -wrap-> -sell->` | wrapper | solana→x-layer | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | 无 | 有（`scripts/xstocks_v2_xlayer_roundtrip.py`，1 次 sold） |
| 3 | `…-> eth -wrap-> -sell->` | wrapper | solana→eth | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | `unmeasured_route` | 无 |
| 4 | `…-> bsc -wrap-> -sell->` | wrapper | solana→bsc | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | `unmeasured_route` | 无 |
| 5 | `…-> arbitrum -wrap-> -sell->` | wrapper | solana→arbitrum | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | `unmeasured_route` | 无 |
| 6 | `…-> optimism -wrap-> -sell->` | wrapper | solana→optimism | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | `unmeasured_route` | 无 |
| 7 | `…-> monad -wrap-> -sell->` | wrapper | solana→monad | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | `unmeasured_route` | 无 |
| 8 | `…-> hyperevm -wrap-> -sell->` | wrapper | solana→hyperevm | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | `unmeasured_route` | 无 |
| 9 | `…-> ink -wrap-> -sell->` | wrapper | solana→ink | solana | Solana | 15.6 / 0.3110 | 368 s | 0.0095 | `unmeasured_route` | 无 |
| 10 | `…-> x-layer -bridge-> mantle -wrap-> -sell->` | wrapper | solana→x-layer→mantle | x-layer | Solana | 20.6 / 0.4120 | 548 s | 0.0095 | `unmeasured_route` 两段 | 无 |

### family 6 的第二个维度：Kraken 出金 rail

SPYx 在 Kraken 有 3 条提币通道，我们只登记了 Solana 一条：

| rail | Kraken 最小额 | 地址簿 | 影响 |
|---|---:|---|---|
| SPYx（Solana） | 0.00055847 | 有 `solanspyx1` | 上面 10 行用的就是它 |
| SPYx - Ethereum | 0.00108656 | 无（`ethstrcx1` 是 STRCx 的） | 多一族 hub=eth 的路径，未建模 |
| SPYx - TON | 0.00271542 | 无，且本项目没有 TON 地址 | 不可执行 |

## 3. 六个 family 横向对比（SPYx，$200）

| family | 例子 | 成本 bps | 时长 | 最少额 | 执行器 |
|---|---|---:|---:|---|---|
| 1 chain→kraken | solana→kraken | 10.0 | 123 s | 0.00056 | 有（0 成交） |
| 2 kraken→chain | kraken→solana | 10.0 | 128 s | 0.00056 | 部分 |
| 3 chain→chain | solana→x-layer | 5.6 | 246 s | 0.0095 | 有，两个方向 |
| 4 dex→dex | - | - | - | - | 无（缺第二个有流动性的池） |
| 5 chain→桥→kraken | x-layer→solana→kraken | 15.6 | 363 s | 0.0095 | 有（未跑过；本轮价差为负，闸门不放行） |
| 6 kraken→提→桥→chain | kraken→solana→x-layer | 15.6 | 368 s | 0.0095 | 有，1 条 |

## 4. 本轮同时修掉的枚举 bug

`paths.py:PathGraph.enumerate()` 原来写的是「只要当前节点是 Kraken 且起点不是
Kraken 就不再往下走」。规则本意是「Kraken 不当跨链中转」，但它把
「从链上到达 Kraken 后卖回 cash」也一起剪掉了，于是**以 Kraken 为出口的路径
一条都枚举不出来**（family 1 / family 5 在枚举器里不可见；本轮实测任何标的都是 0 行）。
现在只在 `leg.kind == WITHDRAW`（从 Kraken 提币去另一条链）时才剪。
影响面只有枚举器和 `scripts/xstocks_v2_paths.py`；扫描与执行器用另一套 builder，行为不变。

## 5. 现在能真跑的路径

| 路径 | 需要的资金 | 今天够不够 |
|---|---|---|
| family 5 #1（solana→kraken） | Solana 买入侧 | 够（USDT 44.5405） |
| family 5 #2（x-layer→solana→kraken） | X Layer USDG 够（55.315343），但**价差是 −67.7 bps**，负基差不做 | 资金够、机会不够 |
| family 6 #2（kraken→solana→x-layer） | Kraken ZUSD | 不够（0.0018，要换汇且未授权） |
| family 5 #3–#10、family 6 #3–#10 | 各条链 | 不够：缺到账样本，多数链也没有资金 |

最少额的实际含义：绑定项几乎都是**桥的最小额 0.0095 SPYx ≈ 7.4 USDT**，
不是 Kraken 的 0.00056。低于它就是过不了桥。
