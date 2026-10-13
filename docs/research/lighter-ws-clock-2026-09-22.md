# Lighter 的场所时钟：用它的 WebSocket 订单簿频道（2026-09-22）

> 触发（operator）："Lighter 的 WebSocket order_book 频道每条推送自带
> timestamp，接 WS 就能拿到真实场所时钟。按这个方案修代码。"
>
> 结论：**已实现**（`data/lighter_stream.py`）。Lighter 从"94.5% 的行判为陈旧"
> 变成"TENCENT 97.2% / XIAOMI 94.4% 的采样点时钟在 15 秒预算内"。档位来源
> **没有换**（仍是 REST 全量快照）——原因是实测发现 WS 帧除第一帧外都是增量，
> 见 §3。

## 1. 问题：REST 盘口完全没有时间字段

`GET /api/v1/orderBookOrders` 的 OpenAPI 定义里只有
`code / message / total_asks / asks / total_bids / bids`，**没有** `ts`、`offset`、
`nonce`（2026-09-22 读 apidocs.lighter.xyz/reference/orderbookorders）。

改造前 `CexPublicAdapter.book()` 的做法是再调一次
`publicGetRecentTrades(market_id, limit=1)`，拿最近一笔**成交**时间当场所时钟。
那回答的是另一个问题：盘口一直在被重新报价、但没人成交的市场，会被判为陈旧。
代价有实测记录：54 小时窗口里 **94.5%** 的 lighter 行带陈旧标记
（`docs/venue-selection-2026-09-20.md` §1.3），venue 级候选率 67.1% 其实只建立在
164 次可评估行上。

## 2. 修法：WS 提供时钟，REST 提供档位

场所自己的推送格式（apidocs.lighter.xyz/docs/websocket-reference）：

```
wss://mainnet.zklighter.elliot.ai/stream
→ {"type":"subscribe","channel":"order_book/{market_id}"}
← {"type":"update/order_book","channel":"order_book:201",
   "timestamp":1774884082326,                    ← 场所 ms
   "order_book":{"bids":[...],"asks":[...],
                 "last_updated_at":1774884082309144,  ← 场所 µs
                 "offset":..., "nonce":..., "begin_nonce":...}}
```

实现（`src/arb_monitor/data/lighter_stream.py`）：

- 一个守护线程、一条连接，订阅我们**从交易所目录里读到的** market id（不写死）；
- 60 秒一次 `{"type":"ping"}`（场所要求 2 分钟内必须有客户端帧）；
- 断线按 2/5/15/30/60 秒退避重连；只读，永不发送任何交易；
- 每个市场保存"最后一次变更的场所时钟 + nonce/begin_nonce + 该帧的档位数"；
- `CexPublicAdapter.book()` 在**发 REST 请求之前**读一次该市场的场所时钟，把它作为
  这次 REST 快照的 `venue_ts`；读不到就退回原来的"最近成交"锚点。

两个刻意的设计选择：

1. **档位不从 WS 取**。见 §3。
2. **时钟在 REST 请求之前读**。如果请求期间场所又改了盘口，那次变更**不可能**被
   算进我们这次的档位里，因此也不能被算进我们这次的时间戳——时间戳只会更旧，
   不会更新（fail closed 的方向）。

## 3. 为什么不用 WS 的档位：它除第一帧外都是增量

场所文档原文（apidocs.lighter.xyz/docs/websocket-reference，2026-09-22 读）：

> "The order book channel sends the new ask and bid orders for the given market in
> batches, every 50ms. … **this channel sends a complete snapshot on
> subscription, but only state changes after that.** To verify the continuity of
> the data, you can check that `begin_nonce` on the current update matches the
> `nonce` (i.e. `last_nonce`) of the previous update."

实测也印证了：同一时刻的帧里，TENCENT / XIAOMI 常常只有**单侧、1–3 档**
（下方 §4 的"帧档位"列中位是 2/0、1/0），而 REST 快照是两侧多档。

所以拿 WS 帧当完整盘口是错的。用增量自己拼盘口则要维护一条 nonce 链：**漏一帧
就静默变成错误的盘口** —— 这正是本项目最怕的失败模式（陈旧/错误行情被当成现价）。
因此本轮的分工是：**REST 出档位（全量、每次采样现读），WS 出时钟**。
（第一帧是完整快照这一点记录在 health 里，将来若真需要超过 REST 20 档的深度，
可以基于它单独论证。）

## 4. 现场实测（2026-09-22 12:0x – 12:1x，180 秒窗口）

命令（已入库，只读订阅，经本地代理 4780）：

```bash
PYTHONPATH=src .venv/bin/python scripts/lighter-ws-probe.py --seconds 180
```

