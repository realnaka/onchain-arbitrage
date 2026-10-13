# X Layer 美股代币 × OKX 永续：扩展实现 + 映射证据（2026-09-22）

> 触发（operator，2026-09-22）："按照这个对话里面的计划plan，执行"
> —— 计划原文 `codex://threads/01a0c7a4-1521-7c82-a36c-554c6b35dc17`
> （《X Layer 美股代币 × OKX 永续扩展计划》）。
>
> 结论：**计划第 1、2 节的代码与配置已经落地并跑通测试（778 项全过），
> 第 3 节的 US 映射证据链本轮补齐了链上读数、暴露出 blocker，第 4–7 节只是
> 框架就位、还没到能上资产的程度**。US 现在是"只读扫描"，**没有任何一个 US
> 资产进入价格监控、候选、提醒或执行白名单**；operator 专属开关（`mode` /
> `auto_orders` / `live_ack` / `[limits]` / `KILL_SWITCH` / `AUTO_HALT`）
> 一个都没动。

---

## 0. 一句话

策略本身早就是通用的（"链上代币多 + USD 线性永续空"），真正要补的是**证据**：
US 资产的发行方 ISIN、wrapper 储备、`multiplier()` 和 OKX 合约规格必须逐条
核过。

> **2026-09-22 晚更正（本节早先的结论已被推翻，保留原文痕迹以免下一轮重犯）**：
> 早先写"5/7 满足不了 `totalSupply == balanceOf`、US 映射过不了 `mapping-verify`、
> 是 blocker"。**这个判断是错的，两处都是自己造成的**：
>
> 1. **配置指错了代币**：US 那 7 个块原先指向**原生代币**（`AAPLx`
>    `0x9d275685…`），而原生代币在 X Layer 上的池子**是空的**（实测深度：
>    AAPL 0、MSFT 0、META 0、AMZN 1e-6 USD、GOOGL 1e-6 USD、NVDA 2e-6 USD、
>    TSLA 0.03/0.40 USD）——深度在 **wrapper** 池里，和港股一样。早先那个
>    "ratio 0.87" 的报警就是从未初始化的 `slot0` 里读出来的。
> 2. **不变量写错了**：`totalSupply == balanceOf` 只在 `multiplier == 1` 时成立，
>    所以 TSLA/AMZN "通过"、其余 5 个"失败"，纯属巧合。真正要成立的是
>    `totalAssets() == native.balanceOf(wrapper)`（见 §3.3）。
>
> 修完之后 **7/7 全部通过 `mapping-verify`**（6/6 条检查），`verified` 仍留空
> 等 operator 签。US 依旧是"只读扫描"：没有任何一个 US 资产进入价格监控、
> 候选、提醒或执行白名单。

**`multiplier()` 的正确含义（operator 2026-09-22 指出，实测证实）**：它就是
股票代币化时处理分红/拆股的 corporate-action 因子，**算股数时要乘上去**。
wrapped xStock 是 ERC-4626 金库，金库的兑换率 `convertToAssets(1e18)`
**就是**发行方的 `multiplier()`（7/7 US + 6/6 HK 实测相等），也就是说
**1 个 wrapper = `multiplier()` 股**，原生代币 = 1 股。

---

## 1. 计划 → 实现对照

| 计划章节 | 落地情况 | 落点 |
|---|---|---|
| §1 提炼通用策略 | ✅ 完成 | `strategy/token_perp_basis.py`（通用）；`strategy/hk_basis.py` 保留旧类名/默认值做兼容，HK 行为与测试不回归 |
| §2 全量 US 自动发现，分层 | ✅ 完成（扫描开、采纳关） | `token_scan_countries=["HK","US"]`、`token_adopt_markets=["HK"]`；`hk_monitor.py` 三层发现 + `report_only` |
| §3 US 映射证据链 | ✅ 7/7 通过 6/6 项检查（`verified` 仍待 operator 签） | `core/mapping_evidence.py` + `arb_monitor mapping-verify`；`wrapper_fully_backed` / `vault_conversion_rate` / `share_conversion` 已按 §3.3 修正 |
| §4 链上可执行性检查 | ✅ 完成 | `core/liquidity_state.py` 五状态；池子需真实 exact-in 报价，`slot0`/历史成交不作执行价；**另加一个"本轮未采样"状态**（见下） |
| §5 OKX 合约与数量模型 | ✅ 完成 | `core/sizing.py`（Decimal、`shares_per_token`/`shares_per_perp_unit`/`contract_multiplier`、残差、最小名义、余额/保证金） |
| §6 成本、资金费与退出 | ✅ 框架完成，参数沿用现值 | `core/cost_table.py`（per asset/venue 覆盖，缺省回落到已审核全局值，**不改任何阈值**）；退出仍程序化 |
| §7 执行白名单与灰度 | ✅ 只读约束落地 | 自动发现**从不写**执行白名单；灰度顺序 1–6 见 §6.2 |
| 文档与运维 | ✅ 本轮 | 本文件 + `HANDOFF.md` / `README.md` / `docs/plan.md` / `docs/architecture.md`；看板新增"代币（symbol · underlying · 地址）"列 |

