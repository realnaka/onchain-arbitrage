# 原生 xStock 池子复查脚本（2026-09-24）

> 触发（operator）：先问「你说原生没有深度，但是为什么这个
> `0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f` 有深度？」，随后要求
> 「把『原生池复查』做成一个只读检查脚本，生成一个列表，包括股票名字、原生的
> 合约地址、wrappered 代币合约地址」。

结论：**这是一个真实的检查盲点，不是措辞问题。** 脚本已落地，只读，尚未联网跑过
（本机沙箱无外网、提权审批被限额挡住），因此本文件不含新的实测深度数字。

---

## 1. 盲点是什么

「原生代币没有市场、有深度的是 wrapper」这句话，证据范围只有 2026-09-22 实测的
**7 个美股名字**（AAPL / NVDA / TSLA / MSFT / AMZN / META / GOOGL，见
`docs/research/us-mapping-2026-09-22.md` §2.2）。它从来不是逐名字的事实。

更关键的是监控自己的发现逻辑：`hk_monitor.py` 的 token-universe 探测只在
`brand_new` 时同时扫 `native` 和 `wrapper` 两个地址，已经见过的 listing 每轮
**只重扫 wrapper**：

```python
candidate_bases = [("wrapper", asset["wrapper_address"]),
                   ("native", asset["native_address"])]
if not brand_new:
    candidate_bases = [c for c in candidate_bases if c[0] == "wrapper"] or candidate_bases
```

后果：**原生池在首次探测之后新建或加流动性，监控永远看不到。** MRNA 的记录就是
这个形状——`token_universe_scan`（2026-09-23 03:02:18）里它是 `brand_new`，两个
地址都扫过，选中 wrapper 池（深度 2534.66 USD），此后原生再没被复查过。

扫描口径本身也很窄，只有一个 V3 factory + 4 个稳定币 × 4 个费率档：

```text
factory = 0x4b2ab38dbf28d31d467aa8993f6c2585981d6804   （config/hk_monitor.toml）
stables = USDC / USDT0 / USDG / USDT
fees    = 100 / 500 / 3000 / 10000
```

所以「原生有深度」这件事至少有三种可能，脚本要能把它们分开：池子是首次探测之后
出现的；池子在这条 factory/稳定币/费率网格之外；或者页面上的「流动性」是聚合器
报价口径而不是单池深度。

## 2. 脚本做了什么

```bash
PYTHONPATH=src .venv/bin/python scripts/audit_native_pools.py \
    --countries US,HK --proxy http://127.0.0.1:4780 \
    --json-out data/native_pool_audit.json
```

| 层 | 落点 |
|---|---|
| 纯逻辑（可离线测试） | `src/arb_monitor/ops/native_pool_audit.py` |
| CLI 薄壳 | `scripts/audit_native_pools.py` |
| hermetic 测试 | `tests/test_native_pool_audit.py`（15 项，全部 fake RPC） |

流程：读发行方登记表（`api.backed.fi/api/v2/public/assets`，可用 `--issuer-json`
改成读本地快照）→ 取每个 listing 的 `address`（原生）与 `wrapperAddressV2` →
对**两个地址都**用 `factory.getPool` 扫稳定币 × 费率网格 → 对每个存在的池子读
`slot0` / `liquidity` / 两端余额 → 取每侧最深池子 → 给一行结论。

输出行（也写进 JSON）：

```text
股票名 · 代码 · 市场 · 原生合约 · wrapper 合约 ·
原生池数 / 原生最深深度 / 原生最深池地址 / 原生错误 ·
wrapper 池数 / wrapper 最深深度 / wrapper 最深池地址 / wrapper 错误 · 结论
```

结论取值：

| 结论 | 含义 |
|---|---|
| `native_only` | 只有原生有 ≥ 阈值的深度 → **推翻「必须用 wrapper」的假设，值得改** |
| `wrapper_only` | 只有 wrapper 有深度（2026-09-22 七个名字的形状） |
| `both` | 两侧都有深度 → 原生可以省掉一层金库风险 |
| `neither` | 两侧都没有达标深度（但**读到了**池子） |
| `no_wrapper_recorded` | 发行方登记表里没有 wrapper 地址 |
| `read_failed` | 两侧都读失败 → **读不到 ≠ 没深度**，不许当 `neither` |

空池不并进「没池」：`pools_found` 与 `best_depth_usd` 分开记，一个
`liquidity()=0` 的池子会带 `depth_usd=0` 留在 `pools` 里。

## 3. 只读保证

- 只发 `eth_call` / `eth_blockNumber` / `eth_getBlockByNumber`；
  `tests/test_native_pool_audit.py::test_only_read_methods_are_ever_called`
  把这条钉成不变量。
- 没有钱包、私钥、签名、广播、approve；不碰 `data/KILL_SWITCH` /
  `data/AUTO_HALT`；不写任何配置。
- 唯一的磁盘写入是 `--json-out`（默认 `data/native_pool_audit.json`），
  `--no-json` 可关。
- 共用本地代理，`--pause-ms`（默认 150）在 listing 之间主动让带宽给采样循环；
  监控运行时不要把它调成 0。

## 4. 当前状态与待办

- 脚本、测试（15/15 通过）、README 与 HANDOFF 记录：✅ 本轮完成。
- 真实全量跑一遍（US + HK，或 `--asset MRNAx` 单查）：⏳ 需要联网放行。
- 若跑出 `native_only` / `both`：那意味着现货腿可以直接用原生地址，少一层
  ERC-4626 金库风险，需要 operator 决定是否重配 `config/hk_monitor.toml`。
- 若跑出 `read_failed` 占多：先查代理与该 RPC 的限频，不得按「没深度」处理。
- 后续可考虑把「原生地址也要定期复查」接回监控的探测循环，或让本脚本按周出报告；
  这属于改监控行为，需要 operator 明确要求。

复现（离线部分）：

```bash
cd /home/operator/Documents/arb
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_native_pool_audit -v
```
