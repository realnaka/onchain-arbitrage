# 港股链上现货多 + 永续空：只读监控 v1

## 范围与判断

沿用本项目，不另起独立交易脚本。当前只采集行情、报价探针、成本条件、声明式 Intent；
不下单、不连接钱包签名、不授权代币、不建纸面仓位、不发送飞书消息。

两篇文章不是完全相同的收益假设：

- Vigo 的双腿思路是交易**持仓期间基差变化**，不需要假定股价本身上涨或下跌。
- siyuan 的特定漏洞要求：09:00 的**专属通道**仍可报价，且在发生隔夜价格变化时仍锚定昨收。
  他宣称通道后来消失，但这是待独立验证的说法。
- 开盘后链上价格上涨，不能单独证明双腿策略失效；同样，几天开盘下跌也不能证明双腿盈利。
- 先卖出现货、继续持有空单是新的方向性交易。本策略不产生这一步 Intent。

初始资金上限 10,000 USDT。默认现货预算 5,000、空头保证金预算 5,000；
这不是各开 10,000 名义金额。100 / 500 / 1,000 USDC 是独立的询价档位，不是三笔已建仓头寸。
空头数量由股票敞口与合约面值决定，不由“资金分成两半”决定。

## 首批名单

交易所列表每 30 分钟重新拉取，以下为 2026-09-12 首次真实采样的匹配关系：

| 标的 | X Layer 现货 | Gate | OKX | Binance |
|---|---|---|---|---|
| 小米 1810.HK | wXIAOx | XIAOMI_USDT；XIAOMIHKD_USDT | XIAOMI-USDT-SWAP | HK1810USDT |
| 腾讯 0700.HK | wTCENTx | TENCENT_USDT；TENCENTHKD_USDT | 未找到对应合约 | TENCENTUSDT；HK0700USDT |
| 泡泡玛特 9992.HK | wPOPMTx | POPMART_USDT | POPMART-USDT-SWAP | POPMARTUSDT |
| 美团 3690.HK | wMEITx | MEITUAN_USDT | 未找到对应合约 | MEITUANUSDT |

共 4 种现货、13 个相关合约。其中 HKD 数值型合约只观察原始盘口；
只有普通美元等价线性合约才进入待验证配对评估。表格中的"未找到"仅指此次公开列表，不是永久断言。
鉴于用户明确要求，本轮在计划的两家 CEX 起步范围上扩为三家；并未扩为自动执行。

2026-09-12 21:00 起，同一份监控纳入 X Layer 上 7 个美股 xStocks 代币
（AAPLx、NVDAx、TSLAx、MSFTx、AMZNx、METAx、GOOGLx）与
Gate / OKX / Binance 的对应 USD 线性永续，链上取价方式见下一节。

链 ID `196`，代币精度均为 18；交易报价使用 X Layer USDC（精度 6）：

| 代币 | 合约地址 |
|---|---|
| wXIAOx | `0x076cf393e701839fc7a5832d2c68aafa235682ae` |
| wTCENTx | `0x41333df9e7639188bbfca5522dc4844398af9f9e` |
| wPOPMTx | `0x4ebf5fd25b02022afad96e2fa25da54a246fded0` |
| wMEITx | `0xad1b65c8556957cf23d1b5e9accdc449b415fa97` |
| USDC | `0xb6ceceab302e2e4948951ee7843fc24e92933061` |

这些地址与 OKX Wallet 公共搜索返回的链、地址、精度核对，并另取 GeckoTerminal 池子数据。
**尚未完成发行人权益、包装代币换股比例及公司行动的独立验证，不能默认每枚等于一股。**
发行人地区访问限制不做绕过；即使行情 API 可读，也不等于该账户/地区有交易资格。

其他发现的合约见研究快照和 `hk-list`。MINIMAX、智谱、快手、比亚迪、中芯等缺少已核实的对应链上现货；
SHEIN、UNITREE、MOONSHOT、ADR、A/H 双上市、杠杆 ETF 和指数另列候选，不混为相同港股证券。

## 经济模型

所有输入先归一到同一股票敞口和同一结算货币。对于支持的线性合约：

```text
目标合约张数 = 现货枚数 × 每枚股票敞口
               / 每个合约价格单位的股票敞口 / 合约乘数
实际张数 = 按交易所 lot size 向下取整

持仓双腿损益 =
  现货枚数 × (退出现货 Bid - 入场现货 Ask)
  + 合约张数 × 合约乘数 × (入场合约 Bid - 退出合约 Ask)
  - 完整开平仓成本 + 实际有符号资金费现金流
```

`OrderBook.quantity` 在本适配器中保留原生合约张数；面值在 `Contract.multiplier` 中。
比如 Gate 腾讯普通版本次乘数为 `0.1`，不能直接把 10 张当作 10 枚现货。
USDC、USDG、USD、USDT 不自动视为同一货币。

