# xStocks 跨链退出：这次亏过的钱和不能再犯的错

2026-09-26 用真钱走完的路径，以及中途锁死的两笔。操作步骤在
`docs/research/xstocks-bridge-to-solana-2026-09-26.md`。这份只回答：
下次再做「链上代币桥到另一条链，再充进 Kraken 卖掉、并平掉 OKX 对冲」时，
哪些判断会亏钱，做到哪一步才算成功。

`docs/plan.md` 的「不自动做跨链」仍然有效。这里不是把跨链放进自动开仓。

## 放在哪里

不放进数据库。`data/xstocks_book.sqlite` 只存发行方登记、部署地址和 Kraken
交易对状态，回答「这个币在哪条链、合约是哪一个」。它不记得一笔失败的消息
为什么取不回来。

流程步骤留在调研说明里。这份教训挂在 `AGENTS.md` 的必读上：讨论 xStocks
跨链、Kraken 充值卖出、或把现货腿挪出 X Layer 时，先读这份，再读操作说明。

## 这次实际走通的框架

仓位原来是：X Layer 上买入原生 MRNAx，OKX `MRNA-USDT-SWAP` 空头对冲。
其他 EVM 链上这个代币没有能成交的池子。Kraken 才是能卖掉的地方。

对 **这个账户的 MRNAx**，Kraken `DepositMethods` 只返回 Solana。
所以路径是：

1. X Layer 用 USDG 换成 **原生 OKB**（DEX 先给 WOKB，再 `withdraw`）。桥费付原生 OKB。
2. Solana 钱包里先有 SOL。按 Token-2022 建好 **我们自己的** MRNAx 关联账户。
3. 桥合约 `send`：目的链是 Solana，附加数据是官方的
   `abi.encode(uint64 bitmap, bytes32[11])`，bitmap = 772。
4. 等 Chainlink 在 Solana 上执行成功，余额出现在我们的关联账户里。
5. 数量不低于 Kraken 最小充值额之后，再转到 Kraken 的 Solana 充值地址。
6. Kraken 账本显示 `deposit` 且余额增加之后，才谈卖出。卖出还要过价格闸门。
7. 卖出成交之后，再平 OKX 上与成交数量匹配的空头。

钱包：EVM `0xabababababababababababababababababababab`（X Layer 和以太坊主网
是同一个地址，余额不共享）。Solana `AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev`。
签名走 OKX 钱包接口，本机没有这把 Solana 私钥。

## 已经亏掉或锁住的

| 数量 | 现在在哪 | 能不能自己救回来 |
|---|---|---|
| 0.02 枚 | 锁在 X Layer 桥的锁定合约里。消息 `0x0000000000000000000000000000000000000000000000000000000000000000` 的账户列表是空的，收款地址是全零 | 不能。消息内容已经固定，重放也加不进账户。要 xStocks 或 Chainlink 处理 |
| 0.0001 枚 | 曾因关联账户不存在而失败。建好账户并在 20 分钟后手动执行，已经到我们的 Solana 钱包，并算进后来充进 Kraken 的 0.54 枚 | 已经回来 |
| 0.5399 枚 | 和上面 0.0001 合成 Solana 上的 0.540000 枚（8 位小数截断），Kraken 账本 `Settled` | 已充进 Kraken，还没卖 |

X Layer 源链成功，只说明币离开了我们的钱包。Solana 余额没增加之前，这枚币
不算到账。

## 不能再犯的错

### 1. 源链模拟通过，不等于对面收得到

`eth_call` 成功只说明 X Layer 上的 `send` 能把币锁进桥、能发出消息。
它不运行 Solana 上的 `ccip_receive`。第一次 0.02 枚就是这样锁死的。

发出去之前要解码即将写入的 CCIP 消息，确认：

- `accounts` 长度是 11，不是 0
- 里面有这个代币的 Solana mint，以及我们这个 mint 的关联账户
- 关联账户在 Solana 上已经存在，并且所有者是 Token-2022
- 消息数据里的收款公钥是我们的钱包，不是全零

只看「模拟没回滚」就广播，是这次最大的亏钱原因。

### 2. 附加数据不是 Chainlink 通用的 SVM 结构

官方桥要的 `extraArgs` 是 `abi.encode(uint64 bitmap, bytes32[] accounts)`。
bitmap `772` 把第 2、8、9 号标成可写。桥合约自己再包成带 `0x1f3b3aba` 的
SVM 参数，并且把 `tokenReceiver` **写成全零**。真正的收款人在消息 `data`
里，也在第 10 号账户。

我们第一次填的是通用 SVM 结构、账户列表为空。源链照样接受，写进消息的
账户数是 0。Solana 报 Anchor `3005 AccountNotEnoughKeys`，点名
`approved_sender`。

`approved_sender` 不是手续费，也不是要再付一笔钱。它是接收程序上已经存在的
权限账户，用来核对「这条源链上的这座桥可以发这个币」。账户没放进消息，
程序读到该放它的位置时后面已经没有账户了。

这条失败消息不能靠加 gas 或重放修好。

### 3. `IncorrectProgramId` 在这里的意思是收款账户还不存在