验证命令（本轮实跑）：

```bash
cd /home/operator/Documents/arb
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -t tests
# → Ran 778 tests ... OK (skipped=2)
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_us_token_extension
# → 66 tests OK
```

**实现中发现并修掉的一个真问题（2026-09-22）**：链上池读取是**异步**的，
实测每 ~3 分钟才落一次（`onchain_pool_state` 事件：14:24 / 14:27 / 14:32 / 14:36 …），
而"一次读取算数"的窗口 `pool_state_max_age_ms = 120000`（2 分钟）。
于是**连续好几轮没有"新鲜"读数**，新加的 `pool_state` 列会把"还没重新采样"
判成 **`no_pool`（无池）** —— 这正是 AGENTS.md 明令禁止的"没采样 ≠ 没数据"。
已修：`no_pool` 只用于"这个代币根本没配池子"；配置了池子但本轮没有新鲜读数时，
报**新增的 `pool_read_stale`（本轮未采样）**，并把上次读数距今多少 ms 写进 `detail`。
`self.onchain_pools` 只被拆成"全部"与"仍在新鲜窗口内"两份，**筛选逻辑与价格口径一字未改**，
`unsampled=None` 时 `pool_state()` 行为与之前完全一致（旧测试原样通过）。
上线后实测：15:26 这一轮的 8 个港股资产**全部**报 `pool_read_stale`
（`detail` 里是"last read 169821–179189ms ago"），不再是假的 `no_pool`。

**实现中发现并修掉的第二个真问题（2026-09-22）**：发现扫描跑在后台线程，
而 `_token_scan()` 同时承担两件事——"要不要开新一轮"和"收上一轮的结果"——
两者原来共用 30 分钟那道闸门。结果是**扫描跑完了也不会被读走**，要等下一次
`due` 才收件：看板上的 `layers` / `report_only` 会在活干完之后继续 `null`
一整个周期。实测复现：15:15:05 的轮次把扫描提交出去，worker 在 15:22 前后
跑完（`last_scan_ms` 随之推进），但 15:23 / 15:24 / 15:25 的 `run_summary`
仍然是 `layers: null`。已修：改为**每轮先收件、到点才开新扫**
（`_token_scan(run, now, due=...)`），**扫描频率完全不变**，结果在一个周期后
即可见。`tests/test_us_token_extension.py::TokenScanHarvestTests` 5 项钉住这个
不变量（未到期但有完成件必须收；未到期且无在飞不得开新扫；在飞不得重开也不得
伪造；失败件报错且不立刻重试）。

---

## 2. 现场已核实的事实（只读）

### 2.1 发行方（api.backed.fi）7 个美股 xStock

来源：`https://api.backed.fi/api/v2/public/assets/<symbol>`，2026-09-22 复采。

| 代币 | token ISIN | underlying ISIN | listingCountry | tradingHoursMode | isTradingHalted | native（X Layer，= 配置） | wrapperAddressV2 |
|---|---|---|---|---|---|---|---|
| AAPLx | CH1436219187 | US0378331005 | US | TwentyFourFive | false | `0x9d275685…75ec890a` | `0x943bf64d…11cc9de8f` |
| NVDAx | CH1436219195 | US67066G1040 | US | TwentyFourFive | false | `0xc845b289…25fe0849d` | `0xa8ddb5cd…642850d5` |
| TSLAx | CH1436219252 | US88160R1014 | US | TwentyFourFive | false | `0x8ad3c73f…69aeb7cf0` | `0xc3fdbe3a…aefe1171` |
| MSFTx | CH1436219203 | US5949181045 | US | TwentyFourFive | false | `0x5621737f…19aa6b35` | `0x166fbe68…39f1fa1d0` |
| AMZNx | CH1436219211 | US0231351067 | US | TwentyFourFive | false | `0x3557ba34…87195081` | `0x910cabde…60fa0f90` |
| METAx | CH1436219229 | US30303M1027 | US | TwentyFourFive | false | `0x96702be5…e4ec989a` | `0xe840946f…a0d0e56` |
| GOOGLx | CH1436219237 | US02079K3059 | US | TwentyFourFive | false | `0xe92f673c…07e803f` | `0xf8c5308f…2caa6f` |

