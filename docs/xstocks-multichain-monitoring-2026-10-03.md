# 多链 xStock 监控：现状与接入（2026-10-03）

> 触发（operator）："我们现在是不是只监控 xlayer 和 sol？其他链比如 monad，op，bsc，也加入监测"

## 1. 改之前的实际口径

| 监控 | 覆盖的链 | 说明 |
|---|---|---|
| 基差监控 `hk_monitor` | 只有 X Layer（chain_id 196） | `config/hk_monitor.toml` 的 `[onchain]` 只写了 `rpc.xlayer.tech`；现货腿 = X Layer 上的 xStock |
| 链上价差看板 `xstocks_gap_live.py` | Solana 全量 + EVM 4 口硬编码池 | Solana 走 Jupiter（177 个 Kraken 在册 mint）；EVM 只有 wNBISx、wSLVx、wCRWVx（X Layer）和 MSTRx/USDT（BSC） |

**结论：是的，实际只有 X Layer 和 Solana 在监控。** BSC 只有 1 口手挑的池子；
Optimism、Monad、Ethereum、Arbitrum、Ink、Mantle、HyperEVM 完全没有进任何一条流水线。

## 2. 发行方登记表：这些链上到底有没有币

来源：`GET https://api.backed.fi/api/v2/public/assets`（1271 个资产，分页拉全，2026-10-03 实测）。

| 链 | 登记在册的资产数 | 相对 2026-09-26 的变化 |
|---|---|---|
| Solana | 1271 | 不变 |
| XLayer | 1271（另有 wrapper） | 不变 |
| BNB Smart Chain | 1271 | 不变 |
| Optimism | 1271 | 不变 |
| **Monad** | **1270** | **新增**（09-26 的本地快照里没有 Monad） |
| Ethereum / Ink | 1271 | 不变 |
| Arbitrum | 1242 | 不变 |
| Mantle | 1264 | 不变 |
| HyperEVM | 1243 | 不变 |

桥（`/api/v2/public/bridges`）也从 9 条变成 **10 条**：X Layer 的 CCIP 桥目的地现在是
Ethereum / BSC / Arbitrum / Solana / Mantle / Ink / **Monad**。
本地库已用 `scripts/xstocks_book.py refresh` 刷新（1271 underlyings / 24367 deployments）。

**但"有币"不等于"有池子"。** 代币是发行方在每条链各自部署的（8 条 EVM 链用的是同一个
CREATE2 地址），二级市场的池子要另一回事。

## 3. 实测：每条链上真正能买的池子

新脚本 `scripts/xstocks_chain_census.py`（只读）扫了 Kraken 在册的**全部 177 个名字**
在这 6 条链上的每个部署地址（native + wrapper，共 354 个 EVM 地址/链），
DexScreener 批量接口 + GeckoTerminal 交叉核对，再按「对手方是稳定币、深度 ≥ 100 USD、
不是 v4 hook 陷阱、链上能读到费率和精度」过闸。

| 链 | 在册部署 | 看到的池子 | 过闸 | 深度不足 | 对手方非稳定币 | v4 陷阱 | **测得最深（USD）** | 结论 |
|---|---|---|---|---|---|---|---|---|
| Solana | 177 mint | 深 | — | — | — | — | 数十万美元级 | 已经在看（Jupiter） |
| BNB Smart Chain | 354 | 10 | **0** | 7 | 3 | 0 | 29.25（USDT 侧） | 只有碎池 |
| **Optimism** | 353 | **0** | 0 | 0 | 0 | 0 | 0 | 没有任何池子 |
| **Monad** | 284 | **0** | 0 | 0 | 0 | 0 | 0 | 没有任何池子 |
| Ethereum | 354 | 24 | 0 | 2 | 2 | **18** | 96.16（USDT/USDC 侧） | 只有 v4 陷阱 + 碎池 |
| Arbitrum | 296 | 0 | 0 | 0 | 0 | 0 | 0 | 没有任何池子 |
| HyperEVM | 192 | 0 | 0 | 0 | 0 | 0 | 0 | 没有任何池子 |

三条独立证据一致（Monad 为例）：

1. DexScreener 收录了 Monad 上的 Uniswap / PancakeSwap / Trader Joe 等 DEX
   （MON/USDC 池 $148 万），但 284 个 xStock 地址一个池子都没有；
