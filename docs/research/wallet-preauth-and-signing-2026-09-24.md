# 钱包预授权、签名与执行速度调研 — X Layer / OKX Wallet / Privy·Safe（2026-09-24）

> 本文是
> `docs/research/xlayer-executable-pricing-wallet-execution-2026-09-24.md`
> 的**续篇**：那一篇回答「怎么最快拿到可成交池价」，这一篇回答
> 「提前授权之后能不能直接调钱包 API / 能不能自己写代码打链上合约 /
> Privy × Safe 有没有预授权与签名方案、能不能更快」。
>
> **没有改动**任何 operator 专属开关：`[limits]`、执行白名单、
> `live_ack`、`auto_orders`、`execution.mode`、`data/KILL_SWITCH`、
> `data/AUTO_HALT` 全部原样。

## 0. 本轮能做与不能做（先读，关系到怎么读下面的证据）

本轮**无法联网取证**，原因不是策略选择，而是环境限制：

1. 沙箱内无出网：`curl` 对 `r.jina.ai`、`docs.privy.io`、`93.184.216.34`
   全部 `Could not resolve host` / `Connection refused`；
2. `require_escalated` 与浏览器（Playwright、in-app Browser）两条提权路径
   都被自动审批后端拒绝，返回 `402 Weekly spending limit reached`
   （示例 request id `bbc560b3-…`、`64d3849f-…`）。这是**审批服务不可用**，
   不是「动作不安全」的判定。

所以本文的证据分三级，请按级别采信：

| 级别 | 含义 |
|---|---|
| **[实测]** | 本仓库文件、配置或已记录的现场测量数字，可在仓库内直接复核 |
| **[存档]** | 今天 00:0x–00:5x 往轮子代理会话里抓到的公开资料原文（含 URL），原始日志在 `/home/operator/.codex/sessions/2026/09/24/`。**本轮没有重新联网复核** |
| **[待确认]** | 本轮无法核验，联网恢复后必须补的项（见 §9） |

## 1. 结论（直接回答四个问题）

**1）「快速获取可以成交的池子价格」最快的是本地链路，不是任何钱包或 OnchainOS。**
顺序是：OKX 行情宽筛 → 本地池深度/映射过滤 → `pending` 标签（Flashblocks）
`eth_call` 打 QuoterV2 → 本地编码固定池 Router calldata。[实测][存档]
聚合 API（OKX DEX 等）多一次 HTTP 往返和一次路由时间差，只适合多跳/未知池。

**2）「提前授权之后，直接调用钱包 API」——能做到「无人值守下单」，但做不到「免签名」。**
Pre-authorization 能提前做掉的是四件事（approve、permit/Permit2 授权、
白名单与额度策略、人工确认），**每一笔交易仍然需要一次密码学签名**。
托管型 WaaS（Privy、Turnkey、Coinbase CDP、Fireblocks/Fordefi）的模式是
「你的服务端出示授权密钥 → 对方 TEE 内校验策略 → TEE 内签名 → 广播」，
比人工点确认快得多，但每笔仍是一次 HTTPS 往返 + 一次 TEE 内签名，
且**没有任何厂商公布 P50/P95/P99**。[存档][待确认]

**3）「直接写好代码去调用链上的合约」——本项目已经在这么做，缺的只是广播端的签名者。**
`[实测]` `config/hk_monitor.toml` 的 `[onchain] pool_quotes = true`、
`quoter_v2 = "0xd1b797d9…"` 已经是本地 RPC 打链上 QuoterV2；
执行侧 `src/arb_monitor/execution/onchain_wallet.py:339` 调的仍是
`wallet contract-call --chain …`（OKX TEE 签名 + 自动广播）。
也就是说：**读链已经是本地代码，写链还挂在 TEE 上。**

**4）Privy × Safe 是「用户端无 gas 首单」模板，不是「低延迟交易」模板。**
用户在 Polymarket 那套模板里见过的组合是：Privy 嵌入式钱包当签名者 +
Polymarket Relayer 无 gas 部署 Gnosis Safe + 批量 approvals
（来源：`/home/operator/.codex/sessions/2026/03/19/…` 的 starter kit 记录）。
在套利热路径上，这个组合的收益是「授权前置、无人值守」，
代价是**多一层账户抽象**（Safe 账户 + 可选 4337 bundler），
通常比受限 EOA 慢，而且 Safe 模块一旦启用即拥有绕过签名校验的能力。[存档]
Privy 真正有价值的部分是 **TEE 内策略引擎**，不是 Safe 本身。详见 §4。