要点：

- `listingCountry = US` 是**唯一**的上市地判据（不用 ISIN 前缀、不用名称猜）；
  7/7 native 地址与 `config/hk_monitor.toml` 里 `[tokens]` 的 `address` **逐字一致**。
- `tradingHoursMode = TwentyFourFive`、`isTradingHalted = false`：发行方自己也按
  24×5 报，采数当时 `currentPeriod=overnight / openNow=true`。这与本仓库
  "不拿正股交易日历当闸门" 的立场一致 —— 而且**策略两腿（X Layer 代币 + CEX
  永续）本来就 7×24**，正股开不开市不是任何一腿的属性。

复采命令（走本机代理；代理偶发超时，重试即可）：

```bash
curl -sS --proxy http://127.0.0.1:4780 \
  https://api.backed.fi/api/v2/public/assets/AAPLx
```

### 2.2 X Layer 上的美股池（**已更正：改指 wrapper 池**）

早先这里写"7 个美股代币共 9 个池（原生代币）"。**那一版是错的**：原生代币的池
要么不存在、要么深度为 0，配置指过去等于让美股腿永远无价。

按 `wrapperAddressV2` 重新用工厂 `getPool` 扫过（4 个稳定币 × 4 个费率档），
实测 2026-09-22：**只有 fee = 500 这一档有深度，其余三档（100/3000/10000）
深度为 0；若干 USDC 池是 `pool_not_initialized`。** 每个名字只有一个有深度的池：

| 代币 | wrapper 池 | 计价币 | 费率 | 池深（USD） | 池价 | OKX 永续中间价 | 池价/永续 | `multiplier()` |
|---|---|---|---|---|---|---|---|---|
| AAPL | `0xc44bd9c8589026d28d1632d7b86b2efb6cdc8fd2` | USDG | 500 | 411,631 | 340.9466 | 339.955 | 1.0034 | 1.003269 |
| NVDA | `0x2a2b11730c2b6d99a58034a869dd810d7300a7b2` | USDG | 500 | 645,436 | 226.8767 | 226.555 | 1.0009 | 1.001701 |
| TSLA | `0x6a58944eed3d2074e137eb4e94b302fe4af247a6` | USDC | 500 | 400,853 | 375.3772 | 376.095 | 0.9982 | 1 |
| MSFT | `0x66187278490a70a8ac26a6e159eb045f82dbfb57` | USDG | 500 | 306,316 | 506.6006 | 504.195 | 1.0053 | 1.005903 |
| AMZN | `0x8c1c0d559d1c7ae6ed921cc77abd0f26ac2fe59a` | USDG | 500 | 66,356 | 259.3603 | 260.005 | 0.9979 | 1 |
| META | `0xfad9e3c7550768fd4f34bc9cefd365cc193c0fb0` | USDG | 500 | 227,684 | 745.6683 | 744.025 | 1.0018 | 1.002852 |
| GOOGL | `0x9f6273e2669cd812e76788b698374c43637c87c2` | USDC | 500 | 616,093 | 358.0156 | 357.685 | 1.0007 | 1.002377 |

`config/hk_monitor.toml` 的 7 个 US 块已按此改写：`symbol = "wAAPLx"`、
`address` = wrapper、`pools` = 上表那个 fee-500 池。**每个名字只配一个池**是有
原因的：`pool_state()` 会拒绝"池的计价币与配置的 `quote_conversion` 不一致"的行
（`quote_conversion_wrong_token`），所以按池的计价币配 `quote_conversion`。

**这两个池子里已经取到真实 exact-in 报价**（只读，`onchainos swap quote`，
100% Uniswap V3 路由）。样本：1 USDG → wAAPLx
`toTokenAmount 0.002931584238492713`、`priceImpactPercent -0.04`、
`tradeFee 0.000331020016551`、`estimateGasFee 162000`、
`tokenUnitPrice 340.9909090999126`。

> **额度限制（待 operator 决定）**：`onchainos` 拒绝报超过钱包余额的数量
> （100 USDG → `funding_required`，余额 10.92387 USDG）。所以**每笔探测目前
> 只能做到 ≤10.9 USDG**，更大的成本样本需要先给钱包充值。

**这些池子目前不在采样循环里**（这是"扫描 ≠ 采纳"的直接证据）：

