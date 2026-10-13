# 链上价差看板：换机继续（2026-10-03）

本机已经在跑，不必再换电脑。页面是 `http://127.0.0.1:8770/`，数据接口是
`http://127.0.0.1:8770/api/live`。进程用 `scripts/xstocks-gap-ctl.sh start|stop|status`。
不占用基差看板的 8765，不下单。

2026-10-03 10:05 实测：有 OKX 永续对照价的大约 84 只，一轮 **39 秒**。
对照价变多之后一轮 **85 秒**（104 只）。页面每 2 秒读上一轮结果，打开时不用等新询价，
数据年龄最多是一轮。4 路并行再加快时，代理上的 TLS 开始超时，所以停在这个速度。
基差监控当时仍在跑（最后一条事件落在询价期间）。

规则和前因如下。Canvas 仍是静态快照，要看当前价差打开上面的网页。

## 为什么现在的看板不能接着用

聊天旁边的 Canvas 是一次性快照，文件在

`~/.cursor/projects/Users-operator-Documents-arb/canvases/xstocks-price-gap.canvas.tsx`

Canvas 不能自己访问网络，所以不会自动更新。2026-10-03 前后还出现过打不开：旧的
`xstocks-price-gap.canvas.data.json` 里留着已经删掉的筛选项 `gapFilter` / `feeGap`，
面板一加载就失败。那份状态文件已删，看板改成纯表格后能打开，但它仍然只是
2026-09-30 00:24–00:30（UTC+9）的一张静态表。

## 前因

1. 仓位起点是 X Layer 原生 MRNAx 多头，对冲是 OKX `MRNA-USDT-SWAP` 空头。
   其他 EVM 池没有够用的卖出深度，退出场所定成 Kraken。
2. 官方桥是 Chainlink CCIP，目的地是 Solana，不是以太坊。Kraken 这个账户的
   MRNAx 充值方式只有 `MRNAx - Solana`。
3. 0.02 枚卡在失败的桥消息里，operator 说先不管。0.54 枚已经充进 Kraken，
   账本 `MRNAx.T = 0.540000`，状态曾是 Settled。OKX 空头仍是 −0.56，均价 177.34。
   卖掉之后只平掉实际卖掉的数量，不要按 0.56 平。
4. 2026-09-28 美股开盘后 Kraken 买一过了 0.5% 闸门，但 `AddOrder` 返回
   `ETrade:User Locked`。同一把密钥对 USDT/USD 的 `validate=true` 能通过，
   对 MRNAx 和 AAPLx 都是 `User Locked`。这是账户还没在 App 里开通 xStocks
   条款，不是密钥没有交易权限。卖出停在这里，空头没有平。
5. 同一时期做了一张「链上代币相对股票便宜多少、池子里能买多少」的看板。
   贵的行删掉。后来发现 GeckoTerminal 的代币价会选中没有成交的小池
   （BNB 上 MSTRx 用 1 美元池的 106.95，对照永续约 159），不能当买入价。
6. 正确口径改成：用池子的真实询价（手续费算进去），再和 OKX 永续中间价或正股价比。
   88% 这类高费率池 hook 是空地址，1 美元成交价远高于股票，是陷阱，不是机会。
   字节码使用 `ORIGIN`、或写进报价合约地址的 hook，模拟会偏便宜，不进名单。

## 2026-09-30 那次刷新的结果（静态表，已经过期）

采样时美股在常规交易。有 OKX 股票永续的用买卖中间价，没有的用 Yahoo
`regularMarketPrice`。10 bps 以内不当机会。

仍便宜超过 10 bps 的只有 5 行：

| 代币 | 价差 | 这个价还能买到 | 成交每股 | 对照 |
|---|---|---|---|---|
| NBISx，X Layer wrapper | −247 bps | 1 美元，5 美元已不便宜 | 235.50 | OKX 241.47 |
| Vx，Solana 原生 | −73 bps | 20 美元 | 363.81 | 正股 366.50 |
| AMBRx，Solana 原生 | −38 bps | 5 美元 | 1.833 | 正股 1.84 |
| MCDx，Solana 原生 | −30 bps | 500 美元 | 234.31 | 正股 235.02 |
| SLVx，X Layer wrapper | −12 bps | 20 美元 | 54.82 | 正股 54.88 |

NBISx 的 235.50 和周六同一口池的询价相同，永续已经走到 241.47。价差是冻住的池子放大的，
页面上必须单独标成「报价未变」，不能排成最好的机会。

周六还在表上、这次询价后不再便宜：MSTRx（BNB 0.25%）、CRWVx、LINx、GEVx、ABTx、PEPx、AZNx、DFDVx。
GMEx、GLDx 仍略便宜，但都在 1 bps 以内。这些名字当时在 Kraken 上都是可吃单。

