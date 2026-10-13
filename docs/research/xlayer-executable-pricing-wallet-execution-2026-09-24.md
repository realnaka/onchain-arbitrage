# X Layer 可成交池价与钱包执行方案调研（2026-09-24）

> **续篇**：「提前授权后能不能直接调钱包 API」「Privy × Safe 有没有预授权/签名方案」
> 这类问题不在本文范围，见
> `docs/research/wallet-preauth-and-signing-2026-09-24.md`（含 Privy policy engine、
> 4337/7702/session key 的收益与代价，以及联网核验清单）。

## Executive Summary

结论先说：当前最快、风险边界最清楚的短期路线不是把所有报价和交易都交给 OnchainOS，而是把“发现机会”和“执行交易”拆开。用 OKX 行情做宽筛，使用 X Layer 自有 RPC/Flashblocks 与 QuoterV2 对少量候选池做精确、同一时点的可成交报价；本地构造已经核验的 Router calldata；最后把 unsigned transaction 交给 OKX Agentic Wallet 的 TEE `contract-call` 完成签名和广播，随后用 Flashblocks 观察 preconfirmation，再用 canonical receipt、余额、Transfer 日志和 OKX 永续腿做最终对账。[1][2][3][4][5]

Bitget Wallet 本轮不纳入：用户已确认其官方 API 支持链列表没有 X Layer。报告只比较 OKX Agentic Wallet/Wallet API、X Layer 原生 RPC/Flashblocks、直接合约调用，以及自建受限 signer、KMS/HSM、MPC/WaaS、Safe/模块和 Permit2 等辅助方案。

现有项目已经验证了瓶颈：16 个资产的 OnchainOS 批量报价约 15,239 ms，`quote_rate_limited` 累计 4,494；这只能证明当前调用路径偏慢，不能推出 TEE 签名本身的 P50/P95/P99。公开资料没有给出 TEE 签名或 `contract-call` 的延迟 SLA，因此必须把“池价、calldata、TEE、广播、preconfirmation、canonical receipt”分段实测，而不是凭产品名称猜速度。[23]

最重要的产品区分如下：

| 路径 | 能否自定义 X Layer 合约调用 | 是否由 OKX 持有/使用 TEE 私钥 | 是否能证明“预授权后免逐笔签名” |
|---|---|---|---|
| Agentic Wallet `wallet contract-call` | 可以，参数包含 `to`、payable `amt`、`input-data` | 是，TEE 签名并自动广播 | 不能。它仍是每次提交交易、每次过安全检查 |
| 通用 Wallet API broadcast | 接收 `signedTx`、`chainIndex`、地址 | 不替调用方签名 | 不能；它是已签名交易广播接口，且文档标注白名单/企业权限 |
| 自建 EOA/KMS/HSM/MPC signer | 可以 | 由自建安全边界决定 | 可以做策略级预授权，但每笔仍要产生密码学签名 |
| 直接 RPC/合约调用 | 可以读取和广播 | 不解决私钥问题 | 不能；RPC 不会替钱包授权 |

推荐分两阶段：短期保留 TEE，先把报价从 OnchainOS CLI 搬到本地 RPC/Flashblocks + QuoterV2；中期若实测 TEE P95 成为主要尾延迟，再把小额执行资金放进独立受限 signer，Safe 作为金库和权限恢复层。任何路线都不能跳过滑点保护、nonce 管理、模拟、未知状态恢复和双腿对账。

## Introduction

### 研究范围与假设

目标是 X Layer 主网（chain ID 196）上的股票代币/稳定币池套利：快速获得“按目标数量真正能成交”的池价，并在价差仍成立时下单；钱包候选为 OKX Wallet。X Layer 官方 RPC 端点包括 `https://rpc.xlayer.tech` 与 `https://xlayerrpc.okx.com`，普通 JSON-RPC 支持 `eth_call`、`eth_estimateGas`、`eth_sendRawTransaction`、receipt 和 nonce 查询。[6] 本报告不把“看见一个中间价”当作可执行价格，而要求报价对应目标数量、池状态、支付币转换、手续费、滑点、Gas 和失败缓冲。