## 2. 先把问题拆成三件互相独立的事

| 子问题 | 现状 | 目标 | 真正的约束 |
|---|---|---|---|
| **价格**：这个数量的可成交价是多少 | 本地 RPC QuoterV2（已实现） | 亚秒内拿到「按目标数量」的 exact-in/out | 状态新鲜度、池深度、quote ≠ fill |
| **签名**：谁替这笔记账产签名 | OKX Agentic Wallet TEE `contract-call` | 把签名尾延迟压到机会窗口内 | 每笔必须有一次签名；托管方策略检查是否可预测 |
| **授权**：哪些事可以只做一次 | 已在做 ERC-20 approve、白名单 | 减少交互次数、限制风险敞口 | allowance/permit/session key **都不等于签名** |

把三件事混在一起讨论，就会出现「是不是提前授权就能免签名下单」这种无解问题。

## 3. 九条候选路径对比

「X Layer 支持」一栏里，只有被明确记录过的才写「有证据」，其余一律 §9 待确认。

| 路径 | 密钥在哪 | 预授权能力 | 每笔仍需签名 | X Layer | 延迟 | 复杂度 | 本项目定位 |
|---|---|---|---|---|---|---|---|
| **OKX Agentic Wallet TEE `contract-call`** | OKX TEE，不可导出 | 不行（每笔过一遍策略+风险检查） | 是（TEE 内） | 有证据：文档给出 X Layer 上的 4337 AA 与 7702 地址 | **[待确认] 无公开 SLA**，且当前 15,239 ms 是**询价**批次，不能归因到签名 | 低（已在用） | **短期保留**，只承接已过滤的少量交易 |
| **OKX 通用 Wallet API broadcast** | 调用方自己签 | 不适用 | 是 | 文档口径与总支持链矩阵不一致 | 一次 HTTP | 低 | 只在「已有 signer」时当广播通道 |
| **自持受限 EOA（本地隔离进程）** | 本机，非策略进程 | 策略级预授权可全做 | 是（本地，毫秒级） | 纯 RPC，无依赖 | **最快**的一档 | 中（自己管 nonce/广播/恢复） | 若 TEE 尾延迟成为瓶颈再上 |
| **AWS KMS / 自建 HSM** | 云 KMS/HSM，不可导出 | 策略在调用侧 | 是（KMS API 往返，且要自己拼 `(r,s,v)`） | 纯 RPC | 中（一次 API RTT） | 中高 | 资金变大后的**审计友好**选项 |
| **Turnkey** | 厂商 enclave（QuorumOS） | 有 policy engine（可在 API 里写允许/拒绝规则） | 是（API 签名） | **[待确认]** | [待确认] | 中 | 只能算候选，需先实测 |
| **Privy** | 厂商 TEE（secure enclave） | **有**：授权密钥 + policy engine（限额、收款方/合约白名单、calldata 约束） | 是（TEE 内） | **[待确认]（X Layer 是否在链分档里）** | [待确认] | 中 | 备选；见 §4 |
| **Coinbase CDP Server Wallets** | CDP 托管 | 有 Policy Engine API（`/v2/policy-engine/policies`） | 是（`/sign/transaction`） | **[待确认]** | [待确认] | 中 | 备选 |
| **Fireblocks / Fordefi** | MPC，厂商侧 | 强策略（Fireblocks：每条路径签名前强制策略，含 API 自动化） | 是 | **[待确认]** | 偏慢（审批/轮询） | 高 | 机构治理，不适合小资金热路径 |
| **Safe（模块）+ 4337 + 7702 session key** | 链上账户 + 签名者 | 链上可编程限额/白名单 | **是**（见 §5） | 有证据：X Layer 官方 AA 页 + OKX 自己的 4337/7702 实现地址 | 多一跳（bundler），通常更慢 | 高 | 金库/权限层，**不做热路径** |

## 4. Privy 专项

### 4.1 已经从公开文档拿到的（[存档]，含原始 URL）

