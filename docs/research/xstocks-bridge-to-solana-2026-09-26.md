# 把 xStocks 桥到 Solana 再充进 Kraken

这份是给以后照着做的。已经用真钱走通的只有一条：X Layer 上的原生 MRNAx → Solana → Kraken。Ethereum、BNB Smart Chain、Arbitrum 上的桥合约已经核对过，但还没有从这三条链广播过。不要把 MRNAx 那 11 个 Solana 账户原样拿去发别的链。

亏过的钱和不能再犯的判断在
`docs/execution-lessons-xstocks-bridge-2026-09-26.md`。发之前先读那份。

`docs/plan.md` 里「不自动做跨链」仍然有效。这里只记录手动路径。

## 先看结论

1. 公开桥是 xStocks 的 Chainlink CCIP。Ethereum、BNB、Arbitrum、X Layer 四条链上的桥合约地址相同，运行时字节码也相同（都是 1159 字节，SHA-256 前 16 位都是 `7a30e7dc94a4ea42`）。调用形式可以共用。填进消息里的 Solana 账户不能共用。
2. 每个代币要在我们自己的 Solana 钱包里有一个 Token-2022 关联账户。它按「钱包 + 这个代币的 Solana mint」唯一，不是按源链唯一，也不是按每一笔转账重建。MRNAx 已经建好了。AAPLx 是另一个 mint，要另建。
3. 关联账户必须在 Chainlink 执行之前就存在。不存在时，Solana 会报 `IncorrectProgramId`，币不会铸出来。不要往那个还不存在的地址直接转 SOL，否则它会变成普通 SOL 账户，这个 mint 的关联账户就永远建不出来。
4. Kraken 收哪条链，按代币查，不能从 MRNAx 外推。这个账户上，MRNAx 只有 Solana。AAPLx、TSLAx、NVDAx、MSTRx、HOODx、CRCLx、SPYx、QQQx、GLDx 都有 Ethereum，另外还有一个不写网络名的方法。那个不写名的方法返回的地址，和 MRNAx 的 Solana 充值地址是同一个。SPCXx、SKHYx 把 Solana 写在方法名里，地址也是同一个。
5. 画布里便宜的那一行，经常是 wrapper，不是这座桥登记的原生代币。地址对不上就不能拿去 `approve` 和 `send`。

## 已经走通的那条路

钱包：

- EVM（X Layer 和以太坊主网是同一个地址）：`0xabababababababababababababababababababab`
- Solana：`AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev`

MRNAx：

- X Layer 原生：`0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f`（18 位小数）
- Solana mint：`XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP`（8 位小数）
- 我们的关联账户：`67asuidxpJYv1FxGmekDPPyixCU7zxtQTNu4QjxLy2xs`

桥合约：`0x9ec0e4a4c411493773e01e2abf4d42395788846b`。目的链选择子固定是 Solana 主网 `124615329519749607`。

成功的那两笔都走这个调用：

```text
send(uint64 destinationChainSelector,
     bytes32 receiver,
     address token,
     uint256 amount,
     bytes extraArgs)
```

`receiver` 是我们的 Solana 公钥，编成 32 字节。`amount` 用源链的 18 位小数。`extraArgs` 是 `abi.encode(uint64 bitmap, bytes32[] accounts)`，不是 Chainlink 文档里那个带 `0x1f3b3aba` 标签的通用 SVM 结构。MRNAx 这次的 bitmap 是 `772`，账户 11 个。桥自己再把这段写进 CCIP 消息。

源链上的 `eth_call` 只能证明「消息能发出去」。它不执行 Solana 上的接收程序。发出去之前要解码 calldata，确认下面四项，而不是只看模拟没回滚：

- 账户个数是 11
- 其中有这个代币的 Solana mint
- 其中有我们这个 mint 的关联账户，并且这个账户已经存在
- 源链登记账户里的链选择子，等于这条源链的选择子

### 两次失败，分别是什么

第一笔 0.02 枚，交易 `0x0000000000000000000000000000000000000000000000000000000000000000`，X Layer 上成功，币锁进了桥。Solana 执行失败，错误是 Anchor `3005 AccountNotEnoughKeys`。消息里的账户列表是空的，收款地址是全零。接收程序要读的第一个账户叫 `approved_sender`，后面没有账户了。这不是要再付一笔钱。这条消息的内容已经固定，手动重放也加不进账户。这 0.02 枚还留在锁定合约里，没有在 Solana 上铸出来。