```bash
sqlite3 data/hk_monitor.db \
  "select asset, count(*), datetime(max(recv_ts_ms)/1000,'unixepoch','+8 hours') latest \
   from monitor_events where kind='onchain_pool_state' group by asset \
   order by max(recv_ts_ms) desc;"
```

HK 名字最新一行是 `2026-09-22 14:23–14:24`（现役监控）；
**7 个美股名字最新一行还停在 `2026-09-15 12:15`**，即当初写配置时的一次性采样，
之后没再写过 —— 它们确实没被价格监控采到。

### 2.3 OKX 合约规格（7 个都是 USD 线性永续）

来源：监控自己的 venue registry（`monitor_events.kind='registry'` 最新一行，
id=1220085，`2026-09-22 14:17:44`，source = `native specifications via CCXT`，
原始出处 `https://www.okx.com/api/v5/public/instruments?instType=SWAP`）。

| 代币 | instId | ctType | ctVal | ctValCcy | ctMult | lotSz | minSz | tickSz | settleCcy | state |
|---|---|---|---|---|---|---|---|---|---|---|
| AAPL | AAPL-USDT-SWAP | linear | 1 | AAPL | 1 | 0.01 | 0.01 | 0.01 | USDT | live |
| NVDA | NVDA-USDT-SWAP | linear | 1 | NVDA | 1 | 0.01 | 0.01 | 0.01 | USDT | live |
| TSLA | TSLA-USDT-SWAP | linear | 1 | TSLA | 1 | 0.01 | 0.01 | 0.01 | USDT | live |
| MSFT | MSFT-USDT-SWAP | linear | 1 | MSFT | 1 | 0.01 | 0.01 | 0.01 | USDT | live |
| AMZN | AMZN-USDT-SWAP | linear | 1 | AMZN | 1 | 0.01 | 0.01 | 0.01 | USDT | live |
| META | META-USDT-SWAP | linear | 1 | META | 1 | 0.01 | 0.01 | 0.01 | USDT | live |
| GOOGL | GOOGL-USDT-SWAP | linear | 1 | GOOGL | 1 | 0.01 | 0.01 | 0.01 | USDT | live |

要点与坑：

- `ctValCcy` 是**股票代码**（不是币），`ctVal=1 ctMult=1` → **1 张 = 1 股**的
  名义口径。但这条只有在 §3.3 的 `multiplier()` 也等于 1 时，才等价于
  "1 张 = 1 个链上代币"。
- OKX 的 registry 行 `min_notional = 0`，**已查明这是本地解析器写的"未知"
  （`data/hk_cex.py:154-155`），不是 OKX 给的 0**：OKX 的 instruments 接口
  **没有任何最小名义字段**（实测字段清单见 §7 第 3 条；对比 aster/binance/
  bybit/bitget 的行都是 5）。**不能拿 0 当"无最小名义"** —— 最小规模按
  `minSz`（= `lotSz`）兜底，下单前再用 §5 的 `min_notional` 逻辑按盘口价换算。
- `listTime`（ms）换算：TSLA ≈ 2026-02-25，NA 组 7 个里其余 ≈ 2026-03-04，
  即这批合约是 2026 年一季度上的，运行时间还不到半年。

**扩展（2026-09-22）**：同一张 OKX 表的 `instCategory="3"` 一共 **181 个**股票
永续，`ctVal=1 / ctMult=1 / lotSz=minSz` 是 181/181 成立的（分布
`0.01`×95、`0.1`×63、`1`×13、`0.001`×8、`0.0001`×2），与上面 7 个的规格同源。
名单与交集见 `docs/research/us-okx-overlap-2026-09-22.md`。

复现命令：

```bash
sqlite3 data/hk_monitor.db \
  "select payload_json from monitor_events where kind='registry' order by id desc limit 1;" \
| .venv/bin/python -c "import json,sys; rows=json.load(sys.stdin); \
[print(r['asset'], r['instrument_id'], 'mult',r['multiplier'],'lot',r['lot_size'], \
'min',r['min_contracts'],'notional',r['min_notional'],'tick',r['tick_size'], \
r['payout_model'],r['active']) for r in rows \
if r['asset'] in ('AAPL','NVDA','TSLA','MSFT','AMZN','META','GOOGL') and r['venue']=='okx']"
```

### 2.4 其他场所（只读，不据此接线）

