# 美股代币 × OKX 股票永续：两张名单与交集（2026-09-22）

> 触发（operator，2026-09-22）："你先把那个名单列出来：1. 有现货代币的名单
> 2. OKX 这边有合约的名单；把这两个名单有重叠的部分筛选出来，再去扫描价格。
> 这样子应该就没有 700 多个合约那么多。"
>
> 结论：**754 个美股代币 ÷ OKX 181 个股票永续 = 交集 114 个**（另加 SHEIN
> 的按名匹配 → 115）。探测闸门已按这张交集接进发现循环，一遍全宇宙从 833 个
> listing（≈19 小时）降到 193 个（≈5 小时）。
>
> **更新（同日，operator 后续指示："现在开始对这114个公司也进行监控"，
> "监控结果也要展示在看板上"）**：US 已进入 `token_adopt_markets`，看板新增
> 按上市地的「市场覆盖」表。**交集只是"可对冲"的必要条件，不是可执行授权**：
> 被采纳的美股 `mapping_verified = false`、不在执行白名单，`execution_blockers()`
> 写着"份额映射未核验"，所以是**监控 + 展示 + 筛选**，不是可执行。

---

## 0. 一句话

不是"700 多个合约扫不完"，而是**其中 640 个根本没有对手腿**：发行方列了 754 个
美股代币，OKX 只挂了 181 个股票永续。两张名单按"发行方 `underlyingSymbol` ==
OKX `ctValCcy`"取交集是 114 个，剩下的扫出来只能是不可对冲的行。闸门就建在这条
交集上，OKX 名单读不到时**不放行**（未知 ≠ 可以全扫）。

---

## 1. 两张原始名单

| 名单 | 来源（只读） | 计数 | 原文落盘 |
|---|---|---|---|
| 发行方现货代币 | `https://api.backed.fi/api/v2/public/assets` | 928 个资产，其中 **US 754 / GB 93 / HK 79 / ES 1 / DE 1** | `/tmp/issuer_assets.json` |
| OKX 股票永续 | `https://www.okx.com/api/v5/public/instruments?instType=SWAP` | 486 条 SWAP，其中 **`instCategory="3"` 181 个（全部 `state="live"`）**、`"4"` 8 个商品、`"1"` 297 个加密 | `/tmp/okx_swap.json` |

两个字段口径要写清楚，否则"交集"是巧合而不是证据：

* 发行方的 `underlyingSymbol` 是**股票自己的代码**（`AAPL`、`NVDA`、`700`），
  不是代币符号（`AAPLx`）、也不是 wrapper 符号（`wAAPLx`）；
* OKX 的 `ctValCcy` 是**一张合约对应的币种**（一笔一币的命名基准），
  在 181 个股票永续上 181/181 等于 `instId` 去掉 `-USDT-SWAP` 的结果；
* 用 `instCategory` 而不是"名字像股票"来判股票永续是必须的：`QNT`、`RAM`、
  `PURR`、`FLY` 这些**加密合约的代码与美股同名**，按代码比会把它当成股票。

---

## 2. 交集：114 个（+1 按名匹配）

`{发行方 US 代币的 underlyingSymbol} ∩ {OKX instCategory=3 的 ctValCcy}`
= **114**。加上 SHEIN（发行方 `hk_code` 是 `625`，OKX 的行是 `SHEIN-USDT-SWAP`，
只能按名匹配）为 115 个待核名单。

七行标 `已配置` 的是 2026-09-22 已有 `mapping-verify` 实测证据的 7 个
（见 `docs/research/us-mapping-2026-09-22.md`），其余 **107 个是新候选**。
`lotSz` 一列是 OKX 自己的最小下单步长（与 `minSz` 在 181/181 上相等）。

