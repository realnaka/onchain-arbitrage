# 接入一家新交易所的执行适配器 —— 分步计划（2026-09-20 调研）

触发条件：`venue-scoreboard` 读出"哪个交易所机会最多"，operator 决定接那一家。
本文把"接入"要做的所有工作列成可逐步执行的清单，附现状耦合点与验收标准。
**执行开关、额度、白名单仍然全部是 operator 专属开关，本文不改变任何一项。**

---

## 1. 现状：执行链目前只有 OKX，而且是在三个地方写死的

监控层已经支持 8 场（ccxt 公开行情，`CexPublicAdapter`）；**执行层只有 OKX**：

| 耦合点 | 位置 | 影响 |
|---|---|---|
| `LiveStack` 直接构造 OKX 客户端 | `execution/live_stack.py`（`build_live_stack()`：`OKXReadOnlyClient` / `OKXTradeClient` / `FullChainRunner(okx=trade)`） | 新增场所必须改这里，否则新场所根本进不了栈 |
| 运行器参数名与调用面都是 OKX | `execution/full_chain.py:210`（`def __init__(..., okx, ...)`）、`:238/303/696/711/751/778/831`；`execution/maker_first.py:159`（`def __init__(..., okx, ...)`）、`:184/189/211/225/231/237/270/275/322/502` | 组件是鸭子类型调用（方法名通用），但**参数名和错误类型**（`OKXTradeError`）是 OKX 的 |
| 合约→场所的判定是写死的后缀启发式 | `execution/full_chain.py:924`（`_venue_of()` 只认 `-USDT-SWAP`→okx、`_USDT`→gate，其余兜底 okx） | 接新场所不修这里，会把它当成 okx |
| 健康检查里硬编码 `okx` | `execution/auto_execute.py:466` | 同上 |

**结论：接入 = 写该场客户端 + 把上面 4 处场所无关化 + 配置与映射核验 + 纸面/实盘分阶段放行。**

---

## 2. 分步清单（每步都有交付物与验收）

### S0 选场（数据依据，不要凭印象）

```bash
PYTHONPATH=src .venv/bin/python -m arb_monitor venue-scoreboard --hours <窗口> --wallet-quote-usdt <余额>
```

同时读三项，缺一不可：**过门槛次数与率**（按交易时段 / 盘前盘后 / 其他休市三列分开看）、
**该场机会集中在哪几个标的**、**不可信（陈旧）行占比**。小样本的场（可评估数很少）不要
因为 rate 高就选它。

交付物：一页选场结论，写明窗口、命令、数字、以及"为什么不是第二名"。

### S1 私有只读客户端（先只读，绝不先写）

新场所实现 OKX 只读面等价物（`execution/okx_account.py`）：

```
configured / account_config / balances / positions / pending_orders / fills
/ instruments(合约元数据) / trade_fee
```

验收：能在**不开启任何交易权限**的 API key 下，读出该场该标的的持仓、挂单、成交、
合约元数据（乘数、步长、最小数量、结算币）与手续费档；数字与交易所网页端一致。

### S2 交易客户端（写的那一半）

实现 OKX 交易面等价物（`execution/okx_trade.py`）：

```
place_ioc(inst_id, side, size, price, ...)        # 平仓/兜底打法
place_post_only(inst_id, side, size, price, ...)  # 开仓 maker-first
get_order(inst_id, ord_id) / cancel_order(inst_id, ord_id)
ticker(instrument) / instrument(instrument)       # 供 plan 与价格保护用
```

硬性要求（来自 `docs/execution.md`「接入真实适配器前的硬性要求」与
`docs/execution-lessons-2026-09-15.md`）：

- 独立子账户；API key 只保留 Read/Trade，**禁止 Withdraw**，绑定固定出口 IP；
- 每单带 client order id，且**撤单必须读回确认**——"撤单未被确认"是**状态未知**，
  不是"未成交"（这是 09-15/09-16 两次事故的核心）；
- 乘数、步长、最小数量、最小名义**全部来自交易所元数据**，不许在代码里写常数；
- 任何写操作失败都要留下"可能已到达远端"的标记（`may_have_reached_remote`），
  不允许把未知当成失败后直接重试。

### S3 场所无关化改造（一次性，之后每接一家都省事）

1. `full_chain.py` / `maker_first.py` 的参数 `okx` → 通用的 `venue`/`trade`（或加一层
   `VenueTrade` Protocol，与 `execution/adapters.py` 的 `CEXAdapter` 对齐）。
2. `_venue_of()`：改成读合约元数据里的 `venue` 字段，**不再按名字后缀猜**；
   遇到没登记的合约直接拒绝（fail closed），不做兜底。
3. `live_stack.build_live_stack()`：按 `[allowlist] venues` 构造对应的客户端，
   多场所时一场所一个客户端；`allow_orders=live` 这个最后一道锁对每一家都要生效。
4. `auto_execute.py:466` 的健康检查改成按 `venues` 遍历。

验收：全套单元测试通过；**在没有任何新场所启用的情况下**，OKX 的行为逐位不变
（用 paper 跑一次前后对比）。

### S4 配置与映射（最容易漏、也最容易出大错的一步）

三处都要改，缺一处就被某个闸门挡住或放错方向：