2. GeckoTerminal **根本没有 Monad 这个网络**（`networks/monad` 返回 404）；
3. 链上直查 Monad 的 Uniswap v3 工厂 `0x204faca1…0498`（由 MON/USDC 池的
   `factory()` 反查得到），用 AAPLx / NVDAx / MSTRx / SPYx 对 USDC 四个费率档
   全部返回零地址。

Monad 上目前唯一的通道是发行方自己的原子兑换（primary market）：
登记表里 MSTRx 等有 `supportsAtomicSwaps=true`，对手方是 Monad USDC
`0x754704bc…b603`，但那是**按净值申购/赎回**、最小单 $1000，不是一个折价池子。

## 4. 这次接进去的东西

| 位置 | 改动 |
|---|---|
| `scripts/xstocks_chain_census.py`（新） | 多链池子普查：DexScreener 批量 + GeckoTerminal 逐代币，产出 `data/xstocks_chain_census.json`（链覆盖 + 候选路由 + 陷阱 + 被拒原因） |
| `scripts/xstocks_gap_live.py` | 看板每轮读普查结果，自动把过闸池子加进询价宇宙；快照和页面新增**链覆盖表**（含"没测到 / 未被收录"，429 不会被当成"没有池子"） |
| `scripts/xstocks_requote.py` | 新增 Monad 的 RPC 与官方 QuoterV2 `0x661e93cc…f08d`；Optimism RPC 换成 `optimism-rpc.publicnode.com`（`mainnet.optimism.io` 经本机代理握手失败）；`quote_v3` 支持每条路由自带 Quoter（BSC 上 Pancake 与 Uniswap 的 QuoterV2 不是同一个合约） |
| `src/arb_monitor/xstocks_book/parse.py` | 网络表加入 Monad；GeckoTerminal 没有这个网络，所以不登记 slug（未知不猜） |
| `data/xstocks_book.sqlite` | 用 `xstocks_book.py refresh` 刷新到 2026-10-03 状态 |

Monad 的报价能力已经验证可用：1 MON → 0.031529 USDC（QuoterV2 实测），
所以 Monad 一出现池子就能直接询价，不用再改代码。

## 5. 复现命令

```bash
# 刷新发行方目录（含各链部署与 Kraken 交易对）
PYTHONPATH=src .venv/bin/python scripts/xstocks_book.py --proxy http://127.0.0.1:4780 refresh

# 只看 DexScreener 覆盖的链，约 20 秒跑完 177 个名字的全部部署
.venv/bin/python scripts/xstocks_chain_census.py \
    --chains bsc,optimism,monad,eth,arbitrum,hyperevm --min-depth 100

# X Layer / Ink / Mantle 只能走 GeckoTerminal 逐代币查，354 个地址约 20 分钟，
# 建议在基差监控和看板都空闲的时段跑，且不要与其它抓取同时进行
.venv/bin/python scripts/xstocks_chain_census.py --chains x-layer --min-depth 100 --pause-gecko 3.5
```

看板页面 `http://127.0.0.1:8770/` 打开就是「链覆盖」表；进程每轮重读普查文件，
普查跑完不用重启看板。

## 6. 还没做的 / 待确认

1. **X Layer 的普查还没跑**（GeckoTerminal 逐代币太慢，本轮先避开与看板抢代理）。
   X Layer 目前由基差监控的 `hk-depth-census` 单独覆盖，看板另有 3 口手挑池子。
2. **Ink / Mantle / HyperEVM** 也还没跑 Gecko 逐代币，同上。
3. **Monad 的 Gas 与转账路径未验证**：即使明天出现池子，也要先确认
   MON 计价的手续费、USDC 提现、以及能不能送去 Kraken（Kraken 目前只支持
   Solana 的 xStock 充值，见 `docs/xstocks-kraken-exit-lessons-2026-09-27.md`）。
4. **普查的 100 USD 深度门槛是我定的默认值**，不是 operator 定的阈值——
   要改就改 `--min-depth`，或者告诉我一个数我写进配置。
5. 本文件里的深度数字是 2026-10-03 18:xx 的一次快照，池子会变。

---

# 第二轮（2026-10-03 晚）：TON / Ink / X Layer 接进看板

> 触发（operator）："还有 ton/ink 这两个链，你也要查一下有没有股票代币和池子"；
> "链上价差看板需要包括 sol 以外的链，比如 xlayer 的，还有其他能够成交的又有价差的代币"。

## 7. TON

**有币，也有池子，但只有 1 个池子值得看。**