| # | ticker | 代币 | OKX instId | lotSz | 状态 |
|---|---|---|---|---|---|
| 1 | AAOI | AAOIx | AAOI-USDT-SWAP | 0.01 | 候选 |
| 2 | AAPL | AAPLx | AAPL-USDT-SWAP | 0.01 | 已配置 |
| 3 | ADBE | ADBEx | ADBE-USDT-SWAP | 0.01 | 候选 |
| 4 | ALAB | ALABx | ALAB-USDT-SWAP | 0.01 | 候选 |
| 5 | AMAT | AMATx | AMAT-USDT-SWAP | 0.01 | 候选 |
| 6 | AMD | AMDx | AMD-USDT-SWAP | 0.01 | 候选 |
| 7 | AMZN | AMZNx | AMZN-USDT-SWAP | 0.01 | 已配置 |
| 8 | APLD | APLDx | APLD-USDT-SWAP | 0.1 | 候选 |
| 9 | APP | APPx | APP-USDT-SWAP | 0.01 | 候选 |
| 10 | ARM | ARMx | ARM-USDT-SWAP | 0.01 | 候选 |
| 11 | ASML | ASMLx | ASML-USDT-SWAP | 0.001 | 候选 |
| 12 | ASTS | ASTSx | ASTS-USDT-SWAP | 0.01 | 候选 |
| 13 | AVGO | AVGOx | AVGO-USDT-SWAP | 0.01 | 候选 |
| 14 | AXTI | AXTIx | AXTI-USDT-SWAP | 0.01 | 候选 |
| 15 | BE | BEx | BE-USDT-SWAP | 0.01 | 候选 |
| 16 | BMNR | BMNRx | BMNR-USDT-SWAP | 0.1 | 候选 |
| 17 | BOT | BOTx | BOT-USDT-SWAP | 0.1 | 候选 |
| 18 | BSP | BSPx | BSP-USDT-SWAP | 0.1 | 候选 |
| 19 | BX | BXx | BX-USDT-SWAP | 0.01 | 候选 |
| 20 | CBRS | CBRSx | CBRS-USDT-SWAP | 0.01 | 候选 |
| 21 | CIEN | CIENx | CIEN-USDT-SWAP | 0.01 | 候选 |
| 22 | COHR | COHRx | COHR-USDT-SWAP | 0.01 | 候选 |
| 23 | COIN | COINx | COIN-USDT-SWAP | 0.01 | 候选 |
| 24 | CRCL | CRCLx | CRCL-USDT-SWAP | 0.1 | 候选 |
| 25 | CRM | CRMx | CRM-USDT-SWAP | 0.01 | 候选 |
| 26 | CRWD | CRWDx | CRWD-USDT-SWAP | 0.01 | 候选 |
| 27 | CRWV | CRWVx | CRWV-USDT-SWAP | 0.01 | 候选 |
| 28 | CSCO | CSCOx | CSCO-USDT-SWAP | 0.01 | 候选 |
| 29 | DDOG | DDOGx | DDOG-USDT-SWAP | 0.01 | 候选 |
| 30 | DELL | DELLx | DELL-USDT-SWAP | 0.01 | 候选 |
| 31 | DKNG | DKNGx | DKNG-USDT-SWAP | 0.1 | 候选 |
| 32 | DRAM | DRAMx | DRAM-USDT-SWAP | 0.1 | 候选 |
| 33 | EWY | EWYx | EWY-USDT-SWAP | 0.01 | 候选 |
| 34 | FLNC | FLNCx | FLNC-USDT-SWAP | 0.1 | 候选 |
| 35 | GEV | GEVx | GEV-USDT-SWAP | 0.001 | 候选 |
| 36 | GLW | GLWx | GLW-USDT-SWAP | 0.01 | 候选 |
| 37 | GME | GMEx | GME-USDT-SWAP | 0.1 | 候选 |
| 38 | GOOGL | GOOGLx | GOOGL-USDT-SWAP | 0.01 | 已配置 |
| 39 | HIMS | HIMSx | HIMS-USDT-SWAP | 0.1 | 候选 |
| 40 | HOOD | HOODx | HOOD-USDT-SWAP | 0.1 | 候选 |
| 41 | HPE | HPEx | HPE-USDT-SWAP | 0.1 | 候选 |
| 42 | HUT | HUTx | HUT-USDT-SWAP | 0.01 | 候选 |
| 43 | IBM | IBMx | IBM-USDT-SWAP | 0.01 | 候选 |
| 44 | INTC | INTCx | INTC-USDT-SWAP | 0.1 | 候选 |
| 45 | INTW | INTWx | INTW-USDT-SWAP | 0.1 | 候选 |
| 46 | IONQ | IONQx | IONQ-USDT-SWAP | 0.1 | 候选 |
| 47 | IREN | IRENx | IREN-USDT-SWAP | 0.1 | 候选 |
| 48 | ISRG | ISRGx | ISRG-USDT-SWAP | 0.01 | 候选 |
| 49 | IWM | IWMx | IWM-USDT-SWAP | 0.01 | 候选 |
| 50 | JNJ | JNJx | JNJ-USDT-SWAP | 0.01 | 候选 |
| 51 | KLAC | KLACx | KLAC-USDT-SWAP | 0.01 | 候选 |
| 52 | KO | KOx | KO-USDT-SWAP | 0.1 | 候选 |
| 53 | KORU | KORUx | KORU-USDT-SWAP | 0.01 | 候选 |
| 54 | LITE | LITEx | LITE-USDT-SWAP | 0.01 | 候选 |
| 55 | LLY | LLYx | LLY-USDT-SWAP | 0.01 | 候选 |
| 56 | LRCX | LRCXx | LRCX-USDT-SWAP | 0.01 | 候选 |
| 57 | MARA | MARAx | MARA-USDT-SWAP | 0.1 | 候选 |
| 58 | META | METAx | META-USDT-SWAP | 0.01 | 已配置 |
| 59 | MRK | MRKx | MRK-USDT-SWAP | 0.01 | 候选 |
| 60 | MRNA | MRNAx | MRNA-USDT-SWAP | 0.01 | 候选 |
| 61 | MRVL | MRVLx | MRVL-USDT-SWAP | 0.01 | 候选 |
| 62 | MSFT | MSFTx | MSFT-USDT-SWAP | 0.01 | 已配置 |
| 63 | MSTR | MSTRx | MSTR-USDT-SWAP | 0.01 | 候选 |
| 64 | MU | MUx | MU-USDT-SWAP | 0.01 | 候选 |
| 65 | MUU | MUUx | MUU-USDT-SWAP | 0.01 | 候选 |
| 66 | MVLL | MVLLx | MVLL-USDT-SWAP | 0.1 | 候选 |
| 67 | NBIS | NBISx | NBIS-USDT-SWAP | 0.01 | 候选 |
| 68 | NET | NETx | NET-USDT-SWAP | 0.01 | 候选 |
| 69 | NFLX | NFLXx | NFLX-USDT-SWAP | 0.01 | 候选 |
| 70 | NOW | NOWx | NOW-USDT-SWAP | 0.01 | 候选 |
| 71 | NVDA | NVDAx | NVDA-USDT-SWAP | 0.01 | 已配置 |
| 72 | OKLO | OKLOx | OKLO-USDT-SWAP | 0.1 | 候选 |
| 73 | OKTA | OKTAx | OKTA-USDT-SWAP | 0.01 | 候选 |
| 74 | ON | ONx | ON-USDT-SWAP | 0.1 | 候选 |
| 75 | ONDS | ONDSx | ONDS-USDT-SWAP | 1 | 候选 |
| 76 | ORCL | ORCLx | ORCL-USDT-SWAP | 0.01 | 候选 |
| 77 | PLTR | PLTRx | PLTR-USDT-SWAP | 0.01 | 候选 |
| 78 | PYPL | PYPLx | PYPL-USDT-SWAP | 0.1 | 候选 |
| 79 | QCOM | QCOMx | QCOM-USDT-SWAP | 0.01 | 候选 |
| 80 | QQQ | QQQx | QQQ-USDT-SWAP | 0.01 | 候选 |
| 81 | RDDT | RDDTx | RDDT-USDT-SWAP | 0.01 | 候选 |
| 82 | RIOT | RIOTx | RIOT-USDT-SWAP | 0.1 | 候选 |
| 83 | RIVN | RIVNx | RIVN-USDT-SWAP | 0.1 | 候选 |
| 84 | RKLB | RKLBx | RKLB-USDT-SWAP | 0.01 | 候选 |
| 85 | ROK | ROKx | ROK-USDT-SWAP | 0.01 | 候选 |
| 86 | SHAZ | SHAZx | SHAZ-USDT-SWAP | 0.1 | 候选 |
| 87 | SKHY | SKHYx | SKHY-USDT-SWAP | 0.01 | 候选 |
| 88 | SMCI | SMCIx | SMCI-USDT-SWAP | 0.1 | 候选 |
| 89 | SMH | SMHx | SMH-USDT-SWAP | 0.01 | 候选 |
| 90 | SNDK | SNDKx | SNDK-USDT-SWAP | 0.001 | 候选 |
| 91 | SNOW | SNOWx | SNOW-USDT-SWAP | 0.01 | 候选 |
| 92 | SNXX | SNXXx | SNXX-USDT-SWAP | 0.1 | 候选 |
| 93 | SOXL | SOXLx | SOXL-USDT-SWAP | 0.01 | 候选 |
| 94 | SOXS | SOXSx | SOXS-USDT-SWAP | 0.1 | 候选 |
| 95 | SPCX | SPCXx | SPCX-USDT-SWAP | 0.01 | 候选 |
| 96 | SPY | SPYx | SPY-USDT-SWAP | 0.01 | 候选 |
| 97 | STRC | STRCx | STRC-USDT-SWAP | 0.1 | 候选 |
| 98 | TEAM | TEAMx | TEAM-USDT-SWAP | 0.01 | 候选 |
| 99 | TER | TERx | TER-USDT-SWAP | 0.01 | 候选 |
| 100 | TQQQ | TQQQx | TQQQ-USDT-SWAP | 0.1 | 候选 |
| 101 | TSLA | TSLAx | TSLA-USDT-SWAP | 0.01 | 已配置 |
| 102 | TSM | TSMx | TSM-USDT-SWAP | 0.01 | 候选 |
| 103 | TTMI | TTMIx | TTMI-USDT-SWAP | 0.01 | 候选 |
| 104 | TTWO | TTWOx | TTWO-USDT-SWAP | 0.01 | 候选 |
| 105 | TWLO | TWLOx | TWLO-USDT-SWAP | 0.01 | 候选 |
| 106 | UNH | UNHx | UNH-USDT-SWAP | 0.01 | 候选 |
| 107 | USAR | USARx | USAR-USDT-SWAP | 0.1 | 候选 |
| 108 | VRT | VRTx | VRT-USDT-SWAP | 0.01 | 候选 |
| 109 | WDC | WDCx | WDC-USDT-SWAP | 0.01 | 候选 |
| 110 | WEN | WENx | WEN-USDT-SWAP | 1 | 候选 |
| 111 | WMT | WMTx | WMT-USDT-SWAP | 0.01 | 候选 |
| 112 | XLE | XLEx | XLE-USDT-SWAP | 0.1 | 候选 |
| 113 | XOM | XOMx | XOM-USDT-SWAP | 0.01 | 候选 |
| 114 | ZM | ZMx | ZM-USDT-SWAP | 0.1 | 候选 |

