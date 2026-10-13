# xStocks 跨链充进 Kraken：流程、亏钱点和不要再犯的错

整理日期：2026-09-27。依据 2026-09-26 至 09-27 这一整段对话里已经发出的交易和只读核对。操作步骤、合约地址和账户表在 [xstocks-bridge-to-solana-2026-09-26.md](research/xstocks-bridge-to-solana-2026-09-26.md)。这份只记框架和教训。

`docs/knowledge/` 是外部公开资料，不能把这次的结论塞进去当「别人说过的话」。也不要做成数据库：下次对话不会先去查表，漏掉的就是会再锁一笔币的那一条。入口在 `AGENTS.md` 第 7 条。做跨链、wrapper 赎回、Kraken 充提之前先读这份。

## 这次实际走通的框架

最初的计划是：X Layer 上的 MRNAx → 以太坊 → Kraken 卖出 → 平 OKX 空头。做完只读之后改了。这个 Kraken 账户的 MRNAx **只收 Solana**，不收以太坊。走通的是下面这条，而且只对 MRNAx、只从 X Layer 出发验证过。

1. 先问 Kraken `DepositMethods`（`aclass=tokenized_asset`）。方法名里写的网络才是能充的网络。没有的网络不要桥过去。
2. 手里如果是 wrapper，在**同一条链**上 `redeem` 成原生。赎回不跨链。
3. 源链准备 gas：X Layer 是原生 OKB，不是 WOKB。这次用 1 USDG 换成官方 WOKB `0xe538905c…`，再 `withdraw` 成原生 OKB。
4. Solana 钱包先有 SOL。给这个 mint 建好 Token-2022 关联账户。SOL 打进钱包 `AmkjszArk…`，不要打进关联账户地址。
5. 只 `approve` 这座桥、只批准这一次的数量。桥合约四条 EVM 链都是 `0x9ec0e4a4c411493773e01e2abf4d42395788846b`，字节码相同，余额不互通。
6. `send` 的附加数据是 `abi.encode(uint64 bitmap, bytes32[] accounts)`，11 个账户，bitmap `772`。不是 Chainlink 文档里带 `0x1f3b3aba` 的通用 SVM 结构。
7. 发出去之前解码 calldata。源链 `eth_call` 成功不够。要看到 11 个账户、这个 mint、已经存在的关联账户、以及这条源链自己的登记账户。
8. 先发最小数量，等 Solana 余额增加，再发剩下的。
9. 到自己的 Solana 钱包之后，再转到 `DepositAddresses` 返回的地址。数量不低于该方法的最小充值。
10. Kraken 入账之后才卖。卖出价不能低于基准的 99.5%。基准先用 OKX 永续标记价，没有合约价就用法币股票的最新价，休市用前一笔。整组（卖出净得、买入成本、桥费、gas、Kraken 费、平空盈亏、已付资金费）算下来是赚的才卖。成交确认之后再平 OKX 上对应的空头。

MRNAx 这次的时间：剩余 0.5399 枚在 22:29（UTC+9）从 X Layer 发出，22:50 源链确认，22:55 Solana 入账。Chainlink 文档把 X Layer 最终确认估成约 1 小时。不要把 26 分钟抄到别的链。

卖出没有做。当时买盘是空的，唯一卖单在 300 美元，OKX `MRNA-USDT-SWAP` 标记价约 199.73。空头还留着。失败的第一笔 0.02 枚还锁在桥里，没有铸到 Solana。

## 会直接亏钱的地方

| 做错什么 | 结果 | 下次怎么停 |
|---|---|---|
| 充进 Kraken 的数量低于 `DepositMethods` 的最小量 | 充值失败，而且永久不到账。后面再补一笔达标的，也找不回第一笔 | 探针留在自己钱包。只有 `余额 ≥ 最小量` 才转到充值地址。MRNAx 的 Solana 最小量是 0.0164 |
| 把 SOL 打进还不存在的关联账户地址 | 那个地址变成普通 SOL 账户，这个 mint 的 Token-2022 账户永远建不出来，桥会一直失败 | SOL 只进钱包 `AmkjszArk…`。建账户用 Token-2022 的关联账户指令 |
| 消息里账户列表是空的，或者用了通用 SVM 附加参数 | 源链扣币成功，Solana 报 `AccountNotEnoughKeys`。消息内容不能改，重放也加不进账户。0.02 枚 MRNAx 就是这样锁住的 | 解码 calldata，账户个数不是 11 就不广播。不要用 `eth_call` 成功代替这一步 |
| 关联账户还没建就发桥，但 11 个账户是对的 | Solana 报 Token-2022 `IncorrectProgramId`。币没铸出来。等 20 分钟可以手动重放**同一条**消息。0.0001 那笔是这样救回来的 | 先建账户，读到 `owner == Token-2022`，再发桥 |
| 在错误的链上转「同一串合约地址」 | EVM 上原生和 wrapper 的地址经常各链相同，余额各算各的。从 Arbitrum 转到 Kraken 的以太坊充值地址，币回不来 | 充值前对 chainId。以太坊充值只在 chainId 1。方法名必须是 `{符号} - Ethereum` |
| 把 wrapper 充进 Kraken，或把原生充进只收另一种网络的方法 | Kraken 收的是该方法指定网络上的原生 xStock | `asset()` 对上原生地址之后才赎回。充值地址只来自 `DepositAddresses`，不抄上一只代币的地址。这次 Solana 充值地址碰巧相同，下一只仍要现查 |
| 先平 OKX 空头，现货还没在 Kraken 成交 | 变成裸多代币。反过来，卖出之后平空失败，变成裸空 | 平空发生在 Kraken 成交确认之后。平空失败就停，不自动重开 |
| 买一高于买入价就卖 | 空头开在 177.34。现货卖在更高的价，平空会把价差亏回去 | 用整组盈亏。卖出价还不能低于基准的 99.5% |
| 池子价格不除 `convertToAssets` | 1 枚 wAAPLx 赎回 1.003269 枚原生。不除这个数，会把乘数看成折价或折溢价 | 便宜的 wrapper 先读 `previewRedeem`，再和正股或 OKX 标记价比 |
| 把 X Layer 那 11 个账户原样拿去发 Ethereum、BNB、Arbitrum | 第 3 号账户里写的是 X Layer 选择子 `3016212468291539606`。源链不对，Solana 执行会失败，而且消息改不了 | 换链就换登记账户。表在跨链说明里。mint、token_config、托管账户、关联账户按代币另算 |

