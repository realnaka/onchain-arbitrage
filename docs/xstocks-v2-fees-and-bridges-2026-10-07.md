# xStocks v2：提币费实测与跨链桥形态（2026-10-07）

两件事被反复问起，这里各留一条实测记录，免得下次再争。

## 1. 提币费：**xStock 币**和**稳定币**是两套费，都"固定"，但量级差 3 倍

实时取自 `/0/private/WithdrawMethods`（2026-10-07，只读）：

| 资产 | 网络 | 提币费 | 折 USDT（按当前价） |
|---|---|---|---|
| SPYx | Solana | 0.00046689 **SPYx** | ≈ 0.36 |
| SPYx | Ethereum | 0.00072059 SPYx | ≈ 0.56 |
| STRCx | Solana | 0.00292278 **STRCx** | ≈ 0.29 |
| STRCx | Ethereum | 0.00563812 STRCx | ≈ 0.56 |
| **USDT** | Solana / Stellar / Avalanche | **1.00** | 1.00 |
| **USDG** | Xlayer | **1.00** | 1.00 |
| USDC | Ethereum | 0.7512 | 0.75 |
| USDC | Stellar / Avalanche | 1.00 | 1.00 |

所以两句话都对：

* **"固定 1 U"** —— 指**稳定币**通道（USDT/USDG 提到链上）。v1 的
  `ops/xstocks_policy.toml` 记的 `withdraw_fee_usdt = "1.2345"`（USDT-Solana）
  和 `withdraw_threshold_usdg = "100"`（一次 1 USDG）都是这个。
* **xStock 代币通道**是按**代币数量**收的固定费（0.0029 STRCx ≈ 0.29 USDT），
  比稳定币便宜，但**与票面无关**，所以票面越小占的 bps 越高。

结论没变，但措辞要说准：**不是"手续费 115 bps"，是"0.29 USDT 的固定费，
在 25 USDT 的票面上等于 116 bps"。** 票面放大它就摊薄：

| 票面 | STRCx/Solana 提币费占比 |
|---:|---:|
| 25 USDT | 116 bps |
| 100 USDT | 29 bps |
| 400 USDT | 7.3 bps |
| 1000 USDT | 2.9 bps |

## 2. Solana → 其他链的桥：**是 Solana 程序，不是 EVM 的 send**

`data/xstocks_book.sqlite` 的 `bridges` 表，Solana 作为源链那一条：

| 项 | 值 |
|---|---|
| 程序地址 | `F2ZYgyh8YDG6rHo9F1T8Aw4WoTyy8eYN3GgM2LaPKcZN` |
| chainId | 727 |
| 管理方 | ChainlinkCCIP |
| 可达目的链 | Ethereum、BinanceSmartChain、Arbitrum、HyperEVM、Ink、**XLayer**、Optimism、Monad |
| 登记品种 | 约 1467 条（每个品种带各链部署地址） |

**X Layer 在目的链列表里**，所以 Solana→X Layer 在登记层面是通的。

**但构造方式与已实测的 X Layer→Solana 完全不同**：

* X Layer→Solana（有成功样本）：EVM 合约 `send`，`abi.encode(uint64 bitmap,
  bytes32[] accounts)`，bitmap 772、11 个账户。
* **Solana→X Layer（无任何样本、无任何实现）**：调用 Solana 上的 CCIP 程序，
  要按 Anchor 指令布局构造账户表与 data，付费也是另一套。

全仓（v1 + v2）**都没有任何 Solana 源方向的 CCIP 代码**。

## 3. 地址簿现状（用于核对登记）

实时 `/0/private/WithdrawAddresses`，2026-10-07：

```
key=hkzabank    asset=USD   method=Bank Frick (SWIFT) International
key=xlayertee1  asset=USDG  method=USDG - Xlayer
key=soltee1     asset=USDT  method=Tether USD (SPL)
key=ethusdt1    asset=USDT  method=Tether USD (ERC20)
key=ethusdc1    asset=USDC  method=USDC
```

**没有 SPYx 条目。** 核对命令：
`PYTHONPATH=src .venv/bin/python scripts/kraken_address_probe.py`

Kraken 的地址簿没有写接口（REST 五个候选名全 404、WebSocket v4 个候选名全
`Unsupported event`），网页添加后要过邮件/2FA 确认才会出现在这个列表里。
