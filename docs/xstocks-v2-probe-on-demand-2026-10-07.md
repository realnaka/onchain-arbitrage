# 按需小样本（probe on demand）—— 设计、现状与缺口

日期：2026-10-07　范围：`xstocks_v2`　触发人：operator

## 1. 起因（operator 原话）

> 在看板里面写那个拒绝的原因里面，我发现好几条都是这个币在这条桥上没有到账样本。
> 我觉得这个原因就不可以接受，就非常奇怪。我们不可能把每一个币在每一条桥上面都做
> 一次样本。所以说，当你发现有价差的时候，你应该主动去针对这个币做一次样本。你买
> 入一个可以转账或者跨链或者是能够交易的最低金额，然后主动去做这个跨链的测试，一旦
> 最小值通过，你就去执行剩余的可以套利的金额。NVDAx SPCXx 两个币都是这个原因。

## 2. 现场核对的结果（先核对，再动手）

| 问题 | 实测 | 证据 |
|---|---|---|
| 那两行真的是「只差一个样本」吗？ | **不是。价差是负的** | 快照 `data-v2/xstocks_v2_scan.json`：NVDAx `chain_to_kraken/x-layer` **−20.04 bps**，SPCXx **−15.42 bps**，TSLAx +3.37 bps（当时 `session=premarket`，门槛 4 bps） |
| 为什么板面还印「没有到账样本」？ | `_row()` 取 `blockers[0]` 当标题原因，而 `chain_to_kraken` 这一族**从来没有算过经济闸门** | 同一份快照里 18 条「ok / 无 blocker」的行**全部是负价差**（−0.95 ~ −100.7 bps） |
| 样本是记在「桥」上还是「币」上？ | 只记 `(源链, 目的链)`，所以 SPYx 的一次样本给所有币开了边 | `scan.load_measured_bridges()`（旧）；样本文件当时只有 2 行，都是 SPYx |
| 真的过桥成功过吗？过桥成功会写样本吗？ | 会到账，但**不写样本**：`xstocks_v2_solana_roundtrip.py` 的 `verify_bridge` 只证明余额上升，证明随进程消失 | 脚本内 `state["sol_after"]` 只进内存与账本 |
| 有没有 X Layer → Solana → Kraken 的执行器？ | **没有**。四个可执行家族里没有 `chain_via_bridge_to_kraken` | `live.EXECUTABLE_FAMILIES` |

**结论**：operator 的观察（这条拒因不该常常出现）成立，但原因不是「样本机制太笨」，
而是两件事叠在一起——**板面说错了原因**，以及**过了桥不记账**。

## 3. 改了什么

### 3.1 拒因说真话

* `scan.headline_reason()`：按 `REASON_ORDER` 选标题原因，**经济/质量 > 管线**。
  同一行仍然把全部 blocker 列出来，只是不再让管线原因抢头条。
* `chain_to_kraken` 与 `chain_via_bridge_to_kraken`（池 → hub → Kraken 一侧）
  现在写 `below_threshold`：`edge_t0_bps < policy.required_edge_bps(session)`。
  门槛照旧随开市/休市变（`ops/xstocks_v2_policy.toml`）。
* `paper.decide()` 把 `below_threshold` 当**可复算**的标记（和 `stale_or_skew`
  一样），所以纸面仍会复价、仍会记 `false_t0`，不会被 t0 的结论顶掉。
* `scripts/xstocks_v2_live.py:candidates()` 同样豁免它，否则链 → Kraken 的复价
  会在休市时整体停掉。
* `paper.decide()` 拒绝时改用**扫描给出的 `reason`**，不再用字母序 `sorted()[0]`。
  看板的「最近判定」表读的就是这个原因，operator 原来就是在这张表上看到
  「这个币在这条桥上还没有到账样本」的。整份 blocker 仍在账本行的
  `extra.blockers` 里，没有丢。

### 3.2 样本记在「币」上

* `scan.load_measured_symbols()` → `(源链, 目的链, 币)`。
* 两个说法现在分得很清楚：