第二次 0.0001 枚，11 个账户是对的。Solana 执行到 Token-2022 的
`TransferChecked`：从托管账户转到我们的关联账户。关联账户当时不存在，
钱包也没有 SOL。Token-2022 看到目标不是自己名下的代币账户，就报
`IncorrectProgramId`。程序 ID 没有选错，桥也没有禁止这笔转账。

修复是：SOL 打进 **钱包** `AmkjszArk…`，用租金创建 Token-2022 关联账户，
等 Chainlink 允许手动执行（这次配置是失败后 **1200 秒**），再执行 **同一条**
消息。不要把 Chainlink 自己那笔失败交易换个付款人重发，那条指令只接受
他们的 transmitter，会报 `Ocr3UnauthorizedTransmitter`。

不要往关联账户地址直接转 SOL。那个地址一旦变成普通 SOL 账户，这个 mint
的关联账户就建不出来了。

### 4. 先发最小额，到账之后再发剩下的

最小额以 Solana `token_config` 里的 `min_transfer_amount` 为准。MRNAx 是
10000 个 8 位小数单位，等于 **0.0001** 枚。源链按 18 位小数填写，
`getSharesByUnderlyingAmount` 要覆盖这个最小额。

0.02 枚高于最小额，但消息格式是错的，所以锁死的是 0.02 而不是 0.0001。
格式没在消息正文里核对之前，不要发整仓。

### 5. Kraken 收哪条链，每个代币单独查

帮助中心写 xStocks 可以走 Solana、Ethereum、TON、Ink。这个账户上 MRNAx
的 `DepositMethods` 只有 `MRNAx - Solana`，最小 **0.0164** 枚，充值费 0。
没有 Ethereum 方法。把 MRNAx 桥到以太坊再充 Kraken，没有充值地址。

别的代币不能照抄。AAPLx 等有 Ethereum 充值。币已经在 Kraken 能收的那条链上
时，不要再桥一次。

充值地址只用 `DepositAddresses` 当次返回的。低于最小额的充值失败且不到账，
后面补一笔也不会找回第一笔。所以 0.0001 枚不能单独充进 Kraken。

Kraken 普通 `Balance` 不列出代币化资产。入账看
`Ledgers`（`aclass=tokenized_asset`）里的 `deposit` 和 `balance`。
这次 0.540000 枚的状态是 `Settled`。

### 6. 卖出看买价，不看那张 300 美元的卖单

2026-09-26 晚，Kraken `MRNAx/USD` 买盘是空的，唯一卖单是 300 × 0.07。
OKX 标记价大约 199.73。没有买价落在标记价的 99.5% 以上，就不能卖。

闸门：

- 整组盈亏为正。现货卖价要和 OKX 空头的开仓价、标记价一起算。只比现货买入价
  168.64，会在空头上亏回去。空头当时开在约 177.34。
- Kraken 能成交的卖出价不低于基准的 99.5%。卖得更高可以。
- 基准先用 OKX 这张永续的标记价。没有这张合约的价格时，用法币股票最新价。
  股票市场休市，用休市前最后一笔，不用盘中已经失效的报价。

`post_only` 表示现在不能用吃单的市价卖，不是「这个币不许卖」。没有合格买价
时，不要挂一张会在以后乱价成交的单来代替「现在就能卖」。

### 7. 其他容易混的细节

- WOKB 是包装后的 OKB，和原生 OKB 1:1。DEX 换出来的是 WOKB
  `0xe538905c…`。CCIP 桥费要原生 OKB，换完必须 `withdraw`。
- 同一套 EVM 地址在每条链上余额分开。主网上的 ETH 付不了 X Layer 的桥费，
  也付不了 Solana 的租金。
- 画布上便宜的池经常是 wrapper（MRNAx 的 `0xce0fbc16…`）。桥登记的是原生
  `0xefd30445…`。批准和 `send` 的必须是原生。
- X Layer 这一跳实测大约 26 分钟（22:29 发出，22:55 Solana 入账，UTC+9）。
  文档里的 1 小时是上限估计，不是到点才许查。用短间隔查 Solana 余额和
  CCIP 状态。源链成功之后继续查，直到 Solana 余额增加或状态变成失败。
- 18 位小数进桥，Solana 按 8 位小数记账。0.539900002628231336 枚进账变成
  0.54000000。对账用原始单位，不要用四舍五入后的显示值决定「还剩多少」。
- 平 OKX 空头的张数对齐 **已经卖出** 的数量。现货还锁在失败消息里的那一部分，
  不要先平成裸空。
- API 密钥放 `config/kraken.env`，权限 600。不要写进文档，不要贴回对话。

## 发下一笔之前的顺序

1. `DepositMethods` 确认 Kraken 收这条链，以及最小充值额。
2. 读 Solana `token_config`：mint、托管账户、`min_transfer_amount`、是否启用。
3. 我们的 Token-2022 关联账户已经存在。SOL 在钱包里，不在关联账户地址上。
4. 组好 11 个账户。源链选择子、mint、托管账户、关联账户都对得上这一笔。
5. 解码 calldata，确认账户个数是 11，关联账户已存在。
6. 先发最小额。Solana 余额增加之后，再发剩下的。
7. 到账数量达到 Kraken 最小额，再转到 `DepositAddresses` 返回的地址。
8. 账本入账之后，买价和整组盈亏都过闸门，才卖，再平匹配数量的 OKX 空头。