第二笔 0.0001 枚，交易 `0x0000000000000000000000000000000000000000000000000000000000000000`，11 个账户是齐的。Solana 仍然失败，错误是 Token-2022 的 `IncorrectProgramId`。接收程序从托管账户往我们的关联账户 `67asuidxp…` 转账，但那个关联账户还不存在，我们的 Solana 地址当时也没有 SOL。给钱包转入 0.05 SOL 之后，用租金把 Token-2022 关联账户建出来，等满失败后的 20 分钟，再手动执行同一条消息，0.0001 枚才到账。

然后用同一套账户把剩下的 0.539900002628231336 枚发出去，交易 `0x0000000000000000000000000000000000000000000000000000000000000000`。22:29 发出，22:50 源链确认，22:55 Solana 入账（都是 UTC+9）。合计 0.540000 枚充进 Kraken，状态 `Settled`。充值交易 `REDACTED_SOLANA_SIGNATURE`。

X Layer 这一跳的实测大约 26 分钟。Chainlink 把 X Layer 的最终确认估成约 1 小时。Ethereum、BNB、Arbitrum 还没有实测，不要把 26 分钟抄过去。

## 那 11 个账户里，哪些换了就会错

下面是成功那笔 `0xde280184…` 解码出来的列表。bitmap `772` 把第 2、8、9 号标成可写。

| # | 地址 | 这一笔里它是什么 | 换代币或换链时 |
|---|---|---|---|
| 0 | `Eqebtzd3ZeZkML1DHqwo9tViEcLXrkohqJ4McwgtEJCo` | 接收程序名下的账户，数据只有 8 字节。空列表时程序就是在这里报 `approved_sender` 不够 | 不要原样抄到别的源链。先确认它对应的是哪条源链 |
| 1 | `3VLdCUjPeBoyFbXQoQK1BpCd79jsCzdeJSnoifM2qN3B` | 接收程序的账户，129 字节。只在 MRNAx 这一笔里见过 | 没验证过能不能给别的代币共用 |
| 2 | `DVhKqb6u4Ukrj217Goi8HtCiJ8yLBUaoVJQpy4hYgtDr` | 接收程序的账户，2056 字节。bitmap 把它标成可写 | 同上 |
| 3 | `5RxSBZJZ1Qai79MPrGTp6qiqp4Bw99F2nfB2ZPjsB5P5` | 里面写着 X Layer 的链选择子 `3016212468291539606`，以及桥地址 `0x9ec0e4a4…` | 源链一变就必须换。Ethereum 的选择子是 `5009297550715157269`，BNB 是 `11344663589394136015`，Arbitrum 是 `4949039107694359620` |
| 4 | `9Unx3Ey7EfnXVGbPST7aGjN6Gqbvye6wQb2wLJ19cCNf` | MRNAx 的 `token_config`。数据里嵌着这个 mint 和托管账户 | 每个代币一个 |
| 5 | `XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP` | MRNAx 的 Solana mint | 每个代币一个 |
| 6 | `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` | Token-2022 程序 | 这些 xStocks 共用 |
| 7 | `8sBYczs8BUvFpWXn8rM5uY2UHUEizC3J1QXjpfdPDJJ8` | 接收程序的账户，41 字节 | 没验证过能不能给别的代币共用 |
| 8 | `9awzznyBPLZRr8AJUT1BgD7JpPBtCobPYFrL7CNefU7b` | MRNAx 的托管代币账户。失败那次就是从这里转出 | 每个代币一个 |
| 9 | `67asuidxpJYv1FxGmekDPPyixCU7zxtQTNu4QjxLy2xs` | 我们的 MRNAx 关联账户 | 每个代币一个。必须事先存在 |
| 10 | `AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev` | 我们的 Solana 钱包 | 同一把钱包就不变 |

接收程序是 `F2ZYgyh8YDG6rHo9F1T8Aw4WoTyy8eYN3GgM2LaPKcZN`。第 0、1、2、3、4、7 号都归它管。

