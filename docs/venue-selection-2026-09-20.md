# S0 选场：该接哪一家交易所的执行适配器（2026-09-20 现场读数）

> **2026-09-22 追加（operator 决定）**："之后不需要再监控 gate 这个交易所，我决定
> 不使用这个交易所"。因此本文里 **gate 不再是候选场馆**（`config/hk_monitor.toml`
> 的 `[monitor] venues` 已删掉 "gate"，行情/盘口/资金费/基差行全部停止采集），
> §0 那份"gate 538 ≈ bitget 518 并列"的读数**仍然是当时测到的原文**，但排序里
> 只剩 **bitget ＞ bybit ＞ binance ＞ aster ＝ okx ＝ 0**。这一改动只影响选场，
> 不构成接线授权，也没有动任何闸门、额度或白名单。

> 目的：按 `docs/venue-adapter-integration-plan-2026-09-20.md` 的 **S0** 出一页结论，
> 供 operator 决定"接谁"。本文**只读**，不改任何闸门、额度、白名单。
> 所有数字都能用 §5 的命令复跑。

---

## 0. 一句话结论

**gate 与 bitget 并列第一，分不出来**；按"我们真的下得了手的机会"排序是
**gate 538 ≈ bitget 518 ＞＞ bybit 105 ＞ binance 50 ＞ aster 0 ＝ okx 0**
（`scripts/venue-candidate-by-asset.py`，2026-09-20 15:2x 复跑）。
binance 的**候选率**第一（9.2%），但它的 623 次过门槛里 **573 次是 TENCENT**，
一股 53.57 USDC，钱包 10.91 USDC **一股都买不起** —— 率第一不等于做得了。

**建议：接 bitget**，gate 是等价的第二选择。分界线不在"次数"（差 4%），而在
数据可信度：bitget 陈旧率 **5.9%** / 可评估率 **94.0%**，gate 陈旧率 **26.5%** /
可评估率 **54.8%**。但要说清楚——**这个 4% 的差距在噪声里**，真正能把两家分开的
是 S5 成本标定，而那需要先有其中一家的账号。

⚠️ **本轮不构成接线授权**。接入要先有该场的只读 API key（S1），当前
`config/` 下**只有 OKX 的凭据**（`config/okx-demo.env`），其余七场一把没有。

---

## 1. 读了哪三项（S0 要求的缺一不可）

### 1.1 三列时段 + 过门槛次数与率

窗口 **09-18 09:22 → 09-20 15:22（54 小时，2970 轮）**，命令见 §5：

| 场所 | 定价行 | 可评估 | 陈旧(不可信) | 有机会 | 过门槛 | 候选率 | 机会率 | 可评估率 | 陈旧率 |
|---|---|---|---|---|---|---|---|---|---|
| lighter | 2960 | **164** | 2794 | 110 | 110 | **67.1%** | 67.1% | 2.8% | 94.5% |
| binance | 8770 | 3369 | 2030 | 2284 | **623** | **9.2%** | 33.9% | 38.3% | 23.2% |
| gate | 17670 | 9685 | 4683 | 3933 | **1120** | 8.6% | 30.3% | 54.8% | 26.5% |
| bitget | 17615 | 16570 | 1033 | 6574 | **1131** | 6.8% | 39.6% | **94.0%** | **5.9%** |
| aster | 7398 | 5909 | 1484 | 1255 | 263 | 4.4% | 21.2% | 79.6% | 20.1% |
| bybit | 17551 | 16552 | 987 | 4990 | 691 | 4.2% | 30.1% | 94.2% | 5.6% |
| hyperliquid | 1481 | 1393 | 87 | 85 | 1 | 0.1% | 6.1% | 93.9% | 5.9% |
| okx（已接） | 8773 | 8276 | 491 | 699 | **0** | 0.0% | 8.4% | 94.2% | 5.6% |

**三列时段**（`venue_scoreboard` 的 `phases`，这张表按 venue 级合并了三列，
分列明细见 §5 的 `--json`）：

| 场所 | 交易时段 过门槛/可评估 | 盘前盘后 过门槛/可评估 | 其他/休市 过门槛/可评估 |
|---|---|---|---|
| binance | 0 / 811 | 0 / 1037 | 623 / 4891 |
| gate | 258 / 1517 | 252 / 2003 | 610 / 9466 |
| bitget | 287 / 1726 | 279 / 2506 | 565 / 12350 |
| bybit | 315 / 1718 | 268 / 2503 | 108 / 12343 |
| aster | 103 / 670 | 63 / 956 | 97 / 4288 |
| okx | **0 / 851** | **0 / 1255** | **0 / 6176** |