### 项目现场证据

项目当前 `[onchain] pool_quotes = true`，已接入 QuoterV2；现有 QuoterV2 地址为 `0xd1b797d92d87b688193a2b976efc8d577d204343`，fee 500 池采用 exact-output 买腿和 exact-input 卖腿。签名/广播仍由 OnchainOS 完成。最近现场记录显示，16 个资产一批的 OnchainOS 询价约 15,239 ms，且有大量 `quote_rate_limited`；这说明“用 OnchainOS 批量询价覆盖全宇宙”不适合低延迟热路径，但不代表应该立即撤掉 TEE。[23]

项目边界仍有效：单笔/日额度、执行白名单、`live_ack`、`auto_orders`、`KILL_SWITCH`、`AUTO_HALT` 不因本调研改变；未知状态一律停止动作。研究结论只进入文档和 benchmark 设计，不进入交易配置。

## Methodology

本调研优先使用 X Layer、OKX、Uniswap、AWS、Safe、Fireblocks 和 Ethereum Improvement Proposal 的官方资料；产品能力与性能结论分开处理。凡官方资料未公开 P50/P95/P99 的地方，明确标记为“待实测”，不以宣传语替代数据。

性能分析采用端到端时间线：

```text
OKX 宽筛 → 本地池/深度过滤 → pending/Flashblocks QuoterV2
        → calldata 编码 → simulation/estimateGas
        → TEE contract-call 或自建 signer
        → raw RPC 广播 → 首次看到 tx
        → Flashblocks preconfirmation → canonical receipt
        → 余额/Transfer/双腿对账
```

每个阶段记录单调时钟、请求 ID、资产、池、数量、RPC endpoint、区块标签、nonce、txHash、错误分类和是否使用已有 allowance。采样至少覆盖冷启动、热连接、已有 allowance、需要 approve、RPC 429、revert、nonce 冲突、TEE 安全拦截和 canonical 未出现等情形。报告最终使用 p50/p95/p99、成功率、超时率、revert 率和未知状态率，不用单次最好成绩。

## Main Analysis

### 1. OnchainOS TEE：安全边界好，但必须拆分测量

OKX Agentic Wallet 文档明确：私钥在 TEE 内生成、保存和签名，Agent 以及 OKX 业务侧都不能导出私钥；交易在执行前经过风险模拟和评分，并支持 X Layer。[1] 这比把原始私钥放进套利策略进程更容易审计，也符合当前“小额、严格白名单、未知状态停机”的项目边界。

更关键的是，官方 `wallet contract-call` 参考明确提供 EVM 的 `--to`、`--amt`（payable native value）和 `--input-data`，描述为“TEE signing + auto-broadcast”，返回 txHash/orderId。[2] 因此可以本地构造固定池 Router calldata，再让 TEE 完成签名和广播；这不是只能调用 OKX 内置 Swap 产品。

代价是：每次调用仍可能包含身份/策略检查、交易模拟、风险评分、TEE 签名和 OKX 服务端排队。公开文档没有签名、风险检查或从 `contract-call` 到 txHash 的 P50/P95/P99/SLA；不能据此说“TEE 一定慢”或“一定够快”。当前实测只证明 OnchainOS 询价批次慢，询价和签名不能混为一个数字。

适合保留的用法：TEE 只承接少量、已经在本地完成池状态过滤、QuoterV2 报价、minOut/maxIn、deadline、allowance 和 simulation 的交易。不要让 TEE 承接一百多个标的的全量报价循环。

### 2. OKX Wallet API：`contract-call` 与 broadcast 必须分开