三个监控量：

1. `entry_basis_bps`：按买入现货、卖空合约的深度/询价所得开仓基差。
2. `current_exit_basis_bps`：按卖出现货、回补空单的当前报价估计退出基差。
3. `immediate_roundtrip_net_usdt`：若刚刚开仓就按当前两腿报价退出的净损益，而非历史持仓盈利。

基差 bps 均以本次现货开仓金额为分母。另记录盈亏平衡退出基差。
没有真实入场记录，就不显示已实现或持仓 P&L。纯函数 `paired_pnl()` 为以后接入人工入场记录保留。

只有显式提供目标退出基差时，才计算 `conditional_profit_usdt`；
成本按当前周转金额估计，未来价格、资金费率和退出价差仍有风险，
因此对应 Intent 标为 `conditional_open`、`not_locked_profit=true`、`execution_allowed=false`。
没有到期强制收敛的永续合约，正基差也不构成无风险锁定利润。

成本包括两次现货交易、两次合约交易、已走深度之外的滑点缓冲、
Gas、资金占用、带正负号的资金费及失败缓冲。读取的当前资金费率只作观察，
不把零费率永久外推，也不自动年化后计入短持仓收益。

## 信号闸门

默认全部为观察状态。要产生条件性 Intent，必须同时满足：

- 代币层 `mapping_verified=true`，且**对应 venue:instrument 的单独映射**已验证、有来源、有有效期；
- 股票份额转换与 USDC→USDT 换算明确且未过期；
- 普通 USD 线性合约、活跃、两侧深度满足数量/最小名义金额要求；
- 现货买卖是相同代币、相同枚数，有真实指定金额报价，时序与 CEX 盘口接近；
- 所有成本已验证且未过期；原生 `estimateGasFee` 不当作 USDT，
  可以提供显式审阅过的 `roundtrip_gas_budget_usdt`；
- 盘口、报价未超龄；报价从**请求开始**计算本地保守期限，不宣称提供方保证成交；
- 资金分配及成本预留在 10,000 上限内，数量取整没有残余敞口；
- 目标退出基差情景明确，情景净边界和金额超过配置阈值；
- 处在策略窗口，不在 09:30 后生成保留裸空单的信号。

示意配置字段如下，**不要复制成已验证配置**；需用实际证据和有效期填写：

```toml
# 在相应 [[tokens]] 内设置 mapping_verified，再提供该 token 下的 mappings 表。
[tokens.mappings."gate:XIAOMI_USDT"]
shares_per_token = "..."
shares_per_perp_unit = "..."
quote_to_usdt = "..."
verified = false
evidence = "发行人/包装合约/交易所规格/汇率来源与核对时间"
valid_until_ms = 0

[scenario]
target_exit_basis_bps = "..."  # 假设，不是已知未来价格
```

`costs.verified`、`costs.valid_until_ms` 另外控制成本的有效期；仅改一个开关不会绕过其他闸门。
当前配置不包含这些未获证实的数值。

## 09:00 的两项验证

按香港现金股票交易日历，在 08:55–09:35 连续记录前后窗口。
只有 09:00–09:29:59 的请求用于“盘前专属通道”判断。周末、节假日、未知年份均不作失效证据。
配置覆盖 2026；个股停牌、异常闭市、发行人单独服务时间仍需人工确认/覆盖，不能只看周一至周五。

### A. 专属报价通道是否仍存在？

记录资产、链、地址、金额、方向、请求/接收时间、错误码、完整路由和原始响应。

- `50114` 等鉴权失败、网络错误、返回结构不明：**测量失败/未知**。
- 聚合器能报价格，但没有识别专属来源：**有报价，专属通道未知**。
- 只出现 Uniswap 等 AMM：**未观察到专属路由**，不是“通道已关闭”。
- 经核实的专属流动性来源确实出现在有效报价中：**在这个时间和金额档观察到通道**。
- 即使大额无报价，小额有报价，结论也要按金额档区分。

`probe.special_route_ids` 初始为空。必须依据真实响应和提供方说明核实精确名称/ID；
不能把任意 xStocks 交易对或底层 AMM 池命名成专属 RFQ。
聚合报价路由混有 AMM 时，可说明专属来源出现过，但不能把整个聚合价归因给该来源。
聚合器不返回某条路由也可能是路径选择，不证明提供方所有通道都消失。

### B. 报价是否仍锚定昨收、未反映隔夜变化？

这与 A 独立。至少要有：

- 前一个**交易日**的收盘价，不是前一天，也不是已经滞后的指数价；
- HKD/报价币换算、wrapper 份额、分红/拆股等公司行动已核对；
- 独立的当前参考价，时间与引用有效期可追溯，不使用事后的开盘结果回填；
- 同资产、方向、金额、当日，至少 3 个不同请求时间的有效专属来源报价；
- 独立隔夜变化超过 50 bps；报价与归一昨收误差默认容忍 20 bps，两个阈值均可调。

