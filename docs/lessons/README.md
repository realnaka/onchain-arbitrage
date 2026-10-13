# 教训库（本仓库自己的实测教训）

> **与 `docs/knowledge/` 的区别**：`docs/knowledge/` 是**外部公开资料**（CJ、CBB 等），
> 只能当参考；本目录是**我们自己踩过的坑**，每条都带证据指针，**可以直接当闸门用**。
>
> 目的：下一次对话不再犯同样的错。开工前扫标题；动手前查对应的清单。

## 这里有什么

| 文件 | 内容 |
|---|---|
| [recent-mistakes-2026-09-24-to-10-06.md](recent-mistakes-2026-09-24-to-10-06.md) | 43 条错误与教训（计算/口径、执行/订单、跨链、估计/规模、环境），含「未修/待确认」清单与「自我更正」记录 |
| [execution-lessons-2026-09-15.md](../execution-lessons-2026-09-15.md) | 双腿执行、平仓、裸腿、人工处置（更早，仍有效） |
| [execution-lessons-xstocks-bridge-2026-09-26.md](../execution-lessons-xstocks-bridge-2026-09-26.md) | xStocks 跨链充 Kraken 的框架与不要再犯的错 |
| [xstocks-kraken-exit-lessons-2026-09-27.md](../xstocks-kraken-exit-lessons-2026-09-27.md) | 跨链、wrapper 赎回、Kraken 充提的完整检查顺序 |

## 什么时候读哪份

| 我要做的事 | 先读 |
|---|---|
| 算成本 / 浮盈 / 净利 / APR | 本页 §A + `recent-mistakes` §2 |
| 下单 / 平仓 / 看订单输出 | 本页 §B |
| 报「哪个场所机会最多」 | 本页 §C |
| 跨链 / 桥 / 充进 Kraken | 本页 §D + [xstocks-kraken-exit-lessons-2026-09-27.md](../xstocks-kraken-exit-lessons-2026-09-27.md) |
| 算净利 / 改成本模型 / 改闸门 | 本页 §E（一份数字只能有一个口径） |
| 平双腿 / 处置裸腿 / 恢复状态 | [execution-lessons-2026-09-15.md](../execution-lessons-2026-09-15.md) |
| 进程 / 沙箱 / 代理 / 磁盘 | `recent-mistakes` §6 |

---

## A. 数字前必查（算成本、浮盈、净利、APR）

1. **不要用看板的派生数当成本** —— `spot_open_costs()` 只取每个资产最新一笔，CRWV 曾差 6.5 倍。用执行账本按**组**相加。
2. **转账费不是 0.000015 SOL** —— 实测一笔 SPL 转账给 Kraken 是 0.003134641 SOL（≈$0.38，含对方账户租金）。改前看 `scripts/xstocks_gap_live.py:95`。
3. **`[costs]` 的 5+5+5 不是每笔成本** —— 实际往返 ≈ **50 bps of spot_open**。
4. **资金费不在成本模型里** —— `conditional_profit_usdt` 不含资金费；跨结算周期时利润偏乐观。
5. **报「每股」前先查乘数** —— `spot_open_usdt ÷ spot_quantity` 是**每枚代币**价；每股要再除 `contracts`（MRK = 1.0259）。
6. **`None` 就是未知** —— 钱包读失败会返回 `balance: null / balances: []` 而不报错。**不许当 0**。

## B. 下单前必查

