# ETH v4 最小实盘测试 runbook（2026-10-07）

目标：用最小金额走通一次

`Kraken USDC → Ethereum → Uniswap v4 买入 STRCx → 充 Kraken → 卖出`

只用于验证路线。不是正式自动执行策略。

## 1. 当前现场

| 项目 | 实测 |
|---|---|
| ETH 钱包 | `0xabababababababababababababababababababab` |
| ETH gas | **0.002 ETH（已经够当前 gas；不是 0）** |
| ETH USDC / USDT | **0 / 0** |
| Kraken USD | 53.2026 |
| Kraken USDT | 0.30 |
| Kraken 提现方法 | `USDC`，network Ethereum |
| 提现最小量 | 1.38144 USDC |
| 提现费 | 1.1512 USDC |
| 地址簿 Ethereum 条目 | 有 `ethusdc1`，但资产是 **USDT / Tether USD (ERC20)**，不是 USDC，也不是 ETH |
| STRCx 充值 | `STRCx - Ethereum`，最小 0.01492617 股 |
| STRCx 提现 | `STRCx - Ethereum`，最小 0.01492617 股，费 0.00995078 STRCx |

## 2. ETH 5U 提现可行性

Kraken 侧数字：

| 项目 | 数值 |
|---|---:|
| ETHUSD 最小下单 | 0.001 ETH |
| ETHUSD 最小成交额 | 0.50 USD |
| ETH 提现最小量 | 0.00018 ETH |
| ETH 提现费 | 0.00015 ETH |

所以买 5U ETH、再提现，在 Kraken 的最小量规则下是可行的。

但地址簿里**没有 ETH 提币 key**。`ethusdc1` 是 USDT ERC20 地址，不能用于 ETH 提现。
如果要把 5U ETH 真的提到钱包，operator 还需要在 Kraken 增加一个 ETH 网络地址条目，
例如 key `etheth1`，地址仍是 `0xabababababababababababababababababababab`。

另外，ETH 钱包已经有 0.002 ETH gas。按当前约 0.15 gwei 计算，gas 不是当前阻塞。

## 3. 买 STRCx 需要的是 USDC，不是 ETH

当前 `STRCx / USDC` 池不能用 USDT 直接买。现在地址簿只有 USDT ERC20，所以有两条路：

| 方案 | 要求 |
|---|---|
| 直接路 | Kraken 增加 `USDC - Ethereum` 白名单 key，提 USDC 到 ETH |
| 间接路 | 用现有 `ethusdc1` 提 USDT，到 ETH 后先 USDT → USDC swap，再买 STRCx |

直接路更短，适合第一次实盘测试。

## 4. 实盘前必须由 operator 手动完成

Kraken API 没有增加提币地址的方法。必须在 Kraken 界面添加：

```text
资产：USDC
网络：Ethereum
地址：0xabababababababababababababababababababab
```

建议 key/名称：`ethusdc1-real` 或 `ethusdcusdc1`。

没有这一步，ETH 链路无法由 Kraken 补资金。

## 4.1 不需要为每个 xStock 买一遍

`scripts/kraken_xstocks_units.py` 对全部 Kraken xStock 做只读
`AddOrder validate=true`。validate 需要私钥签名，但不下单、不要求持仓。
返回的描述同时给出市场单位和底层单位，例如：

```text
sell 0.054600 STRCx/USD @ limit 99.34
     (0.050009 STRCSPV/USD @ limit 108.46)
```

因此可以批量得到：

- 真实最小下单量；
- 市场单位与底层 SPV 的换算；
- pair 状态。

当前生成结果：`config/kraken_xstocks_units.toml`，177/177 成功。

`deposit_credit_units_per_balance_unit` 是另一件事：只有真实充值后才能确认。
目前只有 STRCx 有样本，值为 1。其他名字先标未验证，执行时 fail closed。

## 5. 建议的最小测试金额

测试目的不是赚钱，所以用 **5 USDC 买入档**。

从 Kraken 提 USDC 到 Ethereum：

```text
提现 6.50 USDC
预计到账 ≈ 6.50 - 1.1512 = 5.35 USDC
```