输出 `consistent_with_old_close` 仅表示与旧价假设一致，不证明做市商“故意忽略新闻”；
若隔夜没发生明显价格变化、仅有一个报价、参考资料不完整或路由混合，输出不确定。

应用已录制 Yahoo 的港股日线和 HKD 外汇日线作为**独立供应商参考候选**。
不把供应商的 `regularMarketPrice` 冒充官方昨收，也不把旧日线当作实时 FX。
默认不自动赋予公司行动/份额校验通过。经审阅的归一化证据可用：

```bash
.venv/bin/python -m arb_monitor hk-monitor --anchor-reference /path/to/reviewed-references.toml
```

文件格式：

```toml
[[references]]
asset = "XIAOMI"
quote_currency = "USDC"
previous_session = "YYYY-MM-DD"
previous_close_per_token = "..."
independent_current_per_token = "..."
observed_ts_ms = 0
valid_until_ms = 0
normalized_and_actions_checked = false
source = "收盘/汇率/份额/公司行动的可核对来源与时间"
```

文件每轮重新读取。不满足日期、身份和时间范围的证据会被拒绝。
当前真实报价权限未恢复前，不能完成 A；没有足够归一化参考证据也不能完成 B。

## 运行、存储与边界

- 链上腿 24/7 运行，因此监控不再只在港股 08:55–09:35 采样。现货侧取价按以下优先级：
  ① 有实测深度的已审阅池子（`slot0` + 余额，含手续费档与池地址）；
  ② 最近窗口内解码出的**真实成交打印**（按地址净流量配对代币腿与稳定币腿，
     卖出侧最优为链上 Bid、买入侧最优为链上 Ask）；
  ③ GeckoTerminal 池子参考（仍需高于深度地板）；
  ④ OKX 展示价，只作参考且不与同 venue 盘口做基差（避免同源循环比较）。
  空池（`liquidity()=0` 或深度低于 `min_pool_depth_usd`）只记录证据，不产生价格。
- 链上读取仅使用公开 JSON-RPC：`eth_call` / `eth_getLogs` / `eth_getBlockByNumber` /
  `eth_getTransactionReceipt`，不连接钱包、不签名、不发交易。
  该 RPC 实测限制：单次 `eth_getLogs` ≤100 区块、批量请求上限很小，按分片+重试处理；
  响应不完整时记 `incomplete_rpc_reply` 错误，绝不当成 0 价格。
- 稳定币腿（USDC / USDT0 / USDG / USDT）按 1 USD 处理，只用于筛选；
  真实报价与结算仍以 exact-in 报价为准。
- CEX 使用已测试并固定的 CCXT 4.5.78 的公共、限频传输方法。
  原生字段补充是为了保留股票分类、HKD 数值语义、面值、时间戳和序列；不用私有 API。
- JSON 在解码入口使用 Decimal，禁止先读 float 再转字符串；SQLite 以十进制字符串存储。
- Gate 的事件更新时间与服务器当前时间均留原始响应，保守按更新时间判断陈旧。
- 这版是 REST 研究采样，不是低延迟 WS 执行系统；不能视为 M1 的 24 小时可靠性验收已完成。
- 元数据 30 分钟刷新，展示行情/池子/日线/FX 每 5 分钟刷新；盘口与报价按默认 60 秒轮询。
- 每轮首个鉴权错误会熔断剩余报价，减少重复请求；实际完成的请求数与跳过原因单独记录。
  下一轮重新试探，不把跳过项目伪造成真实报价失败样本。
- `monitor_events` 追加保存原始元数据、盘口、资金费、参考源、报价、评估和汇总，
  保留 `venue_ts_ms / recv_ts_ms / sequence / source`；数据库与 demo 隔离。
- 网络错误不会复用旧盘口冒充新数据；数据源错误缓存到下一次成功刷新。
- 同一资产/venue/方向按冷却期去重，价格变动不会绕过冷却。
- 本地看板绑定回环地址，转义外部字符串，只读，不含下单或签名入口；
  显示快照时间与过期状态，刷新页面不等于后台有采集器。
- 已在当前任务设置工作日 08:55 的盘前采样心跳；程序再按交易日历过滤，
  08:55–09:35 采样，09:36 停止。设备与应用必须在线。
  只有权限恢复、证据/结果变化、新故障或需要用户处理才通知；已知错误不重复提醒。

### 持续运行

```bash
scripts/hk-monitor-ctl.sh start 60   # 单实例、每 60 秒一轮，脱离终端
scripts/hk-monitor-ctl.sh status     # 进程 + 最近事件时间 + 链上状态
scripts/hk-monitor-ctl.sh stop
```