1. **闸门两把钥匙**：`[execution] mode = "preauthorized_program"` **且** `auto_orders = "paper"|"live"`。`live` 还要 `live_ack`。**这三项只有 operator 能改。**
2. **额度是硬闸门**：实际每笔 = `min(单笔上限, 钱包余额 × 0.999)` 按整股向下取整 —— **钱包常常才是真正的约束**。
3. **平腿顺序：先平永续，后卖现货。** 反过来会短暂裸多；卖了不平会裸空。
4. **ordermin 会直接卡死**：`CRWVx/USD` 是 0.06，钱包只有 0.0399 → 低于最小量必被拒。下单前先读 `AssetPairs`。
5. **xStocks 不支持 `post_only`**；Kraken nonce **必须纳秒**；tokenized 交易对必须 `pair=MRNAx/USD` + `asset_class=tokenized_asset`。
6. **入账判定用数量阈值（> ordermin）**，不能只看「余额非零」—— 尘埃（0.000001）会把判定骗过去。
7. **卖出后要轮询最多 2 分钟**再判未成交；下单后 5 秒查到「挂着」不等于没成交。
8. **XOPx 只吃买一，绝不挂 maker 卖单**（挂上去买盘消失 20 分钟）。
9. **`onchainos` 必须显式走代理**：`HTTPS_PROXY=http://127.0.0.1:4780`。
10. **`swap execute` 与 approve 抢跑时，原样重试一次**（3/3 复现）。
11. **不许信任返回码**：下单后回读账户（OKX 无持仓/无挂单、钱包余额、板面 `exposure.unhedged`）。
12. **「广播前先模拟」必须真的挡在广播前面**（2026-10-07 实测）：桥腿的
    `eth_call` 原先只在 `once` 的 preflight 里跑，那是 approve **之前**，所以每次
    都回 `ERC20: insufficient allowance` 然后被忽略 —— 也就是说跨链那一步**从来没
    被模拟过**。`approve` 之后、`broadcast` 之前必须再模拟一次，失败就不广播
    （币留在自己钱包，执行器写 `stopped`）。Solana 侧同理（`simulateTransaction`）。
13. **桥费是 `msg.value`，所以要先查源链 gas 代币余额，再买币**（2026-10-07 实测）：
    钱包只剩 0.0006056 OKB 时，链上直接回
    `insufficient funds for gas * price + value: have 605621410468124 want 791242725185203`。
    这个失败看起来像「桥什么也没做」，实际是买完才发现付不出费。转账费按**条**收，
    一次只够一条消息的钱就只够一条。

## C. 报「哪个场所机会最多」前必查

1. 分母用**该场所该时段能定价的轮数**，不用监控轮数。
2. 排序按**全部已记录时段**，把「其他/休市」一并印出来（全天候原则）。
3. 带数据质量标记的行**不计入机会**，单独计「不可信」。
4. 「没采样」≠「没数据」：隔轮采样当轮不写行；取失败的行照常写并带原因。
5. 分开报「**下得了手**（可行动）」与「**值得下手**（够最小利润）」。
6. `venue_scoreboard` 的「过门槛集中在」与「最高净」**标签是错的**，引用前看 `HANDOFF.md` §11 第 9 项。

## D. 跨链 / 充进 Kraken 前必查

1. 先问 Kraken `DepositMethods`（`aclass=tokenized_asset`）；**方法名里没有的网络不要桥过去**。
2. 探针留在自己钱包；只有**余额 ≥ 最小量**才转到充值地址（低于最小量**永久不到账**）。
3. wrapper 先在**同一条链**上 `redeem`；赎回不跨链。
4. **源链模拟通过 ≠ 对面接住币**。必须解码 calldata，核对 11 个账户、mint、托管账户、关联账户。
5. 换链就要换登记账户（第 3 号槽位写的是源链选择子）。
6. 充值地址当次从 `DepositAddresses` 拿，不要抄上一只代币的地址；先对 `chainId`。
7. **桥要交付费**：X Layer 桥实测 `msg.value` 必须是 0.000791242725185203 OKB（≈$0.10，与数量无关）；value=0 时源链 `eth_call` 回滚。逐笔按回执核，**某一次免费 ≠ 下一次免费**。
8. 先平 OKX 空头再卖现货 = 裸多；卖出后平空失败 = 裸空。**平空只在 Kraken 成交确认之后。**
9. **Kraken 提 xStock 的 API 参数是 `aclass=tokenized_asset`**（2026-10-07 实测）。
   写成 `asset_class` 或不带，`/0/private/Withdraw` 回 `EFunding:Unknown asset`，
   而同一个 key 提 USDT/USDG 正常 —— 看到「Unknown asset」先换参数名，不要先怀疑权限。