高费率陷阱（1 美元成交价，对照是当时的永续中间价）：Ethereum MSTRx 95% 约 10366 对 153；
Ethereum AAPLx 88% 约 4420 对 332；BNB TSLAx 88% 约 3189 对 354；Ethereum MSTRx 88% 约 1149 对 153；
BNB CRCLx 88% 约 1142 对 85；BNB MSTRx 88% 约 968 对 153；BNB HOODx 88% 约 758 对 115；
Arbitrum AAPLx 50% 约 460 对 332。Ethereum 另一口 TSLAx 88% 那次报价没返回，不要沿用旧的 3366。

## 程序该怎么写

可以、也应该写成硬代码。判断只有几条，都是数字比较：

- 每轮顺序（15:10 起，脚本开头 docstring 同步写着）：
  1. 正股对照：Jupiter Price V3 `stockData.price`，177 只 4 次请求，和询价共用 Jupiter 额度；
     某批失败时沿用上次值最多 15 分钟。
  2. Price V3 没给的才退回 FMP / 雅虎（下面这条旧口径只剩兜底用途）。
  3. 每轮按字母轮换抽 3 只，用雅虎 `includePrePost=true` 的最后一分钟成交独立核对；
     差 > 1% 的行照常显示，但标「对照存疑：雅虎 X，差 Y%」，下次核对一致才清除。
     必须比含盘后的价：`stockData` 是含盘后的最后成交（GME 25.16 = 雅虎盘后最后一笔，
     雅虎 `regularMarketPrice` 是 24.70 的常规收盘），比常规收盘会误报。
  4. Jupiter v1 阶梯询价（1、5、20、100、500 USDC），无路由的跳过到 30 分钟复查；然后 EVM 池。
  5. 只留仍便宜超过 10 bps 的行。
- EVM 池参数（地址、费率、稳定币、精度）解析一次就存进 `data/xstocks_gap_evm_routes.json`，
  以后启动直接读。原因：以前每次启动按 0.4 秒间隔连查 4 次 GeckoTerminal，429 就当「没有池」，
  且只查一次，所以 4 条里常只剩 1 条（14:41 那次就是）。GeckoTerminal 免费接口按出口 IP 限流，
  `hk-monitor` 也在同一出口大量请求它。没存过的池子每轮只补查一条，失败写日志、下一轮再试。
- 对照价（旧口径，现仅兜底）：正股价格。有 `config/fmp.env` 里的 `FMP_API_KEY` 时，付费档走 FMP
  `/stable/batch-quote`（一次拉全表，6 分钟刷新一次）。2026-10-03 实测免费档
  这个接口返回 HTTP 402，单只 `/stable/quote?symbol=` 返回 200。免费档因此
  每轮拉 12 只，每只保留约 18 小时，避免一天 250 次额度被打光。没有 key 时用雅虎
  `regularMarketPrice`，每轮补 20 个。不用 OKX 永续。Kraken `AAPLx/USD`
  是代币自己的盘口，空簿不能当正股，不拿来做对照。
- 成交价：按金额阶梯 1、5、20、100、500 美元做买入询价。Solana 走 Jupiter
  （USDC → 该 mint）。EVM v3 走各链 QuoterV2。Uniswap v4 走官方 V4Quoter，
  池子密钥从官方 PositionManager 读，hook 的 `beforeSwap` 会在这笔询价里执行。
- Jupiter 限频（2026-10-03 查明，之前页面上「询价失败 99～177 个」的根因）：
  `lite-api.jup.ag` 不公布限额、正在退役。实测一个窗口放行约 130 次就开始 429，
  429 由 CloudFront 直接返回、不带限额头。旧代码 4 路并发、429 后 1.5 秒重试，
  速率一直压不下来，于是每轮 177/177 全失败；停发约 60 秒就恢复，出口 IP 没被封。
  现在所有询价线程共用一个限速器（`Pacer`）：按 `JUPITER_RPS` 的 90% 匀速发，
  429 时全体暂停到 `x-ratelimit-reset`（没有这个头就停 60 秒）并减半速率，
  连续 30 次成功后回升。`config/jupiter.env` 有 key 时改走 `api.jup.ag`
  （文档限额：不带 key 0.5/s，免费 key 1/s，Developer 10/s，60 秒滑动窗口）。
  Jupiter 返回 `NO_ROUTES_FOUND` / `TOKEN_NOT_TRADABLE` 的代币计入「无路由」，
  30 分钟复查一次，不再算询价失败，也不再每轮占额度。
  ~~`price/v3` 不能拿来预筛~~（14:xx 更正）：`usdPrice` 已经是**每股价**（UI 口径，
  已含乘数），之前又除了一次乘数，27 只里有 6 只是这个口径错误（TQQQx、AZNx、NFLXx、
  PPLTx 等）。改口径后是 21 只。真正的分界是流动性：`usdPrice` 对
  `stockData.price`，流动性 ≥ 1,000 USD 的 42 只最大偏离 3.0%；>20% 的离谱值
  全部在流动性 < 1,000 USD 的池子。`stockData.price` 177 只都有，和 FMP/雅虎
  170 只差 < 1%（135 只 < 0.2%）。周六两次取数价格不变而 `updatedAt` 全变，
  `updatedAt` 是心跳，不能当价格新鲜度；工作日是否实时跟正股，待确认。
