# xStocks v2：Solana → X Layer CCIP（第三个方言）

日期：2026-10-07。这一轮**只做只读调研 + 构造器 + hermetic 测试 + 模拟**。
没有广播，没有动 `mode` / `auto_orders` / `live_ack`，没有创建或删除
`data/KILL_SWITCH` / `data/AUTO_HALT`，没有开机任何 v1 进程。

代码：`src/arb_monitor/xstocks_v2/ccip_solana.py`（构造 + 解码 + 校验），
测试：`tests/test_xstocks_v2_ccip_solana.py`（29 个 hermetic 用例）。

## 1. 结论

| 项 | 结论 |
|---|---|
| 源链构造 | **xStocks 自己的 Solana Anchor 程序**在发指令，不是 Chainlink 的 EVM `send` |
| 程序 | `F2ZYgyh8YDG6rHo9F1T8Aw4WoTyy8eYN3GgM2LaPKcZN`（可升级；升级权限 `5aMNNLQJwAEeoemTEMkv5NVjqKwvvefRYCQ5Z67S38JH`） |
| 指令 | `global:ccip_send`，判别符 `6cd886bff9ea2154`，参数 **92 字节** |
| 账户表 | **27** 个。与 EVM→Solana 的 11 个账户 / bitmap 772 毫无关系 |
| 桥费代币 | **原生 SOL**（`feeTokenMint` = WSOL，`feeTokenUserAssociatedAccount` = 全零） |
| 实测桥费 | 852 448 / 744 731.9 lamports，按**消息**收，与数量无关 |
| 到账形态 | CCIP 消息 `tokenAmounts` 为空；目的链合约从自己的托管钱包放币 |
| 收款地址 | 在消息 `data` 里（72 字节），不在通用 SVM 结构里 |

三种方言互不通用（规格第 7 节）：

| 方言 | 构造 | 账户数 | 状态 |
|---|---|---:|---|
| EVM → Solana | EVM `send(...)` + `abi.encode(bitmap, accounts)` | 11 | 已有成功样本 |
| EVM → EVM | 标准 CCIP `extraArgs` | — | 无样本，只监控 |
| **Solana → EVM** | **Solana 程序指令 `ccip_send`** | **27** | 本文档 |

## 2. 证据：四笔主网交易

全部只读解码，2026-10-07。四笔都是 Solana → XLayer（选择子 `3016212468291539606`）。

| 品种 | 交易 | 金额（8 位小数原始值） |
|---|---|---:|
| QQQx | `REDACTED_SOLANA_SIGNATURE` | 1 197 538 502 |
| TSLAx | `REDACTED_SOLANA_SIGNATURE` | 1 188 767 665 |
| HOODx | `REDACTED_SOLANA_SIGNATURE` | 51 724 301 |
| TSLAx | `REDACTED_SOLANA_SIGNATURE` | 13 681 100 |

日志里能看到完整调用链，这是「是 Solana 程序指令、不是 EVM send」的直接证据：

```text
Program F2ZYgyh8… invoke [1]
Program log: Instruction: CcipSend
  TokenzQd… TransferChecked          <- 币从我们的账户进托管账户
  Ccip842gz… GetFee -> FeeQP… GetFee
  system transfer  (费用从钱包进 CCIP 计费账户)
  Ccip842gz… CcipSend -> RmnXL… VerifyNotCursed -> FeeQP… GetFee
  Ccip842gz… return <messageId>
Program log: CCIP message sent successfully
```

## 3. 指令参数（92 字节，小端）

| 偏移 | 长度 | 内容 | 样本值（QQQx） |
|---:|---:|---|---|
| 0 | 8 | 判别符 `sha256("global:ccip_send")[:8]` | `6cd886bff9ea2154` |
| 8 | 8 | 目的链选择子 | `3016212468291539606`（X Layer） |
| 16 | 4+32 | 目的链桥合约，EVM 地址左侧补零到 32 | `…9ec0e4a4c411493773e01e2abf4d42395788846b` |
| 52 | 4+20 | **收款 EVM 地址** | 样本里是签名者自己的 EVM 地址 |
| 76 | 8 | `productId` | `3581283827036199749`（= 桥表 `productId`） |
| 84 | 8 | 数量，原始 8 位小数 | `1197538502` |

`productId` 逐条与 `data/xstocks_book.sqlite` 的 `bridges.products_json` 对上，
这是「参数语义确定」的证据：

| 品种 | 桥表 productId | 指令里的 u64 |
|---|---:|---:|
| QQQx | 3581283827036199749 | 3581283827036199749 |
| TSLAx | 3800906088853912450 | 3800906088853912450 |
| HOODx | 403934653322669323 | 403934653322669323 |

数量也在参数里，不是「发全部余额」：四笔里 `transferChecked` 的数量恰好等于
参数里的 u64。