通用 Onchain Gateway/Wallet API 的广播接口接收已签名 `signedTx`、链标识和地址；相关页面还注明仅白名单/企业客户可用，当前页面列出的部分能力链表与产品总链表不完全一致。[3][4] 因此不能把 API key 当成钱包私钥权限，也不能据此假设“提前授权一次，以后 API 直接替用户签名”。公开 quickstart 的典型流程仍是构造交易、在本地 signer 签名，再经 Onchain Gateway 广播。[5]

另一方面，Agentic Wallet CLI 的 `contract-call` 是另一条受 TEE 控制的产品路径：调用方提交自定义 calldata，OKX TEE 签名并自动广播。[2] 对本项目而言，真正需要向 OKX 工单或实测确认的是：

1. 当前账户是否有 Agentic Wallet `contract-call` 权限；
2. X Layer chain 196 的自定义 `to/value/data` 是否全部可用；
3. 风控是否允许项目 Router、wrapper token、Permit2 或 Universal Router；
4. 失败时是否返回明确的“未签名/未广播”状态，还是存在 orderId 延迟；
5. 是否有速率限制、并发限制和可接受的尾延迟。

如果答案为“可用”，短期不需要把私钥迁出 TEE；如果只开放已签名 raw tx broadcast，则必须另选 signer，不能把 Wallet API 当作 signer。

### 3. X Layer 原生 RPC 与 Flashblocks：降低状态延迟，不提供签名

X Layer Flashblocks endpoint 为 `https://rpc.xlayer.tech/flashblocks`，官方说明它通过 `pending` 标签暴露最新 pending state；文档列出 `eth_call`、`eth_estimateGas`、余额、nonce、交易和 receipt 等接口。[7] 官方描述约每 200 ms 形成一个 flashblock，一个约 1 秒的 L2 block 通常含 3–5 个 flashblocks。[8]

这对池价很有价值：对同一 pending 状态调用 QuoterV2，可以更快看到刚刚进入 sequencer 状态的储备变化；对发送后的交易，可以更快看到 preconfirmation。Flashblocks 仍不是私有 mempool、bundle、排序保证或防夹通道。官方 FAQ 明确 flashblock 可能重组：交易虽已 pre-confirmed，仍可能不进入 canonical chain。[8] 所以业务完成条件必须是 canonical receipt + 余额/Transfer 日志 + 双腿状态，而不是 `wait(0)` 或“Flashblocks 看到了”。

原生 RPC 的优点是少一层产品 API，适合本地 `eth_call`、`eth_estimateGas`、`eth_getTransactionCount(pending)` 和 raw broadcast。缺点是它不解决 signer、nonce 分配、重试幂等、广播失败与未知状态。普通 RPC 和 `/flashblocks` 的广播行为及 endpoint 级限流要做 A/B benchmark，不能仅根据示例代码假设两者完全等价。

### 4. QuoterV2、固定池与直接 Router calldata

对于已核验的少量池，最短热路径是：

1. 从本地缓存读取 token decimals、pool、fee、tick spacing、Router 和 wrapper 映射；
2. 读取 pending `slot0`/liquidity/余额并做深度过滤；
3. 按真实目标数量调用 QuoterV2 的 exact-input 或 exact-output；
4. 将输出扣除手续费、Gas、资金成本、失败缓冲和 CEX 对冲成本；
5. 编码固定 Router 的 swap calldata，设置 `amountOutMinimum`/`amountInMaximum`、deadline 和 recipient；
6. 先对最终 calldata 做 `eth_call` 或 `eth_estimateGas`，再签名广播。

这样避开了“先请求聚合报价、再请求 approve transaction、再请求 swap、再请求广播”的多次网络往返。直接合约调用本身不会自动授权；ERC-20 allowance 必须预先存在或通过 permit/Permit2 组合处理。无限授权只允许对已核验、不可任意升级且严格白名单的 spender 经过 operator 明确批准，默认使用精确额度或短有效期。

### 5. OKX DEX API：适合通用路由，不是固定池热路径首选