```
frames 325  snapshots 4  continuity_breaks 0  connects 1  pongs 2  err None
clock_offset_ms 420          ← 场所时钟比我们快 0.42 秒（中位）

market   <=15s   中位age_ms   p90    max   (帧档位 bid/ask 中位)
201      97.2%         954   5150  17991   2/0
203      94.4%        2416  10960  22107   1/0
204       7.8%       89584 161042 178121   0/0
230      66.5%        9055  25185  35241   3/1
```

读法：

- **201 (TENCENT) / 203 (XIAOMI) 是真被修好的两个**：94–97% 的采样点场所时钟在
  15 秒内，而改造前这两个市场的行几乎全是陈旧。`continuity_breaks 0` 说明 nonce
  链在我们这 3 分钟里是连续的（我们没有漏帧）。
- **230 (SHEIN) 66.5%**，中位 9 秒：偶尔超过 15 秒预算，会被闸门照常拒绝。
- **204 (POPMART) 7.8%**，中位 89 秒，而且 REST 侧经常没有档位 —— 这个市场在
  Lighter 上基本是死的。这条是"场所不活跃"的结论，不是采集缺陷。
- WS 连接的稳定性：连接 1 次、无重连、无错误、2 个 pong（keepalive 生效）。

## 5. 不变量与边界

### 5.1 上线后监控自己的读数（最强证据）

重启后（2026-09-22 12:15:30 起，稳定轮）逐行核对
`perp_book` 的 `venue_ts_ms` 与 `recv_ts_ms`：

```
market  轮数  走WS  回退  空盘  中位age_ms  <=15s
201        2     2     0     0       2318  100.0%
203        2     2     0     0       6249  100.0%
204        2     2     0     2     147608    0.0%
230        2     2     0     0       1220  100.0%
```

同样的两轮，改造前（12:00–12:10）四行 `venue_ts` 分别是
**10:19 / 10:43 / 10:55（一小时以上）**，全部会被判陈旧拒单。现在 201/203/230
是 1–6 秒。204 仍判陈旧，而且它两次的档位都是 0/0 —— 空盘。

启动瞬间的边界：新进程**第一轮**取盘口时 WS 可能还没收到快照，那一轮会退回
"最近成交"锚点（12:14:13 那轮就是这样：1 个市场走了 WS，3 个回退）。这是
fail closed 的方向，而且下一轮就自愈；行里的 `source` 会如实写出用了哪条路。

- 本轮**没有放宽任何闸门**：`max_age_ms` 仍是 15 秒，`risk/hk_basis.py` 一行未改。
  场所没在预算内动过的盘口，照旧判为陈旧并拒单。
- "盘口长时间不变算不算陈旧"是一个**风控口径问题**，不是数据管道问题：本轮把
  时钟换成了正确的那一个，没有改判定。要放宽须单独论证并由 operator 决定。
- clock 只在 REST 读**之前**取；取不到（socket 断、没订阅到该市场、该帧无
  `timestamp` 且无 `last_updated_at`）就退回"最近成交"锚点，也就是回到改造前的
  行为，而不是给一个没有依据的时间。
- 场所时钟比我们快时不会被用来"提前"时间：`venue_ts = min(场所时间, 我们收到帧
  的时间)`，而且 `OrderBook.age_at` 取 `min(venue_ts, recv_ts)`。

## 6. 代码与测试

| 位置 | 内容 |
|---|---|
| `src/arb_monitor/data/lighter_stream.py` | WS 客户端 + 纯状态机（可注入假 socket） |
| `src/arb_monitor/data/hk_cex.py` | `book()` 里 "先取时钟、再读 REST"；`_arb_source` 带来源 |
| `src/arb_monitor/apps/hk_monitor.py` | `_sync_lighter_stream()` 用交易所目录里的 market id 启动；`close()` 停流；每轮摘要写 `lighter_stream` 健康 |
| `config/hk_monitor.toml` | `[streams.lighter]`（enabled / url） |
| `tests/test_lighter_stream.py` | 14 项，全部离线：时钟来源、µs 归一、未来时钟不被采信、nonce 链连续性、增量帧不得变成盘口、REST 回退、重连、close 幂等 |

全量：`711 项通过（2 跳过）`。

## 7. 复跑命令

```bash
cd /home/operator/Documents/arb
.venv/bin/python -m unittest tests.test_lighter_stream -v
.venv/bin/python -m unittest discover -s tests -q

# 看运行中的监控是否真的在用 WS 时钟（需要新进程；改配置/代码后必须重启）
sqlite3 data/hk_monitor.db \
  "select recv_ts_ms, venue_ts_ms, source from monitor_events \
   where kind='perp_book' and venue='lighter' order by id desc limit 5;"
# run_summary 里的 lighter_stream 健康块：
python3 - <<'PY'
import json, sqlite3
row = sqlite3.connect('data/hk_monitor.db').execute(
    "select payload_json from monitor_events where kind='run_summary' "
    "order by id desc limit 1").fetchone()
print(json.dumps(json.loads(row[0])['lighter_stream'], indent=2))
PY
```
