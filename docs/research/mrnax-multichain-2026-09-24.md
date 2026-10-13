# MRNAx 多链部署与跨链渠道核查（2026-09-24）

> 触发（operator）：我们现在持有的 X Layer `MRNAx`，是不是在别的链上也有？
> 有没有官方渠道能从 X Layer 跨到其他链？

结论先说（2026-09-24 晚补核，代理可用，见第 3 节）：

1. **有，而且是 10 条链。** 发行方登记表里，`MRNAx`
   （ISIN `CH1580495757` → 正股 Moderna `MRNA` ISIN `US60770K1079`）
   列出了 Solana / Ethereum / TON / Mantle / X Layer / Arbitrum / Optimism /
   BNB Smart Chain / HyperEVM / Ink。没有 Starknet。我们持有的是 **X Layer**
   上的原生 `MRNAx`，不是 Starknet。
2. **每条链是独立合约。** 8 条 EVM 链地址相同，只说明部署地址一样；
   X Layer 上的余额不会自动出现在别的链上。
3. **有官方跨链，不是只能赎回再申购。** 公开桥是 xStocks Public Bridge，
   由 Chainlink CCIP 管理，入口 [defi.xstocks.fi/bridge](https://defi.xstocks.fi/bridge)。
   X Layer 上的桥合约 `0x9ec0e4a4c411493773e01e2abf4d42395788846b` 已部署
   （区块 `71485438` 有字节码），产品表登记了 `MRNAx`。从 X Layer 可以发到
   Ethereum、BNB Smart Chain、Arbitrum、Solana、Mantle、Ink。
   Optimism、HyperEVM、TON 上有这个代币，但**不在** X Layer 这座桥的目的地列表里。
4. 同链申购/赎回（USDG、USDC）仍然开着，那是换成稳定币，不是跨链。

---

## 1. 证据与口径

来源：Backed 公开资产登记表（发行方自己维护，不是第三方聚合器）

```bash
curl -sS --proxy http://127.0.0.1:4780 \
  "https://api.backed.fi/api/v2/public/assets/MRNAx"
```

- 抓取时间：2026-09-24（本机代理 `127.0.0.1:4780` 可用时段）
- 该端点是本项目 `scripts/audit_native_pools.py` 已经在用的同一个登记表来源，
  口径与 `config/signed_depth_mappings.toml` 里的 `issuer_symbol = "MRNAx"` 一致。
- 登记表返回的 `deployments[]` 字段才是「在哪些链上有」，与链上监控用的
  `native_address` / `wrapper_address` 可直接对上。

## 2. MRNAx 全链部署表（2026-09-24 实测）

| 网络 | 原生代币地址 | wrapper（`wrapperAddressV2`） | atomicSwaps | 支持申赎的稳定币 |
|---|---|---|---|---|
| X Layer（我们持有的这条） | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | ✅ | USDG `0x4ae46a509f6b1d9056937ba4500cb143933d2dc8`、USDC `0xb6ceceab302e2e4948951ee7843fc24e92933061` |
| Ethereum | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | ✅ | USDC / USDT / PYUSD / USDG 可申赎，EURC 仅可原子兑换 |
| Arbitrum | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | ❌ | USDC |
| Optimism | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | ❌ | USDC |
| BNB Smart Chain | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | ✅ | USDC |
| Mantle | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | ❌ | USDC |
| Ink | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | `0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9` | ✅ | USDC |
| HyperEVM | `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` | 登记表未给 | ❌ | USDC |
| Solana | `XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP` | 登记表未给 | ✅ | USDC / USDG |
| TON | `EQBkB3FguvJgTBDYy34ub-3vLYgxeSewafw4AZuNVIcUhqqc` | 登记表未给 | ❌ | USDT |

两点值得注意：

- **8 条 EVM 链用的是同一个合约地址** `0xefd30445…047f`，wrapper 也同为
  `0xce0fbc16…49ae9`。这说明发行方是用固定部署者 / CREATE2 风格在所有 EVM 链
  部署同一套合约，**同一地址不等于同一条链上的同一个余额**，也不构成可桥接。
- 这与我们 `config/signed_depth_mappings.toml` 记录的
  `native_address = 0xefd30445…`、`wrapper_address = 0xce0fbc16…`（chain_id 196）
  完全一致，映射证据链没有受影响。

## 3. 官方跨链渠道（2026-09-24 晚已核实）

来源有两处，都是发行方自己的：

- 文档：[xStocks For Developers](https://docs.xstocks.fi/developers) 写明持有人可用
  Public Bridge（Chainlink CCIP），入口是 DeFi Dashboard；
  Internal Bridge 只给已开通的机构客户。
- 接口：`GET https://api.xstocks.fi/api/v2/public/bridges`（`api.backed.fi` 同一份）。
  文档定义 `destinationNetworks` = 这座桥能发到的网络，`products` = 登记在这座桥上的代币。

```bash
curl -sS --proxy http://127.0.0.1:4780 \
  "https://api.xstocks.fi/api/v2/public/bridges?network=XLayer"
```

X Layer 这一条（2026-09-24 晚）：

| 字段 | 值 |
|---|---|
| 桥合约 | `0x9ec0e4a4c411493773e01e2abf4d42395788846b` |
| chainId | 196 |
| managedBy | `ChainlinkCCIP` |
| 能发出 / 能接收 | Ethereum、BinanceSmartChain、Arbitrum、Solana、Mantle、Ink |
| `MRNAx` | 在 `products` 里，`productId = 9285715303990376774`，X Layer 部署地址就是我们持有的 `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` |

同一地址在 X Layer 区块 `71485438` 上 `eth_getCode` 有 1159 字节，不是空地址。
桥的是**原生** `MRNAx`，不是 wrapper `wMRNAx`。当前钱包里已经是原生代币。

另外四座公开桥也登记了 `MRNAx`，但路线不同，不能从 X Layer 互相当成同一条路：

| 桥所在链 | 能发到 |
|---|---|
| X Layer | Ethereum、BNB、Arbitrum、Solana、Mantle、Ink |
| BNB | Ethereum、Arbitrum、Solana、Mantle、HyperEVM、Ink、X Layer、Optimism |
| Ink | Ethereum、BNB、Arbitrum、Solana、Mantle、X Layer |
| Optimism | Ethereum、BNB、Arbitrum、Solana |
| HyperEVM | Ethereum、BNB、Arbitrum、Solana |

所以：**Optimism、HyperEVM、TON 上都有 MRNAx 合约，但从我们这条 X Layer 不能用这座公开桥直接过去。** TON 根本没有出现在这 5 座公开桥里。

同链申赎仍然是另一件事：X Layer 的 USDG / USDC 都是 `issuance = true` 且 `redemption = true`。那是换成稳定币，最小单笔仍是登记表里的 `minOrderFiatValue = 1000` 美元。我们这 0.56 枚远低于这个门槛，申赎走不通；公开桥的文档没有写这个 1000 美元门槛。

**还没实测的：** 没有发跨链交易，所以手续费币种、到账时间和 UI 是否对这个小额放行，仍待确认。OKX 自己的 X Layer 桥页面没有当作这条官方渠道。

## 4. 与项目边界的关系（提醒）

- `docs/plan.md` 第 144 行、`docs/final-execution-plan.md` 第 250 行都写着：
  当前阶段**不自动做跨链转账**。本文件只是事实核查，不构成把跨链接进策略的授权。
- 若要把「某链有 MRNAx」变成可用的第二条现货腿，按 `AGENTS.md` 的规矩，必须先
  有监控与执行证据（深度、可执行净价、映射证据链），并由 operator 决定是否扩范围。

## 5. 目标链能不能卖掉（2026-09-24 21:00 补）

同一时刻，X Layer 上把现有 `0.560000002628231336` 枚原生 MRNAx 卖成 USDG 的报价是 **94.346455 USDG**。

Ethereum、Arbitrum、BNB Chain 上，OKX 聚合报价对这笔数量（以及以太坊上再试的 0.01 枚）都返回 `Insufficient liquidity`。GeckoTerminal 对 Arbitrum、BNB 的原生代币返回空池列表，以太坊也是 0 个池；Dexscreener 对这个合约地址返回 `pairs: null`。Etherscan 页面显示链上市值 $0、4 个持有人；Arbiscan 显示 1 个持有人、价格 $0。

所以这三条链上现在没有一笔能拿来和 X Layer 比高的成交价。没有跨链，也没有做「到账后自动卖出」。公开桥和 Chainlink 的 X Layer 目录都没有写到账要几分钟，这一点仍待确认。