---

## 3. 两边的差集（这些就是"不用扫"的名额）

### 3.1 OKX 有股票永续、发行方没有对应美股代币：67 个

`AEHR, AMC, ANTHROPIC, BB, BRKB, CGNX, COST, CRDO, CSOPSAMSUNG2L, CSOPSKHYNIX2L,
CXMT, EWJ, EWT, EWZ, FLY, FWDI, GPRO, HANMI, HYUNDAI, INFQ, JP225, KIOXIA, KR200,
LGELECTRONICS, LUNR, LYTE, MINIMAX, MOONSHOT, NAVER, NOK, NVDL, OPENAI, OSCR,
OUST, PENG, POET, POPMART, PURR, QNT, RAM, RDW, SAMSUNG, SHEIN, SHLD, SHOP, SIMO,
SKDD, SKHYNIX, SKUU, SOFTBANK, SONY, SPCH, SQQQ, TEM, TMF, TSEM, TSLL, UNITREE,
URNM, US100, US500, USO, UVXY, XBI, XIAOMI, ZHIPU, ZHONGJI`

这一列里混着三类东西，不能当"美股单只股票"处理：

* **港股/韩股/日股**：`XIAOMI`、`POPMART`、`SHEIN`、`SAMSUNG`、`SKHYNIX`、
  `NAVER`、`SONY`、`SOFTBANK`、`HYUNDAI`、`KIOXIA` —— 发行方的对应代币是
  HK/其他市场，不是 US；
