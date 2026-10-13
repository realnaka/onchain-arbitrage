# xStocks 全链路推进计划 v2（2026-10-05）

前提：Kraken 的 xStocks 权限已开通。本文记录旧计划、今天核对到的现场、两条可用链路，
以及「看板 → 链上买入 → 充值 Kraken → 卖出」的自动化设计。

**本轮没有下单、没有改配置、没有动停机开关。** 唯一新增的代码是只读的 Kraken 客户端。

---

## 0. 结论先行

| 项 | 状态 |
|---|---|
| Kraken 交易权限 | **已开通**。14 个名字的 `AddOrder validate=true` 全部通过，不再 `ETrade:User Locked` |
| Kraken API key | **一直是好的**。坏的是我们的调用姿势：nonce 用了毫秒、交易对写法不对 |
| Kraken 里的资产 | **0.54 MRNAx 已于 2026-10-05 11:59:41 全部卖出**（成交 190.56，得 `ZUSD 102.9320`）；无挂单 |
| 链路 | 两条：**A = X Layer 出发**、**B = Solana 直接买**。出口都是 Kraken 卖出 |
| OKX 永续腿 | **本轮不参与**。operator 明确：规模做大以后再考虑，当前只做现货差价 |

---

## 1. 两条链路

### 链路 B —— Solana 直接买（本轮主推，步骤最短）

```text
Solana 钱包 USDC/USDT
  → Jupiter 买入原生 xStock（滑点 10 bps）
  → 转到 Kraken 的 Solana 充值地址（数量必须先过最小充值量）
  → Kraken 卖出（post-only 优先，价格 ≥ 基准 99.5%）
  → USD 换成 USDC/USDT，提回 Solana 钱包
```

### 链路 A —— X Layer 出发

```text
X Layer 钱包 USDG
  → 买入 wrapper（如 wSPYx）
  → 同链 redeem 成原生（wrapper 不能充 Kraken）
  → CCIP 桥到 Solana（约 1 小时）
  → 转到 Kraken 的 Solana 充值地址
  → Kraken 卖出 → 换回 USDT → 提回 X Layer 的 USD₮0 → 换成 USDG
```

两条链路的**卖出腿完全相同**；差别只在「币怎么到 Kraken」。买到哪个链，钱就回哪个链的钱包。

### 停用的部分

OKX 永续空头**这一步本轮不做**。旧计划里「卖掉现货 + 平 OKX 空头」的时序约束随之作废：
现在这笔是纯现货差价，卖完就结束。

> 待确认：operator 写的「直接在链上买入后去 **Falcon** 卖出」——按上下文我理解为 Kraken。
> 如果不是，请给一个链接或全名。

---

## 2. 旧计划（保留作对照）

旧文件：`/home/operator/.cursor/plans/mrnax_kraken_exit_93bdf7f0.plan.md`（2026-09-26，
含 mermaid 全链路图）。核心链路是 X Layer 原生 MRNAx → 桥 → Kraken 卖出 → 平 OKX 空头。
六个 todo 里真正没做的是「卖出并平空」和「写代码」。

两处已过时，不要照抄：

| 旧计划写的 | 实际 |
|---|---|
| 桥到以太坊，走 Kraken 以太坊充值 | 这个账户的 MRNAx 只有 `MRNAx - Solana`；实际桥到 Solana |
| 等 `status` 回到 online 才有买盘 | 09-28 已是 online 且买一过闸，真正的拦截是 `ETrade:User Locked` |

---

## 3. 今天核对的现场（2026-10-05 11:30–12:00，只读）

命令：`PYTHONPATH=src .venv/bin/python scripts/kraken_probe.py status`

| 项 | 实测 |
|---|---|
| 余额 | 卖出前 `MRNAx.T = 0.540000` / `ZUSD = 0.0090`；卖出后 **`ZUSD = 102.9320`**，xStocks 持仓清零 |
| 挂单 | 0 笔 |
| 费率（MRNAxUSD） | taker **0.10%**，maker **-0.02%**（挂单返佣） |
| xStocks 交易对 | **177 个，其中 172 个 online**（09-26 是 177 个 / 12 online）。
  注：公开接口返回 354 **行**，每个标的两条记录，去重后是 177 |
| 下单权限 | `validate sell` 对 MRNAx/SPYx/QQQx/NVDAx/TSLAx/AAPLx/CRCLx/GOOGLx/INTCx/SPCXx/MSTRx 全部通过 |
| OKX 持仓 | 只剩 `CRWV-USDT-SWAP −0.04`，被链上 0.0399 wCRWVx 对冲 |
| 钱包（X Layer） | USDG 14.22、wCRWVx 0.0399、OKB 0.0057 |

### 3.1 充值方式（决定「这条链能不能充」）

| 标的 | 可用充值方法（括号是最小量） |
|---|---|
| MRNAx | **Solana**（0.0164）——只有这一条 |
| SPYx / QQQx / NVDAx / TSLAx / AAPLx / CRCLx / GOOGLx / INTCx / MSTRx | Ethereum + TON，外加一条不写链名的方法（地址与 MRNAx 的 Solana 地址相同） |
| SKHYx / SPCXx | Ethereum + **Solana** + TON |