同一行 registry 里，7 个美股在 **aster / binance / bybit / bitget / gate** 上也
有 USD 线性永续候选（`category=us_stock_candidate`），hyperliquid / lighter 亦有
部分条目。按项目边界，**监控范围可以比可执行范围宽**：这些行照常采集，但
"名次靠前"不构成接线授权；本计划只对 OKX 做映射证据链。

---

## 3. US 映射证据链

### 3.1 校验器怎么工作

`arb_monitor mapping-verify` **只遍历已配置的 `[tokens.*.mappings]`**：

```python
# apps/mapping_cli.py
mappings = token.get("mappings") or {}
if not mappings:
    continue          # ← 没有 mappings 块 = 直接跳过，不是"通过"
```

现在 7 个美股在配置里**没有 `mappings` 块**（它们只有 `mapping_verified = false`
和池子定义），所以直接跑 CLI 只会打印 `no configured mappings to verify`。

因此 §3 的正确顺序是：

1. 按 §3.3 的实测值，为每个美股写一条**候选** `mappings` 规格
   （`<venue>:<instId>` → token/underlying ISIN、native/wrapper、`shares_per_token`、
   `perp_value_currency`、`perp_contract_multiplier`…）；
2. 跑 `mapping-verify`，让它在发行方 + 链上 + OKX 三处**重新读一遍**并对齐；
3. **operator 本人**签 `verified` 与 `valid_until_ms`（这是人工动作，程序不自动
   把 `mapping_verified` 置真）。

### 3.2 五条机器检查

`core/mapping_evidence.py::verify_token_mapping` 的检查项：

| # | 检查名 | 读什么 | 本文件数据来源 |
|---|---|---|---|
| 1 | `issuer_registry` | 发行方 token ISIN → underlying ISIN + symbol | §2.1 |
| 2 | `wrapper_1to1_reserve` | `wrapper.totalSupply() == native.balanceOf(wrapper)` | §3.3（本轮实测） |
| 3 | `wrapper_declared_asset` | `wrapper.asset() == native` | §3.3（本轮实测） |
| 4 | `token_multiplier` | `native.multiplier()` == 配置 `shares_per_token` | §3.3（本轮实测） |
| 5 | `perp_contract_value` | OKX `ctVal / ctValCcy / ctMult` | §2.3 |

### 3.3 本轮实测的链上读数（**2026-09-22 晚已更正**）

只读 `eth_call`，**固定单区块**读（`totalSupply` / `totalAssets` /
`balanceOf(wrapper)` / `convertToAssets(1e18)` / `multiplier()` 都读同一块，
避免异步批量把两次读分到不同区块），2026-09-22：

| 代币 | `wrapper.asset()` | `totalAssets()` vs `native.balanceOf(wrapper)` | 差值（wei） | `convertToAssets(1e18)` | `native.multiplier()` | 1 wrapper = 几股 |
|---|---|---|---|---|---|---|
| AAPLx | ✅ 指向 native | 855799548504787474092 vs 855799548504787529057 | 54,965（6.4e-17 相对） | 1.0032690125398187 | 1.0032690125398187 | **1.0032690125398187** |
| NVDAx | ✅ | 3238491195945856895541 vs 3238491195945857117953 | 222,412 | 1.001701196801074 | 1.001701196801074 | **1.001701196801074** |
| TSLAx | ✅ | 716057965080365826335 vs 716057965080365826335 | 0 | 1 | 1 | 1 |
| MSFTx | ✅ | 276714615003662677254 vs 276714615003662683524 | 6,270 | 1.0059033904787456 | 1.0059033904787456 | **1.0059033904787456** |
| AMZNx | ✅ | 181369248373122054545 vs 181369248373122054545 | 0 | 1 | 1 | 1 |
| METAx | ✅ | 169836704031305676125 vs 169836704031305684088 | 7,963 | 1.0028515433272898 | 1.0028515433272898 | **1.0028515433272898** |
| GOOGLx | ✅ | 790212053440467025811 vs 790212053440467063007 | 37,196 | 1.0023772500603487 | 1.0023772500603487 | **1.0023772500603487** |

四个直接结论：

1. **`wrapper.asset()` 7/7 指向 native** → 检查通过。
2. **`totalAssets() == native.balanceOf(wrapper)` 7/7 成立**（差值都在
   1e-13 股以下，即金库自身 share math 与 ERC-20 余额两条代码路径的整数舍入；
   量级 6e-17 相对）。早先用的 `totalSupply == balanceOf` **是错的不变量** ——
   它只在 `multiplier == 1` 时成立，这正是 TSLA/AMZN "通过"、其余 5 个"失败"
   的全部原因。已把检查改名为 `wrapper_fully_backed` 并改用正确等式，
   容差 `1e-9 股`（`mapping_evidence._BACKING_WEI_TOL`），并且**空储备仍然算失败**。