所以：

- 再桥 MRNAx，不管从 X Layer 还是以后从别的 EVM 链，关联账户都还是 `67asuidxp…`，不用再建。从 Ethereum、BNB、Arbitrum 发的时候，第 3 号必须换成那条链的登记账户。这个账户我们还没有找出来，也还没有发过探针。
- 换一个代币，第 4、5、8、9 号都要换成那个 mint 的。第 9 号要先建好。第 1、2、7 号能不能沿用，要等第二个代币的成功消息对比，不能现在就定。

## 每个代币要不要单独建 Solana 账户

要。建一次就够。

关联账户的钥匙是「我们的钱包 + Solana mint」。源链不参与这把钥匙。同一枚 AAPLx，从 Arbitrum 桥过来和从 Ethereum 桥过来，进的是同一个 mint `XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp`，所以只建一个关联账户。

建的时候：

1. SOL 打进钱包 `AmkjszArk…`，不要打进关联账户地址。
2. 按 Token-2022 创建关联账户。租金大约 0.002 SOL，从钱包扣。
3. 创建成功之后再发桥。桥的执行不会替我们创建这个账户。

Kraken 那个充值地址不用我们去建。路径是先桥到自己的钱包，确认 mint 和余额，再做一笔 SPL 转账到 Kraken。直接把 Kraken 的地址写进桥的收款人，我们控制不了它名下的关联账户，这一步没有试过。

钱包里还留着上次没用完的 SOL。新代币建账户之前先读余额。

## 画布上的代币，桥的是哪一个合约

桥的 `products` 登记的是原生代币。EVM 上同一个产品的原生地址，在 Ethereum、BNB、Arbitrum、X Layer 上是同一串十六进制，但余额按链分开。18 位小数进桥，Solana 上按 8 位小数记账。

下面这些是桥登记的原生地址，不是画布里每一个便宜池的地址。采样在 `data/xstocks_book.sqlite`，2026-09-26 的桥列表。