看板（只读、绑定回环地址，读同一份 `data/hk_monitor.db`）：

```bash
.venv/bin/python -m arb_monitor hk-dashboard --port 8765   # http://127.0.0.1:8765/
```

看板分三块：现货多 + 永续空基差、链上现货明细、
永续盘口明细（按交易所列出 Bid/Ask、资金费率、盘口年龄）。

### 双腿方向的口径

本项目只建模一个方向：现货多 + 永续空，且两腿各用自己真正成交的那一侧价格。

| 方向 | 现货腿 | 永续腿 | 毛基差 |
|---|---|---|---|
| 现货多 + 永续空 | 买现货，付链上 Ask | 空永续，卖给该交易所 Bid | (永续 Bid − 现货 Ask) / 现货 Ask |

反方向（卖现货 + 多永续）需要先持有或借到代币。X Layer 上这些代币没有借币做空渠道，
所以那条腿实际开不出来，看板不列它；`strategy/xstocks_screen.py` 里只保留它的算术
用于对照测试，防止以后被误当成同一个方向。两个方向的毛基差本来也不是彼此的镜像
（各自吃盘口不同一侧）。

表格里永续腿一定写明**交易所与合约**，因为"最优价"可能来自不同 venue，
不标来源的数字无法复核。缺哪一侧（例如美股代币本窗口只有卖侧成交）就不给该方向的 bps。

判定规则与信号闸门一致：**净基差 ≤ 0 一律不可执行**（两腿开不出利润，不算信号）；
0 < 净基差 < `min_net_edge_bps` 只算"未达阈值"，同样不计为信号；
只有净基差 ≥ `min_net_edge_bps` 才进入"本轮候选"计数。看板灰底行即本轮不给信号的行，
它们仍然保留价格与来源，便于事后核对判断，而不是被隐藏。

### 预警通道

出现候选时同时触发两条通道，判定口径与看板完全一致（同一份 `run_summary`）：

1. 看板提示音：页面每 30 秒读 `/api/status`，候选出现时响铃 + 红色横幅 + 标题 ⚠️；
   浏览器需要一次点击解锁音频，故首次打开后点一下"启用提示音"。
2. 飞书：`[notify]` 段配置，当前 `channel = "webhook"`，走**群机器人 webhook** 播报到群；
   也可用 `channel = "lark_cli"` 走本机 lark-cli 的 bot 身份私聊 `lark_user_id`
   （用户 token 过期不影响 bot），或 `"webhook,lark_cli"` 两边都发。
   同一 `资产@交易所:合约` 在 `cooldown_seconds` 内只提醒一次；
   已成功发送的记录会写进 `monitor_events(kind='alert')`，进程重启后据此恢复冷却，不重发。

webhook 地址属于密钥：**不写入 `config/hk_monitor.toml`**，而是放在
`config/notify.env`（gitignored、权限 600），由 `scripts/run_hk_monitor.sh` 启动时
source 成 `ARB_FEISHU_WEBHOOK`。发送失败只记录 `status=error`，绝不影响行情采样。

原"09:00 通道与昨日收盘锚定"面板已从看板移除，改由人工核对；相关探针数据仍照常采集并存入
`monitor_events`，`hk-status` 与原始 JSON 里仍可查到。

- 数据仍写在 `data/hk_monitor.db`，看板 `hk-dashboard` 读同一份数据库。
- `scripts/run_hk_monitor.sh` 崩溃后自动重启（10 秒后），日志写入 `data/logs/`。
- `scripts/com.arbmonitor.hk.plist` 为 launchd 版本；macOS 会阻止 launchd
  进程读取 `~/Documents`（实测 `Operation not permitted`），启用前需给
  `/bin/zsh` 授予完全磁盘访问权限，或把仓库移出 `~/Documents`。
- 实测轮次节奏：带链上刷新约 90–110 秒，未触发链上刷新的轮次约 20–25 秒；
  `interval` 是下限而不是保证。以 `run_summary.evaluated_at_ms` 为真实时点，
  不用"应该每 60 秒一次"代替实测。

## 下一道真正的阻塞

OnchainOS 报价通道已于 2026-09-15/16 复核恢复（`quotes_observed`，16/16 覆盖）；
2026-09-12 的 `50114 Invalid Authority` 是 CLI 未走代理造成的测量失败，
不是通道关闭。报价探针现在并发执行并有界退避重试，见
`docs/research/quote-grid-2026-09-16.md`。
不要把 API key 发到对话里，也不需要为此提供交易所交易权限或钱包私钥。
权限恢复后，先检查真实成功响应、专属来源身份、份额/结算币映射，
再等交易日窗口积累证据；不能靠关闭校验开关“制造”套利信号。
