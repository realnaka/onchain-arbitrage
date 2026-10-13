# Uniswap v4 hook 费用、隐藏规则与 xStocks ETH 监控

日期：2026-10-07

范围：只研究 ETH 主网上的 xStocks `Uniswap v4` 池。只做监控，不接执行链。

## 1. 为什么原来的普查把 v4 全部排除了

原来的 v3 报价路径只认识 `QuoterV2` 和池子的静态费率。v4 的费率可以：

- 写在 PoolKey 里，且允许静态或动态；
- 在 `beforeSwap` 里逐笔覆盖；
- 由 hook 另外收一笔 hook fee；
- 由 hook 通过 return-delta 改写买卖双方的余额。

当时为了避免把「池名里的 88% / 95%」这种陷阱当成便宜，代码把所有 v4 池统一挡在 v3 候选之外。

这个保护方向是对的，但结论太粗：**很多 v4 池没有 hook，或者只有非 swap hook。**
因此现在改为两条路：

- v3 路径继续只报价 v3；
- v4 进入独立的 `scripts/xstocks_v4_watch.py`，用官方 V4Quoter 报价，再做 hook 分级。

## 2. 官方规则

来源：

- Uniswap v4 Hooks: <https://docs.uniswap.org/contracts/v4/concepts/hooks>
- Dynamic Fees: <https://docs.uniswap.org/contracts/v4/concepts/dynamic-fees>
- Custom Accounting / Hook fees: <https://developers.uniswap.org/docs/protocols/v4/guides/custom-accounting>
- Security Framework: <https://docs.uniswap.org/contracts/v4/security>
- `v4-core/src/libraries/Hooks.sol`
- `v4-core/src/libraries/LPFeeLibrary.sol`
- `v4-periphery/src/lens/V4Quoter.sol`

### 2.1 hook 权限写在地址最低位

PoolManager 根据 hook 地址的低 14 位决定调用哪些回调：

| bit | 权限 |
|---:|---|
| 13 | beforeInitialize |
| 12 | afterInitialize |
| 11 | beforeAddLiquidity |
| 10 | afterAddLiquidity |
| 9 | beforeRemoveLiquidity |
| 8 | afterRemoveLiquidity |
| 7 | beforeSwap |
| 6 | afterSwap |
| 5 | beforeDonate |
| 4 | afterDonate |
| 3 | beforeSwapReturnDelta |
| 2 | afterSwapReturnDelta |
| 1 | afterAddLiquidityReturnDelta |
| 0 | afterRemoveLiquidityReturnDelta |

`return-delta` 权限必须和对应回调同时存在。它让 hook 可以修改余额，是自定义收费和自定义曲线的核心能力。

### 2.2 费用有四种来源

| 费用来源 | 在哪里 | 对报价的影响 |
|---|---|---|
| LP fee | PoolKey 的 `fee` | 静态费率直接进入 swap 数学 |
| 动态 LP fee | PoolKey 标记 `0x800000`，或在 `beforeSwap` 覆盖 | 每笔可以不同 |
| protocol fee | StateView `slot0` 的 protocol fee | 从输入先收，再收 LP fee |
| hook fee | hook 自己 `take` / `mint` 并返回 delta | 只通过 hook 逻辑出现，不在 PoolKey 里 |

### 2.3 V4Quoter 是 v4 的正确成交价来源

官方 V4Quoter 会调用 PoolManager，`beforeSwap` / `afterSwap` 会实际执行，然后返回 swap delta。
因此：

- 静态 LP fee、动态 LP fee、protocol fee、hook fee，只要 hook 不区分模拟环境，都会进入 quote；
- quote 的 `amountOut` 就是当前 eth_call 下的真实输出；
- quote 还返回 `gasEstimate`，可以用来估算主网 gas。

### 2.4 v4 仍然有一个报价盲区

hook 可以识别模拟。常见模式：

- 使用 `tx.origin`；
- 字节码里写死 V4Quoter 地址；
- 按 `sender` 或 `hookData` 分支；
- 在真实 swap 里才执行某段收费/限流逻辑。

如果出现这些迹象，V4Quoter 的报价不能当作真实成交价。监控必须标红。

## 3. 本项目 v4 watch 的分级

| tier | 条件 | 处理 |
|---|---|---|
| green | hook 地址为零；或 hook 只有非 swap 回调 | 可进入 v4 监控候选 |
| amber | 动态费率；或存在 beforeSwap / afterSwap | 可看经济账，但必须人工审计 hook |
| red | hook 可识别模拟；有 swap return-delta；报价失败；出口不成立 | 只展示原因，不作为候选 |