**读法**：bybit/gate/bitget 在**交易时段**样本里是三强（18.3% / 17.0% / 16.6%，
与 `docs/venue-opportunity-scoreboard-2026-09-18.md` 一致）；把**休市时段**并进
分母之后排序换人 —— bitget 与 gate 的绝对次数升到第一梯队，而 bybit 在休市
时段掉到 0.9%。**"哪个场所机会多"这句话必须先说是哪个时段口径问的。**

### 1.2 机会集中在哪几个标的（含"我们买不买得起"）

**每股价格**（`spot_open_usdt / spot_quantity`，2026-09-20 15:2x 实测）：
TENCENT **53.57** ｜ POPMART 19.94 ｜ MEITUAN 9.32 ｜ SHEIN 4.83 ｜
KUAISHOU 3.90 ｜ XIAOMI 3.39（USDC/股）。

**可部署名义 = min(单笔上限 100, 钱包 10.92387 × 0.999) = 10.913 USDC**，
按整股向下取整。→ **TENCENT 与 POPMART 一股都买不起**，只有
MEITUAN / SHEIN / KUAISHOU / XIAOMI 进得去。

54 小时窗口，过门槛行按 (场所, 标的) 再拆一次，并标出该行**是否带
`below_min_notional`**（那是"1 股的永续名义低于该所最小名义"，不是数据问题）：

| 场所 | 标的 | 过门槛 | 其中名义 OK | 其中低于最小名义 | 该标的 1 股 | 钱包能不能买 |
|---|---|---|---|---|---|---|
| bitget | KUAISHOU | 609 | **0** | 609 | 3.89 | 买得起，但名义不足 |
| bitget | **MEITUAN** | **518** | **518** | 0 | 9.32 | ✅ |
| gate | TENCENT | 578 | 578 | 0 | 53.57 | ❌ 买不起 |
| binance | TENCENT | 573 | 573 | 0 | 53.57 | ❌ 买不起 |
| bybit | KUAISHOU | 537 | **0** | 537 | 3.89 | 买得起，但名义不足 |
| gate | **KUAISHOU** | **506** | **506** | 0 | 3.89 | ✅ |
| aster | KUAISHOU | 160 | 0 | 160 | 3.89 | 买得起，但名义不足 |
| lighter | TENCENT | 110 | 110 | 0 | 53.57 | ❌ + 94.5% 陈旧 |
| aster | TENCENT | 97 | 97 | 0 | 53.57 | ❌ 买不起 |
| bybit | MEITUAN | 79 | 79 | 0 | 9.32 | ✅ |
| bybit | SHEIN | 58 | 26 | 32 | 4.83 | 部分 |
| binance | MEITUAN | 50 | 50 | 0 | 9.32 | ✅ |
| gate | MEITUAN | 32 | 32 | 0 | 9.32 | ✅ |
| bybit | TENCENT | 13 | 13 | 0 | 53.57 | ❌ 买不起 |
| aster | XIAOMI | 3 | 0 | 3 | 3.38 | 名义不足 |
| hyperliquid | SHEIN | 1 | 1 | 0 | 4.83 | ✅（1 次，样本太小） |
| okx | — | **0** | — | — | — | — |

> 这张表由 `scripts/venue-candidate-by-asset.py --hours 54` 直接生成，**窗口每跑一次
> 会滑动几分钟，个位数会有 ±几次的漂移**（上表是 2026-09-20 15:2x 那一次）。

**加两个硬约束（1 股买得起 + 不低于该所最小名义）之后剩下的"可行动"次数：**

| 场所 | 可行动合计 | 构成 | 标的集中度 |
|---|---|---|---|
| **gate** | **538** | KUAISHOU 506 + MEITUAN 32 | 94% 集中在 KUAISHOU |
| **bitget** | **518** | MEITUAN 518 | 100% 集中在 MEITUAN |
| bybit | 105 | MEITUAN 79 + SHEIN 26 | 75% 集中在 MEITUAN |
| binance | 50 | MEITUAN 50 | 100% MEITUAN |
| aster | 0 | — | — |
| lighter | 0（且 94.5% 陈旧） | — | — |
| hyperliquid | 1 | SHEIN 1 | 样本太小 |
| okx（已接） | **0** | — | — |

**这张表是本文最重要的修正**：`venue-scoreboard` 文本表里的"过门槛次数"是
**在 1 股探针上**算出来的，**没有扣"这个钱包买不买得起"和"够不够最小名义"**。
不谈这两个约束，"哪个场所机会多"会被 TENCENT（gate/binance/aster/lighter 的
第一大来源）严重带偏 —— 而 TENCENT 恰恰是这个钱包一股都下不了手的标的。

