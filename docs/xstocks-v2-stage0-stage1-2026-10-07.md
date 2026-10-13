# xStocks v2 阶段 0 + 阶段 1：落地记录（2026-10-07）

编码规格：`docs/xstocks-any-to-any-v2-plan-2026-10-07.md`（第 1–12 节）。
本文记录本轮实际写下的代码、实测数字和发现的缺口。

**没有改任何 `mode` / `auto_orders` / `live_ack`，没有创建或删除停机文件，没有签名。**
`ops/xstocks_v2_policy.toml` 仍是 `phase = "scan"`、`auto_orders = "off"`。

## 1. v1 全部停机（operator 指示）

停机前核对：两条自动链 `positions = {}`、`in_flight = null`，Kraken 无持仓。
停机前现场快照在 `data/logs/v2-baseline-20261007/`。

| 停掉的进程 | PID（停机前） |
|---|---|
| hk-monitor | —（本轮未运行） |
| board（8765） | —（已暂停） |
| account-board（8766） | 2743 |
| xstocks-gap（8770） | 99431 |
| xstocks-v4 watch | 96057 |
| xstocks-auto（Solana 自动链） | 25993 |
| xstocks-eth-auto（ETH 自动链） | 12365 |
| xstocks-book | —（未运行） |

核验（沙箱内 `pgrep` 不可用，须提权）：

```bash
pgrep -fl "hk_monitor|board.py|account_dashboard|xstocks_gap_live|xstocks_v4_watch|xstocks_auto_live|xstocks_eth_auto_live|xstocks_book"
```

恢复 v1 只需三个 ctl 脚本 `start`。`data/AUTO_HALT`、`data/KILL_SWITCH` 未被动过。

## 2. git 基线

| 项 | 值 |
|---|---|
| 仓库 | `git init -b main`（本地，无远程） |
| 基线提交 | `26ec315` |
| tag | `v1-baseline-2026-10-07` |
| 工作分支 | `feat/any-to-any` |
| 入库规模 | 363 文件 / 7.5 MB |
| 未入库 | `config/*.env`、`data/`、`data-v2/`、`.venv/`、`.playwright-mcp/` |

**沙箱把仓库内的 `.git` 设为只读**，所以 `git init` / `add` / `commit` / `tag` 都必须提权执行。

### 2.1 与批准计划的偏差：没有建 `../arb-v2` worktree

沙箱只允许写 `/home/operator/Documents/arb` 之内。仓库外写入被内核拒绝（实测
`mkdir /home/operator/Documents/arb-v2-writetest` → `Operation not permitted`）。
若坚持 `../arb-v2`，之后每一个文件写入都要逐次提权，长任务的迭代会被打断。

改为：**原地 git 分支 + v2 独立运行数据目录 `data-v2/`**（已进 `.gitignore`）。
隔离目的不变：

* v2 从不写 v1 的 `data/`；`xstocks_book.sqlite` 用 `mode=ro` 打开（`feeds/census.py`）。
* v2 的锁、HALT、账本、扫描快照全在 `data-v2/`。
* v1 目录里的代码因为 v1 已停，不存在"运行中被换掉"的风险。

## 3. 本轮代码

| 模块 | 职责 |
|---|---|
| `xstocks_v2/feeds/http.py` | 共用限速器（锁只在记账时持有，不覆盖在飞请求）+ 429 全局暂停 + 代理 opener |
| `xstocks_v2/feeds/kraken.py` | 一次公开 `Ticker` 取回全部 xStock 的**买一和卖一**；`Depth` 逗号分批 |
| `xstocks_v2/feeds/jupiter.py` | `price/v3` 批量（50/次，只排序）+ `swap/v1/quote` 双向真报价 |
| `xstocks_v2/feeds/evm.py` | v3 QuoterV2 / v4 Quoter 构造器、JSON-RPC 批量、`tryAggregate` 编解码、`eth_gasPrice` |
| `xstocks_v2/feeds/census.py` | 离线宇宙：普查池 + v4 watch（重建 PoolKey）+ 发行登记 |
| `xstocks_v2/spot_venue.py` | `Quote` 记录（两侧时间戳、来源、深度、费用档）+ `SpotVenue` 接口 |
| `xstocks_v2/scan.py` | 两阶段扫描、按批配对、`blockers` 与 `executable` |
| `xstocks_v2/ledger.py` | append-only JSONL 账本；`Decimal` 在建行时就转字符串 |
| `xstocks_v2/paper.py` | 纸面判定与 `chain → Kraken` 成本模型 |
| `xstocks_v2/legs_kraken.py` | 卖出腿状态解析（下单后 2 分钟窗口、尘埃不算入账） |
| `xstocks_v2/cycle.py` | 补 `v1_busy()` 检测 + v2 自己的锁/HALT 路径 |
| `scripts/xstocks_v2_scan.py` | `status` / `once` / `once --full-bilateral` / `run --interval` |
| `scripts/xstocks_v2_paper.py` | `status` / `once` / `run` |
| `scripts/xstocks-v2-ctl.sh` | `once` / `full` / `paper-once` / `scan` / `paper` / `status` / `stop` |