注意：`green` 只表示 swap 路径没有 hook 特殊逻辑。v4 本身仍是较新的协议，不能自动升级成执行授权。

## 4. 成本模型

v4 watch 对每口池按 1 / 5 / 20 / 100 / 500 USDC 四档报价，然后计算：

```text
Kraken 卖出毛额 = 逐档吃 Kraken 买盘得到的 USDT
Kraken 手续费   = 毛额 × 10 bps
滑点缓冲        = 买入金额 × 10 bps
Gas             = (V4Quoter gasEstimate + 120000) × gasPrice × ETH/USD
净利            = 毛额 - Kraken 手续费 - 滑点缓冲 - Gas - 买入金额
```

同时检查：

- Kraken 是否有 `Symx - Ethereum` 充值方式；
- 数量是否达到 Kraken 充值最小值；
- 数量是否达到 Kraken `ordermin`；
- Kraken 盘口深度是否吃得下该数量的 VWAP。

`execution_ready` 永远为 `false`。这个 watch 只负责把 v4 机会和风险显示出来。

## 5. 2026-10-07 首次运行结果（修正前，收益结论已作废）

输入：

- 范围：Kraken 在册 177 个名字在 Ethereum 上的 native + wrapper，共 354 个地址；
- 发现 v4 池：12 口；
- 能通过 V4Quoter 报价：8 口；
- hook 分级：green 11、amber 1、red 0；
- monitor 候选：2 口，都是 `STRCx / USDC`。

STRCx 的关键数字：

| 池子 | 深度 | LP fee | 1 枚代币买价 | 折合每股 | Kraken 买一 | 净利（500 USDC 档） |
|---|---:|---:|---:|---:|---:|---:|
| `0xabc4…33bf` | 112,747 USD | 0.0888% | 100.2167 | 91.90 | 99.29 | +38.65 USDT |
| `0x25f4…3d61` | 21,029 USD | 1.0% | 约 101.5 | 93.00 | 99.29 | +32.27 USDT |

核对：

- xStocks issuer API：STRCx Ethereum multiplier = `1.0918128111453242`；
- Kraken Assets：`token_multiplier` 完全相同；
- DexScreener：该池 priceUsd = `100.21`，与 V4Quoter 一致；
- Kraken：`STRCx/USD` 状态 `online`，充值方式 `STRCx - Ethereum`，买一有足够深度。

当前 Kraken 充值方式缓存来自 2026-10-05，已超过 24 小时。v4 watch 会把
`exit_cache_stale=true` 写进结果，并在 `blocked_by` 里写明「执行前必须刷新」。

**更正（2026-10-07 实盘）：这不是套利，是单位口径错误。**

实盘买入并充值后，Kraken 入账 `0.051843`，链上数量 `0.05184276`，比例是 **1:1**。
Kraken 的卖出单说明为 `0.0546 STRCx = 0.050009 STRCSPV`。
官方定义是 `scaled amount = raw amount × multiplier`。
EVM 的 `balanceOf()` 已经返回 scaled amount，所以不能再乘一次 multiplier；
Solana/TON 才需要把 raw amount 乘以 multiplier。老 v4 watch 对 EVM 重复乘了一次。

原来的 `91.9 美元/股` 是错的。按真实 Kraken 单位，链上 token 价格约 100.22，
Kraken STRCx 卖价约 99.34；买入后卖出是亏损，不是 7% 折价。

`scripts/xstocks_v4_watch.py` 已按实盘证据修正：STRCx 使用 1:1 Kraken 单位，
最小下单量按底层换算为 `ordermin × multiplier = 0.0546`。
修正后 STRCx 不再进入 monitor 候选。

但这只是监控结论。执行前仍要人工确认：

1. 真实 swap 路由和 hook（当前这两口池 hook 地址为零）；
2. Kraken 入账数量口径；
3. 首次授权、swap、充值三笔主网 gas；
4. 最小充值量和最小下单量；
5. 报价到上链之间的价格移动。

## 6. 跨链扩展（2026-10-07）

v4 watch 现在覆盖所有 xStocks 链上有官方 Uniswap v4 部署的网络：

| 链 | v4 PoolManager / Quoter 状态 |
|---|---|
| Ethereum | 已接入 |
| BNB Chain | 已接入 |
| Arbitrum | 已接入 |
| Optimism | 已接入 |
| Monad | 已接入 |
| HyperEVM | 已接入 |
| X Layer | 已接入 |
| Ink | 已接入 |

发现策略：

