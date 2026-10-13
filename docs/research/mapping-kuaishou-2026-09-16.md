# wKUAIx ↔ 1024.HK ↔ KUAISHOU 永续 份额核验（2026-09-16）

## 为什么做

页面上出现"候选 1 个：KUAISHOU 空 gate:KUAISHOU_USDT 净 88.0 bps"，operator 问
"是不是出了交易信号了"。这条比 XIAOMI 那次更值得追：XIAOMI 的 −163 bps 最后证明
方向不利（代币贵、永续便宜），而 KUAISHOU 是**代币便宜、永续贵**，也就是我们唯一
能做的那个方向。所以必须先回答一个问题：这 90 bps 是真的价差，还是单位换算错误？

原始质疑（review 的规则）：代币池里买到的 1 个 `wKUAIx` 到底是不是 1 股 1024.HK，
而 1 张合约是不是也是 1 股。任何一个环节错，90 bps 就是幻觉。

## 四层证据（与 XIAOMI 同一套方法）

### 1. 发行方资产表：这是哪一只股票

```text
GET https://api.backed.fi/api/v2/public/assets/KUAIx
symbol            KUAIx                 （原生代币；wrapper 是 wKUAIx）
isin              CH1588657986          ← 代币自身 ISIN（瑞士发行结构）
underlyingSymbol  1024
underlyingIsin    KYG532631028         ← 快手科技（1024.HK）真实 ISIN
listingCountry    HK
currency          HKD
native_address    0xfa8a6a57fc83e416cf45b04b7b92b4f48da9e7b8
wrapper_address   0x096369b9b197bcbf669a3bc827aa119d80d891e4   ← 我们池子里买的那个地址
```

发行方 API **没有任何比例字段**，所以比例只能靠链上储备和合约参数证明。
`wrapper_address` 与我们池子（`0x9d5d9541…a0f2`）的 base 地址一致，且该地址链上
`symbol()` 读回 `wKUAIx`，与配置一致。

### 2. 链上储备：wrapper 与原生代币 1:1

```text
wrapper.asset()                    = 0xfa8a6a57fc83e416cf45b04b7b92b4f48da9e7b8
wrapper.symbol()                   = wKUAIx
wrapper.totalSupply()              = 12382795636305526856676  (12,382.795636306 枚)
native.balanceOf(wrapper)          = 12382795636305526856676  ← 完全相等
native.totalSupply()               = 603689823437895195278000  (603,689.823438 枚)
native.multiplier()                = 1000000000000000000 = 1.0
```

读取方式：X Layer（chain 196）只读 `eth_call`，选择器由 `core/evm.py` 计算：
`asset()` / `totalSupply()` / `balanceOf(address)` / `multiplier()` / `symbol()`。

**一次自摆乌龙值得记录**：第一遍我用自己拼的 ABI 编码读 `native.balanceOf(wrapper)`，
把地址的 `0x` 前缀去掉后补零，得到 0，看起来像"wrapper 不持有原生代币、1:1 不成立"。
改用仓库自己的 `encode_call`（它会自己处理 `0x` 前缀）后，读数完全相等。
**教训：链上读数是证据，但读数本身也会错；用项目里已验证过的编码路径，别手搓。**

### 3. 交易所合约：1 张 = 几股（三家不同！）

| 场所 | 合约 | 交易所自己的字段 | 1 张 = 几股 |
|---|---|---|---|
| gate | `KUAISHOU_USDT` | `type=direct`, `quanto_multiplier=1`, `order_size_min=1` | **1 股** |
| bybit | `KUAISHOUUSDT` | `symbolType=stock`, `marketRegion=HK`, `underlyingTicker=01024`, `fullName=Kuaishou Technology`，线性永续数量单位即标的币，`minOrderQty=0.1`、`tickSize=0.01` | **1 股** |
| bitget | `KUAISHOUUSDT` | `sizeMultiplier=0.01`, `minTradeNum=0.01`, `pricePlace=3`, `symbolStatus=normal`, `isRwa=YES` | **0.01 股** |

**Bitget 的坑**：ccxt 统一接口对同一个合约报 `contractSize = 1`，而交易所自己的
`sizeMultiplier = 0.01`（BTCUSDT 是 0.0001，可交叉对照，说明该字段就是"每张多少标的币"）。
本仓库以**交易所字段**为准，并在适配器里把这个差异写成注释与测试
（`tests/test_cex_venue_expansion.py::BitgetParsingTests`）。如果信了统一接口，
对冲张数会差 100 倍。

### 4. 价格交叉核对（同刻）

```text
链上现货 Ask（USDG 池，100 USDC 档，含 0.05% 池费）  ≈ 3.7672
gate   KUAISHOU_USDT perp bid                          3.8010   (+89.7 bps)
bybit  KUAISHOUUSDT  perp bid                          3.8000   (+87.1 bps)
bitget KUAISHOUUSDT  perp bid                          3.8110  (+116.3 bps)
binance KUAISHOUUSDT perp bid                          3.8040   (+97.7 bps)
```

四家场外所的永续都比链上代币贵 87–116 bps，量级互相印证；OKX **没有** KUAISHOU
永续（`/public/instruments?instType=SWAP` 无 KUAI 条目）。

## 结论

1. **四层全部通过**：`1 wKUAIx = 1 股 1024.HK`，`multiplier() = 1.0`，
   gate/bybit 1 张 = 1 股，bitget 1 张 = 0.01 股。
2. **那 87–116 bps 是真实价差，不是单位换算错误**，方向也对我们有利
   （买链上现货 + 空永续）。
3. **但依然不可执行**：三个场所都没有执行适配器（OKX 根本没有这个合约），
   所以它只会以"候选（不可执行）"的形式出现在看板与报警里。
4. 配置落地：`config/hk_monitor.toml` 的 KUAISHOU 块 `mapping_verified = true`
   + 三个 `[tokens.mappings."…"]`（各自带 evidence 与 `valid_until_ms`）。

## 要真正交易它，还缺什么

| 选项 | 需要做的事 | 风险/成本 |
|---|---|---|
| A. 实现 Gate 执行适配器 | 下单/撤单/回读/clOrdId 幂等、reduce-only、精度与最小张数、错误语义 | 新交易所 = 新的未知状态面；必须先有 paper + 小额演练 |
| B. 实现 Bybit 或 Bitget 适配器 | 同上；Bitget 还要处理 0.01 股/张的换算与 `minTradeUSDT=5` | 同上，且 Bitget 的深度数量单位尚未核实（本仓库暂不把它用于 sizing） |
| C. 找 OKX/Binance 上的 KUAISHOU 永续 | OKX 无；Binance 有但映射未核验（HKD 计价体系）且无适配器 | 与现在的执行白名单一致，但仍需适配器 |
| D. 只监控不交易 | 已经做了 | 看得到吃不到，但零新增风险 |

## 复现命令

```bash
# 链上三层（只读）
PYTHONPATH=src .venv/bin/python -m arb_monitor mapping-verify --asset KUAISHOU   # 需先在配置里写好 mapping 表
# 跨场所机会表（只读）
PYTHONPATH=src .venv/bin/python scripts/threshold_report.py
# 合约规格（交易所自己的字段）
PYTHONPATH=src .venv/bin/python - <<'PY'
from arb_monitor.data.hk_cex import CexPublicAdapter
for v in ("gate","bybit","bitget"):
    for c in CexPublicAdapter(v, proxy="http://127.0.0.1:4780").instruments()[0]:
        if c.asset == "KUAISHOU":
            print(v, c.instrument_id, "乘数", c.multiplier, "最小", c.min_contracts, "tick", c.tick_size)
PY
```
