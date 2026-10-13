# 阶段 2 的 eth v4 版本：一笔交易走两口池（2026-10-08 实测）

结论先行：**执行器写完并在链上证明过了，只差主网 gas 钱才能真广播。**
两口池的环在 eth 上是 **uniswap v4**（不是 v3），所以路径与 X Layer 那套完全不同：
没有 per-pool router，一个 `PoolManager` 装下所有池，swap 必须由合约在
`unlock` 回调里发起。我们用现成的 `UniversalRouter` 的一个 `execute` 调用完成整环，
不需要部署我们自己的合约。

| 项 | 实测值 | 来源 |
|---|---|---|
| PoolManager | `0x000000000004444c5dc75cb358380d2e3de08a90` | `eth_getCode` 24 009 B |
| UniversalRouter | `0x66a9893cc07d91d95644aedd05d03f95e1dba8af` | `eth_getCode` 19 499 B + Sourcify 已验证源码 |
| Permit2 | `0x000000000022d473030f116ddee9f6b43ac78ba3` | `eth_getCode` 9 152 B |
| v4 Quoter | `0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203` | 扫描已在用 |
| 环的两口池 | 买 `0x9b8f0f0d…`（green，0.25%）→ 卖 `0x527e4a83…`（amber，动态费率 + hook `0xbf982845…`） | census `data/xstocks_v4_watch.json` |

## 1. 一笔交易的形状

```text
execute(commands = 0x10 V4_SWAP, inputs = [abi.encode(actions, params)])
    actions = 0x06 SWAP_EXACT_IN_SINGLE   池 A：USDC  -> SPCXx
              0x06 SWAP_EXACT_IN_SINGLE   池 B：SPCXx -> USDC   (amountIn = OPEN_DELTA)
              0x0f TAKE_ALL              把净额 USDC 提到我们钱包
```

闪记会计（flash accounting）让中间代币**从不离开** PoolManager：买腿产生 SPCXx 贷方，
卖腿正好把它花掉，只有最后 USDC 的净额需要收尾。净额是正的（有利润）时**一分钱授权都不需要**。

## 2. 部署版的三个「与上游不同」，都是量出来的

上游 `main` 分支与主网部署的版本不一样，凭源码记忆写会写出一个链上直接 revert 的 calldata：

| 差异 | 上游 main | **主网部署（实测）** | 怎么测的 |
|---|---|---|---|
| `ExactInputSingleParams` | 多一个 `minHopPriceX36` 字段（最少 0x160） | **没有**；最少 `0x140`（10 字） | Sourcify 主网源码 + `probe actions` |
| swap 参数包 | 直接是 struct | struct 前面**多一个指针字**（`calldataload(params.offset)`） | 同上；缺指针 → 空 revert |
| 收尾动作 | 上游可混用 | `SETTLE_ALL` 在 delta 为正时 revert `DeltaNotNegative`；`TAKE_ALL` 在 delta 为负时 revert `DeltaNotPositive` ⇒ **同一张动作表只能收一种符号** | 链上探测，两个错误选择器都实测过 |

因此模块提供两种形态：

| 形态 | 动作 | 什么时候用 |
|---|---|---|
| `profit` | `[swap, swap, TAKE_ALL(min=1)]` | 期望有利润。不满足就 revert，一分钱不动；**无需授权** |
| `gross` | `[swap, swap, SETTLE(票面), TAKE_ALL(min)]` | 允许小亏（先按票面结算，再取回卖出所得）。需要 `USDC→Permit2` + `Permit2→UniversalRouter` 两步授权 |

## 3. 链上证明（两条独立的模拟，都没有签名、没有广播）

```bash
PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_atomic_v4_probe.py actions   # 部署版动作编号
PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_atomic_v4_probe.py ring --symbol SPCXx --usdt 25 --mode profit
PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_atomic_v4_probe.py ring --symbol SPCXx --usdt 25 --mode gross
```

| 检查 | 结果 |
|---|---|
| 我们自己的 `eth_call`（真池子状态；gross 用 state override 伪造两层授权） | **两种形态都 `ok`**；槽位实测：USDC `balanceOf`=9、`allowance`=10、Permit2 allowance base=1 |
| OKX `gateway simulate`（官方模拟器，真钱包、真授权状态） | `failReason: ""`，`assetChange: USDC **+17 255**`（= +0.017255 USDC），`gasUsed: 1 586 564` |
| 只做买腿的探针 | revert `CurrencyNotSettled()` ⇒ swap 确实执行了，只差收尾 |

## 4. 没走完的那一步：主网 gas

广播两次都被钱包服务挡下：`code=10004 This address doesn’t have enough native tokens`。

| 项 | 值 |
|---|---|
| 环的 gas | **1 586 564**（amber 池的 hook 很贵；green-green 环在 eth 上不存在，实测 0 个正价差） |
| 钱包 ETH | 0.001672570977402824（≈$5.6） |
| 需要 ETH | base fee 1.5 gwei → 0.0024；今天 4.1 gwei → **0.0065**；再留 2× 上限缓冲 → ≈0.013 |
| 失败尝试的痕迹 | 无：nonce 仍 3、ETH/USDC 余额不变、`hasPendingTx:false` |
| 稳定币付 gas | 不可用：主网 Gas Station 未激活且 `tokenList: []` |

## 5. 代码与测试落点

| 文件 | 内容 |
|---|---|
| `src/arb_monitor/xstocks_v2/atomic_v4.py` | PoolKey/pool id 重算、动作与 ABI 编码、两种环形态、Permit2 授权 calldata |
| `scripts/xstocks_v2_atomic_v4_probe.py` | `actions` / `pools` / `quote` / `ring` / `allowances` / `approve` / `once`，除后两个外全部只读 |
| `tests/test_xstocks_v2_atomic_v4.py` | 19 例：pool id 对上 census、动作字节、参数布局（指针 / hookData 偏移 0x120 / 最短长度）、两种形态、方向推导、拒绝路径、「只有一次 execute」源码守卫 |