| 代币 | 桥上的 EVM 原生地址 | Solana mint |
|---|---|---|
| AAPLx | `0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a` | `XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp` |
| TSLAx | `0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0` | `XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB` |
| MSTRx | `0xae2f842ef90c0d5213259ab82639d5bbf649b08e` | `XsP7xzNPvEHS1m6qfanPUGjNmdnmsLKEoNAnHjdxxyZ` |
| HOODx | `0xe1385fdd5ffb10081cd52c56584f25efa9084015` | `XsvNBAYkrDRNhA7wPHQfX3ZUXZyZLdnCQDfHZ56bzpg` |
| CRCLx | `0xfebded1b0986a8ee107f5ab1a1c5a813491deceb` | `XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1` |
| NVDAx | `0xc845b2894dbddd03858fd2d643b4ef725fe0849d` | `Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh` |
| SPYx | `0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48` | `XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W` |
| SPCXx | `0x68fa48b1c2fe52b3d776e1953e0e782b5044ce28` | `Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8` |
| QQQx | `0xa753a7395cae905cd615da0b82a53e0560f250af` | `Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ` |
| GLDx | `0x2380f2673c640fb67e2d6b55b44c62f0e0e69da9` | `Xsv9hRk1z5ystj9MhnA7Lq4vjSsLwzL2nxrwmwtD3re` |
| SKHYx | `0x58100046a4afcd4ee4fadbd4244f3f895a341c56` | `XsnhgGRQwhExfS2bmWzR6EYddKGPRGDEjeJsatkmKqU` |
| MRNAx | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP` |

画布 `xstocks-price-gap.canvas.tsx` 里，AAPLx 的 Arbitrum 和 Ethereum、TSLAx 的 Ethereum 和 BNB、MSTRx 的 Ethereum 和 BNB、HOODx 的 BNB、CRCLx 的 BNB、SPCXx 的 Ethereum，合约地址和上表一致，形态是原生，可以拿去桥。

对不上的例子：画布里的 NVDAx、SPYx、MRNAx 有不少行是 wrapper（例如 NVDAx 的 `0xa8ddb5cd…`，MRNAx 的 `0xce0fbc16…`）。那是另一个合约。要先换成上表里的原生地址，再批准桥。MRNAx 这次就是先从 wrapper 赎回，再桥原生。

画布上 `network = Solana` 的行已经在 Solana，不用走这座桥。买到之后确认进的是我们钱包名下的 Token-2022 账户，再转到 Kraken。

## 这三条链实际要做的事

三座桥都能发到 Solana。合约都是 `0x9ec0e4a4c411493773e01e2abf4d42395788846b`。链 id：Ethereum `1`，BNB `56`，Arbitrum `42161`。

| | Ethereum | BNB Smart Chain | Arbitrum |
|---|---|---|---|
| 付桥费和 gas 的币 | 主网 ETH | BNB | Arbitrum 上的 ETH，不是主网那 0.002 ETH |
| 链选择子 | `5009297550715157269` | `11344663589394136015` | `4949039107694359620` |
| 我们有没有广播过 | 没有 | 没有 | 没有 |
| 主网 ETH 余额 | 今天早些时候确认过 0.002 ETH，发桥之前要重读 | 这次没读到 | 这次没读到 |

Kraken 这条账户上，2026-09-26 用 `DepositMethods`（`aclass=tokenized_asset`）看到的充值方式和最小数量：

| 代币 | Ethereum 最小量 | 不写网络名的方法（地址与 MRNAx 的 Solana 充值地址相同） | TON | 方法名里写明 Solana |
|---|---|---|---|---|
| AAPLx | 0.00461503 | 0.00501634 | 0.00401307 | 没有单独这一行 |
| TSLAx | 0.00260000 | 0.00310000 | 0.00230000 | 没有 |
| NVDAx | 0.00621054 | 0.00641088 | 0.00621054 | 没有 |
| MSTRx | 0.00320000 | 0.00270000 | 0.00660000 | 没有 |
| HOODx | 0.00910000 | 0.01210000 | 0.00930000 | 没有 |
| CRCLx | 0.00780000 | 0.00560000 | 0.01350000 | 没有 |
| SPYx | 0.00170971 | 0.00170971 | 0.00160914 | 没有 |
| QQQx | 0.00180622 | 0.00190656 | 0.00180622 | 没有 |
| GLDx | 0.0033000 | 0.0034000 | 0.0028000 | 没有 |
| SPCXx | 0.00810000 | 没有这一行 | 0.00810000 | 0.00810000 |
| SKHYx | 0.00740000 | 没有这一行 | 0.00740000 | 0.00740000 |
| MRNAx | 没有 | 没有 | 没有 | 0.01640000 |

充值费这几行都是 0。低于最小量的充值会失败并且不到账，后面补一笔也不会把第一笔找回来。

因此：

- 币已经在 Ethereum 上，而且 Kraken 有 `代币 - Ethereum`：不要桥。用 `DepositAddresses` 拿 Ethereum 方法的地址，做一笔 ERC-20 转账。主网 gas 用 ETH。
- 币在 BNB 或 Arbitrum 上：Kraken 不收这两条链。要先桥到 Kraken 收的网络。Solana 是已经走通过的落地，Ethereum 也在目的地列表里，但 EVM 到 EVM 用的不是上面这 11 个账户，这条我们没发过。
- MRNAx 只能走 Solana。

Solana 充值地址这次三只代币返回的是同一个：`48SEVUduDW84t9SLxzGdxXb55qnx74wgLA3eGAYtKPyX`，没有 memo。下一只代币仍然要现查，不要把这个地址写死进代码。

## 下一次从 BNB、Arbitrum 或 Ethereum 发往 Solana

按这个顺序。任何一步对不上就停，不要先把整仓发出去。

1. 读手里的合约地址。必须等于上表里该链的原生地址。是 wrapper 就先换成原生。
2. 查 `DepositMethods`。数量要大于准备使用的那条充值方式的最小量。探针可以更小，探针不要打进 Kraken。
3. 给源链准备原生 gas。Ethereum 用主网 ETH，Arbitrum 用 Arbitrum ETH，BNB 用 BNB。桥费用 `getDeliveryFeeCost` 按当次数量报，不要用 MRNAx 在 X Layer 上的 0.0008 OKB 去套。
4. 给 Solana 钱包准备 SOL。如果这个 mint 的关联账户还不存在，先创建。MRNAx 的已经在，跳过。
5. `approve` 只批准这座桥、只批准这一次要发的数量。
6. 组装 `send`。目的选择子用 Solana 的 `124615329519749607`。第 3 号账户里的选择子必须是这条源链的，不能还是 X Layer 的 `3016212468291539606`。mint、token_config、托管账户、关联账户必须是这个代币的。
7. 解码准备广播的 calldata，逐项核对。源链模拟通过不够。
8. 先发最小的一笔。MRNAx 的探针是 0.0001 枚，别的代币不要假设也是这个数，以合约接受的最小数量为准。
9. 等 Solana 执行成功，余额出现在我们的关联账户里，再发剩下的。
10. 到账数量不低于 Kraken 最小充值时，再转到 `DepositAddresses` 返回的 Solana 地址。

源链模拟成功、Solana 执行失败时：消息内容是对的，就建好账户，等 20 分钟再手动执行这一条。消息内容是错的（账户是空的，或者选择子是别的链），重放没有用，不要再从源链补发一笔去「覆盖」它。

## wrapper 怎么赎回

画布里标成 wrapper 的那一行，是**这一条链自己的**包装代币。发行方登记名是 `wrapper_v2`。同一只股票在 Ethereum、BNB、Arbitrum、X Layer、Ink、Optimism、Mantle、HyperEVM 上往往是同一串地址，但是各链单独部署，余额互不相通。这些品种在 Solana 和 TON 上没有 `wrapper_v2`。那边买到的就是原生代币，没有赎回这一步。

赎回出来的是**同一条链上的原生 xStock**。不是法币股票，也不是另一条链上的币。在 Arbitrum 上 `redeem`，得到的是 Arbitrum 上的原生代币。合约地址和以太坊相同，Kraken 的以太坊充值地址也收不到。

调用的合约是 wrapper 自己，不是桥，也不是每条链一个不同的赎回路由。方法是 ERC-4626，发行方说明见 [Wrapped xStocks](https://docs.xstocks.fi/developers/wrapped-xstocks)：

- 按手里的 wrapper 数量赎回：`redeem(uint256 shares, address receiver, address owner)`
- 按想要的原生数量赎回：`withdraw(uint256 assets, address receiver, address owner)`
- 签名前先读 `previewRedeem` 或 `convertToAssets`

我们自己的钱包既是 `owner` 又是 `receiver` 时，不必再单独 `approve`。只认登记表里的 `wrapper_v2`。签名前 `asset()` 必须等于该链的原生地址。旧的 v1 wrapper 只许解包，不能当价格用。

1 枚 wrapper 不是 1 枚原生。2026-09-27 对 wAAPLx `0x943bf64d566c32a2bcd41ac92fb63c111cc9de8f` 读过 X Layer、Ethereum、BNB、Arbitrum：字节码都是 2138 字节，SHA-256 前 16 位都是 `072b307eadb63d95`；`asset()` 都指向原生 AAPLx `0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a`；`convertToAssets(1e18)` 都是 **1.0032690125398187** 枚原生。池子价格不先除这个数，会把乘数看成折价。

各链仍然不同的是 gas 币：主网 ETH、BNB、Arbitrum 上的 ETH、X Layer 的 OKB。赎回这一步不跨链。

登记表里这几只的 `wrapper_v2` 地址（EVM 各链相同）：

| 代币 | wrapper_v2 | 赎回后的原生地址 |
|---|---|---|
| AAPLx | `0x943bf64d566c32a2bcd41ac92fb63c111cc9de8f` | `0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a` |
| TSLAx | `0xc3fdbe3a68ee5de461d30415a8165cf9aefe1171` | `0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0` |
| MSTRx | `0x30987adf0b11dc698438a99ba04ec3a1ab2c7eab` | `0xae2f842ef90c0d5213259ab82639d5bbf649b08e` |
| HOODx | `0x59801175a9b2248f9bf4ba7f82e17045c4672ec8` | `0xe1385fdd5ffb10081cd52c56584f25efa9084015` |
| CRCLx | `0xb11134f14d5b94db60d4599dfdc3bf1bba2150e8` | `0xfebded1b0986a8ee107f5ab1a1c5a813491deceb` |
| NVDAx | `0xa8ddb5cd96b5222afe198316e9a57caa642850d5` | `0xc845b2894dbddd03858fd2d643b4ef725fe0849d` |
| SPYx | `0xe7e553cd128f0011777323a0b44a7b96ea1cb540` | `0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48` |
| QQQx | `0x4c1ae29c159838fc1b224636e28e086eb69101f7` | `0xa753a7395cae905cd615da0b82a53e0560f250af` |
| GLDx | `0x735f1509bff25e27cd442b9bfb231324648ead9b` | `0x2380f2673c640fb67e2d6b55b44c62f0e0e69da9` |
| SPCXx | `0x8e2eed8b8b5e13ea7bf38e50d7821d2c57309072` | `0x68fa48b1c2fe52b3d776e1953e0e782b5044ce28` |
| SKHYx | `0x6215a58ed045d71f2561aaabe54f4c885c522998` | `0x58100046a4afcd4ee4fadbd4244f3f895a341c56` |
| MRNAx | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` |

