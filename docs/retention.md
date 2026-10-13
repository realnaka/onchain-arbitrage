# 监控库留存策略（2026-09-17）

Operator 指示：「这个库太大了，统计之后就不要保存这么多数据，存不下」。

本文说明 `data/hk_monitor.db` 现在怎么被限住、删掉的是什么、留下的是什么，以及
怎么做一次真正回收磁盘的维护。

## 1. 为什么会长到 4.36 GB

2026-09-12 17:52 → 2026-09-17 15:06（4.9 天）的实测分解：

| kind | 行数 | 合计 | 单条平均 | 读回方 |
|---|---|---|---|---|
| `instrument_discovery` | 1,581 | 1,555 MB | 1,007 KB | **没有** |
| `run_summary` | 5,010 | 1,388 MB | 284 KB | 只读最新一条 |
| `perp_book` | 96,263 | 316 MB | 3.4 KB | 看板/评估 |
| `spot_quote` | 78,773 | 209 MB | 2.7 KB | 看板/评估 |
| `basis_evaluation` | 197,971 | 108 KB→MB | 0.6 KB | 场馆统计 |
| `funding` | 96,107 | 102 MB | 1.1 KB | 成本模型 |

两个大户占了 68%：

* `instrument_discovery` 是各交易所的**原始元数据转储**（OKX 一次约 0.5 MB，
  Gate 约 1.3 MB），每个 metadata 刷新周期写一遍，而**代码里没有任何地方读它**。
  它只是「当时的挂牌清单」这类证据，不需要每个周期都留一份。
* `run_summary` 是每轮扫描的完整快照（含 `evaluations`/`instruments`/`probes`/
  `token_references`），但看板、`threshold_report`、`verify_hk_allowlist` 都只读
  **最新一条**；历史只在 `scripts/cost_calibration.py` 里被按窗口回读。

另外两个放大器：`latest_summary()` 用的是 `ORDER BY id DESC LIMIT 1`，而
`monitor_events` 只有 `(kind, recv_ts_ms)` 索引，查询计划里出现
`USE TEMP B-TREE FOR ORDER BY`，实测一次「取最新一行」要 5.5 秒；表本身 4.4 GB，
冷读更慢。

## 2. 现在的处理方式

### 2.1 写入侧（不再产生这么多数据）

* `instrument_discovery` 只在**内容真的变了**时才落库（按规范化 JSON 的 SHA-256
  比对），另外每 24 小时留一条心跳，证明扫描仍在跑。Gate 这种每次 payload 都变的
  交易所省不下来，所以还有下一步的按行数上限兜底。
* 其余 kind 的写入路径没有改动：行情证据该记的照记。

### 2.2 索引（查询不再扫全表）

`EventStore` 现在同时建 `monitor_event_kind_id(kind, id)`，取最新一行从
`TEMP B-TREE` 变成纯索引扫描；「保留最新 N 行」的裁剪也走这条索引。

### 2.3 删除前先统计

`arb_monitor/ops/retention.py` 在删除某些行之前，先把它们折进两张汇总表（同一个
事务，不会「删了但没统计」）：

* `event_daily_stats(day, kind, rows, bytes, first_recv_ts_ms, last_recv_ts_ms,
  stats_json)` —— 每个类别每天的行数/字节/时间范围，外加分类聚合：
  `run_summary` 保留 `cycles_with_candidates` / `max_candidates` /
  `degraded_cycles`；`basis_evaluation` 保留可定价行数、过 20 bps 行数、最高
  入场基差；`source_error` 保留错误行数。
* `venue_daily_stats(day, venue, asset, instrument, phase, rows, priced,
  opportunities, basis_sum, max_entry_basis_bps)` —— 「哪个场馆机会最多」这个问题
  的长期底料，按天×场馆×标的×合约×时段聚合，用与
  `ops/venue_scoreboard.py` 相同的 20 bps 门槛（`DEFAULT_MIN_GROSS_BPS`）。

`day` 用北京时间切分，和操作者看盘的日子一致；否则一个 UTC 日会把每个港股交易
时段劈成两半。

### 2.4 保留期（`config/hk_monitor.toml [retention]`，2026-09-28 改）

Operator：只保存最近 48 小时；超过 48 小时的，只保留曾经产生过信号的数据，
没有产生过信号的直接删除。

| 行 | 超过 48 小时 |
|---|---|
| `alert` | **留下**。这是发出去的信号 |
| `basis_evaluation` 且 `status = conditional_signal` | **留下**。这是策略打出的条件信号 |
| 其余全部 kind（盘口、报价、资金费、`run_summary`、发现转储、非信号评估、错误） | 先记入日统计，再删除 |
| `opportunities` / `manual_executions` | 不在事件表里，不剪。意图本身就是信号 |