**MRNAx 只能从 Solana 充。** 其他名字要么走以太坊/TON，要么走那条不写链名的
（下次充值前必须用 `DepositAddresses` 现查地址，不能沿用上一次的）。

### 3.2 出口深度（50 bps 内买盘，公开 Depth）

| 厚 | | 薄 | |
|---|---|---|---|
| CRCLx | $133,092（2 bps） | NBISx | $2,666（237 bps） |
| NVDAx | $119,870（0 bps） | XOPx | $1,413（184 bps） |
| QQQx | $114,510（1 bps） | SLVx | $939（110 bps） |
| AAPLx | $100,755（1 bps） | Vx | $916（67 bps） |
| GOOGLx | $93,408（1 bps） | AMBRx | $53（511 bps） |

看板上「折价最大」的长尾名字，正好是出口最薄的那批。**出口深度必须进筛选条件。**

---

## 4. 两个坑（已写进代码，别再犯）

| 坑 | 现象 | 正解 |
|---|---|---|
| nonce 用毫秒/微秒 | `EAPI:Invalid nonce`。更坑的是：签名写错时 Kraken 报的是 `EAPI:Invalid key`，看起来像密钥被删 | **纳秒 nonce**，且严格递增 |
| 交易对写法 | `pair=MRNAXUSD` → `EGeneral:Internal error` | `pair=MRNAx/USD` **且** `asset_class=tokenized_asset` |

代码位置：`src/arb_monitor/venues/kraken_spot.py`（只读优先，真下单必须显式
`confirm="live"`）。16 项 hermetic 单测：`tests/test_kraken_spot.py`。

---

## 5. 自动化设计：看板 → 链上买入 → Kraken 卖出

### 5.1 触发条件（operator 定的骨架）

| 条件 | 取值 | 说明 |
|---|---|---|
| 机会来源 | 8770 看板 | `http://127.0.0.1:8770/` |
| 链路 | Solana、X Layer | 其他链先不做 |
| 净利 | **> 0.1 USD** | 必须按**我们的实际下单金额**重算，不能沿用板上 $500 档的数字 |
| 风险 | **低** | 看板的 `risk` 列（<2%） |
| 单笔上限 | 10 USDT | 现有 `[limits] per_trade_quote_usdt`，再受钱包余额 `min(上限, 余额×0.999)` 约束 |
| 出口 | 50 bps 内买盘 ≥ 下单名义 | 新增闸门，见 3.2 |

### 5.2 动作序列

```text
1. 取报价（滑点 10 bps + Jito 小费），确认这条链上真的买得到
2. 链上买入，记录 tx、实际到手数量
3. 查 DepositMethods/DepositAddresses（现查），数量 ≥ 最小充值量才转
4. 等到账（Kraken 入账确认）
5. Kraken 卖出：post-only 限价优先，价格 ≥ 基准 99.5%，超时再考虑吃单
6. 现金换回稳定币，提回买入时用的那条链的钱包
```

### 5.3 三个前置（2026-10-05 12:xx 已完成）

| 前置 | 处理 |
|---|---|
| Kraken 状态列是 10-03 的旧快照 | 改成每轮一次公开 `AssetPairs`（进程内缓存 300 s）：**177 个标的 / 172 online** |
| 净利按 $500 档算 | 幸存行按 **10 USDT** 重新询价，新增 `net_10` / `risk_10` / `size_verified` |
| 没有出口深度列 | 新增出口闸门 + 「出口 / 充值链路 / 可执行」三列 |

出口闸门（`src/arb_monitor/venues/kraken_exit_gate.py`）三条，全部 fail-closed：

1. Kraken 有没有这个交易对、状态能不能成交；
2. Kraken 收不收这条链 —— 数据来自 `scripts/kraken_deposit_refresh.py` 写的
   `data/kraken_exit_cache.json`（**177 个标的，176 个已核对**；未核对的不上榜）；
3. 我们自己的数量会不会把 50 bps 内的买盘吃掉超过 20 %。

**首轮实测**：13 行 → 11 行（CRMx「充值方式未核对」、SLVx「占 50 bps 内盘口 54%」被拦），
其中 **3 行可执行**：VIDAx net $0.186、INTCx $0.112、Vx $0.190（均 net > 0.1 且风险低）。
XOPx 净利 $3.85 但风险「高」→ 不可执行。一轮从 ~185 s 涨到 ~271 s。

### 5.4 分阶段

| 阶段 | 内容 | 门槛 |
|---|---|---|
| S1 只读 | Kraken 客户端 + 出口闸门 + 看板补列 | **已完成**（2026-10-05） |
| S2 paper | 用真实报价完整走一遍，不广播 | 全流程走通、账本落组 |
| S3 live 小额 | 单笔 ≤ 10 USDT，先 Solana | 每一步都可回滚；失败即停 |

### 5.5 全部硬编码（operator 2026-10-05：不要每次靠 LLM 驱动）

目标：一次 `arb-monitor xstocks-cycle` 就把「选 → 买 → 充 → 卖 → 记录」跑完，
LLM 只负责解释结果和处置异常。要写的模块与顺序：

