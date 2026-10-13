# 港股币对×合约映射核验：gate / bybit / bitget（＋binance）2026-09-17

> 触发（operator，2026-09-17）：
> "补齐 gate/bybit/bitget 另外 5 个标的的映射核验——现在它们只有 KUAISHOU 一个
> 标的可评估，这个实在太少了。我需要的是全部可以套利的港股币对和合约。"
>
> 结论：**港股可套利全集就是 6 个标的**（发行人 79 个港股 xStock 里只有 8 个有池子、
> 其中 6 个有永续可对冲）。这 6 个标的 × 5 家场所共 **24 条映射全部通过机器核验**，
> 可评估交易对从 8 个扩到 24 个。

## 0. 一句话

不是"我们少核验了几个"，而是**港股可套利的就是这 6 个**；这次把它们的
gate/bybit/bitget（以及 binance 上真实存在的 3 个）全部补齐，并且把核验从
"人工写证据"变成 `arb_monitor mapping-verify` 可复跑的机器检查。

## 1. 全集是怎么定的（不是猜的）

两腿都必须真实存在才算"可套利"：

```text
现货腿：api.backed.fi 的港股发行清单（listingCountry=HK，79 个）
        → 链上 factory.getPool（base × 4 稳定币 × 4 fee 档）
        → 读池子真实余额算深度（≥1000 USD 才算有价）
永续腿：交易所自己的合约目录（gate/okx/binance/bybit/bitget）
```

结果（`scripts/list_hk_universe.py`，2026-09-17）：

| 层 | 数量 | 说明 |
|---|---|---|
| 发行方港股 xStock | 79 | 绝大多数没有市场 |
| 链上有池子 | 13 行（8 个标的） | 含同一标的的多个稳定币/费率组合 |
| 池子够深（≥1000 USD） | 8 | 6 个已监控 + HKEXCx(388) + MIXUx(2097) |
| **两腿齐全（可套利）** | **6** | XIAOMI / TENCENT / POPMART / MEITUAN / KUAISHOU / SHEIN |

**孤儿 2 个**（有池子、没有任何我们所监控的永续，因此不可套利）：
`HKEXCx`（港交所 388，池深 73k USD）、`MIXUx`（蜜雪冰城 2097，池深 19k USD）。
这两条已由监控的自动发现写进 `config/discovered_tokens.toml`，
一旦某家上永续就会自动进入评估。

## 2. 四层核验（与 XIAOMI/KUAISHOU 同一套方法）

| 层 | 内容 | 本次结果 |
|---|---|---|
| 1 发行方 | 代币 ISIN → 正股 ISIN + listingCountry | 6/6 通过（TCENTx CH1580497357→700 KYG875721634；MEITx CH1580497530→3690 KYG596691041；其余同日复跑） |
| 2 链上储备 | `wrapper.totalSupply() == native.balanceOf(wrapper)` | 6/6 通过 |
| 3 链上乘数 | `native.multiplier()` | 6/6 = **1.0** → 1 代币 = 1 股 |
| 4 交易所合约 | **每张几股** | 逐场所不同，见下表 |

### 2.1 第 4 层是这次真正的发现：同一个标的在不同场所每张股数不同

| 代币（HK 代码） | gate | bybit | bitget | binance |
|---|---|---|---|---|
| XIAOx (1810) | `quanto_multiplier=1` → **1 股** | 1 股 | `sizeMultiplier=0.01` → **0.01 股** | 无 USD 合约 |
| TCENTx (700) | `=0.1` → **0.1 股** | 1 股 | 0.01 股 | 1 股（TENCENTUSDT） |
| POPMTx (9992) | `=0.1` → **0.1 股** | 1 股 | 0.01 股 | 1 股 |
| MEITx (3690) | `=0.1` → **0.1 股** | 1 股 | 0.01 股 | 1 股 |
| SHEINx (625) | `=1` → 1 股 | 1 股 | 0.01 股 | 无 USD 合约 |
| KUAIx (1024) | `=1` → 1 股 | 1 股 | 0.01 股 | payout 未知（见 §5） |

**gate 的合约大小是按标的不同的**：XIAOMI/SHEIN/KUAISHOU 是 1 股/张，
TENCENT/POPMART/MEITUAN 是 0.1 股/张。若按"1 张 = 1 股"写死，这三只在 gate 上的
对冲会**差 10 倍** —— 正是本仓库反复强调的单位类错误。

### 2.2 每层的独立交叉核对（不是只读一个字段）

```text
gate    用交易所自己的 24h 成交量：volume_24h_quote/(volume_24h×last)
        XIAOMI 1.0095 / SHEIN 0.9958 / KUAISHOU 1.0068   → 乘数 1
        TENCENT 0.1011 / POPMART 0.1005 / MEITUAN 0.1011 → 乘数 0.1
bybit   turnover24h/(volume24h×last) ≈ 1.00（六只全部）→ 数量单位就是标的币
bitget  sizeMultiplier=0.01，BTCUSDT=0.0001 作为量纲对照；
        深度数量单位按"张"计，由跨场所基差一致性佐证（若按币计会放大 100 倍）
binance 名称型与代码型并存：TENCENTUSDT=54.61(USD) vs HK0700USDT=428.16，
        比值 7.8403 = USD/HKD → 代码型是 HKD 计价，不能与 USD 代币相减
        quoteVolume/(volume×last) ≈ 1.00 → 1 张 = 1 股
```