1. **执行环境与授权模型**：Privy wallet API 由 TEE（secure enclave）执行；
   请求要带**授权密钥签名**，enclave 先验证授权密钥签名再执行。
   授权密钥是 **P-256 / secp256r1** 非对称密钥，私钥生成在客户端、Privy 自己也看不到。
   支持 service-controlled wallets（授权密钥由你的服务持有）与
   **m-of-n key quorum**。
   → `https://docs.privy.io/security/wallet-infrastructure/policy-and-controls`
2. **Policy engine**：可配转账限额、收款方 allow/deny、智能合约 allow/deny，
   以及对 calldata 的约束；默认在 enclave 内评估。
   **但文档明确写着：像「转账金额上限」这类规则需要交易模拟，而模拟目前在
   enclave 之外执行。** 这句是选型关键——策略并非 100% 都在 enclave 内。
   → 同一个 URL。
3. **代用户交易的两代模型**：老的是 on-device wallet execution 下的
   session signers / delegated actions（`delegateWallet`，dashboard 里可开
   「Require signed requests」并生成 signing key，同样是 P-256）。
   文档现在的口径是：2025 年 5 月之后创建的应用按 **session signers overview**
   走，老应用**建议迁移到 TEE wallet execution**。
   → `https://docs.privy.io/guide/server/wallets/delegated-actions/usage/setup`、
   `https://docs.privy.io/wallets/using-wallets/signers/delegate-wallet`
4. **服务端一步签名+发送**与限流：服务端 SDK 提供
   `signAndSendTransaction` 之类的一步接口；文档同时提醒
   **REST API 有 rate limit**（数值未记录）。
   → `https://docs.privy.io/basics/nodeJS-node/setup`
5. **链支持分档**：文档把链分成多档，只有最高档是「原生完整功能」；
   低档只提供 raw sign（签名后由你的应用自己构造并广播交易）。
   → `https://docs.privy.io/wallets/using-wallets/other-chains`
6. 安全架构页：`https://docs.privy.io/guide/security/architecture/`；
   机构钱包产品页（宣称支持全部 EVM 链）：`https://www.privy.io/organization-wallets`。
7. 行业写法（存档检索到的第三方总结）：**厂商 TEE 服务**（Coinbase、Privy、
   Phantom、Turnkey、Crossmint、Thirdweb）的共同形态是
   「密钥在 TEE、**厂商跑策略引擎**、暴露签名 API」；
   反面是自持密钥 + 自建策略。这段不是 OKX/Privy 官方口径，只当行业惯例参考。

### 4.2 「Privy 的 Safe wallet」到底指什么（[待确认] + 已知映射）

用户看到的组合来自 Polymarket 模板：**Privy 嵌入式钱包（签名者）+
Polymarket Relayer 无 gas 部署 Safe + 批量 approvals**。
在 Privy 自己的语境里，这条线对应的是它的 **smart wallet / 4337** 能力
（用 Kernel 或 Safe 作为账户实现，Privy 出签名），X Layer 是否在支持列表、
是否支持 Safe 作为账户实现、是否支持 Safe 4337 模块——**本轮无法联网确认**。

需要核的是这几个页面（联网恢复后逐个看，见 §9）：

- `https://docs.privy.io/llms.txt`（文档总索引，先拿这个再定位）
- Privy Smart Wallets / 4337 与 Safe 集成页（在总索引里搜 `smart wallet`、`4337`、`Safe`）
- Privy 支持链与分档页（确认 `chainId 196` / X Layer）
- Privy policy engine 对 **calldata 白名单**的粒度（能不能锁死 Router + selector + 参数范围）
- Privy 的 rate limit / QPS 数值与是否有企业 SLA

**判断口径（即使没联网也不会错）**：无论 Privy 的 Safe 支持到什么程度，
只要签名发生在厂商 TEE、请求走 HTTPS，**它就比自持签名多一次网络往返**；
它换来的是「没人守机器也能下单 + 策略可审计」。
对本项目 1 万 USDT 级别的资金和 60 秒级的机会窗口，
这属于**可选项而不是必需项**。

## 5. Safe / 4337 / 7702 / session key：收益与代价

- **Safe 模块**：能实现限额、白名单、自动化。代价要说清楚——
  存档里引用的 Safe 官方表述是
  「模块一旦启用，可以是任意以太坊地址，并且**可以绕过签名校验逻辑**
  通过账户执行交易」（`https://docs.safe.global/advanced/smart-account-modules`）。
  换句话说，**给模块的权限等价于交出一部分私钥**，必须按同一标准审计。