OKX DEX API 支持 X Layer（chainIndex 196），典型流程是 quote、approve-transaction、swap，再签名和广播；`swap` 返回路由交易数据。[9][10] 这对多 DEX、动态路由和快速接入很方便，但会增加 API 往返，并且路由结果可能与本地刚读到的池状态产生时间差。当前项目只做少量已核验固定池，优先 QuoterV2 + 本地 calldata；只有需要多跳或跨协议路由时才把 DEX API 作为比较基线。

OKX DEX swap 文档还注明 ExactOut 的链支持受限，不能把“API 支持 X Layer”直接等同于“X Layer 所有 ExactOut 路由都支持”。[10] 需要 exact-output 买腿时，先以本地 QuoterV2/Router 为事实来源。

### 6. Uniswap Universal Router、Permit2 与授权压缩

Uniswap 官方部署表已经列出 X Layer 的 Universal Router/Permit2 体系；交易 API 的支持链表也列出 chain ID 196 与 Universal Router 2.1.2 地址。[11][12] 这说明在目标池确实是 Uniswap 兼容部署、地址和 bytecode 已核验的前提下，Universal Router 可作为多跳执行器。

Permit2 不是免签名机制。官方文档区分两种能力：`SignatureTransfer` 是一次性签名、nonce/deadline 约束；`AllowanceTransfer` 是指定额度和有效期的 allowance；在此之前，token 仍需对 Permit2 做一次 ERC-20 approve。[13] 它可以把某些授权与 swap 组合成一笔交易、减少 approve 往返，但每次 `SignatureTransfer` 仍要有签名。对于本项目，Permit2 只在需要多 token/多路由并且已核验 spender 时评估，不作为第一阶段降延迟的必选项。

### 7. 自建受限 signer、KMS/HSM、MPC/WaaS 与 Safe

**独立受限 EOA signer。** 这是低延迟最直接的长期方案：策略服务只提交结构化意图，Policy Engine 解码并拒绝不符合规则的交易，隔离 signer 产生 ECDSA 签名，广播服务发送 raw tx。Signer 至少校验 chainId 196、`to`/selector/token/spender/recipient 白名单、数量和每日额度、minOut/maxIn、deadline、nonce、simulation hash 和 allowance。原始私钥不进入策略进程、日志或环境变量。

**KMS/HSM。** AWS KMS 的非对称 key spec 支持 secp256k1，签名私钥留在 KMS；但 Ethereum raw transaction 需要把 KMS 返回的 ECDSA 签名适配为 `(r,s,v)`，并处理 low-s、chainId/EIP-155 和 typed transaction 编码。[14][15] 这种方案审计性强，但每次签名会增加 API RTT，且需先测区域、连接池、重试和并发配额。HSM 自托管可减少云依赖，但运维复杂度更高。

**MPC/WaaS。** Fireblocks 等托管钱包支持提交自定义 EVM transaction 数据，但交易创建、策略审批和异步状态轮询通常带来尾延迟；X Layer 支持、Router 白名单和 API SLA 必须向供应商书面确认，不能因为“支持 EVM”就视为支持 X Layer。[16]

**Safe/模块。** Safe 模块可以在多签金库旁增加自动化、白名单、速率限制和 spending cap。[17] 适合作为大额资金金库、恢复和变更控制层，不适合作为每一笔套利热路径的多签门。推荐 Safe 金库 + 小额受限热钱包/signer，而不是让 Safe 本身承受高频签名。

### 8. “提前授权后直接调用钱包 API”到底能做到什么

可以提前做的是：

- 对固定 Router 做一次或分段额度的 ERC-20 approve；
- 若 wrapper 支持 ERC-2612，则用 permit 合并授权；具体 token 必须逐个检查，不能默认支持。[18]
- 用 Permit2 的 `AllowanceTransfer` 设置额度与过期时间，或 `SignatureTransfer` 做一次性授权。[13]
- 在自建 Policy Engine 中预先加载白名单、额度和 selector 规则；
- 对 Safe 模块设置 rate limit/spending cap。