### 1.3 不可信（陈旧）行占比

见 §1.1 最后两列。要点：

- **lighter 94.5% 陈旧**（2794/2958），54 小时只有 **164 次可评估**、且集中在
  TENCENT。它的 venue 级候选率 67.1% 是**小样本**，按 S0 的"小样本不选"直接排除。
- **gate 26.5% 陈旧**（4683/12976）；`XIAOMIHKD_USDT` 盘口还有
  `NetworkError`（`source_error` 里的 `depth`）。
- **binance 23.2% 陈旧**；且 `HK1810USDT` / `HK0700USDT` 两个 HKD 兄弟合约被
  `quanto_or_unknown_payout_requires_separate_hedge_model` +
  `unverified_or_expired_share_ratio_fx_mapping` 挡了 8790 次，从来没进过统计。
- **bitget 5.9% / bybit 5.6% / okx 5.6%** 是本窗口最干净的三家。

---

## 2. 为什么不是第二名（逐条）

- **为什么不是 binance（候选率 9.2% 第一）**：它的 623 次过门槛里 573 次是
  TENCENT（53.57 USDC/股），剩 50 次 MEITUAN；扣掉钱包约束后**只剩 50 次**，
  是 bitget 的 1/10。另外 `docs/venue-adapter-integration-plan-2026-09-20.md` §3
  已实测 **binance TENCENT 最优买价深度只有 0.12 张**，顶档远小于 1 股。
- **为什么不是 lighter（候选率 67.1% 第一）**：可评估只有 164 次、94.5% 行陈旧，
  S0 明文"小样本不选"；且它的 110 次过门槛**全部是 TENCENT**（买不起）。
- **为什么不是 bybit**：可行动 105 次（bitget 的 20%）；交易时段候选率 18.3%
  虽然最高，但**休市时段掉到 0.9%**（108/12343），把周末算进来后排位下滑。
- **为什么不是 aster**：可行动 **0 次** —— KUAISHOU 160 次与 XIAOMI 4 次全部
  `below_min_notional`，TENCENT 97 次买不起。采样密度也只有前三家一半。
- **为什么不是 hyperliquid**：54 小时**只有 1 次**过门槛（SHEIN），样本太小；
  且股票永续在 builder dex（`xyz:SHEIN`）里、下单是 EIP-712 签名，
  与 REST key 完全是两条接入路径。
- **为什么不是 okx（已接的那家）**：54 小时**过门槛 0 次**（三个时段全 0），
  最高毛 39.8 bps 扣掉成本后过不了 20 bps 门槛。它是接口基准，不是机会来源。

## 2.1 为什么不是 gate（与 bitget 只差 4%）

gate 的可行动次数 **538** 比 bitget 的 **518** 高 4%，两家在次数上**分不出来**。
选 bitget 的理由是数据质量，不是次数：

| 判据 | gate | bitget |
|---|---|---|
| 可评估率 | 54.8% | **94.0%** |
| 陈旧率 | 26.5% | **5.9%** |
| 可行动次数 | **538** | 518 |
| 可行动标的 | KUAISHOU 506 + MEITUAN 32 | MEITUAN 518 |
| 接入已知坑 | 同标的另有 HKD 计价兄弟合约 `TENCENTHKD_USDT`，部分行带 quanto/映射未验证（见计划 §3） | MEITUAN 最优买价 68.25 张 = 0.68 股，顶档不够 1 股，必须吃深度加权价 |

**两家都有"必须吃多档深度"的问题**，所以这一列不构成区分。
**"gate vs bitget"真正需要的是一个 S5 成本标定**（各自的 taker/maker、资金费
结算周期、真实成交反算的往返成本）——而 S5 需要先有其中一家的账号，
所以这里只能停在"并列"，由 operator 选。

---

## 3. 今天（09-20，周日）最近 8 小时的读数：全 0 可行动

`venue-scoreboard --hours 8 --wallet-quote-usdt 10.92387`，窗口
**09-20 07:17 → 15:15（456 轮）**，**全部落在 `其他/休市`**（周日没有交易时段样本）：