3. **`convertToAssets(1e18) == native.multiplier()` 7/7 成立** → 这就是
   operator 说的 corporate-action 因子：**1 个 wrapper = `multiplier()` 股**。
   检查名 `vault_conversion_rate`，并据此推导 `shares_per_token`（`share_conversion`）。
4. **早先说的"MSFTx 是离群点"是假的**：那是把 `totalSupply` 与 `balanceOf`
   读在不同区块造成的读数错位。固定单区块重读后 7/7 精确成立。

价格侧的独立交叉核对（同一分钟，2026-09-22）：OKX **现货 XAAPL-USDT 339.72
对 AAPL-USDT-SWAP 中间价 339.855 = 1.00 股**，而同一时刻 wAAPLx/USDG 池价
340.9466 = 1.0032 × 永续 —— **原生代币 = 1 股、wrapper = `multiplier()` 股**，
两套读数互相印证。

### 3.4 对冲股数怎么算（operator 的问题）

```text
equity_shares = wToken 数量 × convertToAssets 汇率（= multiplier()）
合约张数      = equity_shares ÷ (ctVal × ctMult) 向 lotSz 取整
              = equity_shares ÷ 1 股        （OKX 这 7 个名字 ctVal=ctMult=1）
```

例子：100 wAAPLx → 100.3269 股 → 100.32 张（`lotSz = 0.01`）；
100 个**原生** AAPLx → 也是 100.3269 股（原生 = 1 股）。
残差 0.0069 股 ≈ 0.07 bps。落点：`core/sizing.py::plan_size`
（`shares_per_token` / `shares_per_perp_unit` / `contract_multiplier` /
残差 / 最小可执行量 / 钱包与保证金），本来就是通用的，**没有为 US 改公式**。

---

## 4. 数量模型（sizing）

计划第 5 节的公式已落到 `core/sizing.py::plan_size`（全 `Decimal`，阈值外传）：

```text
链上实际 token 数量
× shares_per_token          (multiplier()：1 代币 = 几股)
÷ shares_per_perp_unit      (该场所合约按什么单位报价)
÷ contract_multiplier       (每股几份合约)
→ 向下取整到 lot_size
```

同时算并回报：合约取整残差（token 与 bps）、最终未对冲数量、
最小可执行张数（`min_contracts` 与最小名义两者取高）、钱包与保证金是否吃得下；
**负向残差（空头大于现货）直接拒绝**，不是四舍五入。

**本轮实测暴露的核心陷阱**：OKX 的 `ctValCcy=AAPL、ctVal=1` 是"1 张 = 1 股"，
而链上 1 个 AAPLx = 1.003269 股。**若照旧按"1 代币 = 1 张"下单**：

- AAPLx 会**少空约 0.327%** 的名义（≈ 32.7 bps 的裸多方向敞口）；
- MSFTx 会**少空约 0.587%** 的名义（≈ 58.7 bps）。

这不是"裸腿"，但对冲是错的 —— 正是计划第 3 节
"不能因为合约名为 `AAPL-USDT-SWAP` 就默认 1 token = 1 contract" 要防的事。
`shares_per_token` 必须以 §3.3 的 `multiplier()` 实测值写进 mapping spec。

另一个坑：策略内部算 **`perp_price = perp_open_usdt / contracts`**（单位名义/张），
不是盘口原始价 —— 因为盘口价是"每张"，乘以 `multiplier` 才是名义。二者混用会把
基差整体放大/缩小 `multiplier` 倍。`token_perp_basis.py` 已统一走前者。

---

## 5. 成本、计价币换算与退出

### 5.1 成本

`core/cost_table.py`：每个 `[tokens.mappings."<venue>:<inst>"]` 可带 `cost`
子表覆盖全局 `RoundTripCosts`，缺省回落到**已审核的全局值**；**未知字段直接报错**
（防止拼写错误静默套用全局默认）。覆盖项覆盖计划第 6 节要的：链上池费、
exact-in 滑点、OKX maker/taker、gas、funding、失败缓冲、maker 等待期价格风险、
平仓盘口深度。**本模块只描述"一次往返要花多少"，不碰任何利润阈值** ——
US 的资产/venue 级阈值要不要设，仍归 operator。

### 5.2 计价币换算（美股特有）