- DexScreener 覆盖的链：批量查 token pairs；
- X Layer / Ink：GeckoTerminal 缓存优先，每轮最多补 2 个地址；
- 池子发现结果缓存 30 分钟；常驻进程只对已发现池子重新报价。

首轮多链结果：

| 项 | 数值 |
|---|---:|
| 发现 v4 池 | 36 |
| 能报价 | 13 |
| green / amber / red | 21 / 3 / 12 |
| monitor 候选（修正后） | 0 |
| 有池子的链 | Ethereum / X Layer / Ink |

X Layer 和 Ink 的多数候选目前是「需要桥到 Solana 再充 Kraken」，且桥路线未实盘验证。

## 7. Kraken 出口与资金路线

`data/xstocks_route_matrix.json` 把每个 `(chain, symbol)` 标成：

| exit_route | 含义 |
|---|---|
| direct_deposit | 该链可直达 Kraken |
| bridge_to_solana | Kraken 不收这条链，登记支持桥到 Solana |
| no_exit | 没有已登记出口 |

当前 ETH 的 `STRCx / SPCXx / TSLAx / NVDAx` 可以直达 Kraken。
X Layer / Ink 的名字多数只能走桥。

资金路径单独看：

| 链 | Kraken 可提稳定币 | 地址簿 |
|---|---|---|
| Ethereum | USDC / USDT | **没有 Ethereum 条目** |
| X Layer | USDG - Xlayer | 已有 `xlayertee1` |
| Solana | USDT SPL | 已有 `soltee1` |
| Ink | USDC - Ink | 没有 Ink 条目 |

ETH 当前链上 gas 0.002 ETH，USDC/USDT 都是 0。
最小实盘 runbook 在 `docs/xstocks-eth-min-live-runbook-2026-10-07.md`。

### 7.1 Kraken 单位与最小量配置

`scripts/kraken_xstocks_units.py` 对 177 个 Kraken xStock 逐个执行私有的
`AddOrder validate=true`。这个调用需要签名，但不广播、不要求余额。

返回描述包含两侧数量：

```text
sell 0.054600 STRCx/USD @ limit 99.34
     (0.050009 STRCSPV/USD @ limit 108.46)
```

据此生成 `config/kraken_xstocks_units.toml`：

- `market_units_per_balance_unit`：Kraken 盘口数量单位；EVM `balanceOf()` 已经是 scaled 单位；
- `underlying_symbol` / `underlying_per_market_unit`：底层 SPV 换算；
- `effective_min_units`：真实最小下单量；
- `deposit_credit_units_per_balance_unit`：真实充值后才回填，未验证为空。

当前结果：177/177 validate 成功，0 失败。
STRCx 的真实最小量是 `0.05459063574070758198421675539`。

### 7.2 为什么以前的代币看起来没有错

错误不是「multiplier 的定义」错了，而是**把 EVM 的 scaled balance 又乘了一次**。

| 标的 | 之前路径 | multiplier | 为什么没暴露/是否暴露 |
|---|---|---:|---|
| MRVLx | Solana Jupiter raw | ≈1.0017 | 即使用 raw×multiplier，误差也只有 0.17% |
| VIDAx | Solana Jupiter raw | ≈1 | 乘 1 不产生误差 |
| MRNAx | Solana raw | 接近 1 | 误差小于噪声/阈值 |
| STRCx | Ethereum v4，EVM balanceOf | 1.0918 | `balanceOf` 已 scaled，再乘一次 → 约 9.2% 假折价 |
| TQQQx | Solana raw（看板） | ≈2.0117 | 旧公式对 Solana raw 方向是对的；若误套到 EVM 会产生巨大假信号 |

结论：

- Solana/TON：`scaled = raw × multiplier`，老公式方向正确；
- EVM：`balanceOf()` 已经是 `scaled`，不能再乘 multiplier；
- 之前 STRCx 是 EVM，代码却用了 Solana 的 raw→scaled 逻辑，所以只有它产生了明显假折价。

## 8. 复现命令

```bash
# v3 主报价用的 ETH 普查
.venv/bin/python scripts/xstocks_chain_census.py --chains eth --min-depth 20

# v4 只读 watch
.venv/bin/python scripts/xstocks_v4_watch.py --once

# 路线矩阵（Kraken 私有只读 + 链上余额）
.venv/bin/python scripts/xstocks_route_matrix.py

# 常驻
scripts/xstocks-v4-ctl.sh start 300
scripts/xstocks-v4-ctl.sh status
scripts/xstocks-v4-ctl.sh stop
```

输出：`data/xstocks_v4_watch.json`
