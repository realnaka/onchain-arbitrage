# 港股代币—永续合约套利自动执行最终方案

版本：v1.0（2026-09-14）

## 1. 目标与边界

目标策略：

```text
X Layer 买入港股代币
→ Binance 或 OKX 做空对应 USD 线性永续
→ 退出时卖出代币并回补永续
```

第一版严格采用分阶段上线：

1. 纸面交易与历史回放；
2. Binance Futures Testnet / OKX Demo；
3. X Layer 小额链上单腿；
4. CEX 小额单腿；
5. 人工批准的双腿；
6. 带 kill switch 的小额自动双腿。

LLM、Agent Trade Kit 和 MCP 不进入实时执行热路径。它们只用于配置、诊断、
人工审批和非实时运维。

## 2. 双通道执行架构

```text
行情与策略
  → ExecutionIntent
  → PreTradeRisk
  → ExecutionCoordinator
      ├─ Binance Futures API / OKX Futures API
      └─ OKX DEX Swap API
           → Agentic Wallet/TEE Signer
           → X Layer RPC 或受支持的 Broadcast API
  → 成交/回执
  → 数量配平与账本
```

OKX Agent Trade Kit 不作为 CEX 热路径执行器。CEX 使用原生 REST/WebSocket API，
必要时使用官方技能作为接口参考。

OKX DEX Swap API 负责报价和交易数据构造，不应被当作自动替用户签名的钱包 API。
签名与广播必须独立建模。

## 3. 链、资产与稳定币

- X Layer chain ID：`196`。
- wXIAOx：`0x076cf393e701839fc7a5832d2c68aafa235682ae`。
- wTCENTx：`0x41333df9e7639188bbfca5522dc4844398af9f9e`。
- wPOPMTx：`0x4ebf5fd25b02022afad96e2fa25da54a246fded0`。
- wMEITx：`0xad1b65c8556957cf23d1b5e9accdc449b415fa97`。

USDC、USDG、USDT0、USDT 必须按合约地址分别核算，不能默认 1:1 合并。每次
报价需要记录输入/输出 token、数量、路由、price impact、Gas、有效期和来源。

## 4. 链上交易流程

```text
读取余额和 allowance
→ 获取 exact-in quote
→ 校验 chain/token/router 白名单
→ eth_call 模拟
→ 设置 amountOutMinimum 与 deadline
→ 必要时发送有限额度 approve
→ 通过受控 signer 签名
→ 进入 MEV 检查
→ 私有/受保护路径广播
→ 等待 receipt 和确认数
→ 解码实际输入输出
```

钱包签名器应使用 OKX Agentic Wallet/TEE 或等效 KMS/HSM/MPC 服务。私钥不得进入
本项目进程、日志、配置文件或 Agent 上下文。

## 5. MEV、夹子交易与广播安全

### 5.1 防护时点

防夹必须在以下顺序中完成：

```text
报价前：控制金额、price impact、滑点和路由
签名前：模拟交易、校验 calldata、router、token、deadline
广播前：选择私有/受保护传输并附加 MEV 参数
广播后：监控 receipt、实际成交和异常价格
```

广播后无法撤回已经进入公开 mempool 的交易。广播后监控只能发现问题或触发
对冲/止损，不能事后消除夹子交易。

### 5.2 OKX MEV Protection

OKX Broadcast Transaction API 的 MEV 防护是广播参数，不是钱包默认永久开启的
全局开关。使用时必须显式设置：

```json
{
  "extraData": {
    "enableMevProtection": true
  }
}
```

官方文档说明该功能默认关闭，并且只对官方支持的链开放；不能假设 X Layer
chain ID `196` 已支持。当前代码对未验证链采取 fail-closed：不自动退回公开
mempool。

### 5.3 X Layer 的处理规则

在确认 OKX Broadcast API 对 X Layer 可用前，执行器只能：

1. 使用已验证的 X Layer 私有 RPC/交易保护服务；或
2. 停止广播并转人工。