- **策略引擎对比（[存档]，原始页 URL 未记录全）**：Fireblocks 的策略在
  MPC 签名前对每条路径强制生效（含 API 自动化），实现不可被内部人改动；
  Fordefi 出厂默认规则是不需要审批（要客户自己收紧）；
  Safe Shield 是平台层策略自动化，策略变更受双审批上限约束。结论：
  「有策略引擎」不等于「策略一定在签名前、在 enclave 内生效」，要逐项问。
- **ERC-4337**：多一跳（bundler，可选 paymaster），换来 gas 代付、批量调用、
  链上限额。X Layer 官方有 Account Abstraction 概览页
  （`https://web3.okx.com/…/xlayer/docs/developer/tools/account-abstraction-overview`，
  列出 Particle Network、Safe 等），OKX 自己的 Agentic Wallet 就是
  ERC-4337 AA + EIP-7702（存档里记到 X Layer 上的 AA 钱包地址
  `0x872c4c0c5648126a3ac5cb140a2f1622a0b2478d`、
  7702 delegate `0xe40ccb2d94975c51bff0c004efdfd9b3a5796fa4`）。
  同时，X Layer 关于 ERC-8021 归因的文档里有一句
  「**ERC-4337 User Operations — not supported yet**」——**口径待确认**，
  不能据此断言 X Layer 不支持 4337（OKX 自家钱包就在用），
  但说明「哪个子系统支持到什么程度」必须逐个核。
- **EIP-7702**：OKX Agentic Wallet 用它把执行逻辑挂到 EOA 上，
  好处是原子多步（approve → swap → settle）不暴露私钥。
  风险是**原 EOA 私钥永远保留完整权限、delegate code 可被替换**，
  存档里的结论也是「7702 权限与 delegate code 风险较高，
  当前不应作为第一版套利执行方案」。
- **Session key**：OKX 的批量支付（x402 `aggr_deferred`）用预授权 session key，
  但文档同时写明：**session key 的签名本身不能上链，必须由 TEE 内的 owner key
  再签一次并在链上聚合**，而且「买家必须使用 Agentic Wallet，
  普通 EVM 钱包不支持这套 session key 机制」。
  **这条最关键**：所谓「预授权」在 OKX 的架构里并没有消灭每笔的 TEE 参与。

## 6. 推荐路线（四档，带触发条件）

**档 0（现在，已经在做）**：报价热路径本地化
（OKX 宽筛 → 本地池过滤 → pending QuoterV2 → 本地 calldata）。
不要回退到「用 OnchainOS 批量询价覆盖全宇宙」——[实测] 16 个资产一批约
15,239 ms、`quote_rate_limited` 累计 4,494。

**档 1（下一步，不换 signer）**：把 TEE 调用变成**可测量的黑盒**。
补三件事：(a) approve/allowance 全部前置，热路径里不允许再出现 approve；
(b) calldata、`amountOutMinimum`/`amountInMaximum`、deadline 全部在本地预编码；
(c) 给 `contract-call` 分段打点，拿到 P50/P95/P99。

**档 2（触发条件：TEE 段 P95 明显吃掉机会窗口）**：
上一个**自持受限 signer**——独立进程或 AWS KMS（secp256k1），
策略进程只提交结构化意图，签名进程校验 chainId=196、`to`/selector/token/
spender/recipient 白名单、额度、minOut/maxIn、deadline、nonce、
simulation hash，通过后才签名；广播走 X Layer 原生 RPC。
**这不是安全降级**：现在的信任边界是「OKX TEE + OKX 策略」，
换过去是「本机隔离进程 + 自己的策略」，而且能把尾延迟从「未知」变成「已知」。

**档 3（资金变大后）**：Safe 放金库/权限层（限额模块 + 恢复），
热钱包只留小额；托管 WaaS（Privy/Turnkey/CDP）留给「必须无人值守」的场景。

**明确不采纳**：无限 approve；永久 session key；把 Safe/4337 当热路径；
把 7702 当主账户；把 Flashblocks preconfirmation 当成交完成。

## 7. Benchmark 契约（不改任何开关就能跑）

每笔（先纸面，`auto_orders` 不动）记录这些阶段，用单调时钟：

```text
screen → pool_state(latest|pending) → quote(QuoterV2)
      → encode → simulate(eth_call/estimateGas)
      → sign(TEE contract-call 或自持 signer)
      → broadcast → first_seen(txHash) → preconfirmation
      → canonical receipt → 余额/Transfer 日志 → OKX 永续腿对账
```