美股池只用 USDG(`0x4ae4…`) / USDT0(`0x779d…`)，不是探针自己的报价币。
`core/hk.py::QuoteConversion` 要求把换算做成**带来源与失效时间的观测**：
记录 quote token 地址、稳定币→USDT 的换算来源与时间、检查脱锚；
**换算缺失/过期或脱锚 → 按 `quote_unusable` 拒绝执行**，而不是默认按 1:1 算。
`hk_monitor.pool_state()` 补了一条 fail-closed：非港股行的
observed/配置 `quote_address` 任一缺失即判 `quote_conversion_wrong_token`。

### 5.3 退出（程序执行，含 US）

退出策略与 HK 共用一条链，不按正股开市时间挂靠：

- 基差回到配置目标（净基差 ≤ 3 bps）→ 程序平两腿；
- 资金费止损独立生效（累计支付达阈值 → 结算前 1 分钟无条件平仓）；
- 按**套利组**核验两腿归零，不把单腿成功当完成；
- 订单/余额/成交/链上状态任一 unknown → 写 `AUTO_HALT`，禁止继续开仓。

---

## 6. 灰度闸门

### 6.1 进入执行白名单的前置（计划第 7 节）

每个 US 资产上 live allowlist 前必须**同时**具备：已审核 token/underlying/合约
映射 · 当前有效链上报价 · 当前有效 OKX 盘口 · 已验证成本模型 · 钱包与保证金满足 ·
纸面交易与对账记录 · operator 明确准入。

### 6.2 rollout 顺序与实际进度

| 步 | 内容 | 现状 |
|---|---|---|
| 1 | 全量 US 自动**发现** | ✅ 开启（`token_scan_countries=["HK","US"]`，探测前先过 OKX 股票永续闸门） |
| 2 | 监控全部**有永续腿**的候选 | 🟡 已开（2026-09-22 operator："现在开始对这114个公司也进行监控"）：`token_adopt_markets=["HK","US"]`，但**只监控与展示**——`mapping_verified=false`、不在白名单、`execution_blockers()` 写"份额映射未核验" |
| 3 | 纸面执行并统计 | ⛔ 未开始 |
| 4 | 单个最成熟资产进入极小灰度 | ⛔ 未开始 |
| 5 | 双腿对账通过后逐个增加 | ⛔ 未开始 |
| 6 | 不批量把自动发现资产塞进 live allowlist | ✅ 结构性保证（见下） |

### 6.3 "发现 ≠ 授权"在代码里的硬约束

- `_discover_tokens` **从不写** `execution_allowlist`；
- 扫描到但**未采纳**的市场，整行进 `run_summary.token_universe_scan.report_only`
  （带 `depth_usd` / `quote_symbol` / `cex_match` / `has_perp` / `reasons`），
  **不写** `config/discovered_tokens.toml`、不进价格监控、不发提醒。
  US 自 2026-09-22 起已换成"采纳"，所以走的是另一条路：写进
  `config/discovered_tokens.toml` 并进入价格监控，**但仍不写**
  `execution_allowlist`，且 `mapping_verified=false`——"进了监控"与
  "可下单"是两件事，横幅与看板的「监控 ≠ 可执行」就是这句话；
- `run_summary` 新增 `layers`（`issuer_universe` / `pairable_candidates` /
  `executable_allowlist`，按市场计数）与 `probes_pending_after_cap`；
- `config/automation_policy.toml` 的执行白名单仍是 **6 个港股资产 / venues=["okx"]**，
  本轮**一个字节都没改**。

---

## 7. 待确认（不编造，逐条挂账）

1. ~~US wrapper 的 1:1 不变量到底是什么？~~ **已解决（2026-09-22 晚）**：
   不变量是 `totalAssets() == native.balanceOf(wrapper)`（7/7 成立，容差内），
   而 `totalSupply == balanceOf` 从一开始就是错的问题。见 §3.3。
2. ~~`multiplier()` 5/7 ≠ 1 的机制~~ **已解决**：就是 operator 说的
   corporate-action 因子（分红/拆股），且 **wrapper 金库的兑换率直接等于它**
   （`convertToAssets(1e18) == multiplier()`，7/7 US + 6/6 HK）。算股数时
   乘上去即可：`1 wrapper = multiplier() 股`，`原生代币 = 1 股`。