| 场所 | 可评估 | 有机会 | 过门槛 | 候选率 | 最高毛(bps) | 过门槛标的 |
|---|---|---|---|---|---|---|
| lighter | 9 | 6 | 6 | 66.7%（小样本） | 86.0 | TENCENT |
| binance | 998 | 544 | 219 | 22.1% | 84.1 | TENCENT |
| gate | 1856 | 841 | 181 | 9.7% | 78.8 | TENCENT |
| aster | 863 | 192 | 63 | 7.3% | 96.9 | TENCENT |
| bybit | 2583 | 889 | **0** | 0.0% | 69.1 | — |
| bitget | 2582 | 1571 | **0** | 0.0% | 67.0 | — |
| hyperliquid | 217 | 11 | 0 | 0.0% | 55.5 | — |
| okx | 1296 | 176 | 0 | 0.0% | 39.8 | — |

（`scripts/venue-candidate-by-asset.py --hours 8` 的同一窗口读数略小 —— binance 217、
gate 181、aster 63、lighter 5 —— 因为两次跑的窗口相差几分钟。）

**今天 8 小时里过门槛的那 466–469 次全部是 TENCENT —— 一股都买不起。**
（这与 54 小时窗口里 bitget 的 MEITUAN 518 次并不矛盾：MEITUAN 的机会在那 8 小时
窗口里没有出现，最高毛 67.0 bps 在 bitget 上**全部不到 20 bps 净**。）
**不要用这 8 小时去选场**，它的信息量只有"今天 TENCENT 在动、可我们做不了"。

---

## 4. 还没做、也做不了的部分（不要当成已验证）

1. **S1 私有只读客户端做不了**：`config/` 下只有 OKX 凭据（`okx-demo.env`），
   gate / bitget / binance / bybit / aster / hyperliquid / lighter **一把 key 都没有**。
   S1 的验收要求"用无交易权限的 key 读出持仓/挂单/成交/合约元数据/手续费"，
   需要 operator 先在目标场建**独立子账户 + 只读 key + 固定出口 IP**。
2. **成本模型未标定（S5）**：现在所有场所的"过门槛"都借用 **OKX 账户实测的
   ≈50 bps 往返成本**。跨场所比较因此是**同一把尺子量不同场**，不是各场的真实成本。
   这意味着 §1.1 的"过门槛次数"对 gate/bitget 都不一定准。
3. **最小名义要按场逐个核**：本文的 `below_min_notional` 用的是**监控已记录的
   合约元数据**（`min_notional` / `lot_size` / `multiplier`），没有在目标场私有接口
   上复核过 —— 这正是计划 S4 要求"先算最小名义 ≤ 可部署名义"的那一步。
4. **周末窗口仍然是主样本**：54 小时里 `其他/休市` 占多数。周一（09-21）盘中
   放开后的第一份含盘中样本的排名要重算，见 §5 的复跑命令与 HANDOFF §11。

---

## 5. 复现

```bash
cd /home/operator/Documents/arb

# 钱包余额（需外网 → 加权限跑；沙箱里会误报"未验证"）
PYTHONPATH=src .venv/bin/python -m arb_monitor account-status --json
# → grey_scale.wallet_quote_balance = 10.92387，affordable_trade_quote = 10.91294613

# §3 的 8 小时表（本文命令）
PYTHONPATH=src .venv/bin/python -m arb_monitor venue-scoreboard --hours 8 \
  --wallet-quote-usdt 10.92387

# §1.1 的 54 小时表 + 分列 phases（机器可读）
PYTHONPATH=src .venv/bin/python -m arb_monitor venue-scoreboard --hours 54 \
  --wallet-quote-usdt 10.92387 --json > /tmp/vsb54.json

# §1.2 的"标的 × 可负担 × 最小名义"拆解。本轮新增的只读脚本，口径与 scoreboard 一致
#   （只算带 evaluated_at_ms 的行、剔掉 DATA_QUALITY_REJECTIONS、成本读 roundtrip_cost_usdt、
#    单笔上限读 config/automation_policy.toml 的 per_trade_quote_usdt）
PYTHONPATH=src .venv/bin/python scripts/venue-candidate-by-asset.py \
  --hours 54 --wallet-quote-usdt 10.92387
PYTHONPATH=src .venv/bin/python scripts/venue-candidate-by-asset.py \
  --hours 8  --wallet-quote-usdt 10.92387

# 钱包与账户现场（需外网）
PYTHONPATH=src .venv/bin/python -m arb_monitor account-status --json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["grey_scale"], d["okx"]["positions"], d["okx"]["pending_orders"])'
```

**本文所有现场读数的时间戳**：2026-09-20 15:17–15:25 CST（周日）。
监控 `running (37006)`（启动 14:45:58），最后事件 15:17:09；
`data/KILL_SWITCH` 不存在，**`data/AUTO_HALT` 存在**（见 HANDOFF §1/§11）。