## 3. binance 的额外发现（顺带收紧一个过宽的排除规则）

仓库此前对 binance 港股合约一律不提升为 `usd_linear`，理由是"Binance 的 tradifi
符号混了 HKD 编号体系"。这句话方向对、**范围过大**：

| 符号 | 价格 | 计价 | 结论 |
|---|---|---|---|
| `TENCENTUSDT` | 54.61 | USD | ✅ 与 X Layer 代币可比 |
| `HK0700USDT` | 428.16 | HKD（比值 7.8403） | ❌ 不同标尺，继续排除 |

所以规则收紧为：binance 上**只有名称型（非 `HK<代码>`）且 `underlyingType=HK_EQUITY`、
`quoteAsset=USDT` 的合约**才可映射。这样 binance 的 TENCENT/POPMART/MEITUAN 进入
可评估集合（它没有 XIAOMI/SHEIN 的 USD 合约）。

## 4. 核验从"人工证据"变成"机器检查"

此前 `mapping-verify` 只能核 OKX（有 `ctVal/ctValCcy/ctMult`）；gate/bybit/bitget
的映射只有手写 evidence、没有可复跑检查 —— 也就是"半成品当成完成"的形状。

新增 `core/mapping_evidence.verify_venue_contract()`：

| 检查 | 内容 |
|---|---|
| `perp_contract_size` | 交易所自己的字段（`quanto_multiplier`/`sizeMultiplier`/bybit 的"无尺寸字段"）是否等于配置的 `perp_contract_multiplier` —— **场所改合约大小会直接报错** |
| `perp_venue_identity` | 交易所自己说它是哪只股票：bybit `symbolType=stock`+`marketRegion=HK`+`underlyingTicker`（补零代码或 `SHEIN` 这种字符串都接受）；gate `type=direct`+合约名；bitget `baseCoin`+`isRwa=YES`；binance `underlyingType=HK_EQUITY`+名称型 base |
| `perp_contract_active` | 合约仍在交易 |

核验结果：**24/24 全部 PASS**（`PYTHONPATH=src .venv/bin/python -m arb_monitor
mapping-verify`，每条 5～7 项检查）。

沿用并修正的两处：

- **KUAISHOU 的 `token_symbol` 写成 `wKUAIx`（wrapper 符号）**，而发行方登记的符号是
  `KUAIx`；其余五只写的都是发行方符号，所以只有这三条核验失败。已改为 `KUAIx`。
- bybit 的身份字段不统一：多数是补零港股代码（`00700`、`09992`），
  **SHEIN 却是字符串 `SHEIN`**，所以匹配逻辑要同时接受代码与代号（并且只有这两种）。

## 5. 结果与仍未覆盖的部分

生效后监控的**可评估交易对从 8 个变成 24 个**（每轮）：

```text
gate    6/6（XIAOMI TENCENT POPMART MEITUAN KUAISHOU SHEIN）
bybit   6/6
bitget  6/6
okx     3/3（XIAOMI POPMART SHEIN —— OKX 没有另外三只）
binance 3（TENCENT POPMART MEITUAN —— 没有 XIAOMI/SHEIN 的 USD 合约）
```

仍未覆盖（都不是"漏核验"，而是客观不存在或需要单独决策）：

| 项 | 原因 |
|---|---|
| binance:KUAISHOUUSDT | 分类为 payout 未知（KUAISHOU 不在 `CORE`，binance 又不在 `USD_LINEAR_VENUES`）。要纳入需单独确认其结算语义 |
| HK 代码型合约（`HK0700USDT`/`HK1810USDT`/`HK0625USDT`、gate/binance 的 `*HKD_USDT`） | 报价是 HKD，与 USD 计价的代币相减会凭空造出 7.84 倍的"价差"。需要一条独立的 HKD↔USD 换算与对冲模型，不在本次范围 |
| HKEXCx / MIXUx | 有池子但没有永续，无法对冲 |
| 集群内的 US 标的（AAPL 等） | 阶段边界是港股，US 另作研究 |

## 6. 复现命令

```bash
# 港股可套利全集（发行方 + 链上池子 + 5 家合约目录，只读）
PYTHONPATH=src .venv/bin/python scripts/list_hk_universe.py --proxy http://127.0.0.1:4780
# 24 条映射逐条机器核验
ARB_HTTP_PROXY=http://127.0.0.1:4780 PYTHONPATH=src .venv/bin/python -m arb_monitor mapping-verify
# 单只
... -m arb_monitor mapping-verify --asset POPMART
```

测试：`tests/test_mapping_evidence.py::VenueContractTests`（10 项）钉住每一条
"不能想当然"的规则，包括"gate 把 0.1 改成 1 必须报错"和"binance 的 HK 代码型必须被拒"。