新增配置（`ops/xstocks_v2_policy.toml`）：

```toml
[feeds]
jupiter_rps = "0.9"; jsonrpc_batch_max = 10; full_scan_timeout_s = 900
[costs]
kraken_taker_bps = "10"; slippage_buffer_bps = "10"
sol_transfer_first = "0.00313"; sol_transfer_repeat = "0.000015"
evm_gas_units = "441831"
max_depth_share = "0.2"; venue_round_trip_max_bps = "150"
```

## 4. 实测

| 项 | 数值 |
|---|---|
| 常驻一轮（两阶段，深报上限 20） | **69 秒**（配对批大小 4） |
| 一次性全量双边（170 mint × 买/卖） | **448 秒（7.5 分钟）**，Jupiter 285 次 / Kraken 45 次 / 429 一次 |
| 全量结果 | 303 行：204 行 `below_depth`、38 行 `stale_or_skew`、3 行 `no_dest_pool` |
| 两阶段结果 | 57 个真报价；可用行 96（solana 70 / x-layer 20 / eth 6） |
| Kraken 侧成本 | 一轮 1–7 次公开调用（一次 Ticker 覆盖 170 个标的） |

结论：**两阶段漏斗能把一轮压进 60 秒量级，全量真双边不行（7.5 分钟）**。
常驻循环因此用两阶段；全量计时只作为一次性标定。

## 5. 本轮发现的四个缺口（都已进闸门）

### 5.1 薄池会伪造两个方向的假价差

首轮扫描把 BSC 上一个 **29 USD** 的 MSTRx 池算成 **+4697 bps**，把 Solana 的
KLACx 算成 **+285%**。机理：同一口薄池把买入价推高、把卖出价压低，于是
`chain → Kraken` 和 `Kraken → chain` 两个方向**同时**看起来有机可图。

修法（`scan._blockers`）：

* `depth_usd < 名义 / max_depth_share` → `below_depth`
* 同一口池的**买入价 → 卖出价往返损耗 > 150 bps** → `below_depth`

修正后 204/303 行被拦下。教训 5.3、5.4、5.5 在 v2 里第一次有了可执行形式。

> **2026-10-08 更正（operator 选 A）**：第一条口径已废。`深度 < 名义/0.2`（= 200/0.2 =
> $1000）是拿池子去凑票面；现在的规则是**票面按池子定**——
> `有效票面 = min(per_trade_quote_usdt, 池深 × max_depth_share)`，报价就按这一档问，
> 闸门是 `有效票面 < min_trade_quote_usdt`（25 USDT）。同一口池的往返损耗仍 > 150 bps 即拒，
> 但现在是在**这个尺寸**上量。实测：x-layer 有五口池深 $313k–$433k 被判 `below_depth`，
> 其中三口是单边池（往返 5400–10000 bps），**深度门槛从来不是它们的原因**。

### 5.2 同时刻才叫一对报价

第一版用一轮**开始时**的 Kraken 快照去配一轮**结束时**的链上报价。103 秒的轮次
让**每一行**都带 `stale_or_skew`——不是因为市场有问题，而是配对方式错了。

修法：按批报价（`pair_batch = max_pair_skew_ms/2000 × jupiter_rps` ≈ 4 个），
每批报完**立刻**刷新 Kraken 再配对。skew 回到 10 秒内，stale 行从 14 降到 2–3。

### 5.3 净利公式必须减掉本金

第一版 `chain_to_kraken_net` 只减手续费，不减 25 USDT 本金，于是任何一行都显示
净赚约等于名义金额。已修，并加测试锁「1% 的价差不可能是 24 USDT」。

### 5.4 Kraken 充值方法是按币种登记的，桥梁不是可选项

| 币 | Kraken 充值方法 |
|---|---|
| TSLAx / NVDAx | `- Ethereum`、`- TON`（**没有 Solana**） |
| STRCx | `- Ethereum`、`- Solana` |
| AAOIx | `- Solana` |

所以「Solana 买入 → 充 Kraken」对多数美股名字走不通，必须 Solana→Ethereum 桥；
桥没有到账样本 → `unmeasured_route`（fail closed）。这解释了为什么纸面第一轮
两条候选（TSLAx +12.4 bps、NVDAx +4.6 bps）都被拒。

`data/kraken_exit_cache.json` 生成于 **2026-10-05 12:06（48 小时前）**，
执行前必须刷新（v1 的 `scripts/kraken_deposit_refresh.py`）。

### 5.5 复价必须同时刷两条腿

纸面第一版只重取 Kraken 盘口，买入腿仍用扫描快照里的价（可到 70 秒前）。结果是
**每一行**都被判 `stale_or_skew`——结论正确但毫无信息量。