不能据此推出的是：

- API key 变成钱包私钥；
- allowance 变成交易签名；
- 一次风险检查永久覆盖未来任意 calldata；
- Flashblocks preconfirmation 变成最终性；
- `eth_sendRawTransaction` 可以在没有 signer 的情况下工作。

如果目标是“策略代码不碰私钥、每笔仍有可审计授权”，OKX Agentic Wallet TEE 的 `contract-call` 已经是可行路径；如果目标是“把 TEE 网络往返也压到最低”，则必须切换到受限 signer/KMS/HSM/MPC，并接受新增的密钥、审计和恢复责任。

## Claims-Evidence Table

| 结论 | 证据 | 证据强度/待确认项 |
|---|---|---|
| Agentic Wallet 私钥在 TEE，支持 X Layer | OKX Agentic Wallet 官方文档 [1] | 官方事实；账户级权限仍需实测 |
| 可提交自定义 EVM `to/value/data` 并自动广播 | OKX wallet CLI reference [2] | 官方命令参考；需测当前环境的链/Router 风控 |
| 通用 broadcast 接受 signedTx，而非替你签名 | Wallet API 文档/quickstart [3][5] | 官方事实；白名单和 X Layer 可用性存在页面差异 |
| Flashblocks 约 200 ms，可能 reorg | X Layer 官方 overview/FAQ [7][8] | 官方事实；实际延迟和 reorg 率需现场采样 |
| 固定池应优先 QuoterV2 + 本地 calldata | 项目现场 + RPC/DEX 文档 [6][9] | 综合判断，不是官方 SLA |
| Permit2 可限制额度/时间但仍需签名和初始 approve | Uniswap Permit2 [13] | 官方事实；目标 wrapper 兼容性待确认 |
| Safe 适合金库/限制层，不适合热路径 | Safe Modules [17] + 延迟分析 | 综合判断 |
| KMS/HSM/MPC 的尾延迟和 X Layer 可用性 | AWS/Fireblocks 文档 [14][15][16] | 能力可行；本项目 SLA、X Layer 支持待确认 |

## Counterevidence Register

1. **产品矩阵冲突。** OKX 总体支持链页面将 X Layer 列为支持，但 Wallet API broadcast 页面对链、白名单和企业权限的描述更窄。[3][4] 处理方式：以当前账号真实 API 返回、HTTP 状态和 OKX 工单为准，不以矩阵单页授权实盘。
2. **Flashblocks 可能比普通 RPC 更快，但不是绝对更快。** pending state 可能有节点同步、连接和负载差异；必须对同一时刻、同一 calldata 做普通 RPC 与 Flashblocks A/B。
3. **TEE 可能慢，也可能不是主瓶颈。** 当前 15,239 ms 是 OnchainOS 询价批次，不能归因到 TEE；必须单独打点 `contract-call` 请求到 txHash。
4. **Permit2/Universal Router 地址已在官方部署资料中，但目标池/代币未因此自动兼容。** 地址、bytecode、token approval、selector 和回滚路径仍需逐个核验后才可进入白名单。
5. **MPC/WaaS 的安全性不等于低延迟。** 托管策略审批、风控和异步轮询可能比隔离 EOA/KMS 更慢；只有实测 P95 后才能决定。

## Synthesis

### 延迟分解与可优化项