1. `config/automation_policy.toml [allowlist]`：
   - `venues` 加新场名；
   - `instruments` 加**该场的 instrument id**（命名各家不同：okx `XIAOMI-USDT-SWAP`、
     bybit/bitget `MEITUANUSDT`、gate `TENCENT_USDT`、binance `TENCENTUSDT`、
     hyperliquid `xyz:SHEIN`）；
2. `config/hk_monitor.toml` 每个 token 的 `[tokens.mappings."<venue>:<instrument>"]`：
   `shares_per_token` / `shares_per_perp_unit` / `quote_to_usdt` + `verified=true` +
   `evidence` + `valid_until_ms`。**映射是"每标的 × 每场所"的**，不是每标的。
   复核命令：`PYTHONPATH=src .venv/bin/python -m arb_monitor mapping-verify --asset <ASSET>`
   与 `scripts/verify_hk_allowlist.py`。
3. 若新场最小名义大于我们的可部署名义（`min(单笔上限, 钱包×0.999)`，今天 ≈10.9 USDC），
   该场在纸面阶段就会全部被 `below_min_notional` 挡掉——**先算这一步，别写完适配器才发现**。

### S5 成本模型（否则门槛是假的）

`config/hk_monitor.toml [costs]` 现在按**本 OKX 账户实测**标定（perp taker 5 bps、
maker −1.6 bps 等）。换场要重新标定：该场 taker/maker 费率、资金费结算周期与费率、
滑点与失败缓冲、Gas（链上腿不变）。参考 `scripts/cost_calibration.py`。

验收：用该场真实成交（哪怕是 paper 的模拟成交）反算的往返成本，与配置里的值差 < 20%。

### S6 纸面（`auto_orders = "paper"`，operator 专属开关）

- paper 用真实报价与真实风控、不产生成交；
- 要覆盖的路径：正常开仓、挂单超时被撤、现货腿失败后的**永续腿回收**、
  平仓（maker 超时 → taker 兜底）、以及"撤单未确认"这类未知态；
- 全部通过后再谈 live。**顺序不能颠倒**（`docs/execution.md`「接入顺序」）。

### S7 小额实盘（两把钥匙 + `live_ack`）

只有 operator 亲自落 `live_ack`；单笔与日额度由 `[limits]` 管，**不为了跑通而放宽**。
第一笔按 `docs/execution-lessons-2026-09-15.md` 的"正确开仓与平仓流程"逐步走，
每个回执都要留档。

### S8 运维闭环

对账（`ledger-triage --check-account`、`pending-scan`）、
停机开关（`data/KILL_SWITCH` 双向、`data/AUTO_HALT` 只挡开仓）、
报警频道、以及每笔的账本记录（`data/execution.db`）。

---

## 3. 各候选场所的已知差异（2026-09-20 现场实测，接入前需逐条复核）

| 场所 | 监控状态 | 已知特点 / 接入风险 |
|---|---|---|
| okx | 正常 | **唯一已接**，可作为接口与验收的基准 |
| gate | 正常 | 合约命名 `TENCENT_USDT`；**同一标的还有 HKD 计价的兄弟合约**（`TENCENTHKD_USDT`），部分行带 `quanto_or_unknown_payout_requires_separate_hedge_model` + `unverified_or_expired_share_ratio_fx_mapping`，接入前必须确认用哪一条 |
| binance | 正常 | 命名 `TENCENTUSDT`；实测 **TENCENT 最优买价深度只有 0.12 张**，顶档深度远小于 1 股，必须走深度加权价 |
| bitget | 正常 | 命名 `MEITUANUSDT`；MEITUAN 最优买价 68.25 张（倍率 0.01，= 0.68 股），同样吃深度 |
| bybit | 正常 | 分页合约目录（已处理）；命名 `KUAISHOUUSDT` |
| aster | 正常 | 走 Binance 形状的公开接口（`fapiPublicGetV1PremiumIndex`），资金费读取刚修好；**私有 API 形态待核实** |
| hyperliquid | 正常 | 股票永续在 builder dex（`xyz:SHEIN`）里，不是主 universe；下单是 EIP-712 签名动作，**与 REST key 完全不同的接入路径** |
| lighter | 采样稀疏 | 常态 **~90% 行带陈旧标记**（今天的"可评估"大多来自休市时段），zk L2；接入性价比最低 |

## 4. 验收清单（照 `docs/execution-lessons-2026-09-15.md` 的防回归项，逐条勾）

- [ ] 未知状态（撤单未确认 / 网关超时 / 部分成交）不会被当成"未成交"或"已完成"；
- [ ] 现货腿失败时，永续腿一定被回收（有标记、有回执、有对账）；
- [ ] 单腿成功不算完成：按套利组核验双腿；
- [ ] 所有金额/价格/数量用 `Decimal`，无 `float`；
- [ ] 每个 Intent、订单、回执、成交、费用、Gas、余额变化都持久化；
- [ ] paper 与 live 走同一套风控与同一批阈值（只有"是否真的发单"不同）；
- [ ] 新增场所不影响既有 OKX 路径的行为。

## 5. 明确不做

- 不在 paper 跑通前开 live；
- 不因为"某场机会多"而放宽额度、`min_net_profit_quote`、`min_gross_bps` 或数据质量过滤；
- 不在没有独立子账户 / 固定出口 IP / 只读只交易 key 的场所下单；
- 不用"跨场所同向"之类的前置条件去过滤信号（见 `AGENTS.md`「全天候原则」）。
