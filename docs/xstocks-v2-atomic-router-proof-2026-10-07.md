# 阶段 2（同链两池）：现成 SwapRouter02 就能一笔完成，不需要自建合约

日期：2026-10-07。结论先行：**模拟通过。X Layer 上不需要部署任何我们自己的
合约。** 同链两池这条 family 真正缺的是**第二个有流动性的池子**，不是代码。

## 1. 这次要回答的问题

`atomic.py` 的规则是「同链买、同链卖必须是一笔交易」。2026-10-07 早些时候的
结论是「聚合器拒绝环形报价，所以要自建 router 合约（教训 26）」。本轮把这句话
拆成两件事分别验证：

| 说法 | 是否成立 |
|---|---|
| 聚合器不支持 `--from == --to` 的环形**报价** | ✅ 成立（实测原文：`fromToken and toToken are the same address`） |
| 所以 router **调用**也不能一笔走两口池 | ❌ **不成立**。报价工具的限制被错当成了链上能力的限制 |

## 2. 地址不是猜的，是链上核对的

来源：Uniswap 官方 X Layer v3 部署表（developers.uniswap.org，页面最后修改
2026-08-25），然后用 `eth_call`/`eth_getCode` 逐项核对：

| 合约 | 地址 | 链上核对 |
|---|---|---|
| UniswapV3Factory | `0x4B2ab38DBF28D31D467aA8993f6c2585981D6804` | code 24 535 B |
| **SwapRouter02** | `0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca` | code 24 497 B；`factory()` = 上面的 factory；`WETH9()` = WOKB |
| QuoterV2 | `0xd1b797d92d87b688193a2b976efc8d577d204343` | code 8 273 B；单跳报价成功 |
| Multicall | `0xe2023f3fa515cf070e07fd9d51c1d236e07843f4` | code 1 383 B |
| NonfungiblePositionManager | `0x315e413A11AB0df498eF83873012430ca36638Ae` | code 24 384 B |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` | code 9 152 B |

chain id = `0xc4` = 196（X Layer）。SPYx 深池 `0x07c40850d14064d20eb0afdef9574675392f2c11`
自己也回答同一个 factory，`token0 = USDG`、`token1 = wSPYx`、`fee = 500`、
`liquidity = 976420372246176849`。

复跑命令（只读）：

```bash
PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_atomic_probe.py discover
```

## 3. 模拟：一笔交易走两个真实池

calldata 由执行器自己的模块生成（`atomic.ring_calldata`），不是探针里另写一份：

```text
multicall([ exactInput((bytes,address,uint256,uint256)),  sweepToken(address,uint256,address) ])
    exactInput:  recipient = router，path = USDG || 000064 || WOKB || 0001f4 || USDG
    sweepToken:  token = USDG，amountMinimum = 1778，recipient = 我们的钱包
```

因为还没真的 approve，模拟用 `eth_call` 的 **state override** 把
`allowance[我们][router]` 设成 max（USDG 的 allowance 映射在 **slot 3**，不是
OpenZeppelin 常见的 slot 1——`balances` 在 slot 1，实测找出来的）。
**全程没有签名、没有 approve、没有广播。**

| 环 | 池 | 结果 |
|---|---|---|
| 两个池 | `USDG/WOKB 1 bps` `0x01cd955c…`（liq 4.8e10）+ `USDG/WOKB 5 bps` `0x5d7e3ad0…`（liq 6.65e16） | ✅ `ok:true`。QuoterV2 预测 1 780，multicall 返回 `0x6f4` = **1 780** |
| 三个池 | `USDG/USDC 1 bps` + `USDC/WOKB 5 bps` + `USDG/WOKB 5 bps` | ✅ `ok:true`。预测 998 586，返回 `0xf3cb6` = **998 586** |

自检（同一份代码里锁住）：三个 selector 与链上 router 的分发器逐字节一致
（`exactInput` `0xb858183f`、`multicall` `0xac9650d8`、`sweepToken` `0xdf2ab5bb`、
`exactInputSingle` `0x04e45aaf`），calldata 里 path 按 `token/fee/token/fee` 编码，
`recipient` 是我们自己，`amountOutMinimum > 0`，`deadline` 在 SwapRouter02 里不存在
（V2 版 router 已去掉 deadline，靠 multicall 原子性）。

复跑命令（只读）：

```bash
PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_atomic_probe.py simulate --ring usdg-wokb
PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_atomic_probe.py simulate --ring usdg-usdc-wokb
```

## 4. 为什么 SPYx 自己这条环跑不了

想要的环是「SPYx 的两个池」：`USDG → wSPYx`（深池）+ `wSPYx → …`。
实测 X Layer 上每个 xStock 只有**一个**有流动性的池：

| 池 | fee | liquidity |
|---|---:|---:|
| `wSPYx/USDG`（深池） | 500 | 9.76e17 |
| `wSPYx/USDC` | 500 | **0**（池存在，从没初始化过） |
| `USDG/WOKB` | 100 / 500 / 3000 | 4.8e10 / 6.65e16 / **0** |
| `USDG/USDC` | 100 | 1.0e15 |
| `USDC/WOKB` | 500 | 2.59e17 |

所以 `dex_to_dex` 在 X Layer **今天没有可交易对象**（扫描快照里 0 行候选，
与链上事实一致）。这是**流动性问题**：自建一个 router 合约不会凭空造出第二个池子。

## 5. 那么「部署合约」还需要吗

需要，但**不是**为了「一笔走两口池」。现有 SwapRouter02 已经能做这件事。
真正需要自建合约的场合只有这些（都要 operator 单独点头）：

| 场合 | 为什么现成 router 不够 |
|---|---|
| 闪电贷式无本金原子套利 | 现成 router 不做借贷 |
| 一笔里同时做「换池 + 还币 + 补差价」 | 需要自定义还款逻辑 |
| 跨场所原子（CEX 腿不在这条链上） | 链上合约管不到 CEX |

部署成本（真要做时）：目标链 = X Layer（两池都在这里）；gas 用 OKB；部署 +
一次 `approve` + 一次 swap 的 gas 我们本机实测 **0 OKB**（OKX TEE 委托账户代付，
见 HANDOFF §15.2），但这是**当前账户的优惠**，不能当作合约的长期假设。
建议形态（真要部署时按这个写）：Owner 多签/单签门限、只允许白名单 token 与池、
不在合约里长期托管资金、`amountOutMinimum` 必填、无常驻授权。

## 6. 代码与测试落点

| 文件 | 内容 |
|---|---|
| `src/arb_monitor/xstocks_v2/atomic.py` | 地址常量（带链上核对说明）+ `encode_path` / `exact_input_calldata` / `sweep_token_calldata` / `multicall_calldata` / `ring_calldata` |
| `scripts/xstocks_v2_atomic_probe.py` | `discover` / `pools` / `ladder` / `simulate`，全部只读 |
| `tests/test_xstocks_v2_atomic.py` | 新增 6 个 hermetic 用例：selector、地址、path 编码、exactInput 头部、multicall 两个元素、ring 单笔 |

全套测试：**378 通过**（改动前 370）。
