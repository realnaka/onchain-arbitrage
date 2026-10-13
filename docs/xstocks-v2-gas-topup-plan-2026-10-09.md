# 开火前自动补 gas（C）——方案（2026-10-09，待 operator 填两个上限）

> 状态：**方案 + hermetic 测试已就绪；执行未接线，代码默认关闭。**
> 代码：`src/arb_monitor/xstocks_v2/gas_topup.py`（`decide` / `ledger_row`）
> 测试：`tests/test_xstocks_v2_gas_topup.py`（11 例）
> 需要 operator 给的两个数：**单次金额上限（USDT）**、**每日次数上限**。
> 不给就是 `cap_not_set`（拒绝），不是「无限」。

## 1. 为什么

实测（2026-10-07 起持续到 2026-10-09）：

| 项 | 值 | 说明 |
|---|---|---|
| X Layer 钱包 OKB | **0.0006056** | `eth_getBalance` |
| 一条 CCIP 消息的费率 | **0.00080244199238106** OKB | 链上干跑读回（A45） |
| 结果 | `gas_low` | 在**买腿之前**拒绝（对的），x-layer 出发的家族一律做不了 |
| 现有补 gas 手段 | **没有** | v2 全仓没有任何补 gas 路径 |

余额不够时「买完才发现搬不走」才是真危险（币在钱包里、链上付不出费）。所以拒绝是对的；
缺的是**自动把差额补上**这条腿。B5（人工补 OKB）是它的替代方案，但没有自动化就会反复发生。

## 2. 做什么（最小形状）

在**源链 precheck 报 `gas_low` 之后、买腿之前**插一步：

1. 读差额 `shortfall = reserve_messages × fee_per_message − native_balance`；
2. 用**同链稳定币**换「刚好够 + 小余量」：X Layer 用 USDG→OKB（走已有的 USDG/WOKB v3 池，
   与 A7 的定价源同一个池），Solana 用 USDC/USDT→SOL（**本方案不启用**，见 §6）；
3. 写账本一行 `gas_topup`（换了多少、拿到多少、tx hash）；
4. **重跑 precheck**，仍然不够就拒绝（不重试第二次）。

顺序上它永远在买腿之前：先有 gas，再买币。金额**不做轮询**，也不「尽量多买」——
只在差额上补，因为 gas 币价格波动带来的敞口不是本策略要承担的风险。

## 3. 参数（operator 填；留空 = 不动作）

| 参数 | 建议面 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | 开关 | `false` | 默认关；开了还要两个上限都在 |
| `max_usdt_per_topup` | **单次上限** | 空 → `cap_not_set` | 硬上限，**超了就拒**（不做 clamp：clamp 过会留下付不出的腿） |
| `max_topups_per_day` | **每日次数上限** | 空 → `cap_not_set` | 硬上限，按 UTC 日计 |
| `reserve_messages` | 想预存几条消息 | `2` | 2 条 = 一次 x-layer→solana + 一次 x-layer 出发的其它动作 |
| `max_slippage_bps` | 兑换自身滑点上限 | `10` | 与全仓 slippage buffer 同一个量级 |

**建议的初值（供参考，不是默认）**：`max_usdt_per_topup = 1`、`max_topups_per_day = 2`。
按当前费率 0.000802 OKB × 124.47 ≈ **0.0999 USDT 一条**，1 USDT 上限 ≈ 10 条消息，
足够把一个「买 + 桥」周期补满且远小于 25 USDT 的票面。

## 4. 失败语义（每一格都要有名字）