| # | 模块 | 职责 | 现状 |
|---|---|---|---|
| 1 | `venues/kraken_spot.py` | 余额 / 下单 / 撤单 / 充值方式 / 提币 | **已有**（缺提币地址簿处理） |
| 2 | `venues/kraken_exit_gate.py` | 出口能不能成立（交易对 / 充值链 / 盘口冲击） | **已有** |
| 3 | `scripts/kraken_deposit_refresh.py` | 充值方式缓存（每 6 小时） | **已有** |
| 4 | `execution/xstocks_buy.py` | Solana 买入：报价 → 构造交易 → 模拟 → 广播 → 记账 | 待写（复用 `scripts/xstocks_swap_sim.py`） |
| 5 | `execution/xstocks_deposit.py` | 转到 Kraken 充值地址 + 等到账 | 待写 |
| 6 | `execution/xstocks_sell.py` | Kraken 卖出：读盘口 → 闸门 → 挂单 → 超时处理 → 记账 | 待写 |
| 7 | `execution/xstocks_cycle.py` | 编排 + 状态机（fail-closed）+ 账本落组 | 待写 |
| 8 | `ops/xstocks_policy.toml` | 阈值（净利、风险、单笔上限、滑点、提币费上限） | 待写 |

三条硬规则（写进代码，不靠人记）：

1. **先验证出口再下单**：出口闸门不过就不买。
2. **数量先过最小充值量**：低于 `DepositMethods` 最小值不转（转丢不可追回）。
3. **每次只允许一笔在飞**：状态机不允许多笔并发，异常即停并报警。

### 5.6 看板的两个调整（operator 2026-10-05 指出）

1. **不再为 10 USDT 单独询一次价**。报价阶梯本来就是带数量问的，直接用
   `size = min(10 USDT, 阶梯上仍便宜的最大档)`；买 1 U 就推动价格的名字最多买 1 U。
2. **盘口探测并行**。Kraken 的 Depth 探测与链上询价没有依赖，改成 6 路并发，
   不再串行等（一轮从 271 s 回到 **243 s**）。

---

## 5.5 回款路径：卖出后怎么把钱搬回去（2026-10-05 实测）

**问 1：卖出得到的是 USD 还是 USDT？**

**USD。** `MRNAx/USD` 的报价币是 `ZUSD`。实测这笔卖出后余额是
`ZUSD 102.9320`，不是 USDT。

**问 2：能不能直接 USD → USDG 搬回 X Layer？**

**能，而且不用绕 USDT。** Kraken 挂了 USDG，也支持提到 X Layer：

| 路径 | 成本 | 步骤 |
|---|---|---|
| **ZUSD → USDG → X Layer**（推荐） | 交易 maker **0%** / taker 0.01% + 提币 **1 USDG** | 2 步 |
| ZUSD → USDT → X Layer → 换 USDG | 交易 0.2% + 提币 1 USDT + 链上 swap | 3 步 |
| ZUSD 电汇出金 | 最低 $100，费 $13 | 不走 |

实测到的网络与最低量：

| 资产 | 网络 | 最小量 | 费用 |
|---|---|---|---|
| USDG | **Xlayer** | 2.50 | 1 USDG |
| USDG | Solana | 1.4796 | 1.2330 USDG |
| USDG | Ethereum | 1.01616 | 0.8468 USDG |
| USDT | Xlayer (USDT0) | 2.50 | 1 USDT |
| USDC | Solana (SPL) | 1.47972 | 1.2331 USDC |

所以两条链路各自回哪个钱包：

- **X Layer 买入** → `USD → USDG → 提 X Layer` → 到账就是我们钱包里一直在用的
  USDG `0x4ae46a…2dc8`；
- **Solana 买入** → `USD → USDG → 提 Solana`（或 USDC/USDT 回 Solana）。

---

## 6. 待 operator 确认

1. **Falcon** 指什么（我按 Kraken 理解）。
2. 卖完的稳定币：X Layer 买就回 X Layer 的 USDG，Solana 买就回 Solana 的 USDC/USDT —— 对不对。
3. 现在这笔 **ZUSD 102.93 是否立刻换成 USDG 并提回 X Layer**（费 1 USDG），还是先留在 Kraken。
4. 8765 监控（含旧的 OKX 自动链）是否重启 —— **operator 已定：先暂停，保持停止**。
5. 是否开始做 S2（paper 全流程）。

---

## 7. 证据指针

- 旧计划与全链路图：`/home/operator/.cursor/plans/mrnax_kraken_exit_93bdf7f0.plan.md`
- 跨链操作步骤与地址表：`docs/research/xstocks-bridge-to-solana-2026-09-26.md`
- 退出腿教训（必读）：`docs/xstocks-kraken-exit-lessons-2026-09-27.md`
- 卡点记录（`ETrade:User Locked`）：`docs/xstocks-gap-board-handoff-2026-10-03.md`
- Kraken 客户端：`src/arb_monitor/venues/kraken_spot.py`
- 只读探测命令：`scripts/kraken_probe.py status`