48 小时以内的原始行全部留下，看板和默认 24 小时场馆统计不受影响。
`raw_hours` 生效时，不再用按 kind 的天数或行数上限把这 48 小时切短。

监控进程在每个采样周期结束之后检查一次：上一轮裁剪已经结束、并且距上次启动
已超过 `interval_seconds`（默认 1 小时），就在旁边的线程里跑，不占采样线程。
手工命令仍然是 `arb-monitor db-retention`。

### 2.5 回收磁盘

删除只把页还给 SQLite 的 freelist，文件大小不会自己变。两条路径：

```bash
scripts/db-maintain.sh --dry-run   # 只打印计划
scripts/db-maintain.sh             # 在线删除（监控不用停）
scripts/db-maintain.sh --vacuum    # 删除 + 停监控/看板 + 重写文件 + 重启
```

`--vacuum` 会：检查剩余空间（需要 2× 文件大小）→ 停看板与监控（用现成的
`board-ctl.sh` / `hk-monitor-ctl.sh`，退出时无条件重启）→ `VACUUM INTO` 生成新库
→ 自检 `quick_check` → 把旧库改名成 `data/hk_monitor.db.pre-prune-<时间戳>` 作为
备份 → 换上新库。新库带 `auto_vacuum=INCREMENTAL`。

重写之后就不必每次都重写了：带 incremental auto-vacuum 的库，普通的
`scripts/db-maintain.sh` 在删完行之后会自动把空闲页还给文件系统
（`PRAGMA incremental_vacuum` 循环 + 一次 `wal_checkpoint(TRUNCATE)`）。
实测这个 SQLite（3.51/Homebrew）一次 `incremental_vacuum` 只还一页，所以循环调用；
5,000 页约 0.15 秒（WAL）/ 0.97 秒（rollback 日志），有 20 万次上限兜底。
如果哪天的库又因为别的原因涨起来（例如新增了高写入量的 kind），再跑一次
`--vacuum` 即可。

旧库备份在核对后可以删除：

```bash
rm data/hk_monitor.db.pre-prune-*
```

## 3. 代价（明确写出来）

* 超出 48 小时的**逐轮细节**没有了，除非那一行本身是信号（`alert` 或
  `conditional_signal` 评估）。`scripts/cost_calibration.py` 只能在 48 小时内
  重跑；`venue-scoreboard` 的默认 24 小时窗口不受影响，但更长的窗口要用
  `venue_daily_stats` 的日汇总来回答，而不是原始行。信号行不进这次删除，所以
  也不会被重复记进日统计。
* `event_daily_stats.stats_json` 里的极值是**跨批次合并**的结果（计数器相加、
  极值取最大），和「一次性重算整段窗口」的结果一致，但不保留分布（例如中位数）。
* `instrument_discovery` 的不变 payload 不再重复落库；如果哪天有代码要按周期
  比对元数据，心跳间隔（24 小时）就是它的粒度下限。

## 4. 验证

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_retention -v
```

测试分别钉住：统计先于删除、行数上限不会吃掉最新行、dry-run 不改动任何东西、
场馆统计在原始行删除后仍然成立、重写在有写入者时拒绝执行、重写后 `quick_check`
与数据完整。

## 5. 事故记录：第一次重写把监控写进了备份（2026-09-17 15:18）

第一次 `--vacuum` 的重写期间出过一次真事故，记在这里：

* `scripts/run_hk_monitor.sh` 是**带重启的 supervisor**：监控进程退出 10 秒后它
  会再拉起一个。`hk-monitor-ctl.sh stop` 同时 kill 监控和 supervisor，但监控先死
  的那一瞬间 supervisor 已经拉起了新进程（15:18:01），新进程抢在文件改名
  （15:18:12）之前打开了旧 inode。
* 结果：监控照常跑、日志照常写，但它写的是已经改名为
  `hk_monitor.db.pre-prune-20260917-151812` 的旧库；线上库停在 15:17:00，
  看起来像"监控自己停了"。`lsof data/hk_monitor.db` 是空的，那条命令是识别它的
  最快方式。
* 修复（两道）：`scripts/db-maintain.sh` 的 `stop_quietly()` 会 kill 一轮、等待
  进程树真的消失（最多 15 秒，期间反复 kill -9）；`compact()` 在改名**之前**
  再查一次 `_in_use()`，发现被重新打开就放弃重写并报错，而不是继续搬文件。
* 恢复：停掉孤儿监控 → 把旧库里 `recv_ts_ms` 大于线上库最大时间戳的 305 行
  `INSERT` 回线上库 → 重启监控。15:17:00 之后的数据一行没丢。

教训：**"停掉服务"不是发一次信号**，而是要等到进程树真的消失，尤其是外面还套着
一层会自动重启的 supervisor。
