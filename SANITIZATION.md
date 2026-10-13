# 脱敏说明（公开快照相对上游私有仓库改了什么）

本仓库不是上游私有仓库的原样拷贝。下面是**全部**改动类别。目的只有一个：把
个人标识与真实资金凭据留在私有仓库里。

## 1. 替换成占位符的标识

| 类别 | 处理 |
|---|---|
| EVM 钱包地址 | 全部替换为 `0xabababababababababababababababababababab`（含无 `0x` 前缀的裸写法） |
| Solana 钱包地址 | 全部替换为 `AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev`（一个合法、与真人无关的 32 字节公钥） |
| 该钱包派生的 token 账户（ATA） | 用新钱包重新推导出的地址替换（旧的关联账户地址同样是个人标识） |
| 邮箱 | 替换为 `operator@example.com` |
| 本机路径 | `/Users/<用户名>/…` → `/home/operator/…` |
| 上游仓库地址 | `github.com/<用户>/arb-monitor` → `github.com/your-org/arb-monitor` |
| 交易哈希与 Solana 签名 | 文档中的 `0x` + 64 位十六进制替换为全零；Solana 签名替换为 `REDACTED_SOLANA_SIGNATURE`。`src/` 与 `scripts/` 中的同类写法一并处理 |

保留不动的：

- **公开合约地址**（代币、router、CCIP 合约、SPL mint、Solana 程序 ID 等）；
- **标准常量**（例如 ERC-20 `Transfer` topic
  `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`）；
- **测试里的公开派生物**（如 v4 pool id = `keccak256(PoolKey)`，任何人都能复算）。

> 替换后**派生值必须一起改**：钱包地址变了，它的 ATA 也会变。`tests/` 里期望的
> ATA 与板面缩短显示（`0xabab…ababab`）都已按新占位符更新。上游 2195 个测试在
> 本仓库里同样全绿（7 个 skip，与上游一致）。

## 2. 执行开关改成安全默认值

上游 10-10 的部署是「已授权 live」状态。公开版把三把钥匙全部归零：

| 文件 | 键 | 上游 | 本仓库 |
|---|---|---|---|
| `config/automation_policy.toml` | `mode` | `preauthorized_program` | `off` |
| `config/automation_policy.toml` | `auto_orders` | `live` | `off` |
| `config/automation_policy.toml` | `live_ack` | 所有者授权串 | `""` |
| `ops/xstocks_policy.toml` | `mode` / `auto_orders` / `live_ack` | `live` / `live` / 授权串 | `off` / `off` / `""` |
| `ops/xstocks_eth_policy.toml` | 同上 | `live` / `live` / 授权串 | `off` / `off` / `""` |
| `ops/xstocks_v2_policy.toml` | `phase` / `mode` / `auto_orders` / `live_ack` | `paper_kraken` / `live` / `live` / 授权串 | `scan` / `off` / `off` / `""` |

这些改动只影响**公开副本**的默认值；上游私有仓库的开关没有被动过。

## 3. 未收录的内容

以下内容属于内部运维记录或第三方版权材料，公开版不收：

| 未收录 | 原因 |
|---|---|
| `HANDOFF.md`（约 560 KB 的内部状态流水） | 含账户余额、提币地址簿、逐笔资金流水等运营细节 |
| `AGENTS.md` / 子代理手册 / 逐日交接文档 | 内部协作规程，对外无意义 |
| `docs/knowledge/`（第三方推文与电子书的归档） | 第三方版权内容，且带来源许可限制 |
| `dexfundingarb/`（第三方扫描器的抓取产物） | 第三方代码与其原始数据 |
| `data/`、`data-v2/`、`config/*.env` | 运行期数据与真实凭据。上游本来就不入库 |
| `arb-dashboard-top.png` | 早期看板截图，无法逐像素确认为模拟数据，故不放 |

需要更完整的版本时，可以在同一棵树上按相同规则继续补齐。

## 4. 复核方式

公开版推送前已做的检查：

```bash
# 1) 敏感串应为 0 命中（此文件本身除外，它列出了要查的模式）
rg -uu -c '<本地用户名>|<GitHub 用户名>|<旧钱包地址前缀>|gmail\.com|/Users/'

# 2) 不应有硬编码密钥（sk- / AKIA / gh*_ / Telegram / 飞书 webhook）
rg -n '(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|gh[pous]_[A-Za-z0-9]{20,}|open-apis/bot/v2/hook/)'

# 3) 测试与上游同口径
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```