3. ~~OKX 最小下单名义~~ **已按文档确认（2026-09-22，详细证据见
   `us-okx-overlap-2026-09-22.md` §5.1）**：`GET /api/v5/public/instruments
   ?instType=SWAP&instId=AAPL-USDT-SWAP` **根本没有最小名义字段**——返回的字段
   只有 `ctVal/ctValCcy/ctMult/lotSz/minSz/tickSz/settleCcy/state/…/maxLmtAmt/
   maxMktAmt/maxLmtSz/maxMktSz/maxIcebergSz/posLmtAmt/posLmtPct/lever`。
   我们 registry 里的 `min_notional = 0` 是**本地解析器写的"未知"**
   （`data/hk_cex.py:154-155`，与 Hyperliquid 同一约定），**不等于"无最小名义"**。
   官方文档（`docs-v5/en`）对 SWAP 只定义两条：`sz ≥ minSz`、`sz` 必须是
   `lotSz` 的整数倍（违规报 `51020` / `51121`），**没有单独的美元最小名义**；
   181/181 股票永续都是 `lotSz == minSz`，AAPL 为 `0.01 张 ≈ 3.40 USDT`。
   **仍待确认**：没有用真实下单跑过一次"刚好 `minSz`"的对照（见该文 §5.1
   第（3）点：`order-precheck` 对本账户返回 `51010`，是账户模式问题，不能当
   规模证据）。**另外发现**：`config/okx-demo.env` 里的 key 其实是**实盘** key
   （不带 `x-simulated-trading` 时 `code=0`，带上则 401 `50101`）——本轮没有
   下过任何单，改名与否由 operator 定。
4. ~~US 成本分布无实测样本~~ **已有首批真实样本**：7 个 wrapper 池的池深、
   `fee=500`、池价、以及一条真实 exact-in 报价（§2.2）。**待确认**：只能做到
   ≤10.9 USDG/笔（钱包余额 10.92387 USDG，`onchainos` 拒绝报超过余额的数量），
   更大的成本样本要先充值；USDG/USDC → USDT 的脱锚核验也还没做，所以
   `quote_to_usdt` 故意留空、美股行不具可执行性。**US 阈值/额度是否另设归 operator。**
5. ~~US 池是否真有 exact-in 报价~~ **已实测**：7 个 wrapper 池 100% Uniswap V3
   路由、都能返回 exact-in 报价；**另外 3 个费率档（100/3000/10000）深度为 0，
   若干 USDC 池 `pool_not_initialized`**，所以配置每个名字只留那个 fee-500 池。
6. **监控重启后的表现（部分已核，剩下一项）**：需在重启后核对最新
   `run_summary` 的 `token_universe_scan.scope_countries=["HK","US"]`、
   `adopt_countries=["HK","US"]`、`layers`、`report_only`、
   `probes_pending_after_cap`，**新增 `perp_gate`（OKX 股票永续数 / 被闸掉数 /
   实际探测数）**，并确认 HK 采样节奏（`cycle_gap_ms`）没被拖坏。已核：
   `perp_gate = {okx_stock_perps: 181, gated_out: {US: 640}, probed: {HK: 79,
   US: 114}}`、`cycle_gap_ms ≈ 60004`；`layers` 现在由扫描写进
   `token_universe_scan` 事件并在重启后读回（见
   `us-okx-overlap-2026-09-22.md` §9）。
7. **US 名单就绪但未接线**：两张名单的交集（114 + SHEIN）见
   `docs/research/us-okx-overlap-2026-09-22.md`。**2026-09-22 已更正并接线**：
   114 = 99 个单只股票 + 15 个 ETF/指数；原先算作"5 个未上市"的
   `SPCX/SHAZ/BOT/BSP/STRC` 已逐个核实为 Nasdaq 上市标的（Nasdaq + SEC 双证据）。
   目前 US 已进入 `token_adopt_markets`（监控 + 看板展示），但
   `mapping_verified = false`、不在执行白名单，`execution_blockers()` 写
   "份额映射未核验"——**监控 ≠ 可执行**。剩下的口径问题是 2x/3x 杠杆 ETF 的
   持仓期衰减，属于纸面阶段评估项。

---

## 参考

- 计划原文：`codex://threads/01a0c7a4-1521-7c82-a36c-554c6b35dc17`
- 港股映射核验方法（同一套四层核验）：`docs/research/mapping-hk-venues-2026-09-17.md`
- 执行/参数对照（涉及下单、成本、阈值）：`docs/knowledge/execution-playbook.md`
- 执行与对账教训：`docs/execution-lessons-2026-09-15.md`
- 代码落点：`strategy/token_perp_basis.py`、`core/markets.py`、`core/sizing.py`、
  `core/liquidity_state.py`、`core/cost_table.py`、`core/mapping_evidence.py`、
  `apps/hk_monitor.py`、`apps/mapping_cli.py`
