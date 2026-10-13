# Arb Monitor

面向小资金、**监控优先**的 CEX/DEX 套利研究框架。

这是一份**脱敏后的公开快照**：代码、测试、配置种子与设计文档来自一个持续运行的
实盘研究系统，抓取时点是 **2026-10-10 收工时**（上游 `main` 当晚的最后一个提交）。
所有个人标识（钱包地址、邮箱、本机路径、交易哈希、授权串）已替换为占位符，执行
开关已改为安全默认值。改动清单见 [SANITIZATION.md](SANITIZATION.md)。

## 1. 这个项目解决什么问题

一句话：**把「发现价差 → 风控 → 执行 → 记账」这条链路只写一次**，之后每加一个
策略（跨所现货、期现基差、三角、同链跨 DEX、DEX↔CEX、资金费率、跨链）都只写
策略逻辑本身。

当前只跑一类策略：

```text
链上股票代币（现货多）  ×  CEX 的 USD 线性永续（空）
```

两条腿都在 24/7 市场交易，因此底层股票是否开市**不构成任何闸门**（见 §5.2）。

## 2. 快照来源与版本

| 项 | 值 |
|---|---|
| 快照时点 | 2026-10-10 收工 |
| 来源提交 | `222a829` — xstocks v2: an unknown broadcast keeps its evidence (B24) |
| 历史 | 上游是私有仓库；本仓库为**单提交**初始版本，不带私有历史 |
| 版本清单 | 见 [VERSIONS.md](VERSIONS.md) |

上游从 2026-10-07 起建立可回溯的版本节奏（v1 基线 → xStocks v2 决策核心），之后
每天推进。公开版截取到 10-10 收工；10-11 与 10-12 的更新列在 VERSIONS.md 里作为
参考，但**不在本仓库的代码里**。

> 为什么是单提交：上游历史里包含早期硬编码的凭据与个人地址。公开版从一棵已经脱敏
> 的树重新起历史，比清洗整条历史更可靠。后续版本可以继续以新提交叠加。

## 3. 目录结构

```text
src/arb_monitor/
  apps/         # 进程入口：监控循环、看板、账户面板
  core/         # 统一模型、仓位与成本换算、合约规格
  data/         # 行情与链上数据接入、归一化
  venues/       # 各 CEX/DEX 适配器（CCXT 优先，必要时原生）
  strategy/     # 策略插件：只产出声明式 Intent，不碰交易所/钱包
  risk/         # 闸门：净价差、额度、数据质量
  execution/    # 执行适配器、自动执行链、钱包只读门面
  ops/          # 记账、留存、报表
  xstocks_v2/   # 跨链路线：探针、桥费、余额、CCIP/Jupiter 等
scripts/        # 运维与一次性探针脚本
tests/          # 单元测试（hermetic，不碰真实网络与密钥）
config/         # 配置种子（*.env 一律不入库，只留 *.env.example）
docs/           # 架构、执行教训、研究记录
ops/            # 每条策略的 policy TOML（额度、阶段、开关）
```

## 4. 快速开始

```bash
# 依赖（uv 管理，Python 3.14）
uv venv .venv
uv pip install --python .venv/bin/python -e '.[live]'

# 跑测试（不需要密钥，也不联网）
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests

# 演示模式：造两家 venue 的模拟盘口，算净价差，写 SQLite
PYTHONPATH=src .venv/bin/python -m arb_monitor demo

# 单次真实只读采样（需要网络；走代理时加 --proxy 或设 ARB_HTTP_PROXY）
PYTHONPATH=src .venv/bin/python -m arb_monitor hk-monitor --cycles 1

# 本地只读看板（绑定 127.0.0.1，读同一份 SQLite）
PYTHONPATH=src .venv/bin/python -m arb_monitor hk-dashboard --port 8765
```

## 5. 三条决定行为的原则

### 5.1 赚钱优先：唯一裁判是真实成交均价算出的净利

不看「占池子比例」，也不看「滑点上限」是否好看。

```text
先用池子侧定本次能做多大的金额 Q（阶梯 25 → 50 → 100 → 200，过门槛才加档）
→ 再把 Q 拿到另一侧按 Q 成交，算平均价（VWAP）
→ 两腿平均价一起进成本模型
→ 净利过 min_net_edge_bps 与 min_net_profit_usdt 就做，不过就不做
```

比例类与冲击类阈值（占池深 20%、50 bps 滑点、300 bps 冲击）**只做证据、印在
板面上，不做否决**。唯一保留的硬门槛是**数据质量**：报价与参考价呈整倍数、或偏离
≥ 3000 bps 判 `price_outlier` —— 那是「数据错了」，不是「价格便宜」。