10. **Solana 源 CCIP 必须带 ComputeBudget**（2026-10-07 实测）。默认 200 000 CU
   不够，会在「币已转出、路由费已算完」之后才报 `Computational budget exceeded`。
   样本和我们的成功探针都设 400 000。
11. **Solana→X Layer 的到账数量是缩放后的 UI 数量，不是 raw**（2026-10-07 实测）。
   raw 947893（UI 0.00953309）→ X Layer 0.009533097916934181。
12. **返回 txHash 不等于广播成功**（2026-10-07 实测）。`onchainos swap execute` 回
   `ok:true` + txHash，实际是钱包后端 `in-flight transaction limit reached for
   delegated accounts`，交易没上链、回执永远不来。每步用 `wallet history --tx-hash`
   + 回执 + 余额三处确认，缺一不可。
13. **X Layer 的深池是 wrapper 池**（2026-10-07 实测）。census 里 SPYx/USDG 的
   $1.89M 池子标的是 wrapper `0xe7e553cd…`；桥上给我们的是原生 `0x90a2a4c7…`，
   卖出前必须先 wrap，否则找不到路线。
   **池价也必须除以乘数才算「入账单位价格」**（2026-10-07 晚补）：`convertToAssets(1)`
   = 1.00571456 原生，池报 779.40/wrapper = 774.97/入账单位。拿池价直接比 Solana/Kraken
   的价（都是每入账单位），**会把乘数本身当成收益**——SPYx 就这样在板上印了 51–62 bps，
   实际 −2 bps。X Layer 17 个池有 16 个是 wrapper、Ink 7 个也是；
   `scan.pool_credit_factor()` 现在按 `form` 归一（认不出的 form 当 wrapper，宁可不报）。
   判据：板面价差 ≈ 乘数（SPYx 55 vs 57.14 bps）时，先怀疑单位，不是机会。
14. **门闸不要把自己拿到的锁当外部锁**（2026-10-07）。执行器先 `CycleLock` 再查
   `lock.exists()`，于是永远 `gate:lock`。自己的锁由 `O_EXCL` 保证唯一，门闸只查
   别人的。
15. **Solana xStock 的 raw ≠ UI×1e8**（2026-10-07）。Token-2022 scaled-UI 带乘数：
   raw = UI×1e8/multiplier。拿 raw 比 UI 会把「已到账」判成「没到」，白写 HALT。
16. **买了之后 funding 余额有结算延迟**（2026-10-07）。刚成交就提币会得到
   `EFunding:Insufficient funds`，隔十几秒就成功 —— 这是延迟不是拒绝。
17. **后续腿被拒 ≠ 没仓**（2026-10-07）。买入成交、提币被拒时若写 `refused` 并
   释放锁，等于把真实库存忘了。凡是「这一步之后持仓」的腿要标出来，后续被拒/回滚
   升级成 `stopped` + HALT。
18. **runner 抛异常必须兜住**（2026-10-07）。桥已广播、卖出腿抛 AttributeError，
   异常直接冒泡 → 锁被释放、不写 HALT，而币在跨链途中。执行器现在把异常按
   `broadcast` 归类：广播过的步骤一律 unknown + HALT。
19. **重试判据要能由 verify 触发**（2026-10-07）。`ok:true` + txHash 可能是
   `wallet history` 里的 `ERROR`（根本没广播）。只在 runner 里判 RETRY 不够——
   这种错只有 verify 看得见。重试硬上限 2 次；桥只有 `ERROR` 才许重发，
   `SUCCESS/PENDING` 一律不重发（重发=第二条 CCIP 消息+第二笔费）。