| 阶段 | 当前/预期 | 优化方向 |
|---|---|---|
| 宽筛行情 | OKX ticker 一次拉取 | 保持一次筛选，不逐标的请求 |
| 池深度/slot0 | X Layer RPC | 本地连接池、缓存、pending 标签 |
| 精确池价 | OnchainOS 批量约 15.2 s（现场） | QuoterV2 `eth_call`，只对候选池 |
| calldata | 本地可同步完成 | 固定 ABI 编码、预热 token metadata |
| simulation | X Layer `eth_call/estimateGas` | 与最终 calldata 完全一致 |
| 签名 | 未知，需 p50/p95/p99 | 先保留 TEE；尾延迟高再替换 signer |
| 广播 | endpoint 依赖 | 普通 RPC/Flashblocks A/B，多 endpoint 记录 |
| preconfirmation | Flashblocks 约 200 ms 级更新 | 只作状态反馈，不作最终完成 |
| canonical receipt | 约束最终性 | 异步追踪、未知状态停机和双腿对账 |

### 推荐架构

```text
OKX ticker/永续盘口
        ↓
本地池深度、映射、库存和净价差过滤
        ↓
X Layer Flashblocks pending + QuoterV2 精确报价
        ↓
本地固定池 Router calldata + minOut/maxIn + deadline
        ↓
eth_call/estimateGas（与最终交易同 calldata）
        ↓
OKX Agentic Wallet TEE contract-call
        ↓
X Layer raw RPC / Flashblocks 广播
        ↓
preconfirmation 监控
        ↓
canonical receipt + Transfer/余额 + OKX 永续腿对账
```

短期保持 OnchainOS TEE，优化报价面；中期准备独立受限 signer，但只有当 benchmark 证明 TEE P95 明显压过机会窗口时才迁移。Safe 放在资金管理层，Permit2/Universal Router 只在真实需要多跳/授权压缩时启用。

## Recommendations

### 立即做（不改变实盘开关）

1. 建立 `xlayer-execution-benchmark`，先纸面、再沿用现有 operator 开关做小额验证；不改额度、白名单、`live_ack`、`auto_orders`、`KILL_SWITCH` 或 `AUTO_HALT`。
2. 把报价链路改成“OKX 宽筛 → 本地池状态 → pending QuoterV2”，OnchainOS 只保留 TEE contract-call 和必要的安全检查。
3. 记录普通 RPC 与 Flashblocks 两条广播路径的 txHash 首见、preconfirmation、canonical receipt，并把 endpoint、nonce 和未知状态写入事件。
4. 向 OKX 确认当前 Agentic Wallet 账户是否允许 X Layer 自定义 `contract-call`，并确认具体 Router/wrapper 是否会被安全策略拦截。

### Benchmark 必测字段

- `screen_start/end_ms`、`pool_state_start/end_ms`、`quote_start/end_ms`；
- RPC endpoint、`latest/pending` 标签、block/flashblock number、pool、fee、amount；
- calldata hash、simulation result、gas estimate、allowance 状态；
- `tee_request_ms`、TEE 返回 orderId/txHash 时间、是否风控拒绝；
- raw broadcast 请求/响应、第一次 `eth_getTransactionByHash` 时间；
- Flashblocks preconfirmation 时间、canonical receipt 时间、receipt status；
- 余额和 Transfer 日志核验、OKX 永续腿订单/成交/持仓核验；
- p50/p95/p99、HTTP 429、超时、revert、nonce 冲突、未知状态率。

### 暂不采用

- 不使用 Bitget Wallet；
- 不假设 OKX API key 可以绕过逐笔签名；
- 不把无限 approve、永久 session key 或未知 spender 放进执行白名单；
- 不把 Flashblocks preconfirmation 当 canonical 成交；
- 不在没有 P95/P99 数据时宣称 TEE、MPC、KMS 或某 RPC “最快”。

## Limitations

1. 本报告没有获得 OKX TEE、`contract-call`、Wallet API broadcast 的公开 P50/P95/P99；性能结论必须通过项目 benchmark 补齐。
2. Wallet API 总支持链矩阵与 broadcast 页面存在口径差异，X Layer 的具体账户权限待 API 实测/工单确认。
3. Uniswap/Permit2/Universal Router 的官方部署不代表项目当前所有股票 wrapper、池和 Router 都兼容；进入执行白名单前必须核对 bytecode、selector、approve 和回滚。
4. KMS/HSM/MPC 的签名适配、费用、并发配额和灾备未在本项目实施；本报告只给出架构取舍。
5. Flashblocks 是 preconfirmation 反馈机制，不提供交易排序或最终性；canonical receipt 和双腿对账仍是完成条件。