修法：`scan.requote_buy()` 对候选逐条重取买入腿（Solana 走 Jupiter 双向，
EVM 走配额器），紧跟着取该币的 Kraken `Depth`，再用两个**新鲜**时间戳算
`pair_skew_ms`。扫描快照里的 `stale_or_skew` 不再继承——它描述的是扫描自己的配对。
实测：纸面一轮 60 秒，候选从「全 stale」变成给出真实拒因（`unmeasured_route`）。

## 6. 当下没有过门槛的机会

| 路径族 | 最好一行 | 门槛 |
|---|---|---|
| 链 → Kraken | TSLAx / Solana **+12.4 bps** | 20 bps |
| Kraken → 链 | SPYx / X Layer **+50.3 bps** | 但要先把币提回 X Layer（阶段 4） |

这与 v1 看板同期的「没有候选」一致：现在的价差本来就不够。

## 7. 测试

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_xstocks_v2 tests.test_xstocks_v2_lessons \
  tests.test_xstocks_v2_feeds tests.test_xstocks_v2_paper
```

121 个 hermetic 测试（原 48 + 新 73）。全部不碰网络、不碰 CLI、不碰停机文件。
新增用例锁住：双边解析、缺失一侧不是 0、限速器在飞 ≥2 与 429 全局暂停、
JSON-RPC 批量上限、坏行不拖垮整批、CCIP 三种方言、v4 PoolKey 重建、
同一口池拒绝配对、薄池双向假价差、按批配对的新鲜度、本金必须进净利、
纸面 `false_t0`、卖出后 2 分钟窗口、尘埃不算入账、`v1_busy` 检测。

### 既有失败（与 v2 无关）

全仓 1228 个测试跑下来有 **18 failures + 6 errors**，全部落在本轮没有改过的
v1 模块（`test_maker_first` / `test_full_chain_live` / `test_hk_monitor`）。
用基线 tag 导出到 `/tmp/arb-baseline` 复跑对比，`test_maker_first` 在两棵树里
都是 **16 个测试 / 14 失败 / 1 错误**，数字完全相同。判定为 v1 既有测试债，
不是本轮引入（最可能的原因是 2026-09-23「只留 X Layer 现货 + OKX 永续」那次
场馆范围变更没有同步改测试）。

## 8. 下一步（等 operator）

1. 纸面 `scripts/xstocks-v2-ctl.sh paper 180` 常驻 ≥3 天，收集候选数、复价后净利
   分布、`false_t0` 计数。
2. 纸面分布出来之后再决定 `phase`/`auto_orders`（operator 专属）。
3. 恢复 v1：三个 ctl 脚本 `start`。

## 9. 看板（8771）与心跳

```bash
scripts/xstocks-v2-ctl.sh paper 180    # 纸面常驻
scripts/xstocks-v2-ctl.sh board 8771   # 只读看板
scripts/xstocks-v2-ctl.sh health 180   # 心跳自检，异常退出码非 0
```

看板只读 `data-v2/*.json`，每 30 秒刷新。页面从上到下回答四件事：

1. **心跳**：扫描与纸面进程还在不在（教训 6.3 —— v1 曾经静默停了 6 小时没人知道）。
   没文件、或超过 `3 × 间隔` 没更新，顶部横幅变黄/红。
2. **路径族**：每族的行数、**可信行数**、**可信最好价差**、主要拒因。
   「可信」= 只带 `phase_scan` / `chain_out_of_phase` 这类信息性标记的行。
   薄池、陈旧、没桥样本的行**不进**这个数——否则 29 USD 的池子会把标题数字
   写成 +4697 bps。
3. **可信行**表 + **被闸门拦下的最高价差**表。后者故意列出来：拦住的东西要看得见。
4. **纸面判定**与累计账本：每条的状态、复价后净利、`false_t0` 计数。

开机重启后需要手动拉起（v1 有 `scripts/post-reboot-restore.sh`，v2 暂时没有）：

```bash
scripts/xstocks-v2-ctl.sh paper 180 && scripts/xstocks-v2-ctl.sh board 8771
```

## 10. 教训回归的显式对照表

`tests/test_xstocks_v2_lessons_map.py` 把 `docs/lessons/recent-mistakes-2026-09-24-to-10-06.md`
里的 **43 条**逐条映射到具体用例（`LESSON_TESTS`）。控制台里另有 3 条标注为
`NOT_APPLICABLE` 并写清理由（2.4 资金费 / 3.10 平腿顺序 / 6.5 磁盘），6.4 日志轮转
落在 ctl 脚本里。

这张表由两个测试自己守着：

* 43 条里任何一条既没映射也没理由 → 测试失败（防止以后新增教训被忽略）；
* 表里引用的每个用例 id 必须真实存在且只匹配 1 个用例 → 改名或删除会立刻失败。

本轮为补缺口新加的闸门：`no_address_book`（Kraken→链没有提币地址簿条目，教训 3.2）、
严格 EVM 地址校验（`0x` 和 `0xdead` 都不是地址）、心跳自检（6.3）、启动时日志轮转（6.4）。