20. **`onchainos wallet contract-call` 的 calldata 是 ERC-4337 包两层**
   （2026-10-07）。源交易是 `handleOps(765e827f)` → 账户的 `executeUserOp(8dd7712f)`
   → 真正的 `send(5d2a9df2)`。按 userOp 结构硬解第一层会解出 `0x20` 这种指针而
   不是 sender；**可靠做法是在 input 里定位 `5d2a9df2` 再解**（`bytes.find`）。
   证据：反向闭环桥交易 `0x0e981d94…`，解出 dest=Solana、bitmap 772、11 账户、
   receiver = 我们的 Solana 钱包。
21. **对账闸门的「预测」必须是下单前的报价**（2026-10-07）。拿手打的参考价
   （`--price 780`）去比，会把一次正常执行判成 31 bps 偏差；拿聚合器的
   `swap quote` 的 `toTokenAmount` 去比，同一次只差 0.75 bps。没有报价就写
   `measured: false`，**不要编一个数**。
22. **`verify_*` 覆盖 runner 的 `observed` 会把 tx 哈希丢掉**（2026-10-07）。
   卖出/桥的 verify 原来把 `observed` 整个换成了余额快照，账本里 `ok sell`
   只剩 `usdc_before/after`，收尾时只能回链上按时间翻交易。现在两边 `update` 合并。
23. **USDG 是 6 位小数，xStocks 是 18 位**（2026-10-07）。正向脚本一直用
   18 位的 `xlayer_balance` 读 USDG：`before/after` 同比例缩放，「有没有动」看得见，
   **「动了多少」全错 1e12 倍**——任何按金额比较的对账都会误报。现在统一走
   `units.scale_raw(raw, decimals)` 与 `units.USDG_DECIMALS`。
24. **桥费表要两个东西才成立**（2026-10-07）：费用本身（源链 gas 代币、按消息收）
   和 gas 代币的美元价。价格缺失或过期（>3 小时）一律当**未知**，`cost_unknown`
    保留；**不许用文档里的「≈$0.10」回填**，否则闸门自己证明自己。表在
    `data-v2/xstocks_v2_bridge_fees.json`（带交易哈希），价在
    `data-v2/xstocks_v2_gas_prices.json`（`scripts/xstocks_v2_bridge_fees.py refresh`）。
25. **桥家族的选择器必须带净利闸门**（2026-10-07）。`cost_unknown` 一消失，行就会
    开火；原来两个桥家族只看「信息类标记」，没有任何最小净利检查——等于每轮按
    亏损价成交。现在要过 `bridge_net_ok`：`net_usdt` 非空、`edge ≥ 快照阈值`、
    `net ≥ min_net_profit_usdt`。**没有 `net_usdt` 就是拒绝。**
26. **聚合器不支持环形报价 ≠ 链上不能原子**（2026-10-07，当晚自我更正）。
    前半句成立：`swap quote --from USDG --to USDG` 回
    `fromToken and toToken are the same address. Cannot swap a token to itself.`
    后半句**错了**——那是报价工具的限制，不是链的能力。用 X Layer 上现成的
    Uniswap v3 `SwapRouter02` `0x4f0c28f5…` 跑真实的
    `eth_call multicall([exactInput(path), sweepToken])`：两个真实池
    （`USDG -100-> WOKB -500-> USDG`）与三个池都**成功**，QuoterV2 预测与返回
    逐位相等（1 780 / 998 586）。**不需要自建 router 合约。**
    真正的卡点是流动性：X Layer 上每个 xStock 只有一个有流动性的池
    （SPYx 的第二个池 `wSPYx/USDC` `liquidity == 0`）。证据：
    `docs/xstocks-v2-atomic-router-proof-2026-10-07.md`、
    `scripts/xstocks_v2_atomic_probe.py`。**不要再用「报价被拒」推断「链上做不到」。**