`approved_sender` 报错不是要再付一笔授权费。它是接收程序要读的一个账户。列表是空的，程序读到该放它的位置时后面没有账户了。

## 当时没先注意到、后来才对上的

- 帮助中心写 Kraken 的 xStocks 可以走 Solana、Ethereum、TON、Ink。这个账户的 MRNAx 实际只有 `MRNAx - Solana`。以 `DepositMethods` 为准，不以帮助中心的总表为准。AAPLx 等方法名不写网络的那一条，地址和 MRNAx 的 Solana 充值地址相同，但它不叫 Solana，下次仍要现查。
- 源链模拟只跑桥的 `send`。它不执行 Solana 上的接收程序。两次失败都是源链成功、Solana 失败。
- 9 月 24 日 Solana 托管账户是空的，所以停了。9 月 26 日账户已经在。失败原因从「托管账户不存在」变成「消息格式不对」，又变成「关联账户不存在」。不能用三天前的链上状态决定今天发不发。
- 画布里便宜的一行经常是 `wrapper_v2`，不是桥登记的原生代币。桥不收 wrapper。Solana 和 TON 上这些品种没有 wrapper，买到的就是原生。
- 赎回和充值是两件事。在 Arbitrum 上 redeem，币还在 Arbitrum。合约地址和以太坊一样，Kraken 的以太坊地址也收不到。
- 用户说转入了 0.02 ETH。链上交易 `0xa63f3395…` 实际是 **0.002 ETH**。以后以回执金额为准。这条路径后来没有用到这笔 ETH，因为 MRNAx 没走以太坊充值。
- 钱包里能花的是 USDG 15.22，不是 USDT。USDT 和 USD₮0 余额是 0。DEX 换出来的 OKB 是 WOKB，桥费要的是原生 OKB。
- Kraken 周末 `MRNAxUSD` 的状态是 `post_only`，买盘可以是空的。这不是「美股一收盘就永远不能卖」。周日 20:00 美东到周五 20:00 美东可以成交。operator 后来要求：不卡美股开市，但价格偏离基准超过 0.5% 不卖。`post_only` 时段只能挂不会立刻吃单的限价单，不要排队市价单去赌开盘。
- 卖出记入 Kraken 里的美元（`ZUSD`），不是 USDT，也不是链上的 USDG。提回 X Layer 是先买 USDT，再提到 USD₮0 `0x779ded0c…`，然后才换成 USDG。固定提币费量级大约 1 USDT，比交易手续费大。卖出密钥这次按 operator 的要求同时有提币权限。生产密钥不要把提币和测试密钥混用，也不要贴进对话。
- 计划开头写了「这一步不赎回」。待办又写了「有币就做最小赎回」。结果从对冲仓 wCRWVx 里赎回了 0.0001。功能证明了，仓位也被改了。下次「最小探针」如果打在正在对冲的仓上，先问 operator，不要从互相打架的句子里自己选一条去广播。
- 公开接口的交易对要带 `aclass=tokenized_asset`。普通现货写法会报未知交易对。`status=post_only` 时不能当成买一可以吃。

## 下次开工按这个顺序停

做之前读 `DepositMethods` 和钱包余额。方法里没有的网络，不桥。

手里的合约地址先和登记表对。是 `wrapper_v2` 就只在该链 `redeem`。`asset()` 不等于原生地址，不签名。

目的地是 Solana 时，关联账户先存在。用 `getMultipleAccounts` 看 `owner`，不要看「地址推算得出来」就算存在。

组装 `send` 之后把 calldata 解码出来，对着跨链说明里的表核对。对不上就不广播。先发最小量。Solana 余额没增加，剩下的不动。

充值地址当次从 `DepositAddresses` 拿。数量低于最小量，不转。

Kraken 成交状态不是 `Settled` 或成交回报没确认，不平 OKX。平空数量对这一个套利组，不按旧的持仓快照。

从 Ethereum、BNB、Arbitrum、Optimism、Ink、HyperEVM 出发的桥，还没有一笔成功样本。登记账户已经找到，token_config 和托管账户没有按第二只代币验证过。在第二只代币的最小探针到账之前，不要把 MRNAx 的另外 10 个账户套过去。Mantle 桥不到 Solana。TON、Tron 不在这座公开桥里。
