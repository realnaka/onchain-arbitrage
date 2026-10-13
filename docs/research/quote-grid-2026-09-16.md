# OnchainOS 报价探针：50114 复核与报价网格重做（2026-09-15/16）

## 结论先说

1. **`50114 Invalid Authority` 已经不是当前状态**。2026-09-12 记录的那次失败是
   CLI 没有走代理导致的测量失败；通道本身没问题。2026-09-15/16 实测：
   `quote_status = quotes_observed`，每轮 16/16 覆盖，单轮 0 失败。
2. 真正卡住“双腿报价”的是**探针网格本身的设计**：每个资产 × 4 个金额档 × 双向，
   共 64 次 CLI 往返，串行执行约 70 秒。订单簿与报价在同一轮里并行抓取，
   所以等策略做配对判断时，**盘口已经 80 秒旧**，而配对预算是 15 秒——
   于是每一轮都以 `missing_or_stale_*` / `non_simultaneous_quotes` 被拒。
3. 修完之后（2026-09-16 00:02 实测）：探针 16 次、**整轮 6.0 秒**、
   盘口年龄中位数 5.1 秒，配对的陈旧类拒绝**全部消失**。

## 一、50114 复核

```text
2026-09-15 23:3x 单次调用（走本地代理）
  onchainos swap quote --chain 196 --from <USDG> --to <wXIAOx> --amount 1000000 --swap-mode exactIn
  → {"ok":true,"data":[{"action":"ok",...}]}
```

同一天监控进程的汇总里 `quote_status=quotes_observed`、`actual_quote_requests=48`、
`quote_errors=[]`。即：**当月早些时候记录的 50114 是历史状态**，README/plan 里
“报价仍被 50114 阻塞”的表述已经过期，本次一并更正。

> 教训：把一次测量失败写进文档时，要连“在什么网络条件下、用什么命令测的”一起写，
> 否则下一次会把历史状态当成当前事实。

## 二、报价网格的实际问题（测量值）

| 配置 | 探针数/轮 | 整轮耗时 | 盘口年龄（评估时） | 失败 |
|---|---|---|---|---|
| 4 档 × 8 资产 × 双向，串行 | 48–64 | ~70 s | ~83 s | — |
| 同上，12 并发 | 64 | 17.0 s | 12.8–37 s | ~40% 被限流 |
| 同上，6 并发 | 64 | 25.2 s | 23–41 s | 40 个 “Network unavailable” |
| 同上，4 并发 | 64 | 37.3 s | 34–53 s | 0 |
| **只探执行金额，4 并发** | **16** | **6.0 s** | **3.1–18.4 s** | **0** |

三个约束互相拉扯：并发高 → 被限流；并发低 → 网格太慢 → 盘口过期；
网格越大 → 两个问题同时恶化。

## 三、改法：开仓只需要一个报价

“双腿报价”不是“两次链上报价”。拆开看：

| 用途 | 需要什么 | 需要多少次 |
|---|---|---|
| 开仓 | 链上 exact-in 报价（花多少 → 得多少代币）+ CEX 盘口 | **1 次链上报价** |
| 平仓评估（策略的往返模型） | 同一数量反向的 exact-in 报价 | 1 次（可选、跟随买入报价的数量） |
| 深度研究 | 不同金额档的池内价格 | **0 次**（由池子 slot0 + 流动性推算） |

所以：

```toml
# 只对"真正会下单的金额"询价
quote_sizes = ["4"]
# 深度阶梯由池子状态推算，不额外消耗报价
depth_sizes = ["4", "100", "500", "1000"]
quote_workers = 4
quote_retries = 3        # 读请求可重试；写请求仍然 0 次
quote_backoff_ms = 600
```

校验写在 `load_config` 里：**最小的 depth_size 必须等于最小的 quote_size**，
否则看板展示的“链上 Ask”与实际下单会付的价不是同一笔交易。

## 四、重试策略

```text
api_error（限流 / Network unavailable / 代理抖动）→ 有界退避重试，抖动 0.5–1.5×
auth_error（50114 等）                            → 不重试，直接停整张网格
schema_error（返回结构不明）                       → 不重试，按未知处理
```