## Kraken 从以太坊充值

这个账户上，方法名是 `DepositMethods` 返回的 **`{符号} - Ethereum`**，例如 `AAPLx - Ethereum`。再调 `DepositAddresses`（`aclass=tokenized_asset`，`new=false`）拿 `0x` 地址。返回里没有 memo。转账是以太坊主网（chainId 1）上的**原生**代币，普通 ERC-20 `transfer`。不要转 wrapper。不要从 Arbitrum 或 BNB 转同一串地址。

最小量和「不写网络名 / Solana / TON」的对照见上面的充值表。充值费是 0。低于最小量会失败并且不到账。MRNAx 没有 Ethereum 这一条。

这条充值还没有用真币走过。MRNAx 进 Kraken 走的是 Solana。钱包在以太坊上没有这些原生代币，所以这一步停着。

## 2026-09-27 核对结果

画布上 33 个 wrapper 部署，加上 Kraken 能收的 12 只在 HyperEVM、Ink、Optimism、Mantle 上的 `wrapper_v2`，读的是 `asset()`、`convertToAssets(1e18)`、`previewRedeem(1e18)`、原生合约的 `multiplier()`，以及这个钱包的余额。字节码对得上的都是 2138 字节，SHA-256 前 16 位 `072b307eadb63d95`。`asset()` 等于该品种的原生地址。`convertToAssets`、`previewRedeem` 和 `multiplier()` 三者相等。1 枚 wrapper 赎回几枚原生，以这个数为准，不是 1。