## 4. CCIP 消息体

程序把参数翻译成 72 字节 `data`，`tokenAmounts` 为空、`feeToken` 为 SOL：

| 偏移 | 长度 | 内容 |
|---:|---:|---|
| 0 | 32 | 收款地址（EVM，左侧补零） |
| 32 | 8 | `productId`，**大端** |
| 40 | 32 | shares，**8 位小数 × 10¹⁰ → 18 位小数**，大端 |

与发行方公开仓库 `backed-fi/backed-ccip-contract` 的
`BackedCCIPReceiver._ccipReceive` 逐行对上：

```solidity
bytes32 tokenReceiverBytes = bytes32(any2EvmMessage.data.slice(0, 32));
uint64  tokenId            = uint64(bytes8(any2EvmMessage.data.slice(32, 40)));
uint256 sharesAmount       = uint256(bytes32(any2EvmMessage.data.slice(40, 72)));
...
token.transferSharesFrom(_custodyWallet, tokenReceiver, sharesAmount);
```

三个直接推论：

1. **消息里没有 tokenAmounts**，不需要目的链的 CCIP token pool 登记。
2. 目的链合约从自己的托管钱包放币，`data` 是唯一指令。
3. `tokenReceiver == address(0)` 被合约拒（`TOKEN_RECEIVER_INVALID`）——
   和 2026-09-26 那笔「收款地址全零、币锁死」是同一类错。

发送方身份也对上了：四笔来自**不同用户**的消息，`sender` 都是同一个
`8797ea82a79a90eb688a1ca9b85b8992fda822c7922883c957d8626586a89a64`
（程序自己的 PDA）。目的链白名单认这个 PDA，不认我们的钱包。

## 5. 27 个账户

四笔样本账户表**完全一致**，只有 5 个槽随人/币变化：

| # | 角色 | 说明 | 变化 |
|---:|---|---|---|
| 0 | `config` | xStocks 程序配置 | 固定 |
| 1 | `signer` | 我们的 Solana 钱包（可写 + 签名） | **每用户** |
| 2 | `fee_authority` | 程序付费 PDA | 固定 |
| 3 | `system_program` | | 固定 |
| 4 | `source_token_account` | 我们的 Token-2022 关联账户 | **每用户每币，可推导** |
| 5 | `custody_token_account` | 托管账户 | **每币，从 token config 读** |
| 6 | `mint` | Solana mint | **每币** |
| 7 | `ccip_router_program` | `Ccip842gz…` | 固定 |
| 8 | `ccip_router_config` | | 固定 |
| 9 | `dest_chain_state` | X Layer | 每目的链 |
| 10 | `router_nonce` | 保存序号（样本 1016） | 固定（同一条 lane） |
| 11–18 | fee quoter / RMN | 费用查询、诅咒检查 | 固定 |
| 19–20 | `fee_billing_signer` / `fee_token_receiver` | SOL 费用 | 固定 |
| 21–24 | WSOL / 旧 SPL / Token-2022 / system | | 固定 |
| 25 | `lane_config` | 54 字节，含 X Layer 选择子 + 桥地址 | 每目的链 |
| 26 | `token_config` | 90 字节，含 productId、mint、托管账户 | **每币** |

可写位（实测）：`{1, 2, 4, 5, 9, 10, 19, 20}`。

## 6. 两个每币账户怎么取（重要）

| 账户 | 取法 | 不要用的取法 |
|---|---|---|
| `source_token_account`（#4） | 推导：`ATA(我们的钱包, mint)` | — |
| `custody_token_account`（#5） | **读 `token_config` 的第 48–80 字节** | **不要**推导成 `ATA(pool_authority, mint)` |
| `token_config`（#26） | `getProgramAccounts(offset=16 memcmp mint)` 现查，或读缓存 | 猜 PDA 种子 |

`ATA(S7vYFFWH6BjJyEsdrPQpqpYTqLTrPRK6KW3VwsJuRaS, QQQx mint)` =
`FMQUsCaaq7vZ9BPipEyRqQcoLBp7q4giiV8PMsBETYBX`，而真托管账户是
`Ah9P5Lxh9e1uN1x254jSo6bSGXHd2zzeRrkjXDEkJRrw`。两个都像正常地址，
推导出来的那个是错的——所以托管账户必须从 `token_config` 读。

`token_config`（90 字节）布局：

| 偏移 | 长度 | 内容 |
|---:|---:|---|
| 0 | 8 | 判别符 `5c49ff2b6b337565` |
| 8 | 8 | `productId`，小端 |
| 16 | 32 | mint |
| 48 | 32 | **托管账户** |
| 80 | 10 | 尾部（含一个 u64 = 10000） |