* **指数/ETF/杠杆**：`US100`、`US500`、`SPCH`、`JP225`、`KR200`、`EWJ`、`EWT`、
  `EWZ`、`SQQQ`、`TMF`、`UVXY`、`XBI`、`URNM`、`USO`、`NVDL`、`TSLL`；
* **未上市/私募或非股票**：`OPENAI`、`ANTHROPIC`、`MOONSHOT`、`ZHIPU`、
  `MINIMAX`、`UNITREE`、`CXMT`、`PURR`、`RAM`、`QNT`、`FLY`。

### 3.2 发行方有 US 代币、OKX 没有股票永续：640 个

754 − 114 = 640。**这是被闸门挡掉的全部名额**：它们有链上代币，但 OKX 这边
没有对手腿，扫出来只能写进 `report_only` 里当"不可对冲"的证据。

### 3.3 按名匹配的松口径（待确认，不并入 114）

| 发行方 | OKX | 为什么只能按名匹配 | 处置 |
|---|---|---|---|
| `SHEINx` / `625`（HK 上市） | `SHEIN-USDT-SWAP` | 发行方给的是 HKEX 代码 625，不是名字 | 待确认 |
| `SNXx` / `SNX` | `SNXX-USDT-SWAP` | `SNXX` 是 SNDK 2x ETF，与 `SNX` 差一个字母 | **不匹配**（见 §4） |
| `VRTXx` / `VRTX` | —（OKX 无 `VRTX`） | OKX 未上市该名字 | 无对手腿 |

---

## 4. 114 个的标的性质：99 个单只股票 + 15 个 ETF/指数

> **更正（2026-09-22，operator 指出）**：本节初版把 `SPCX / SHAZ / BOT /
> BSP / STRC` 写成"未上市 / 私募 / 结构化 … 没有二级市场股数可对照"，并因此把
> 可推进的候选池算成 94 个。**那是错的**。operator 说"SPCX/SHAZ/BOT/BSP/STRC
> 全部上市了，你需要核实"——核实结果：operator 是对的，五个全部在 Nasdaq 挂牌，
> 有真实二级市场、有实时报价。下面每条都附了 2026-09-22 读到的证据。

### 4.1 那 5 个：全部已上市（实测）

读法：`GET https://api.nasdaq.com/api/quote/<TICKER>/info?assetclass=stocks`
（只读、公开），再用 SEC 的注册代码表 `https://www.sec.gov/files/company_tickers.json`
交叉核对"这是不是一个 SEC 注册发行人"。两处都命中才算"上市"。