| 项目 | 结果 | 来源 |
|---|---|---|
| 代币 | 1271 个 jetton 部署（Kraken 在册 177 个） | 发行方登记表 + 本地库 |
| 池子来源 | ston.fi（**一次**请求返回全部 48724 个池子，带储备） | `GET https://api.ston.fi/v1/pools` |
| 有池子的在册名字 | 7 个，共 22 口池子 | 2026-10-03 实测 |
| 过 $100 闸 | **1 口**：`SPYx / USD₮` **$107,876** | 其余 AAPLx $17、TSLAx $0.20、GOOGLx $0.04、NVDAx $0.03 |
| 能不能买到便宜 | **不能**：1 USD₮ → 0.00127128 SPYx = $786.61/股，20 美元档 $786.88，100 美元档 $788.04，都比正股价高约 2% | `POST https://api.ston.fi/v1/swap/simulate`（实测） |

细节：ston.fi 的报价接口参数在**查询串**里（用 JSON body 会返回 400），
而且不带 User-Agent 会被 Cloudflare 挡成 403。看板的 `quote_ton()` 两条都处理了。

## 8. Ink

**有币，有**深**池子，走的是 Reservoir v3。**

| 项目 | 结果 |
|---|---|
| 代币 | 1271 原生 + 1271 wrapper（Kraken 在册 354 个地址） |
| 已确认的池子（实测） | `wAAPLx / USDG 0.05%` 深度 **$51,788**，24h 量 $1,023（Reservoir v3） |
| | `wNVDAx / USDG 0.05%` 深度 **$53,053**，24h 量 $6,308（Reservoir v3） |
| | 另有 `uniswap-v4-ink` 的同名池（$3,639）——v4 hook 池按规则不报价 |
| 报价方式 | **不能用 Uniswap QuoterV2**：标准 CREATE2 地址在 Ink 上只有 2110 字节，`quoteExactInputSingle` 返回空。改读池子的 `slot0()` + `liquidity()`，用仓库自己的 v3 数学算价 |
| 定价实测 | wAAPLx 买入价 $333.07（$1 档）→ $333.11（$20 档），与正股同量级 |
| 池子发现 | GeckoTerminal 逐代币查 Ink 会被限流（请求挂起），改成直查 Reservoir 工厂 `0x640887a9ba3a9c53ed27d0f7e8246a4f933f3424` 的 `getPool(token, 稳定币, 费率)`；已用它复现出 wAAPLx/USDG 池地址 |
| 深度算法校验 | 用池子余额算 wAAPLx/USDG = **$51,789.56**，GeckoTerminal 报 $51,787.91，差 0.003% |

全量工厂探测跑完了：354 个地址 × 2 个稳定币 × 2 个费率档 = 1416 次 RPC，
找到 **13 口池子**（其中 9 口过 $100 闸），14 次 RPC 报错（代理偶发 SSL 断流）。

| Ink 上的候选池（2026-10-03 21:57） | 深度 | 费率 | 池子 |
|---|---|---|---|
| SPYx / USDG | **$194,088** | 0.30% | `0x383d9dd7…ca3b` |
| QQQx / USDG | $187,874 | 0.30% | `0x5f75cc89…9f4c` |
| MSTRx / USDG | $55,382 | 0.05% | `0x169f2ab4…069d` |
| NVDAx / USDG | $52,904 | 0.05% | `0x01951ddb…4727` |
| SPCXx / USDG | $52,794 | 0.05% | `0xdf153145…1406` |

报价实测：SPCXx 5 美元档成交每股 **$158.94**，对照正股 $159.11 → 便宜 10.6 bps
（刚好过看板的 10 bps 门槛）。看板每轮重读普查文件，跑完自动接上，**不需要重启**。

## 9. X Layer 进了看板

不再依赖 GeckoTerminal 逐代币查 X Layer：直接复用仓库自己的 RPC 深度普查
（`data/depth_watchlist.json`，45 口 wrapper 池，带实测深度与池价）。

| 项目 | 结果 |
|---|---|
| 深度 ≥ $50 的候选 | 37 口（最深 `wSPYx/USDG` $1,893,245） |
| 看板每轮询价 | 深度最大的 12 条按轮转窗口进入询价（见第 10 节） |
| 报价实测 | `wQQQx / USDC` 500 美元档成交每股 $752.16；`wSPYx / USDG` 20 美元档 $774.94 |
| 手工挑的 3 口 | wNBISx、wSLVx、wCRWVx 保留，不受普查影响 |