输出 p50/p95/p99 + 成功率/超时率/revert 率/**未知状态率**，场景矩阵至少覆盖：
冷启动 vs 热连接、已有 allowance vs 需 approve、`latest` vs `pending`、
普通 RPC vs `/flashblocks` 广播、HTTP 429、nonce 冲突、TEE 风控拒绝、
canonical receipt 迟迟不出现。

判定规则：**只有当「sign」段 P95 占端到端的主要部分时才迁移 signer**；
否则迁移只是把密钥风险搬到自己家里，换不到速度。

## 8. 与已有结论不冲突的地方

- 前一篇报告的架构（本地 QuoterV2 + 本地 calldata + TEE 签名 + canonical 对账）
  **不变**；本文只回答「预授权能做什么、Privy/Safe 值不值得上」。
- X Layer Flashblocks（约 200 ms 级、1 秒 L2 block、**可能 reorg**）只用于
  更快读状态与更快看到 preconfirmation，**不是最终性**，也不是私有交易或排序保护。
- 本项目当前「未知状态一律停机/不动作」的口径继续适用；
  换成任何新 signer 都不改这一条。

## 9. 待确认清单（联网恢复后必须补的核验）

1. Privy 支持链分档里是否有 X Layer（`chainId 196`）——决定 Privy 能否作为候选。
2. Privy smart wallet / 4337 是否支持 Safe 作为账户实现；
   若有，账户地址、EntryPoint、bundler、paymaster 分别是哪一套。
3. Privy policy engine 能否约束到「Router 地址 + selector + 参数范围」粒度；
   以及哪些规则在 enclave 内、哪些在 enclave 外（已知「金额上限」在 enclave 外）。
4. Privy / Turnkey / CDP 的 rate limit、并发上限、以及有没有 P95 SLA。
5. OKX Agentic Wallet `contract-call` 在 X Layer 上的真实速率上限、
   并发限制，以及风控对「项目自建 Router / wrapper token」是否放行。
6. X Layer 上真正可用的 bundler / paymaster 清单与延迟；
   以及 X Layer 文档里那句「ERC-4337 User Operations — not supported yet」
   到底限定在哪个子系统（归因？模拟？还是全局）。
7. 是否存在 X Layer 上的私有广播/MEV relay。**不要把 Flashblocks 或
   ERC-8021 Builder Codes 误当私有交易或排序保护。**

以上 7 项全部完成前，**不建议**为了「更快」更换 signer 或引入
Privy/Safe/4337 —— 先有分段实测，再谈架构。

## 10. 证据来源

**[实测]（仓库内可复核）**

- `HANDOFF.md`（2026-09-23/24 各条追加、15,239 ms 与 `quote_rate_limited` 记录）
- `config/hk_monitor.toml`（`[onchain]`：`pool_quotes`、`quoter_v2`、`rpc_url`、`chain_id`）
- `src/arb_monitor/execution/onchain_wallet.py:339`（`wallet contract-call --chain …`）
- `src/arb_monitor/execution/wallet.py`（`wallet signing is disabled` 闸门）
- `docs/research/xlayer-executable-pricing-wallet-execution-2026-09-24.md`（前一篇，含 [1]–[23] 外部来源）

**[存档]（往轮会话检索到的公开资料，URL 见正文）**

- Privy：policy-and-controls、delegate-wallet、delegated-actions/usage/setup、
  basics/nodeJS-node/setup、wallets/using-wallets/other-chains、
  guide/security/architecture、privy.io/organization-wallets
- Safe：`https://docs.safe.global/advanced/smart-account-modules`、safe.global/safeshield
- X Layer：account-abstraction-overview、Flashblocks overview/FAQ、ERC-8021 归因页
- OKX：Agentic Wallet 文档与 Agent Payments Protocol 材料（4337 + 7702 + session key 口径）
- Turnkey（`docs.turnkey.com/concepts/policies`、QuorumOS）、
  Coinbase CDP（`/v2/policy-engine/policies`、`/sign/transaction`）、
  Fireblocks/Fordefi 策略对比材料
- 用户历史会话：`/home/operator/.codex/sessions/2026/03/19/…`（Privy + 无 gas Safe + relayer 模板）

**[待确认]**：§9 的 7 项。

---

## 11. 附：现场实测 —— 一笔成功套利的时间到底花在哪（2026-09-24 加）

Operator 明确瓶颈是「签名和广播太慢」。为回答「换 Privy 会不会更快」，
先把出一笔真实套利时的**网络跳数**和**已记录耗时**点清楚（全部 [实测]，
可在仓库内复核）。

### 11.1 一笔现货腿要串 5 次远程调用

代码路径（`src/arb_monitor/execution/onchain_wallet.py` +
`src/arb_monitor/execution/okx_wallet.py`），每一步都是一次独立的
OnchainOS CLI/HTTPS 往返，且全部走同一条本地代理：

| # | 调用 | 目的 | 能否搬到本地 |
|---|---|---|---|
| 1 | `swap quote` | 拿路由与报价 | **能**：QuoterV2 `eth_call`（本项目已接） |
| 2 | `swap swap` | 让 OKX 生成 calldata | **能**：本地按固定池 Router 编码 |
| 3 | `gateway simulate` | 远程模拟 | **能**：本地 `eth_call`/`estimateGas` |
| 4 | `security tx-scan` | 远程风控扫描 | 策略问题，见下 |
| 5 | `wallet contract-call` | **TEE 签名 + 自动广播** | 只有这一步是「签名」 |

之后 `receipt_waiter` 用本地 X Layer RPC 轮询，**间隔 2.0 秒、最多 15 次**
（`attempts=15, sleep_seconds=2.0`），所以确认环节本身有最多 2 秒的粒度损失。

### 11.2 已记录的两笔真实数据

**A. 成功的一笔（2026-09-15 23:09，XIAOMI，allowance 已预授权）**

```text
23:08:41  allowance_ready（approved，链上 approve 已完成）
23:09:14  precheck
23:09:23  spot_filled   ← 现货腿 9 秒（含第 1–5 跳 + 等 receipt）
23:09:29  perp_submitted（+6 秒）
23:09:32  hedged
```

**B. 最近一笔（2026-09-24 10:35，CRWV）**

```text
perp maker_order 第1次  wait_ms 31516  canceled
perp maker_order 第2次  wait_ms 31382  canceled
perp maker_order 第3次  wait_ms 30996  canceled
perp maker_order 第4次  wait_ms 58293  filled   ← 永续腿共等 152.3 秒
spot_filled             block 71448498, gas_used 292052
```

同一天稍早的 SHEIN：3 次 maker 全部 `canceled`、`filled=0`；
2026-09-23 两轮、2026-09-22 一轮也是同样形态。

### 11.3 这些数字说明什么

1. **签名不是唯一项、而且在成功那一笔里不是最大项**。现货腿端到端 9 秒里，
   真正属于「TEE 签名」的只有第 5 跳；第 1–4 跳是四次额外的远程往返。
2. **永续腿的挂单等待才是最大的一块**：CRWV 等 152 秒才能对冲。
   这是策略选择的结果，不是技术慢——
   `config/automation_policy.toml`：`style = "maker_first"`、
   `perp_order_type = "post_only"`、`maker_timeout_seconds = 120`、
   `maker_max_reprices = 3`、`maker_open_taker_fallback = false`。
   另外 `maker_max_naked_seconds = 90` 要求现货腿在永续成交后 90 秒内完成。
3. **本地 CLI 启动开销可以排除**：`onchainos --version` 实测 real 0.00 s
   （6 次），进程启动不是瓶颈；瓶颈是那 5 次串行网络往返。
4. 因此：**换 Privy 只影响第 5 跳，前 4 跳的开销一点都不会消失**；
   而 Privy 的第 5 跳同样是「远程 TEE 签名」，不比自己广播更快。

### 11.4 由此得到的提速优先级（按收益排序）

1. 把第 1–4 跳搬到本地（QuoterV2 + 本地 calldata + 本地 `eth_call` 模拟），
   把 5 次串行远程调用压成 1 次签名调用。**这一步不需要换钱包。**
2. `receipt_waiter` 的 2 秒粒度 → Flashblocks `pending` 观察 + canonical 复核。
3. 永续腿的 `maker_timeout` / reprices / taker fallback 是否对小额单放宽
   ——**这是 operator 的额度与策略决定，本调研不代为修改。**
4. 只有当第 5 跳（签名）在搬完 1–4 跳后仍占主要时间，才谈换
   Privy / 自持受限 signer / session key。