| ticker | Nasdaq 上的全名 | 交易所 | `lastSalePrice`（2026-09-22 盘前） | SEC CIK |
|---|---|---|---|---|
| SPCX | Space Exploration Technologies Corp. Class A Common Stock | NASDAQ-GS（`isNasdaq100=true`） | $152.09 | 1181412 |
| SHAZ | SharonAI Holdings, Inc. Class A Common Stock | NASDAQ-CM | $59.2799 | 2068385 |
| BOT | RoboStrategy, Inc. Common Stock | NASDAQ-GM | $29.3742 | 2081119 |
| BSP | Bending Spoons S.p.A. Ordinary Shares | NASDAQ-GS | $38.9399 | 2004711 |
| STRC | Strategy Inc Variable Rate Series A Perpetual Stretch Preferred Stock | NASDAQ-GS | $98.90 | 1050446 |

三点对映射口径的影响：

1. **`shares_per_token` 的算法不变**：仍然是
   `wrapper.convertToAssets(1e18) == issuer.multiplier()`，再用"链上代币价 ÷
   永续中间价"交叉核对。上市与否只决定**有没有第三方股数可以对照**，现在这 5 个
   有了。
2. **`SPCX` 尤其值得注意**：它不只是"上市了"，它已经被收进 Nasdaq-100。
3. **`STRC` 是优先股**（Strategy 公司的可变利率 A 系列永续优先股），不是普通股。
   "1 张 = 1 股"这句话里的"股"是**优先股份额**，OKX 的 `ctValCcy=STRC` 指的也是
   这个标的。做基差没有影响，但任何"按普通股股本/稀释口径"算的东西都不适用。

### 4.2 剩下的 15 个是 ETF / 指数，不是单只股票

`SPY`（SP500）、`QQQ`（Nasdaq）、`IWM`（Russell 2000）、`SMH`（VanEck 半导体）、
`XLE`（能源 SPDR）、`EWY`（iShares MSCI Korea）、`DRAM`（Roundhill 存储）、
`SOXL`（半导体 3x 多）、`SOXS`（半导体 3x 反向）、`TQQQ`（Nasdaq 100 3x）、
`INTW`（INTC 2x 多）、`KORU`（韩国 3x 多）、`MUU`（Micron 2x 多）、
`MVLL`（Marvell 2x 多）、`SNXX`（SNDK 2x 多）。

这 15 个**也都有真实二级市场**，所以本节初版写的"`shares_per_token` 口径必须
另定"要收窄成一句更小的话：**一条腿和对手腿跟踪的是同一个产品**（代币 1 份 =
该 ETF 1 份，OKX 永续 1 张 = 该 ETF 1 份），所以交叉核对方法与单只股票**完全一样**；
不同的只是"股数"这个词指的是 ETF 份额。**唯一真正的差别在风险侧**：持有 2x/3x
杠杆产品的多空对冲会有杠杆重置/衰减的路径依赖，那不是映射问题，是持仓期问题，
留到纸面阶段评估。

### 4.3 结论

**114 = 99 个单只股票 + 15 个 ETF/指数**，全部都是"有二级市场、有可对照股数"的
标的，都可以用同一套方法核。初版那个"94 个真正可推进"是错的，正确的数是 **99
个单只股票（其中 5 个是新确认的上市标的）**，另外 15 个 ETF 按 §4.2 的口径同样
可推进。

---

## 5. 合约规格：181/181 都是"1 张 = 1 股"

| 字段 | 实测值 | 分布 |
|---|---|---|
| `ctVal` | 全部 `1` | 181/181 |
| `ctMult` | 全部 `1` | 181/181 |
| `ctValCcy` | = `instId` 去掉 `-USDT-SWAP` | 181/181 |
| `settleCcy` / `ctType` | `USDT` / `linear` | 181/181 |
| `lotSz` = `minSz` | 相等 | `0.01`×95、`0.1`×63、`1`×13、`0.001`×8、`0.0001`×2 |

所以对冲股数就是 `合约张数 × ctVal × ctMult = 张数 × 1 股`，步长按上表的
`lotSz`。

### 5.1 最小规模：`min_notional` 不存在，`minSz` 就是官方下限

operator 指出"读出来是 0 不能当作无最小名义，需要实测或文档确认"。查下来是
**三件事分开的**：

**（1）字段确实没有。** 实测 `GET /api/v5/public/instruments?instType=SWAP&instId=AAPL-USDT-SWAP`
（2026-09-22，走本地代理）返回的键里没有 `min_notional`／`minNotional`：

```json
{"instId":"AAPL-USDT-SWAP","instType":"SWAP","instCategory":"3","state":"live",
 "ctVal":"1","ctValCcy":"AAPL","ctMult":"1","lotSz":"0.01","minSz":"0.01",
 "tickSz":"0.01","settleCcy":"USDT","ctType":"linear","last":"340.29", ...}
```

所以本地解析器里的 `0` 是"字段不存在"，不是"交易所说没有下限"。这一点 operator
的判断是对的。

