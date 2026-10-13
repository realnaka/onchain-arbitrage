# wXIAOx ↔ 1810.HK ↔ XIAOMI-USDT-SWAP 份额比例核验（2026-09-15）

## 问题

策略赚不赚钱，取决于一条很容易被当成事实的假设：

```text
1 wXIAOx = 1 股小米(1810.HK) = 1 张 OKX XIAOMI-USDT-SWAP
```

如果这条成立，那么链上代币与永续之间那 130–190 bps 的差就是**真实基差**；
如果不成立（例如 1 个代币其实是 0.5 股，或代币有分红再投资乘数），那这个差
就是**单位换算错误**，任何“套利”都是幻觉。在核验之前，
`mapping_verified=false` 让这条假设直接拒绝执行，方向是对的。

复现命令：

```bash
PYTHONPATH=src .venv/bin/python -m arb_monitor mapping-verify --asset XIAOMI
```

## 证据链（四层独立来源）

### 1. 发行方文档：1:1 抵押

> "Each xStock token is fully collateralized **1:1** by the corresponding
> underlying asset held with a regulated custodian."
> "**1:1 Collateralized.** Each xStock is backed by the underlying stock or ETF
> on a 1:1 basis."

来源：<https://docs.xstocks.fi/>（Introduction，页面标注 Last updated 2 months ago）

同一套文档的《Dividends and Stock Splits》页说明**公司行动如何处理**：
每个 xStock 发行时乘数（multiplier）为 `1.0`，"one token represents the value
of one share"；分红按再投资处理、拆股按比例调整，**持仓余额自动按乘数重算**，
因此“1 个（已重算的）代币 = 1 股的经济敞口”在分红/拆股后依然成立。
来源：<https://docs.xstocks.fi/docs/dividends-and-stock-splits>

### 2. 发行方资产登记表：这是哪一只股票

```text
symbol          XIAOx
isin            CH1580497480            ← 代币自身的 ISIN（瑞士发行结构）
underlyingSymbol 1810
underlyingIsin  KYG9830T1067            ← 小米集团（1810.HK）真实 ISIN
listingCountry  HK
trading.exchange XHKG / HKEX
deployments[XLayer].address             0xfb4f81f511b40b80996062032260a539e60adfc0
deployments[XLayer].wrapperAddressV2    0x076cf393e701839fc7a5832d2c68aafa235682ae
```

来源：<https://api.backed.fi/api/v2/public/assets/XIAOx>

关键点：**我们要买的那条池子里的代币地址，就是发行方自己登记的 wrapper**，
不是第三方同名代币。发行方 API 里**没有**任何比例字段——所以比例只能靠
链上储备和合约参数来证，不能靠 API 声称。

### 3. 链上储备：wrapper 与原生代币 1:1

```text
wrapper.asset()                    = 0xfb4f81f511b40b80996062032260a539e60adfc0  （原生 XIAOx）
wrapper.totalSupply()              = 49469801027291989008932
native.balanceOf(wrapper)          = 49469801027291989008932   ← 完全相等
native.multiplier()                = 1000000000000000000 = 1.0
```

wrapper 的**发行量与它实际持有的原生代币数量完全相等**，而不是“自称 1:1”。
结合发行方文档的 1:1 抵押与乘数机制，得到：

```text
1 wXIAOx = 1 XIAOx = 1 × multiplier = 1 股 1810.HK 的经济敞口
```

读取方式（只读 `eth_call`，X Layer 196）：
`totalSupply()` / `asset()` / `balanceOf(address)` / `multiplier()`。
选择器由本仓库的 `core/evm.py` 计算，并已用仓库里已在真实合约上验证过的
选择器（`balanceOf`、`token0`、`slot0` 等）做过交叉校验。

### 4. 交易所合约：1 张 = 1 股

OKX `XIAOMI-USDT-SWAP`：

```text
ctType linear   ctVal 1   ctValCcy XIAOMI   ctMult 1   settleCcy USDT
```

来源：`GET https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId=XIAOMI-USDT-SWAP`

而 OKX 自己公布的**指数成分**（`/api/v5/market/index-components?index=XIAOMI-USDT`）
说明这个 “XIAOMI” 单位是什么：