这样留下约 0.35 USDC 给价格移动，实际可买 5 USDC。

当前 5 USDC 档是负净利（约 -0.18 USDT，主要来自 gas），这是测试成本。

## 6. 实盘步骤（明天做）

1. 在 Kraken 界面添加 `ethusdc1` 白名单。
2. 从 Kraken 提 6.50 USDC 到 Ethereum。
3. 核对 ETH 钱包收到约 5.35 USDC。
4. 在 Ethereum 上执行 v4 买入：`STRCx / USDC` 池。
5. 核对链上收到 STRCx。
6. 转入 `STRCx - Ethereum` Kraken 充值地址。
7. 等 Kraken 入账。
8. 在 Kraken 卖出 STRCx。
9. 记录实际到账、gas、Kraken 费、充值等待时间。

## 7. 现在还不能执行的原因

| 阻塞 | 状态 |
|---|---|
| Kraken Ethereum USDC 白名单 | 未添加 |
| Kraken Ethereum ETH 提币 key | 未添加；若要额外提 ETH 才需要 |
| ETH USDC 资金 | 0 |
| Ethereum v4 swap 执行适配器 | 未实现 |
| 链路实盘样本 | 0 次 |

监控、路线矩阵和成本模型已经就绪；执行适配器留到明天的正式自动化工作。

## 7.1 手动执行脚本

现已提供：

```bash
# 只报价
.venv/bin/python scripts/xstocks_eth_live.py quote --symbol STRCx --size-usd 5

# 买入（需要显式确认）
.venv/bin/python scripts/xstocks_eth_live.py buy --symbol STRCx --size-usd 5 --confirm-live

# 充值 Kraken
.venv/bin/python scripts/xstocks_eth_live.py deposit --symbol STRCx --confirm-live

# 卖出 Kraken 余额
.venv/bin/python scripts/xstocks_eth_live.py sell --symbol STRCx --confirm-live

# 查看 ETH JSONL 账本
.venv/bin/python scripts/xstocks_eth_ledger.py show
```

`--force` 只有在前一次 CLI 返回 confirming、operator 再次明确确认后才允许使用。

## 7.2 自动链

常驻入口：

```bash
scripts/xstocks-eth-auto-ctl.sh start       # 只按 policy；默认 off
scripts/xstocks-eth-auto-ctl.sh once
scripts/xstocks-eth-auto-ctl.sh status
scripts/xstocks-eth-auto-ctl.sh stop
```

live 需要先由 operator 修改 `ops/xstocks_eth_policy.toml`：

```toml
mode = "live"
auto_orders = "live"
live_ack = "..."
```

然后：

```bash
scripts/xstocks-eth-auto-ctl.sh start-live
```

## 8. 实盘结果（2026-10-07 09:1x–09:4x）

最小实盘已经完整走完：

| 步骤 | 实测 |
|---|---|
| 链上买入 | 5.2 USDC → 0.05184276126657151 STRCx |
| Kraken 入账 | 0.051843 STRCx（与链上 token 1:1） |
| Kraken 卖出 | 由于最小卖出量实际是 0.0546，先补买两次各 0.055 STRCx，最终全部卖出 |
| 最终仓位 | 0 |
| 链上 gas | 0.000327429 ETH ≈ 0.88 USDT |
| USDC 提币费 | 0.8602 USDC |
| 结果 | **约 -1.88 USDT** |

**关键结论：没有套利。**

原来的“7% 折价”来自单位错误：

- 链上 `STRCx` token 数和 Kraken `STRCx.T` 余额是 **1:1**；
- 官方定义是 `scaled amount = raw amount × multiplier`；
- EVM `balanceOf()` 已经是 scaled amount，不能再乘一次 multiplier；
- Kraken 的 `0.0546 STRCx = 0.050009 STRCSPV` 中，STRCx 是 scaled 单位，STRCSPV 是 raw 单位；
- 之前对 EVM `balanceOf()` 再乘 1.0918，导致链上价格被低估。

修正后 v4 watch 的 STRCx 结果是负净利，monitor 候选从 2 变成 0。