**（2）官方文档定义了另一套下限。** OKX 文档（`https://www.okx.com/docs-v5/en/`，
需带浏览器 UA 才不被 Cloudflare 1010 拦）对这两个字段的定义是：

- `minSz`："Minimum order size" —— 派生品**以张为单位**，不是美元名义；
- `lotSz`："Lot size … the value is the number of contracts"，且订单的 `sz`
  必须是 `lotSz` 的整数倍，否则报 `51121`。

对应的错误码（同一文档）：`51020` = "Order amount must be greater than or equal
to the minimum order amount"；`51120` = 下单数量不足 `{param0}` 张；`51121` =
下单张数应为一手张数的倍数。

**文档里对 SWAP 只描述这两条约束（`sz ≥ minSz` 且 `sz` 是 `lotSz` 的整数倍），
没有单独的美元最小名义。** 181 个股票永续里 `lotSz == minSz` 全部成立（见上表），
所以下限就是 `minSz` 张。AAPL 现价 ≈ 340.29 → `0.01 张 ≈ 3.40 USDT`。

**（3）真下单一轮没有跑成，原因是账户模式，不是数量。** 想用
`POST /api/v5/trade/order-precheck` 实测时，它对本账户的每个 `tdMode`
（`cross`/`isolated`/`cash`，带不带 `posSide=net`）都返回 `51010`
"当前账户模式不支持此操作"。该错误码讲的是账户模式，不能拿它当规模证据。
可用的旁证是 `GET /api/v5/account/max-size?instType=SWAP&instId=AAPL-USDT-SWAP&tdMode=cross`
→ `maxBuy = maxSell = 0.08`（非零 ⇒ 可交易；`XIAOMI-USDT-SWAP` 是 `8`）。

⚠️ **顺带发现一处安全隐患（未擅自改名）**：`config/okx-demo.env` 里的 key
**不是模拟盘**。同一个 key 访问 `GET /api/v5/account/balance` 不带
`x-simulated-trading` 头返回 `code=0`，`totalEq = 9.9450282651836`；带上
`x-simulated-trading: 1` 则 HTTP 401 `50101 APIKey does not match current
environment`。也就是说它是**实盘只读+交易**的 key（`perm = read_only,trade`，
`acctLv=2`，`posMode=net_mode`），余额与 2026-09-15 那次真实单腿 smoke test
同一个账户。**本轮没有下过任何单**（`OKXDemoClient.request()` 在
`simulated=False` 时会拒绝，且 `GET /api/v5/trade/orders-pending` 为 `[]`）。
文件名与真实环境不符，属于容易误用的一处，改不改名由 operator 定。

---

## 6. 代码落点：闸门在哪

| 位置 | 作用 |
|---|---|
| `data/xstocks_registry.py::fetch_okx_stock_perps` | 读 OKX 的 `instCategory="3"`，返回 `{ticker: 行}` |
| `data/xstocks_registry.py::okx_hedge_ticker` | 交集判据：发行方 `underlyingSymbol` ∈ OKX ticker 集合 |
| `apps/hk_monitor.py::refresh_token_universe` | 探测前先过闸门；结果写进 `run_summary.token_universe_scan.perp_gate` |
| `config/hk_monitor.toml::onchain.token_scan_perp_gate_markets` | 哪些市场受闸门约束（当前 `["US"]`） |

设计上的三个取舍，写在这里以免下一轮被当成 bug：

1. **只闸 US**：HK 的可对冲集合还包括 Gate/Bybit/Bitget，探测范围允许比单一
   场所宽（AGENTS.md「监控范围可以比可执行范围宽」）；
2. **读不到名单时不放行**：`perp_gate.error` 非空时该市场这一轮一个都不探，
   不退回"全扫 754"——未知不等于可以全扫；
3. **闸门只影响"扫不扫"**，不影响采纳、候选、白名单：`token_adopt_markets`
   在 2026-09-22 operator 指示"现在开始对这114个公司也进行监控"之后是
   `["HK", "US"]`，但采纳仍然要过"交集 114 之内 + X Layer 池深度 ≥
   `min_pool_depth_usd`"两道闸门，而且被采纳的 US 名字
   `mapping_verified = false`、不在执行白名单里，`execution_blockers()`
   会写"份额映射未核验"——**监控 ≠ 可执行**。执行白名单、额度与
   `KILL_SWITCH`/`AUTO_HALT` 本轮都没动。

---

## 7. 复现命令