| blocker | 含义 |
|---|---|
| `unmeasured_route` | 这条**链对**从来没有人走通过 |
| `bridge_unmeasured_symbol` | 链对走过，但**这个币**没有到账记录（Solana 的 CCIP token config 是按 mint 的，链对的样本不能外推） |

### 3.3 到账就写样本

`xstocks_v2_solana_roundtrip.py:verify_bridge()` 在「Solana 余额上升」那一支里
append 一行到 `data-v2/xstocks_v2_route_samples.jsonl`
（`scan.route_sample` / `scan.record_route_sample`，`arrived: true`）。
**源链模拟通过不写**，只有真到账才写。

### 3.4 按需小样本：判据与计划

| 函数 | 作用 |
|---|---|
| `scan.probe_shaped(blockers)` | 这行是不是「只差一个样本」（允许再缺一个**桥费**） |
| `scan.probe_eligible(blockers)` | 除样本外没有别的毛病（可运行的那一档） |
| `scan.probe_plan(...)` | 这条路线的最小可行量计划：hub 链的**充值最小额**、**桥费 USD**、`waiting_on`、`runnable` |
| `live.probe_decision(row)` | 三问：价差过门槛？只差样本？计划里每个最小额都已知？ |
| `live.probe_candidates(snapshot)` | 本轮所有「走一次就能变成可交易」的行 |

计划缺输入时（`waiting_on` 非空）**只出计划、不出钱**。

### 3.5 看板

新增「自动小样本（价差够就买最小额过桥，到账后补足剩余）」一栏：

| 列 | 含义 |
|---|---|
| 标的 / 路线 | 哪个币、哪条 `源链->hub` |
| 价差 t0 bps / 门槛 bps | 触发条件与当时的门槛 |
| 充值最小额（币）/ 桥费 USDT | 计划需要的两个数；未知就印「未知」 |
| 能不能跑 | `能` / `等：<缺的那个输入>` |

## 4. 现在到底会不会花钱

**不会。** 两层原因，缺一不可：

1. `probe_decision` 只在**价差 ≥ 门槛**时返回可跑。当前快照里这条路线全是负价差，
   所以 `probe_candidates` 是空的。
2. 本轮**没有**新增任何广播路径：`chain_via_bridge_to_kraken`（X Layer 买 → 桥
   Solana → 充 Kraken → 卖）**还没有执行器**，本轮也没有把它接上。
   按 AGENTS.md「新增自动执行能力前，必须先通过纸面交易、人工执行统计和对账验证」。

## 5. 「到账后补足」那条腿（operator 2026-10-07 批准）

一个脚本两种结尾，交叉那一段完全共用（`scripts/xstocks_v2_solana_roundtrip.py`）：

| `--exit` | 落地的最后两步 | family | 结束状态 |
|---|---|---|---|
| `jupiter`（原来） | Solana 上换成 USDC | `xlayer_via_bridge_to_solana` | `sold` |
| `kraken`（新） | 充进 Kraken → 卖进买一 | `chain_via_bridge_to_kraken` | `sold` |
| `--probe-only` 叠加在任一种上 | 到账即停 | 同上 | **`probed`** |

三个关键设计：

1. **充值转的是「钱包里这个币的全部余额」**，不是这一轮那条腿。所以探测留下的那一小
   片会被下一笔交易一起充进 Kraken，不会被搁浅。
2. **`probed` 是一个新状态**：既不是 `sold`（什么都没卖），也不是 `stopped`（没出事）。
   它释放锁、不写 HALT，但计入持仓（`ledger.carried_inventory()`），下一轮看得到。
3. **转账那一腿不重试**：SPL 转账报错时，币可能已经在飞，重发就是双付。

## 6. 触发顺序（常驻进程里）

```text
先算四个已有家族（链→Kraken / Kraken→桥→链 / XLayer→桥→Solana / Solana→桥→XLayer）
   ↓ 一个都没成交
第五个家族：chain_via_bridge_to_kraken（有样本 + 净利过门槛，按钱包余额重算）
   ↓ 还是没成交
probe_candidates：只差一个样本 + 价差过门槛 → 买最小额过桥，到账即写样本
```

一笔在飞时锁是独占的，所以这五步不会重叠。

## 7. 还没做