## Bibliography

[1] OKX Onchain OS, “Agentic Wallet introduction,” https://web3.okx.com/id/onchainos/dev-docs/wallet/agentic-wallet

[2] OKX onchainos-skills, “wallet-cli-reference.md,” https://raw.githubusercontent.com/okx/onchainos-skills/main/skills/okx-agentic-wallet/references/wallet-cli-reference.md

[3] OKX Onchain OS, “Wallet API broadcast transaction,” https://web3.okx.com/id/onchainos/dev-docs/wallet/onchain-gateway-api-broadcast-transaction

[4] OKX Onchain OS, “Supported chains,” https://web3.okx.com/zh-hans/onchainos/dev-docs/home/supported-chain

[5] OKX Onchain OS, “Build Your DApp quickstart,” https://web3.okx.com/cs/onchainos/dev-docs/home/run-your-first-dapp

[6] X Layer / OKX Onchain OS, “RPC endpoints,” https://web3.okx.com/it/onchainos/dev-docs/xlayer/developer/rpc-endpoints/rpc-endpoints

[7] X Layer / OKX Onchain OS, “Flashblocks overview,” https://web3.okx.com/zh-hans/onchainos/dev-docs/xlayer/developer/flashblocks/overview

[8] X Layer / OKX Onchain OS, “Flashblocks FAQ,” https://web3.okx.com/sv/onchainos/dev-docs/xlayer/developer/flashblocks/faq

[9] OKX Onchain OS, “DEX API supported chains,” https://web3.okx.com/zh-hans/onchainos/dev-docs-v5/dex-api/dex-supported-chain

[10] OKX Onchain OS, “DEX API swap,” https://web3.okx.com/zh-hans/onchainos/dev-docs-v5/dex-api/dex-swap

[11] Uniswap Developers, “Uniswap v4 deployments,” https://developers.uniswap.org/docs/protocols/v4/deployments

[12] Uniswap Developers, “Supported chains and tokens,” https://developers.uniswap.org/docs/trading/swapping-api/supported-chains

[13] Uniswap Developers, “Permit2 overview,” https://developers.uniswap.org/docs/protocols/permit2/overview

[14] AWS, “KMS key spec reference,” https://docs.aws.amazon.com/kms/latest/developerguide/symm-asymm-choose-key-spec.html

[15] AWS, “KMS Sign API,” https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html

[16] Fireblocks, “Create transaction API,” https://fireblocks.readme.io/reference/createtransaction

[17] Safe, “Safe Modules,” https://docs.safe.global/advanced/smart-account-modules

[18] Ethereum Improvement Proposal, “ERC-2612: Permit Extension for ERC-20,” https://eips.ethereum.org/EIPS/eip-2612

[19] Ethereum Improvement Proposal, “ERC-4337: Account Abstraction Using Alt Mempool,” https://eips.ethereum.org/EIPS/eip-4337

[20] OKX onchainos-skills, “Agentic Wallet security and wallet reference,” https://github.com/okx/onchainos-skills/blob/main/skills/okx-agentic-wallet/references/wallet.md

[21] Uniswap Developers, “Uniswap v3 X Layer deployments,” https://developers.uniswap.org/docs/protocols/v3/deployments/v3-xlayer-deployments

[22] EIP-20, “Token Standard,” https://eips.ethereum.org/EIPS/eip-20

[23] Arb Monitor project, “HANDOFF.md,” local project evidence, `/home/operator/Documents/arb/HANDOFF.md` (现场数据与配置事实，非外部产品承诺)