```bash
cd /home/operator/Documents/arb
# 两张原始名单（只读，走本地代理）
curl -sS --proxy http://127.0.0.1:4780 'https://api.backed.fi/api/v2/public/assets?page=0' > /tmp/issuer_assets.json   # 需翻页合并
curl -sS --proxy http://127.0.0.1:4780 'https://www.okx.com/api/v5/public/instruments?instType=SWAP' > /tmp/okx_swap.json
# 交集（上面两张名单的本地合并结果）
python3 - <<'PY'
import json
iss = json.load(open('/tmp/issuer_assets.json'))
okx = json.load(open('/tmp/okx_swap.json'))['data']
perps = {r['ctValCcy'] for r in okx if r.get('instCategory') == '3'}
us = [a for a in iss if a['listing_country'] == 'US' and a.get('hk_code')]
print(len(us), len(perps), len({a['hk_code'] for a in us} & perps))   # 754 181 114
PY
# 测试（闸门本身是 hermetic 的，不碰网络）
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_us_token_extension -v
```

---

## 8. 待确认（不编造，逐条挂账）

1. **SHEIN 的按名匹配**：发行方 `hk_code = 625`，OKX 是 `SHEIN-USDT-SWAP`，
   中间没有共同的机器可读字段。是否算进交集，需要 operator 或发行方文档确认。
2. **15 个 ETF 的映射口径收窄为一条**（§4）：初版的"20 个非单只股票"已拆开——
   5 个（`SPCX/SHAZ/BOT/BSP/STRC`）已逐个核实为 Nasdaq 上市标的，不再是疑点；
   剩下的 15 个是 ETF/指数，"1 张 = 1 股"里的"股"指 **ETF 份额**，与代币一侧
   跟踪同一产品，核对方法不变。真正的未决项只剩**风险侧**：2x/3x 杠杆产品做多空
   对冲有杠杆重置/衰减的路径依赖，属于持仓期问题，留到纸面阶段评估。
3. **`token_scan_perp_gate_markets` 是否要扩到 HK**：当前只列 US。若扩到 HK，
   HK 的 OKX 名单必须先把 `XIAOMI/POPMART/...` 这类"名对码"的匹配规则定下来，
   否则会误挡掉只有 Gate/Bybit/Bitget 对手腿的港股。
4. **OKX 最小名义**（详见 §5.1）：
   - **已确认**：该接口确实没有 `min_notional` 字段（`0` 是"字段不存在"）；
     官方文档对 SWAP 只定义 `sz ≥ minSz` 与 `sz` 为 `lotSz` 的整数倍
     （违规报 `51020`/`51121`），没有单独的美元最小名义；181/181 是
     `lotSz == minSz`，AAPL 为 `0.01 张 ≈ 3.40 USDT`。
   - **仍是待确认**：没有用真实下单走通一次"刚好 `minSz` / 略低于 `minSz`"的
     对照实验。`order-precheck` 对本账户返回 `51010`（账户模式），不能用作规模
     证据。要把它变成实测，需要一个不成交的探针（例如远低于中间价的限价单，
     或一次会被拒的下单）——**属于要在实盘账户上发请求的动作，等 operator 明确
     授权再做**，本轮没有下单。
5. **交集名单要不要落进 config**：已部分回答。114 个 ticker 现在**写进了代码**
   （`data/hk_cex.py::US_CANDIDATES`），作为 OKX 股票永续的解析白名单，同时避免
   `QNT/RAM/PURR/FLY` 这类与加密同名 ticker 的串味；但**没有**把 114 行写进
   `config/hk_monitor.toml` 的 `assets`。名单仍有漂移风险（OKX 上下架），所以真
   正的闸门是扫描时**即时读 OKX 名单**（`token_scan_perp_gate_markets`），
   `US_CANDIDATES` 只是解析侧的护栏。要不要再写死一份快照，operator 定。