## 10. 看板改了什么

| 改动 | 说明 |
|---|---|
| 链覆盖表 | 页面上按链列出：在册部署 / 看到的池子 / 过闸 / 深度不足 / 对手方非稳定币 / v4 陷阱 / 没测到 / 未被收录 / 测得最深 / 本轮询价 / 本轮上榜 |
| 轮转窗口 | 候选多于 10 条时每轮只询价 10 条（`EVM_BUDGET`），几轮内轮完；没轮到的行如果上一轮还便宜会保留并标「本轮未询价」 |
| TON 报价 | `quote_ton()` 走 ston.fi；失败就是没有行，不猜价 |
| Ink 报价 | `slot0_pool_state()` + `xstocks_screen.pool_side_prices()`，只覆盖当前 tick 区间（`within_active_liquidity` 为假就不给价） |
| 普查合并 | 分链跑的结果会合并进同一个文件，单链重跑只覆盖该链（`--no-merge` 可关） |

---

## 11. 深度门槛改成 20 USD（operator 2026-10-03 指定）

`scripts/xstocks_chain_census.py` 的 `DEFAULT_MIN_DEPTH` 从 100 改成 **20**，并按新门槛
重跑了各链。多出来的候选：

| 链 | 新增候选 | 深度 | 费率 | 备注 |
|---|---|---|---|---|
| BNB Smart Chain | MSTRx / USDT | $29.25 | 0.25% | Pancake v3，走 Pancake 的 QuoterV2 |
| BNB Smart Chain | TSLAx / USDT | $22.97 | 0.25% | 报价实测 $395.95/股 |
| BNB Smart Chain | CRCLx / USDT | $22.69 | 0.25% | |
| Ethereum | TSLAx / USDC | $240.96 | 0.30% | Uniswap v3 |
| Ethereum | MSTRx / USDC | $96.16 | 0.30% | 上板后出现 **-733 bps** 的行（净 +$0.06，尺寸 $1） |
| X Layer | 第 38 口（原来 37 口） | $94 | 0.05% | 其余不变 |

仍然没有合格池子的链：Optimism、Monad、Arbitrum、HyperEVM（0 口）。
Ethereum 另有 17 口是 v4 hook 陷阱，BSC 有 1 口用比特币计价（不计入）。

### 本轮修掉的两个环境坑（都会让"有池子"被误报成"读不到"）

1. **RPC 少了 User-Agent 会被挡**：以太坊 publicnode 对没有 UA 的请求直接断流，
   表现是 `fee()` 一直读不到。给所有 eth_call 加上 `User-Agent: arb-monitor/0.1` 后恢复。
2. **RPC 域名**：`ethereum.publicnode.com` 经本机代理大量 SSL 断流，
   换成 `ethereum-rpc.publicnode.com`（实测可用）。

看板新一轮实测（22:41，候选 23 条）：Solana 13 行 + **Ethereum MSTRx 1 行** +
X Layer 1 行 + Ink 1 行上榜；一轮 200 秒。

### Ink 按 20 USD 门槛重跑（23:31）

1416 次 RPC、14 次报错，找到 11 口池、7 口过闸：
SPYx $194,088（0.3%）、MSTRx $55,437、NVDAx $52,869、SPCXx $52,782、
AAPLx $51,783、TSLAx $50,598、PLTRx $50,250（都是 0.05% 档）。

> ⚠️ 上一次跑到的 `QQQx / USDG` $187,874 **这次没出现**：池子发现是确定性的
> （工厂 getPool），掉的是**深度读取**那一步——代理偶发断流时那一口池会被整条跳过。
> 这是已知缺口：单轮 RPC 出错会暂时少一口池，下一轮重跑会回来。
> 修复方向（未做）：把上一轮测到、这一轮读失败的池子标成"本轮未测"而不是丢掉。

**看板当前一轮（23:40，候选 25 条）**：Solana 13 行 +
X Layer 3 行（INTCx -42 bps 净 +$0.31 / TSLAx -14 bps 净 +$0.17 / CRWVx -72 bps 净 -$0.01）+
Ethereum MSTRx -733 bps（净 +$0.06，$1 档）+ Ink SPCXx -11 bps（净 -$0.007）上榜。
一轮 185–280 秒（EVM 询价与 Solana 并行，上限 16 条/轮，其余轮转）。