例外只有两处：

- HyperEVM 上没有 MRNAx 的 `wrapper_v2`。登记表本来就没列这条，合约字节码是空的。不是赎回失败。
- X Layer 上 wCRWVx `0xadf63a79c9813a8e0c4d1d334ebdfaeef04bdf98` 的 `asset()` 是原生 `0x16314d1032e6476c9451d1f02ba365a249ff36c7`，乘数是 1。钱包里原来有 **0.04000766620831878** 枚 wrapper，这是对冲仓，不是画布上的新买。

Optimism 和 Mantle 上这 12 只全部对过。Ink 也全部对过。Mantle 的桥目的地里没有 Solana，所以那边赎回出来的原生代币不能用这座桥发往 Solana。

钱包在以太坊主网上，上面这 12 只原生代币加上 CRWVx，余额都是 0。Kraken 的以太坊充值因此没有发。低于最小量的探针也不打给 Kraken。

### 同链最小赎回

wCRWVx 的 `previewRedeem(0.0001)` 和 `eth_call redeem` 都返回 0.0001 枚原生。已经广播，交易 `0x0000000000000000000000000000000000000000000000000000000000000000`，X Layer 区块 71667849，状态成功。赎回后 wrapper **0.03990766620831878**，原生 CRWVx **0.0001**，两笔都还在 `0xabab…ababab` 的 X Layer 上。没有桥，没有充进 Kraken。其余 wrapper 仓位不动。

### Solana 关联账户

11 只新建的 Token-2022 账户，所有者都是 `AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev`，余额都是 0。MRNAx 的旧账户跳过。租金从钱包的 SOL 扣，剩下 **0.02969072** SOL。