6. **114 个里链上真的有池的有几个（进行中）**。交集的 114 个只能保证"OKX 有永续
   可对冲"，能不能进价格监控还要看 X Layer 上有没有池（深度 ≥
   `min_pool_depth_usd = 1000`）。轮转每轮 20 个探针，约 10 轮（≈5 小时）走完一遍。
   前两轮的 20 个（HK 9 + US 11）实测全部无池。为了排除"扫描悄悄失败"，
   2026-09-22 22:1x 对工厂（block 71317430）做了一次独立只读对照
   （`discover_pools`，同一段代码）：

   | 标的 | 地址 | 工厂里的池 |
   |---|---|---|
   | AAPLx | wrapper `0x943bf64d566c32a2bcd41ac92fb63c111cc9de8f` | 5 个（含配置里的 USDG/500 `0xc44bd9c8589026d28d1632d7b86b2efb6cdc8fd2`）|
   | AAPLx | native `0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a` | 1 个（USDG/500 `0xce0954e2eaa9f3475a68aa895f5a0601a97f17b2`）|
   | FLNCx | wrapper `0xdaf515b7de6ccf5efe17a106a1312039fef2e31b` | 0 |
   | FLNCx | native `0x4aaf56c5c9a04d6af855d3c963026d9e4bdd0ab6` | 0 |

  已知有池的名字照样找得到池、且与 config 一致，故"探针无池"是真测量。

  ⚠️ **2026-09-22 深夜更正："≈5 小时走完一遍"在频繁重启时不成立，已修**。
  原先探针的轮转位置是**内存里的一个下标**（`token_scan_cursor`），而扫描时钟
  （`last_token_scan`）在每次启动时被故意重置为 now，所以每次重启的第一次扫描
  都从下标 0 开始——库里实测：

  | 扫描时刻 | `layers.probed_this_round` | `probes_pending_after_cap` |
  |---|---|---|
  | 2026-09-22 22:07:02 | `{HK: 9, US: 11}` | 158 |
  | 2026-09-22 22:48:55（重启后的第一次） | `{HK: 9, US: 11}` | 158 |

  两轮之间夹着一次重启，**同样的 20 个名字、`no_pool` 也完全一样**：也就是说
  "扫完 193 个"这件事会被重启无限推迟。修法：轮转改成**"最久未探优先"**，
  每个 listing 上次被探的时刻写进 `token_universe_scan` 事件
  （`probe_seen_ms`，键 = `市场:发行方符号`），`_restore_last_scan_report()`
  在启动时把它读回来。邻居字段也一起报出来：`probed_assets`（这一轮真的花了
  探针的名字）与 `probe_rotation`（`cap/pending/truncated/scoped/never_probed`）。
  测试 `tests/test_us_token_extension.py::ProbeRotationPersistenceTests`（5 项）
  钉住：重启后续扫的是下一批、未探过的名字优先、`ceil(N/cap)` 轮内每个名字至少
  被探一次、账本写进事件并能被下一个进程读回、账本按当前范围内名字剪枝。

   **待确认**：整轮扫完之后，114 个里实际有多少个能进监控（现在无法预判）。

---

## 9. 本轮落地清单（2026-09-22）

| 位置 | 改动 |
|---|---|
| `config/hk_monitor.toml` | `token_adopt_markets = ["HK", "US"]`；`quote_slice_markets = ["US"]`、`quote_slice_assets_per_cycle = 3`（轮转报价，避免一轮 100+ 个新标的把采样逼爆）；`token_scan_full_every = 1`、`token_scan_max_probes_per_round = 20`；`[retention.kinds] token_universe_scan = 7` |
| `data/hk_cex.py::US_CANDIDATES` | 7 → 114 个 ticker；okx 分支加 `instCategory == "3"` 护栏（挡住 `QNT/RAM/PURR/FLY` 这类同名加密合约） |
| `apps/hk_monitor.py` | 发现漏斗按市场统计（`layers.gated_out/probed/probed_this_round/no_pool`）；扫描结果落成 `token_universe_scan` 事件，重启后由 `_restore_last_scan_report()` 读回，`scan_age_ms` 报的是**那次测量**的时间而不是进程启动时间；看板新增 `market_coverage_html()`（按上市地：监控中标的／本周期有报价／本轮候选 + 发现漏斗一行） |
| 漏斗的两个探针口径 | `layers.probed` = 交集总量（市场级上限，即 114 这个数）；`layers.probed_this_round` = **这一轮真的量过**的名字数（`token_scan_max_probes_per_round = 20` 轮转）。看板两个都印（"交集内 114 · 本轮探针 N"）。`layers.pairable_candidates` 只数**刚进监控**的（`already_monitored()` 会让早先几轮的不再出现），标签写作「本轮新增有池候选」 |
| `data/hk_cex.py::parse_contracts`（**回归修复**） | 扩大 US 名单后 Gate 整体从监控消失：Gate 的 `SOXS_USDT` 带 `order_size_min = 0`，而解析器对"规格 ≤ 0"是 `raise` → 整场解析失败 → `discover()` 只保留解析成功的场馆，Gate 的 152 个合约被顺手删掉（其余 7 家照常出数，summary 看不出异常）。现在坏行**丢弃并上报**（`source_error` → `contract_spec_skipped:SOXS_USDT`，含 `skipped` 计数），场馆保留；不传 `skipped` 列表的调用方仍 `raise`。实测：最新 `registry` 里 gate = **152** 个合约，`perp_book` 恢复出数。**代价：SOXS 在 Gate 上不再被监控**（它的 0 最小量无法据此下单），SOXS 的 OKX 永续不受影响 |
| 测试 | 全量：`PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -t tests` → **807 tests OK (skipped=2)** |

没有动的东西（operator 专属闸门）：`[execution] mode` / `auto_orders` /
`live_ack`、`[limits]` 单笔与日额度、执行白名单、`data/KILL_SWITCH`、
`data/AUTO_HALT`。

---

## 参考

- 发行方资产表：`https://api.backed.fi/api/v2/public/assets`
- OKX 合约表：`https://www.okx.com/api/v5/public/instruments?instType=SWAP`
- US 映射证据链：`docs/research/us-mapping-2026-09-22.md`
- 港股映射核验方法：`docs/research/mapping-hk-venues-2026-09-17.md`
- 全天候与统计口径：`AGENTS.md`「全天候原则」「监控统计口径」