27. **链间行不要用 Kraken 的提币闸门判**（2026-10-07）。`_blockers` 的
    `no_withdraw_method` / `bridge_unmeasured_symbol` 只在 Kraken 是路径一端时成立；
    套到 `chain_to_chain` 行上，会让每条反向行永久不可执行。现在就 `kraken_rail=False`。
28. **扫描的 Solana 侧只给 stage-2 命中名报价**（2026-10-07）。`chain_to_chain`
    因此经常看不到 Solana 边（SPYx 只有 ton / x-layer 行）。反向家族要显式把
    「EVM 池所在链有实测桥到 Solana」的名字加进本轮报价名单（`reverse_watch_symbols`，
    有上限）。
29. **拒绝原因要说真话：经济判定排在管线原因前面**（2026-10-07）。看板把
    `blockers[0]` 当标题原因，于是 NVDAx（−20.04 bps）和 SPCXx（−15.42 bps）
    被印成「这个币在这条桥上没有到账样本」——读起来像「只差一个样本就能做」。
    真相是价差为负，而且 `chain_to_kraken` 这一族**根本没算经济闸门**：当时快照里
    18 条「ok」全是负价差。现在 `headline_reason()` 按 `REASON_ORDER` 选
    （价差/成本/深度 > 管线），`chain_to_kraken` 与 `chain_via_bridge_to_kraken`
    补 `below_threshold`（门槛随开市/休市变）。**改标签不是改口径：负数就是负数。**
30. **不要给「每个币 × 每条桥」预做样本，要按价差触发**（operator，2026-10-07）。
    到账样本按 `(源链, 目的链, 币)` 记；某行若**只差这一个样本**、价差又过门槛，
    就标成 `probe`：买「过得了每一腿的最小额」过桥，到账即写样本，再按同一个价差
    补足剩余额度。桥费按条收，所以样本是这笔交易的第一段，不是额外一笔。缺一个
    输入（充值最小额、桥费）时**只出计划不出钱**，`probe.waiting_on` 写明缺哪个。
31. **到账了要写下来**（2026-10-07）。`xstocks_v2_solana_roundtrip.py` 的
    `verify_bridge` 早就证明了「Solana 余额上升」，但那份证明随进程消失，扫描于是
    继续报「没有到账样本」——等于每次过桥都白做。现在确认到账就 append 到
    `data-v2/xstocks_v2_route_samples.jsonl`（`scan.route_sample` /
    `scan.record_route_sample`），并且**只有 `arrived: true` 才开边**。
32. **有周期在飞的时候不要改仓库代码**（2026-10-07 实测）。family 5 跑到最后一步时，
    另一个会话正在改同一个仓库的文件，充值步骤读到半成品模块，抛了偶发
    `NameError`（`deposit:credit_unreadable:NameError`）。状态机按设计写成
    `stopped` + HALT + 保留锁——**这是正确的**（币在 Solana 钱包里，位置明确但不该继续）。
    教训有两条：① 跑钱的时候别改代码，两个会话同时操作尤其危险；
    ② 状态机能挡住这种意外，人只要按 `held_at` 对账、补完、再删 HALT。
    对账证据：Solana raw 1100697、Kraken `SPYx.T` 为 0（充值没发生）、
    充值 tx `2p88zbEX…`、卖单 `OO4TRP-WIFRO-NXYCC`。
32. **长任务必须 `os.setsid()` 脱离进程组，`nohup` 不够**（2026-10-07 实测）。
    用 `nohup ... &` 起的探针在命令会话结束时被整组杀掉：日志停在广播前，留下一个
    5 字节的空 `data-v2/xstocks_v2_cycle.lock`。判断它是不是空锁的三条证据：
    进程不存在、链上净值未变、账本没有新行。清锁前必须同时核这三条。
    正确做法是仓库 ctl 脚本里的那一段：`os.setsid()` 之后再 `os.execv`。
33. **「锁」不等于「停机」**（2026-10-07）。`cycle.open_gate` 原来把 `lock` 和 `halt`
    都返回 `halt`，于是一笔正在过桥的实盘（持有锁约 25 分钟）让纸面运行器在板面上写
    「halt」——读起来像「需要人工对账」。现在两者分开报：`halt` 只有真停机文件才有。
    看到 `lock` 就等，看到 `halt` 才找人。