不得因为 OKX Swap API 能返回 X Layer 报价，就推断 Broadcast API 也支持
X Layer MEV Protection。

### 5.4 其他防护

- exact-in；
- 严格 `amountOutMinimum`；
- 短 `deadline`；
- 小额分拆仅在纸面测试证明有帮助后使用；
- 禁止无限滑点；
- 禁止未知 router；
- 禁止 quote 与 calldata 的 token/金额不一致；
- 禁止公开 RPC 作为默认生产广播路径；
- 交易进入 pending 后监控 nonce、Gas 和确认时间；
- 发现异常成交或严重延迟时暂停新开仓并保留平仓能力。

## 6. 两腿执行顺序

默认开仓顺序：

1. 重新读取盘口和链上 quote；
2. 风控检查并冻结额度；
3. 先用 IOC/受限限价单卖空永续；
4. 以实际成交数量重新计算链上买入量；
5. 获取 Swap calldata；
6. 模拟、签名并进行 MEV 保护检查；
7. 广播链上交易；
8. 交易确认后对账；
9. 链上失败时立即 reduce-only 回补永续。

任何部分成交、超时、RPC 故障或 nonce 冲突都进入
`MANUAL_INTERVENTION_REQUIRED`。

## 7. 交易订单类型

- 链上：exact-in swap，不存在传统市价单/限价单；
- 永续开仓：IOC 或主动限价；
- 永续正常平仓：reduce-only IOC/限价；
- 紧急平仓：reduce-only 受限市价；
- Maker 只在 paper trade 统计成交率和裸露时间后作为可选优化。

## 8. CEX 接入

统一接口：

```text
get_balances
get_positions
get_instrument
place_order
cancel_order
get_order
get_fills
set_leverage
set_margin_mode
private_order_stream
private_fill_stream
```

Binance 和 OKX 都使用独立子账户、API IP 白名单、Read/Trade 最小权限，
禁止 Withdraw。每个合约必须读取并保存乘数、步长、tick、最小名义金额、
结算币、持仓模式和 funding 规则。

## 9. 风控与 kill switch

必须限制：

- 总资金；
- 单笔名义金额；
- 单资产；
- 单交易所；
- 单钱包；
- 最大滑点；
- 最大 price impact；
- 最大 Gas；
- 最大信号年龄；
- 最大裸露时间；
- 最大每日亏损；
- 稳定币脱锚；
- RPC/API 错误次数。

kill switch 要同时停止：

- 新 CEX 开仓；
- 新链上签名；
- 新链上广播；
- 新信号消费。

仍必须保留人工平仓和 reduce-only 能力。

## 10. 测试与上线门槛

必须覆盖：

- 错链、错 token、错 router；
- quote 过期；
- amountOutMinimum 不满足；
- allowance 不足；
- approve 失败；
- liquidity 为零；
- RPC 失败；
- MEV 保护链不支持；
- 禁止公开 mempool fallback；
- 永续拒单、部分成交、未成交；
- 链上 revert、pending、nonce 冲突；
- 进程在两腿之间重启；
- kill switch 中途触发；
- 账本与外部余额不一致。

只有在 7–14 天 paper trade、Demo/Testnet、小额单腿和人工批准双腿全部通过后，
才允许开启受限自动双腿。

## 11. 当前代码实现状态

已实现：

- `ExecutionIntent`；
- CEX/钱包适配器协议；
- fail-closed 的 Binance/OKX 默认适配器；
- X Layer 钱包 façade；
- 风控与 kill switch；
- 永续先开、链上后买的状态机骨架；
- 两腿数量对账；
- MEV 广播策略与不支持链拒绝；
- 纸面与异常路径测试。

尚未启用：

- 真实 Binance 私有 API；
- 真实 OKX 私有 API；
- OKX Agentic Wallet 签名；
- X Layer 真实广播；
- 自动充值、提币、跨链；
- 无人工批准的实盘自动执行。