- 无路由的来由（14:xx 实测 + Jupiter 文档 Market Listing）：Metis 每 30 分钟按
  「$500 买进再卖出差 < 30%」或「$1000 与 $500 买入每枚价差 < 20%」复查池子，不过就移出
  路由。54 只死名单里能查到流动性的只有 0.14～255 USD，其余没有池子；`/swap/v2/order`
  （含 JupiterZ 做市商、OKX、Dflow）对其中 9 只抽查也全部报不出价。两种错误码会互相
  翻转（ACNx、AAOIx 五分钟内对调），名单在 45～54 之间变。30 分钟复查与 Jupiter 自己的
  复查节奏一致，不建议拉长到 24 小时。
- v1 与 v2 的差别：v1 `swap/v1/quote` 不收 Jupiter 平台费；v2 `/order` 每笔 `feeBps=10`，
  同一笔 AAPLx $100 v2 贵约 10 bps。v2 对小池（XOPx $20、SKHYx $5）返回「做市商无报价」，
  v1 能报。Jupiter 文档写明 v1（Metis Swap API）不再积极维护。
- 报价不含：Solana 基础费与优先费、首次收币的 Token-2022 账户租金、报价到上链之间的
  价格变化（我们设 `slippageBps=50`）、以及整条卖出腿（Kraken 充值、手续费、只可挂单）。
  XOPx 实测：$20 买到 0.139591 枚（÷1.00633 = 142.37/股），单池 Meteora DAMM v2、
  流动性 179 USD；毛差最大约 +7.25 USD（买 $40 时），$80 时归零。Kraken 上 XOPx 只可挂单。
- 每股价：Solana 用 `花的美元 / 得到的代币 / multiplier`。multiplier 来自
  `GET /api/v2/public/assets/{符号}/multiplier`，含义是 1 枚代币对应多少股。
  EVM 上次看板的结论是余额已经是调整后的股数，不再除 multiplier。换机后先用
  AAPLx（multiplier 约 1.003）抽查一次，确认 EVM 询价返回的是股还是枚，再写死。
- 只留下成交每股仍低于对照、而且价差绝对值超过 10 bps 的行。记下「仍便宜的最大金额」
  和「下一档已经不便宜」。
- 费率 ≥ 50% 的池、hook 字节码含 `ORIGIN`、或字节码里出现报价合约地址的池，
  不进便宜名单。可以另表显示成陷阱。
- 若本次询价和上次同一口池的成交价相同、而对照价变了，标成报价冻结。
- Kraken 交易对状态读 `AssetPairs?aclass_base=tokenized_asset`：`online` 标可吃单，
  `post_only` 标只可挂单。只可挂单仍然显示，不要因为收市就整行丢掉。
- 页面只读。不调用 `AddOrder`，不读 `config/kraken.env` 的私钥去做交易。

已有脚本可以拆来用，不要整段重跑周六那种全市场 Gecko 扫描：

- `scripts/xstocks_requote.py`：v3 / Jupiter 阶梯询价。它会跳过 v4，也会把费率 ≥ 50% 丢掉。
- `scripts/xstocks_v4_audit.py`：v4 池密钥、lpFee、V4Quoter。
- `scripts/xstocks_gap_scan.py`：Gecko 参考价。参考价不是成交价，页面主表不要用它排序。
- `data/xstocks_book.sqlite` 和 `scripts/xstocks-book-ctl.sh`：目录和慢速参考价采样。
  2026-09-26 没启动采样，就是为了不和基差监控抢 `127.0.0.1:4780`。新进程也要慢，
  一轮和下一轮之间留间隔，不要和 `hk-monitor` 的 60 秒采样抢带宽。

网页用另一个端口，不要占用看板的 8765。8765 是基差监控加账户，只读
`data/hk_monitor.db`。价差页读自己的结果文件或自己的表。

建议形态：一个常驻进程按固定间隔写 `data/xstocks_gap_live.json`（带采样时间和失败原因），
本机 HTTP 只渲染这个文件。进程挂了，页面显示上一轮时间和「已停止」，不要显示空白表假装没有价差。

## 不要做的事

- 不把这条扫描并进 `hk_monitor` 的主循环。
- 不改 `config/hk_monitor.toml`、`[limits]`、`live_ack`、`data/AUTO_HALT`、`data/KILL_SWITCH`。
- 不重试卡死的 0.02 枚桥消息 `0xd33b7cc0…`。
- 不在 xStocks 账户仍是 `ETrade:User Locked` 时卖出那 0.54 枚 MRNAx。开通条款是 operator 在 Kraken App 里做的事。
- 不把 Gecko 的 `price_usd` 当成能买到的价格。