依据是项目既有的网络策略：**读请求允许有界重试，写请求 0 次重试**。
限流和代理抖动都是瞬时的读失败，重试是把“网格上的一个洞”变成“一个价格”；
而鉴权失败重试只会把失败放大 16 倍。

## 五、修完之后的复盘

同一轮里 XIAOMI/OKX 的拒绝理由（`basis_evaluation`）：

```text
修之前：quanto…, unverified…mapping, unverified…costs, missing_or_stale_perp_book,
        missing_or_stale_spot_buy_quote, unknown_buy_gas, missing_or_stale_spot_sell_quote,
        unknown_sell_gas, non_simultaneous_quotes, no_future_exit_basis_assumption
修之后：unverified_or_expired_full_roundtrip_costs,
        unknown_buy_gas_in_quote_currency, unknown_sell_gas_in_quote_currency,
        no_future_exit_basis_assumption,
        contract_rounding_leaves_excess_directional_exposure
```

即：映射、盘口、链上买卖报价、双腿同时性**四条门已经打开**。剩下的三条是
成本与策略假设，不是数据接入问题：

1. `unverified_…costs`：`[costs] verified=false`，需要平仓侧 + 更大规模的成交样本；
2. `unknown_buy/sell_gas_in_quote_currency`：OnchainOS 的 `estimateGasFee` 是链原生单位，
   代码拒绝把它当 USDC。实测 X Layer 三笔交易 `effectiveGasPrice=0`，可以据此把
   `roundtrip_gas_budget_usdt = "0"` 显式写进配置（**未做，等确认**）；
3. `no_future_exit_basis_assumption`：策略不假设未来退出基差，需要人来给出一个
   明确假设（**未做，等确认**）。

另外新暴露一个**下单数量**问题（**已于 2026-09-16 修复**，见文末）：

```text
contract_rounding_leaves_excess_directional_exposure
4 USDG → 1.168 股 → 只能卖 1 张合约 → 0.168 股裸露 = 1441 bps ≫ max_unhedged_bps(5)
```

合约张数是整数，所以按“4 USDG”这种金额下单必然留残差。要把残差压到 5 bps 以内，
应当**按整股下**（例如 5 股 → 5 张合约，残差 0），再让链上金额去匹配那个股数。
这是下一轮该改的下单尺寸逻辑。

## 六、2026-09-16：按整股下单（exact-out）已落地

`onchainos swap quote --swap-mode exactOut` 支持「固定输出、输入有上限」：

```text
1 股 → 3,421,168 最小单位 USDG；2 股 → 6,842,345；3 股 → 10,263,258
tx.maxSpendAmount 给出该笔允许花掉的上限（报价 + 0.5%），tx.minReceiveAmount = 精确股数
```

改动：

- 钱包适配器新增 exact-out 报价/构造/模拟校验（`--amount` 传**输出**最小单位；
  `--readable-amount` 对 exact-out 会按输入币精度换算，实测报 `82112`，不能用）；
- 执行器按整股下单，`--shares N` 或按预算反推 N，对冲张数 = N，`residual_bps = 0`；
- 监控探针同样改成整股（`probe_shares = ["1"]`），因为现金式探针返回小数股，
  整数张合约永远 hedge 不干净，策略会一直以
  `contract_rounding_leaves_excess_directional_exposure` / `exact_hedge_required_for_intent` 拒绝。

探针网格大小与配对预算的关系（实测）：

| 探针 | 次数 | 整轮 | 结果 |
|---|---|---|---|
| 3 个档位 × 8 资产 × 双向 | 48 | 23.9 s | 盘口 15–31 s，超出 15 s 预算 |
| 2 个档位 | 32 | 17.4 s | 仍略超预算 |
| **1 个档位（1 股）** | **16** | **6.3 s** | 盘口 2.9–14.9 s，全部在预算内 |

结论：监控循环只评估**最小可交易仓位**，具体下多大在执行时决定（那时会重新报价）。
这样既保住了 15 秒配对预算，又没有放松任何阈值。