| 代币 | 关联账户 |
|---|---|
| AAPLx | `fWXVKPhsvMxkGHNotdS9PArWhHfYLEkQXbnCaQkLVqZ` |
| TSLAx | `9vM7y4tddsV3w5bDmbAYwVQCeQNn5tRERUQ3CqV4LF6d` |
| NVDAx | `4vPFLrGUEBtd4B9WkE6zmwTi9JZ3c1TT5cc81MsTp8EB` |
| MSTRx | `CuMVkbQzG9zwnMqPppDjXy1LHcFQgXcXuojjW5EHGvm2` |
| HOODx | `Cif3Qde1et2xXFkkfnBQoVeaaRFCtVTziPWKU4oeSGDK` |
| CRCLx | `JtMbd7enpYx2mjafmrwYe5VLG2StR1gL1BfDhvHzRA5` |
| SPYx | `FRhMhtCxF2H9H1hP3KV4uQzR5r5mYN6ALkua9gRPC5hv` |
| QQQx | `DKCCSWLcPCoRrE5SWYgybXjffFWGC2sxDBYxwCDNyvRa` |
| GLDx | `8G5b1fggQeXvaariuRh92UqQGoRBFXLbmnYxTiXZbyyY` |
| SPCXx | `44TqXB4qYS8FfAhtdyqtPJv2qNkhkhXjCJYteYVMYn7N` |
| SKHYx | `Cxv4HPhc2r6w1ye9w6KnZKc9WDWL3iebifcuTCQwr8fr` |

创建交易：`REDACTED_SOLANA_SIGNATURE`（AAPLx、TSLAx、NVDAx），`REDACTED_SOLANA_SIGNATURE`（MSTRx、HOODx、CRCLx），`REDACTED_SOLANA_SIGNATURE`（SPYx、QQQx、GLDx），`REDACTED_SOLANA_SIGNATURE`（SPCXx、SKHYx）。

### 各链发往 Solana 时要换的登记账户

接收程序 `F2ZYgyh8…` 名下、数据长度 54、并且含桥地址 `0x9ec0e4a4…` 的账户，按里面的链选择子对上了。这是成功那笔 MRNAx 消息里的第 3 号账户这一类。换源链时换这一格。mint、token_config、托管账户、关联账户仍然按代币另算，这张表不代替它们。

| 源链 | 选择子 | 账户 |
|---|---|---|
| Ethereum | `5009297550715157269` | `6VU9T42q3bGUTmoZ8LA5rxZ2Tdom86uLekj5c2N4w8mp` |
| BNB | `11344663589394136015` | `EAcd4PLvs42et73FSVoCCfbuXshZ9AE29yN5H6Cic8dz` |
| Arbitrum | `4949039107694359620` | `5rjC32Zd4ugbU74ttkWubH836hSA7oBcCH9neeF2iY5p` |
| X Layer | `3016212468291539606` | `5RxSBZJZ1Qai79MPrGTp6qiqp4Bw99F2nfB2ZPjsB5P5` |
| Optimism | `3734403246176062136` | `9eXPiYQzGiCHE7K5CTfhf4TCny7TFdFeWTfgiyyiKWai` |
| HyperEVM | `2442541497099098535` | `76xfTatJgf2GEFHmkPkkWZXmwbSyXp6yLYsR6XwSJdRz` |
| Ink | `3461204551265785888` | `B5pRXw8nqyFc9EjkfdGrL2KdA4r1oQuY458RMkf9EAp1` |

非 X Layer 的桥探针没有发。这几条链上没有我们的原生代币，而且除了登记账户和已经建好的关联账户，token_config 和托管账户还没按新代币拼进一条完整消息。Mantle 不在这张表里，也桥不到 Solana。

## 还没有做完的

- 没有从 Ethereum、BNB、Arbitrum 发桥。第 3 号账户已经找到，第 4、5、8 号仍要按那只代币现查。第 1、2、7 号是不是所有代币共用，只有 MRNAx 一笔成功消息，不能下结论。
- Kraken 以太坊充值没有发。主网上那 12 只原生余额是 0。
- BNB 或 Arbitrum 桥到 Ethereum、再按 ERC-20 充进 Kraken，这条更短，但也还没发过。不要用 Solana 的 `extraArgs` 去发它。
- TON、Tron 不在这 9 座公开桥里。
