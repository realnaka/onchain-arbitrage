# xStocks 全链目录与 Kraken 折价本（2026-09-26）

这套东西和正在跑的基差监控不是同一个进程，也不是同一个库。

- 库：`data/xstocks_book.sqlite`
- 代码：`src/arb_monitor/xstocks_book/`，只由 `scripts/xstocks_book.py` 启动
- 不 import `hk_monitor`，也不改 `config/hk_monitor.toml`、`xstocks_registry.py`、额度、`live_ack`、停机开关
- `scripts/db-maintain.sh` 只剪 `data/hk_monitor.db`，不会动这个文件

本阶段退出场所只有 Kraken 现货。其他交易所的现货和合约没有请求。

## 股权关系（公开资料，不是本仓库实测）

Kraken（Payward）收购的是 **Backed**，不是 Bakkt。查不到 Bakkt 与 xStocks 的股权关系。

- 发行方是 Backed Finance AG，泽西发行主体是 Backed Assets (JE) Limited。xStocks 在 2025 年 6 月作为 Kraken 与 Backed 的合作产品上线。
- 2025-12-02 Kraken 宣布同意收购 Backed Finance AG。[Payward 新闻稿](https://www.payward.com/press-release/kraken-acquires-backed-xstocks-expansion)、[BusinessWire](https://www.businesswire.com/news/home/20251202666578/en/Kraken-to-Acquire-Backed-Accelerating-xStocks-Expansion-and-Unlocking-Global-Demand-for-Tokenized-Equities)。
- 泽西律师所 Walkers 与 Jersey Finance 用完成时态写这笔收购，并写明包括 Backed Assets (JE) Limited。Jones Day 2026 年 1 月的客户是 Payward, Inc.。对价和交割日新闻稿没有写，交割日仍是待确认。

所以 Kraken 的现货名单比其他分销所宽。其他所这次不采集。

## 2026-09-26 目录同步（本机代理 `127.0.0.1:4780`）

```bash
PYTHONPATH=src .venv/bin/python scripts/xstocks_book.py refresh --pause 0.2
```

| 对象 | 数量 |
|---|---|
| 发行方资产 | 1124 |
| 部署行（链 × 地址 × 原生或 wrapper） | 19298 |
| 公开桥 | 9（都标 Chainlink CCIP） |
| Kraken `tokenized_asset` 交易对 | 177（接口返回 354 行，每个交易对有一把内部键和一把 altname，状态相同，库里只留一把） |
| 其中 `status=online` | 12：AAPLx、CRCLx、GLDx、GOOGLx、HOODx、MSTRx、NVDAx、QQQx、SKHYx、SPCXx、SPYx、TSLAx，报价都是 ZUSD |
| 其中 `status=post_only` | 165 |

`post_only` 的盘口不能当成可以吃进去的卖出。采样时这种交易对即使有买一，质量也写成 `post_only`，不打 50 bps 筛选。

部署覆盖的网络：Ethereum、BNB Smart Chain、XLayer、Ink、Optimism、Mantle、Arbitrum、HyperEVM、Solana、TON、Tron。原生和 wrapper 分行。EVM 上同一个地址出现在多条链，只说明部署地址相同，不是同一笔余额。HyperEVM 的 wrapper 明显少于原生（217 对 1096），Tron 只有 74 个原生、没有 wrapper。

部署对象本身没有 `chainId`。链 id 只从桥列表回填到同名网络；TON、Tron 不在这 9 座桥里，链 id 留空。原子兑换字段是发行方的 `supportsAtomicSwaps`。

公开的 Kraken `Assets?aclass=tokenized_asset` 没有给出充值网络。`deposit_networks` 全部是 `unknown`。充哪条链能进 Kraken，仍待确认，不凭文档猜。

资产列表 `GET /public/assets` 里没有每股比例。比例在另一个公开接口：`GET /api/v2/public/assets/{符号}/multiplier?network=`。官方说明见 [docs.xstocks.fi/developers/multipliers](https://docs.xstocks.fi/developers/multipliers)：新币从 1 枚 = 1 股起，分红再投资、拆股、合股之后改 multiplier。2026-09-26 抽查 AAPLx，Ethereum 与 Solana 都是 `1.0032690125398187`；XIAOx 在 XLayer 是 `1`。采样程序还没读这个接口，所以库里的 `shares_per_token` 仍是空的，50 bps 筛选也就还不会打上。目录 Excel 里已经按这个接口填了当前 multiplier。

## 价格采样

只扫 Kraken 已对上发行方符号的名字。正股用 Yahoo 日线：常规交易时段用 `regularMarketPrice`，时段之外用最后一根日线收盘。Yahoo 没有给出时段时，会话记 `calendar_unknown`，昨收照写，不打筛选。港股代码补成 4 位，价格用 `HKD=X` 除成美元。

各链参考价来自 GeckoTerminal，原生和 wrapper 分开。没有池、储备为 0，都记「无池」。24 小时成交量为 0 记 `stale`。这是参考价，行上 `not_executable=true`，不是可下单价格。

折价（只有份额比已知时才算）：

```text
discount_bps = (正股美元/股 − 代币美元/股) / 正股美元/股 × 10000
```

正数表示代币更便宜。要同时满足：份额比已知、会话不是 `calendar_unknown`、链上参考价质量为 `ok`、Kraken 交易对 `online`、买盘数量大于 0、折价至少 50 bps，才打 `screen_hit`。

同一天对 `AAPLx` 做了一次采样（美国周六，正股休市）。Yahoo 上一根收盘 **341.07** 美元。Kraken `AAPLxUSD` 是 `online`，买一 **340.81 × 1.46705**，买盘在 mid 下方 50 bps 内的名义约 **24124.63** 美元。`screen_hit` 为 0，因为份额比是空的，不是因为价差不够。

有价的参考（GeckoTerminal，不可执行）：Solana 原生 340.86，X Layer wrapper 341.81，Ink wrapper 337.62，Arbitrum 原生 254.16（池子储备约 109 美元）。这四行都是 `share_ratio_unknown`，没有折价数字。BNB 原生 247.79 和 Ethereum 原生 297.61 的 24 小时成交量是 0，记 `stale`。Tron 没有 GeckoTerminal 网络名，记 `gecko_network_unknown`。慢速采样进程没有留在后台。

每天目录：

```bash
PYTHONPATH=src .venv/bin/python scripts/xstocks_book.py refresh --proxy http://127.0.0.1:4780
```

慢速采样（一轮结束到下一轮开始默认隔 15 分钟；本轮没有启动它，避免和正在跑的监控抢代理）：

```bash
scripts/xstocks-book-ctl.sh start
scripts/xstocks-book-ctl.sh status
scripts/xstocks-book-ctl.sh stop
```

看结果：

```bash
PYTHONPATH=src .venv/bin/python scripts/xstocks_book.py report
PYTHONPATH=src .venv/bin/python scripts/xstocks_book.py report --symbol AAPLx
```

单名采样：`scripts/xstocks_book.py sample --once --symbol AAPLx`。

## 明确没做

- 没有重启 `hk-monitor`，没有下单、跨链、赎回、向 Kraken 充提。
- 没有采集 Bybit、Gate、OKX、Bitget、Hyperliquid 或其他所的现货和合约。以后补的是这张库里的新行，不是新库。
- 这个库不进 `db-maintain.sh`。采样一旦长期开着，`observations` 会按 15 分钟累积，留存还没定。