| 权重 | 来源 | 符号 | 原始价 | 折算价(USD) |
|---|---|---|---|---|
| 62.05% | Binance_Index | HK1810/USDT | 26.37853627 **HKD** | 3.36473797 |
| 35.12% | OKX_LINEAR_PERPETUAL | XIAOMI/USDT | 3.367 | 3.36701809 |
| 2.83% | OKX 现货 | XXIAOMI/USDT | 3.426 | 3.42103897 |

第一行就是**港股正股价格按美元换算**：26.37853627 / 3.36473797 = **7.8397**
（OKX 指数内用的 USD/HKD）。也就是说，指数把“1 股 1810.HK”与“1 个代币单位”
放在**同一个数值刻度**上，配合 `ctVal=1 / ctValCcy=XIAOMI / ctMult=1`，
1 张合约 = 1 股。

## 价格交叉核对

同一时刻（2026-09-15 23:2x CST）四个独立报价：

| 观察 | 价格 | 折成 USD |
|---|---|---|
| 港股正股（Binance HK1810/USDT，HKD 计价） | 26.3785 HKD | 3.3647（按 OKX 指数内 7.8397） |
| OKX 现货 XXIAOMI/USDT | 3.426 | 3.426 |
| 链上 wXIAOx 池价（Uniswap V3 中价，X Layer） | 3.4229 | 3.4229 |
| OKX XIAOMI-USDT-SWAP | 3.367 | 3.367 |

相对**正股**（3.3647）：

- 代币 +1.7%（链上）；+1.8%（OKX 现货）
- 永续 +0.07%

相对**代币**（3.4229）：永续 **−1.63%**。

## 结论与它改变了什么

1. **比例得到证实**：`shares_per_token = 1`、`shares_per_perp_unit = 1`、
   `quote_to_usdt = 1`，写入 `config/hk_monitor.toml`，并带
   `valid_until_ms`（30 天）与可复现的核验命令。
2. **那条 −163 bps 不是换算错误，是真实价差**：代币相对正股贵 1.7%，
   永续基本贴着正股。也就是说，**有溢价的方向是我们做不了的那一边**
   （卖现货 + 买永续），而能做的那一边（买代币 + 空永续）一开仓就付 163 bps。
   X Layer 上没有这些代币的借币渠道，所以看板继续拒绝，但拒绝理由从
   “比例未知”变成了“**比例已知，方向不利**”。
3. 这正是先前把 `mapping_verified` 当作硬门禁的价值：它挡住的不是机会，
   而是**把单位错误当成机会**的风险。

## 仍然不确定 / 会让这条结论失效的因素

1. **乘数变化**：分红（再投资）或拆股会改变 `multiplier()`，届时
   `shares_per_token` 必须重新读取并重新写入配置。核验脚本会在任何时候跑出
   `token_multiplier` 不通过。
2. **wrapper 版本更替**：发行方若部署新的 wrapper，地址与储备等式必须重证
   （`wrapper_1to1_reserve` / `wrapper_declared_asset`）。
3. **合约规格变化**：OKX 改 `ctVal`/`ctValCcy`/`ctMult` 会让
   `perp_contract_value` 不通过。
4. **稳定币按 1 USD 处理**：`quote_to_usdt = 1` 是筛选口径（USDG/USDC 视为 1 美元），
   不是核验过的汇率；脱锚要另外计。
5. **汇率口径差异**：OKX 指数用 7.8397，本仓库 FX 快照是 7.80。
   本方向（代币 vs 永续）两腿都是美元计价，**不需要 FX**；只有拿正股（HKD）
   做基准比较时才会受这个 0.5% 影响，所以本文里凡是“相对正股”的数字都注明了汇率口径。
6. **指数自引用**：OKX 指数有 35% 权重来自它自己的永续、2.8% 来自它自己的现货代币，
   指数与永续之间不是完全独立的两个数；这也是为什么核验以**正股 HKD 价**为准。

## 复现清单

```bash
# 一次跑完四层核验（发行方 / 链上储备 / 乘数 / 合约规格）
PYTHONPATH=src .venv/bin/python -m arb_monitor mapping-verify --asset XIAOMI

# 单看指数成分（正股、永续、现货代币三者的权重与折算价）
curl -sS "https://www.okx.com/api/v5/market/index-components?index=XIAOMI-USDT"
```