| # | 缺口 | 说明 |
|---|---|---|
| 1 | SPYx/TON | 卡在没有 TON 钱包地址（见 `HANDOFF.md`） |
| 2 | 两段桥的等待 | 已接受：桥费按条收（$0.105/条），X Layer → Solana 实测 22–27 分钟一段 |

## 8. 测试与证据

```bash
PYTHONPATH=src:scripts .venv/bin/python -m unittest tests.test_xstocks_v2_probe
# 34 个 hermetic 用例：样本 schema、标题原因排序、probe 判据、计划缺输入、
# 扫描行产出、板面渲染、probed 状态与持仓结转、执行器终止状态、
# 第五个家族的选择器、--probe-only / --exit kraken 的参数传递
```

全量 v2 回归：**404 个用例通过**（`tests.test_xstocks_v2*` 共 15 个模块）。

### 第一次真跑：NVDAx 与 SPCXx（2026-10-07，operator 批准）

| 币 | 买卖 | 桥广播 | 到账 | 耗时 | 桥 tx |
|---|---|---|---|---|---|
| NVDAx | 1.544010 USDG → 0.006514984659090923 | 21:12:31 | 21:36:35 | **24 分 04 秒** | `0x0000000000000000000000000000000000000000000000000000000000000000` |
| SPCXx | 1.379240 USDG → 0.008155176708988511 | 21:44:00 | 22:09:13 | **25 分 13 秒** | `0x2e6131de…` |

到账后两条路线立刻解锁：**两个币的 `bridge_unmeasured_symbol` 在下一轮扫描里全部消失**，
只剩 `below_threshold` / `below_net`。币留在我们自己的 Solana 钱包（`probed` + `dest_wallet`），
由下一笔交易一起充进 Kraken。

### 这次跑出来的三个坑

| 坑 | 现象 | 处理 |
|---|---|---|
| `nohup` 不够 | 探针被命令会话连进程组一起杀掉，留下空锁 | 用 `os.setsid()` + `os.execv`；清锁前核「进程不在 + 链上净值未变 + 账本无新行」 |
| 结束位置写错 | `held_at` 写成 `in_bridge`（桥的**中途**最坏情况） | 执行器新增 `final_held_at` |
| **OKB 价刷不出来** | 聚合器报价**按余额放行**；钱包只剩 0.000308 WOKB 时 `WOKB->USDG` 被拦成 `no_route`，OKB 价过期 → `x-layer->solana` 变 `cost_unknown` → 第 5 家族**静默失去定价能力** | 加反方向备用来源 `USDG->WOKB`（同一池子、我们能付的一侧），同样的双尺寸 + 标签一致性校验 |
| 纸面行抹掉真实持仓 | `open_positions` 把纸面 `refused` 当成「该符号的最新一句话」，于是 NVDAx 从持仓里消失（币还在钱包） | 只信 `mode=live` 的行；纸面视图可以通过 `modes=("paper",)` 单独取 |
| 只读最后 50 行 | `recent()` 默认 50 行，而一轮纸面就写 ~60 行 → `probed` 行掉出窗口 | 位置问题扫全账本（`latest_by_symbol`），不再读尾巴 |

### 上线后的实测（2026-10-07 20:55 重启四进程后）

| 看的地方 | 改动前 | 改动后（实测） |
|---|---|---|
| 扫描行 `reason`（NVDAx / x-layer） | `bridge_unmeasured_symbol` | `below_threshold`（blockers 仍是 `['bridge_unmeasured_symbol','below_threshold']`） |
| 纸面「最近判定」表（NVDAx / SPCXx / TSLAx，x-layer） | 「这个币在这条桥上还没有到账样本」 | 「价差没过门槛（门槛随开市/休市变）」 |
| 纸面 `states` | `refused:bridge_unmeasured_symbol: 3` | `refused:below_threshold: 3`（当前 3 条 x-layer 行全是负价差） |
| 看板新栏「自动小样本」 | 不存在 | 已渲染；当前显示「现在没有『只差一个样本』的行」 |
| 实盘状态 `probe_candidates` | 不存在该字段 | `[]`，`broadcast: false`，`executed: []`（候选仍全部被拒） |