34. **跨链广播前必须模拟，而且必须模拟在 approve 之后**（2026-10-07 晚实测）。
    EVM→Solana 桥腿的 `eth_call` 原先只在 `once` 的 preflight 里跑，那是 approve
    **之前**，所以每次都是 `ERC20: insufficient allowance`、每次都被忽略——那条
    唯一真的搬钱、真的付费的腿**从来没有被模拟过**。现在干跑搬进桥步骤
    （`bridge_step_runner`）内部：precheck → 干跑 → 广播，干跑回滚就拒绝，
    币留在自己钱包（执行器写 `stopped` + HALT），原因写进账本。Solana 侧
    （`simulateTransaction`）同样是硬闸门：`xstocks_v2_ccip_solana_probe.py send`
    以前也不模拟，现在一并补上。**「另一个模式里模拟过了」不是模拟。**
35. **桥费查的是源链 gas 代币，且要在买之前查**（2026-10-07 晚实测）。链上原话：
    `insufficient funds for gas * price + value: have 605621410468124 want
    791242725185203`（钱包 0.0006056 OKB，一条消息要 0.000791）。这个失败看起来像
    「桥什么也没做」，实际是「买完之后才发现付不出费」。`precheck` 现在读
    `eth_getBalance`：不够 → `gas_low`，读不到 → `gas_unknown`（未知不是有钱）。
    两个都在**买腿之前**拒绝，所以一笔 cycle 不会买下自己搬不走的币。

## E. 一份数字只能有一个口径（2026-10-07 晚）

1. **同一行的净利只能有一处算法。** 实测：扫描 `scan.bridge_net_usdt` 只对真带
   Kraken 腿的路径收 taker 费，而实盘闸门 `bridge_net_ok` 对**所有**桥家族都收，
   于是同一条 `chain_to_chain` 行有两个净利，正好差 10 bps（200 票面差 0.20 USDT）。
   板上印一个、闸门判另一个 —— 闸门会把成本模型认为能赚的行判成不能做。
   现在两者都调 `costs.net_after_costs(..., family=…)`，家族决定要不要收 taker：
   只有 `chain_to_kraken` / `kraken_to_chain` / `chain_via_bridge_to_kraken` /
   `kraken_via_bridge_to_chain` 收；`chain_to_chain`（两个桥方向）与 `dex_to_dex`
   不收。**认不出的家族一律收**（少算成本会亏钱，多算只会错过大机会）。
2. 判「能不能做」的尺寸也只有一个来源：`costs.judged_notional(名义, 钱包余额×0.999)`。
   余额读不到 → 拒绝，绝不用 200 的名义值顶替（8.48 USDC 的教训）。
3. 回归测试锁住这条不变量：`tests/test_xstocks_v2_costs.py`（六个家族两两对齐 +
   那条「旧口径会把 +0.1222 判成 +0.0777」的翻转用例）。
34. **位置问题要读全账本，不能读尾巴**（2026-10-07）。`ledger.recent()` 默认 50 行，
    而一轮纸面就写 ~60 行；`open_positions` 用尾巴判断，NVDAx 的 `probed` 行因此
    掉出窗口、持仓从板面消失。另外纸面行（`mode="paper"`）不是库存，不能抹掉真实持仓。

---

## 维护约定

- 每轮大改动后：把新踩的坑**追加**到 `recent-mistakes-*.md`（或新开一份按日期的文件），
  并同步更新本页的 A–E 清单。
- 每条必须写清：**症状 / 根因（文件:行）/ 正确口径 / 有没有修 / 证据**。
- 只写核对过的事；猜的写「待确认」。
- 修好一条就把状态从「未修」改成「已修 + 日期 + 证据」，不要删条目 —— 历史本身是证据。