| 情形 | 判定 | 动作 |
|---|---|---|
| 关闭 / 上限没填 | `disabled` / `cap_not_set` | 什么都不做，按原 `gas_low` 拒绝 |
| 原生币余额读不到 | `balance_unknown` | 拒绝（不动作）。**未知不是 0，也不是有钱** |
| 费率读不到 | `fee_unknown` | 拒绝（不动作）——费率走 A45 的链上读数 |
| gas 币美元价读不到 | `gas_price_unknown` | 拒绝（不动作）——价格走 A44 的刷新 |
| 差额 > 单次上限 | `over_cap` | 拒绝，并在账本/板面写明差额 |
| 当天次数已用完 | `daily_cap` | 拒绝 |
| 稳定币不够 | `stable_short` | 拒绝（不部分成交） |
| 兑换报价失败 / 429 | `quote_failed` | 拒绝，**不重试到成功**（一次重试上限与现有 `REQUOTE_TRIES` 一致） |
| 兑换成交但到账不足 | `topup_short` | 不写「成功」，照常走 precheck → 仍然 `gas_low` |
| 广播结果未知 | 停机 + 人工对账 | 与全仓规则一致：未知状态不动作 |

三条不变量：

1. **补的金额 ≤ 差额 + 余量**，且 **≤ 单次上限**；
2. **每一笔兑换都写账本**（`event = gas_topup`，含 tx hash 与拿到多少）；
3. **gas 不足时先补、再买**，任何情况下不出现「先买币、后补 gas」。

## 5. 接线点（本方案不做，等 operator 点头）

| 文件 | 位置 | 改动 |
|---|---|---|
| `scripts/xstocks_v2_solana_roundtrip.py` | `precheck()` 的 `gas_shortfall` 之后 | 差额非空 → `gas_topup.decide(...)` → 执行兑换 → 重跑 precheck |
| `scripts/xstocks_v2_xlayer_roundtrip.py` | `sol_shortfall` 之后 | 对称（Solana 侧，本方案暂不启用） |
| `data-v2/xstocks_v2_cycles.jsonl` | 新事件 | `gas_topup` 行（`ledger_row` 的形状） |
| 看板 | 「桥费定价」卡片旁 | 今日 top-up 次数 / 累计金额 / 上一次差额 |

## 6. 明确不做

* **Solana 侧不启用**：SOL 的费是「转账 lamport 线」，没有稳定币计价的市场深度问题要处理，
  但谁付 ATA 租金（145 bps）需要单独决定，混进来会让参数失真。
* **不自动重复补**：一轮最多一次；连续不足就报出来让人看。
* **不动 `[limits]`**：这是**运营补币**，不是交易额度；两个上限写在 `[gas_topup]` 自己名下。
* **不改 live 的三把钥匙**：补 gas 也走 preauthorized 链，但它自己有一个开关。

## 7. 纸面验证路径（接线后的验收顺序）

1. **只算不做**：`enabled=false`，跑 `scripts/xstocks-v2-ctl.sh paper-once`，确认
   `gas_topup` 决策出现在快照里且全是 `disabled`（无副作用）。
2. **上限填上、仍不执行**：把两个上限填上，用**假钱包**（余额 0、稳定币 50）跑
   `gas_topup.decide`，确认 `size_usdt` 落在上限内且 = 差额 × 价。
   （已由 hermetic 用例覆盖；这一步是复跑给人看。）
3. **真读不真写**：用真钱包跑一次决策（`enabled=true`，但兑换函数仍不接线）→ 确认
   决策给出的差额与 `eth_getBalance` 一致，且 `over_cap`/`daily_cap` 在该拒时拒。
4. **最小额真补一次**：operator 点头后，用 `max_usdt_per_topup` 的最小可行额补一次，
   核对：钱包 OKB +Δ、USDG −Δ、账本 `gas_topup` 一行、tx hash 上链。
5. **再验一次完整周期**：补完立刻跑一个 family 5/6 周期，确认 precheck 通过、
   且桥费与 gas 都从链上读到。

## 8. 相关

* `AGENTS.md`「缺样本不是拒绝理由」（2026-10-09）：**缺输入是 bug，要修掉**——本方案就是
  `gas_low` 这条输入的修法。
* A44（gas 币价自动刷新）、A45（桥费每轮读链）：本方案的两个输入都由它们提供。
* B5（人工补 OKB）：本方案的替代方案；B5 的「补多少」= 本方案 `reserve_messages` 的答案。