### 5.2 全天候：底层股票开市与否与本策略无关

现货腿在链上（7×24），永续腿在 CEX（7×24）。周末、假日、盘前、盘后与盘中
**同等对待**，`closed` 也在策略时段集合里。只有「日历定位不了时段」
（`calendar_unknown`）才拒绝 —— 那是 fail closed，不是开市规则。

### 5.3 缺样本不是拒绝理由

任何桥或提币路线，只要缺「这个币在这条边到账过」的样本，触发条件就是**用最小可行
额走一次试探**。缺输入（桥费、gas 币价、最小额）是 bug，要修掉，不允许让探针无限
期挂着。

## 6. 安全模型：自动执行的两把钥匙

自动执行**默认关闭**。要签名必须同时成立：

| 键 | 位置 | 含义 |
|---|---|---|
| `mode` | `[execution]` | 程序被允许签名 |
| `auto_orders` | `[execution]` | `paper` 用真实报价与真实风控但不成交；`live` 是真金白银 |

`live` 还需要仓库所有者亲自落下的 `live_ack` 串。三者任一不合则整条链只读。

**本公开快照里这三项已全部改成安全值**（`mode = "off"`、`auto_orders = "off"`、
`live_ack = ""`；xStocks v2 的 `phase` 为 `"scan"`）。上游私有仓库里它们是所有者
的专属开关，不在任何自动流程的授权范围内。

其余停机开关：`data/KILL_SWITCH`（停一切新单）与 `data/AUTO_HALT`（只挡开仓，
不挡平仓）。

额度是硬闸门：实际每笔 = `min(单笔上限, 钱包余额 × 0.999)` 按整股向下取整
—— 钱包常常才是真正的约束。

## 7. 数据与统计口径

| 规则 | 原因 |
|---|---|
| 分母用「该场所该时段**能定价**的轮数」，不用监控轮数 | 否则「监控得久」会伪装成「机会多」 |
| 带数据质量标记的行（陈旧盘口、陈旧报价、非同时刻、成本过期）单独计「不可信」 | 带坏数据的价不算机会 |
| 「没采样」不等于「没数据」 | 隔轮采样的场所当轮不写行；取失败的行照常写并带原因 |
| 场馆目录里的坏行只丢该行并上报 | 一行规格不可用不能让整个场馆静默退出监控 |

## 8. 工程约定

- 所有金额、价格、数量用 `Decimal`，**禁止**用 `float` 算交易结果。
- 策略只实现 `Strategy` 接口，产出声明式 `Intent`；策略不得直接调用交易所、RPC、
  钱包或通知 API。
- 行情适配器必须保留 venue 时间、本地接收时间、序列号和来源。
- 单元测试必须 hermetic（不碰真实网络、CLI、停机文件），断言打在不变量上
  （如「同时在飞的请求数 ≥ 2」），不打在耗时上。
- 净价差必须考虑盘口深度、手续费、滑点、Gas、资金成本和失败缓冲。

## 9. 文档入口

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 架构计划与分层说明 |
| [docs/plan.md](docs/plan.md) | 实施计划与阶段边界 |
| [docs/execution.md](docs/execution.md) | 执行链说明 |
| [docs/lessons/README.md](docs/lessons/README.md) | 本仓库自己的错误与教训库（带证据指针） |
| [docs/execution-lessons-2026-09-15.md](docs/execution-lessons-2026-09-15.md) | 执行、平仓、恢复的教训 |
| [docs/execution-lessons-xstocks-bridge-2026-09-26.md](docs/execution-lessons-xstocks-bridge-2026-09-26.md) | 跨链桥的教训 |
| [docs/xstocks-v2-arbitrage-taxonomy-2026-10-07.md](docs/xstocks-v2-arbitrage-taxonomy-2026-10-07.md) | xStocks v2 套利分类 |
| [docs/research/](docs/research/) | 成本标定、池子审计、映射核验等研究记录 |

## 10. 版本

见 [VERSIONS.md](VERSIONS.md)。当前公开版对应 2026-10-10 收工状态。

## 11. 免责声明与许可

本项目是研究框架，**不是投资建议**。代码按「原样」提供，不保证可盈利。链上交易与
合约交互有资金损失风险，请自行评估。

本仓库暂未附开源许可协议（默认保留所有权利）。需要公开授权时推荐 MIT 或
Apache-2.0，由仓库所有者决定后再补 `LICENSE`。