## 7. 验证（命令与结果）

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_xstocks_v2_ccip_solana
# Ran 29 tests ... OK

PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_ccip_solana_probe.py plan \
    --symbol QQQx --amount 100000            # 打印 27 个账户与 92 字节参数

PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_ccip_solana_probe.py simulate \
    --symbol QQQx --amount 100000            # 只读 simulateTransaction
```

把构造器输出的账户表与四笔真实交易逐槽比对：**4/4 完全一致**。

模拟结果（2026-10-07，只读，`sigVerify:false`）：

```text
Program F2ZYgyh8… invoke [1]
Program log: Instruction: CcipSend
Program TokenzQd… invoke [2]
Program log: Instruction: TransferChecked
Program log: Error: insufficient funds
```

也就是：程序接受了我们的指令和 27 个账户，走到「从我们的关联账户
`DKCCSWLcPCoRrE5SWYgybXjffFWGC2sxDBYxwCDNyvRa` 转币」这一步才因为
**我们 Solana 钱包里 QQQx 余额为 0** 而失败。这是当前能拿到的最强验证，
不需要广播。

## 8. 现在还缺什么（探针的前置条件）

| 项 | 现状 |
|---|---|
| Solana 钱包 SOL | 0.013647382 SOL（够付约 16 次 0.00085 SOL 的桥费） |
| Solana 钱包 xStock | **17 个 Token-2022 账户，余额全部为 0** |
| Kraken 提币地址簿 | **没有 SPYx/QQQx 等 xStock 条目**（`data/xstocks_route_matrix.json`），所以「Kraken 买 → 提到 Solana」这一步当前发不出去 |
| 桥最小额 | **未确认**。`/public/bridges` 有字段但 16.7 MB，本轮没拉全 |
| 单笔上限 | 仍是 25 USDT，未改（operator 专属） |

所以探针要落地，先要解决「Solana 上得有币」这一条：

1. operator 在 Kraken 网页给某个 xStock 加一条 Solana 提币地址（要过邮件/2FA），
   然后走计划里的「Kraken 买 → 提到 Solana → 桥到 X Layer」；或
2. 用钱包里已有的 USDT 在 Solana 上买最小额（Jupiter），再桥。

两条都要 operator 点头，因为都会动真钱，而且第 2 条会改变「币的来源」。

## 9. 变量与函数（给执行器用）

`ccip_solana.py` 对外只有纯函数，不签名、不联网：

| 函数 | 作用 |
|---|---|
| `build_accounts(signer, mint, token_config, custody)` | 生成 27 个账户（含角色、可写位） |
| `validate_accounts(...)` | 账户数、mint、托管、重复、可写位；返回稳定字符串列表 |
| `instruction_data(recipient, product_id, amount)` | 92 字节参数 |
| `decode_args(data)` / `CcipSendArgs.encode()` | 严格解码 / 编码 |
| `message_payload(recipient, product_id, amount)` | 72 字节消息体（8→18 位小数） |
| `validate_args(...)` | 选择子、桥地址、收款非全零、productId、数量 |
| `token_config_custody(raw, mint, product_id)` | 从 `token_config` 取托管账户 |
| `associated_token_address(owner, mint)` | Token-2022 关联账户 |
| `build_legacy_transaction(...)` / `parse_legacy_transaction(...)` | 广播前解码复核 |

## 10. 实盘探针：已成功（2026-10-07 16:27–16:29）

operator 授权一次最小额：Kraken 买 → 提到 Solana → 桥到 X Layer。
币留在自己的 X Layer 钱包，不进 Kraken。

### 10.1 每一步的实测数字

| 步骤 | 结果 | 证据 |
|---|---|---|
| Kraken 买 | 0.01 SPYx，成交 `OOK47H-KNBH3-KWK7OI`，ZUSD 25.9126 → 18.1103（花 $7.8023） | 市价单，成交价约 **780.23** |
| Kraken 提币 | 0.01 SPYx → Solana，`refid=FTF5dVh-6awHKOt0trGaDvTjiW3OMQ`，费 **0.00046689 SPYx** | key `solanspyx1`，网络 Solana |
| Solana 到账 | **0.00953309 SPYx**（raw 947893） | ATA `FRhMhtCxF2H9H1hP3KV4uQzR5r5mYN6ALkua9gRPC5hv` |
| 桥（CCIP） | 交易 `REDACTED_SOLANA_SIGNATURE`，slot 454169012，`err=null` | 日志 `CCIP message sent successfully` |
| 桥费 | **852 448 lamports** = 0.000852448 SOL（≈ $0.1010，按 SOL $118.5034）＋ tx fee 5 000 lamports | 钱包 SOL 13 647 382 → 12 789 934 |
| X Layer 到账 | **0.009533097916934181 SPYx**（`0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48`，在 `0xabab…ababab`） | 从 0 变到这个数 |
| 用时 | 广播 16:27:20 → 到账 16:29:24，**约 2 分钟** | 比 X Layer→Solana 的 22–27 分钟快很多 |

路线账已写入 `data-v2/xstocks_v2_route_samples.jsonl`（`arrived: true`），
`scan.load_measured_bridges()` 现在返回 `{('solana', 'x-layer')}`，这条边打开。

### 10.2 这一轮新学到的四件事

| # | 结论 | 证据 |
|---|---|---|
| 1 | **Kraken 提 xStock 必须带 `aclass=tokenized_asset`**。用 `asset_class` 或不带，`/0/private/Withdraw` 一律回 `EFunding:Unknown asset`（试了 10 种写法，只有 `aclass` 过） | 对照：同样参数下 USDT 报「Fee amount is greater than asset amount」、USDG 报「Insufficient funds」，说明是**资产**问题不是权限问题 |
| 2 | **`ccip_send` 必须带 ComputeBudget**。默认 200 000 CU 会在「币已转出、路由费已算完」之后报 `Computational budget exceeded`——最坏的失败位置。样本里也有这条指令，上限 **400 000**，实测用 242 254 | 去掉 CU 上限能复现失败，加上就 `CCIP message sent successfully` |
| 3 | 目的链拿到的是**缩放后的 UI 数量**，不是 raw。Solana raw 947893（8 位小数、带 multiplier 的 UI 是 0.00953309）→ X Layer 收到 0.009533097916934181 | 947893 × 1.005714560286254 × 1e10 = 9.53309791693418e15，逐位相等 |
| 4 | Solana 侧没有我们想象的「先发最小额再发余量」需求：**一笔发够**，费用按消息收（852 448 lamports，与金额无关） | 与 X Layer→Solana 的 0.000791 OKB 是两套费，不能互相套用 |

### 10.3 这轮的成本（$7.80 的票面）

| 项 | 金额 | 占票面 |
|---|---:|---:|
| Kraken 吃单价差 + taker 费 | 约 $0.009（10 bps） | 12 bps |
| Kraken 提币费 0.00046689 SPYx | $0.3643 | 467 bps |
| CCIP 桥费 0.000852448 SOL | $0.1010 | 129 bps |
| **合计摩擦** | **≈ $0.474** | **≈ 608 bps** |

固定费在 $7.80 上就是 600 bps。这与 `docs/xstocks-v2-fees-and-bridges-2026-10-07.md`
的结论一致：**票面越大，固定费摊得越薄**；25 USDT 上单是提币费就 116 bps。

## 11. X Layer 卖出腿：已成功（同日 16:34–16:38）

用 §10 桥回来的 **0.009533097916934181 SPYx** 当卖出腿样本
（operator 2026-10-07 指示保留它做这件事）。

### 11.1 路线与结果

聚合器（`onchainos swap`）给的路是**两跳**，不是一条池子直接换：

```text
原生 SPYx 0x90a2a4c7…  --(xStocks wrap V2)-->  wSPYx 0xe7e553cd…
wSPYx                  --(Uniswap V3, 5 bps)-->  USDG 0x4ae46a50…
```

census 里 X Layer 只有 **wrapper** 池（`0x07c40850…`，深度 $1.89M）；
桥给我们的是**原生**币，所以卖出必须先 wrap。这条很容易漏。

| 项 | 值 |
|---|---|
| 卖出数量 | 0.009533097916934181 SPYx（全部，剩 1 wei 尘埃） |
| 滑点上限 | **0.5%**（探针放宽；policy 是 10 bps，走通后再收紧） |
| 报价冲击 | −0.02% |
| approve | `0x0000000000000000000000000000000000000000000000000000000000000000`，gas 53 710 |
| swap | `0x0000000000000000000000000000000000000000000000000000000000000000`，SUCCESS，gas 740 934 |
| 到手 | **7.433480 USDG**（报价 7.431432，实际更好） |
| 我们自己付的 gas | **0 OKB**（OKX TEE 委托账户代付） |

### 11.2 一次失败：返回了 txHash，但交易没广播

第一次 `swap execute` 返回 `ok:true` 和 txHash
`0x9646d4c1…`，但 `eth_getTransactionReceipt` 一直拿不到回执。
查 `onchainos wallet history --tx-hash` 看到真相：

```text
txStatus: ERROR
failReason: com.okcoin.strategy.exception.ErrorBroadcastResultException:
            in-flight transaction limit reached for delegated accounts
```

approve 成功了，swap 被钱包后端的在飞上限挡掉。重试同一命令（
`approveTxHash: null`，说明 allowance 已存在会跳过 approve）即成功。

**结论：txHash 不是广播成功。** 每一步都要回执 + 余额双确认，这条要进执行器。
