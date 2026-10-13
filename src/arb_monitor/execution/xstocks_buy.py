"""模块 4：Solana 链上买入 xStock。硬编码，不让 LLM 参与决策。

分工：
  * 这里只做「报价 → 判定 → 构造交易 → 链上模拟」。**不签名、不广播。**
  * 签名与广播走已有的钱包通道（OnchainOS TEE / 预授权），单独一步，方便人工卡点。

判定用 `ops/xstocks_policy.toml`，阈值不写死在这里。
"""
from __future__ import annotations

import json
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "ops" / "xstocks_policy.toml"
JUP_QUOTE = "https://api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://api.jup.ag/swap/v1/swap"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


@dataclass(frozen=True)
class Policy:
    per_trade_quote_usdt: Decimal
    min_net_profit_usdt: Decimal
    max_risk: str
    max_slippage_bps: int
    jito_tip_lamports: int
    maker_first: bool
    maker_timeout_s: int
    min_price_vs_ref: Decimal
    withdraw_threshold_usdg: Decimal
    solana_swap_mint: str
    solana_usdt_mint: str

    @classmethod
    def load(cls, path: Path | str = POLICY_PATH) -> "Policy":
        raw = tomllib.loads(Path(path).read_text())
        t, e, c = raw["trade"], raw["exit"], raw["capital"]
        return cls(
            per_trade_quote_usdt=Decimal(t["per_trade_quote_usdt"]),
            min_net_profit_usdt=Decimal(t["min_net_profit_usdt"]),
            max_risk=str(t["max_risk"]),
            max_slippage_bps=int(t["max_slippage_bps"]),
            jito_tip_lamports=int(t["jito_tip_lamports"]),
            maker_first=bool(e["maker_first"]),
            maker_timeout_s=int(e["maker_timeout_s"]),
            min_price_vs_ref=Decimal(e["min_price_vs_ref"]),
            withdraw_threshold_usdg=Decimal(c["withdraw_threshold_usdg"]),
            solana_swap_mint=str(c["solana_swap_mint"]),
            solana_usdt_mint=str(c["solana_usdt_mint"]),
        )

    def plan_size(self, ladder_sizes: list[Decimal]) -> Decimal:
        """单笔尺寸 = min(单笔上限, 阶梯上仍便宜的最大档)。

        阶梯本来就是带着数量问价的：买 1 U 就推动价格的名字，最多买 1 U。
        """
        if not ladder_sizes:
            return Decimal(0)
        return min(self.per_trade_quote_usdt, max(ladder_sizes))


@dataclass
class BuyPlan:
    mint: str
    size_usd: Decimal
    in_usd: Decimal
    out_tokens: Decimal
    share: Decimal
    ref: Decimal
    impact_pct: Decimal
    risk: str
    net_usdt: Decimal
    route: str
    swap_tx: str | None = None
    simulation: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.simulation.get("ok"))


def classify_risk(impact_pct: Decimal) -> str:
    """与看板同一套分档（<2% 低、2–5% 中、≥5% 高）。"""
    if impact_pct >= 5:
        return "高"
    if impact_pct >= 2:
        return "中"
    return "低"


def decide(policy: Policy, *, size_usd: Decimal, share: Decimal, ref: Decimal,
           impact_pct: Decimal, exit_cost_bps: Decimal = Decimal("10"),
           fixed_fee_usd: Decimal = Decimal("0")) -> tuple[bool, str]:
    """买入前的两道闸门。返回 (是否放行, 原因)。"""
    risk = classify_risk(impact_pct)
    if risk != policy.max_risk:
        return False, f"风险 {risk} 高于 {policy.max_risk}"
    if share <= 0:
        return False, "成交价无效"
    tokens = size_usd / share
    sell_price = ref * (Decimal(1) - exit_cost_bps / Decimal(10000))
    net = tokens * (sell_price - share) - fixed_fee_usd
    if net <= policy.min_net_profit_usdt:
        return False, f"净利 {net:.4f} ≤ {policy.min_net_profit_usdt}"
    return True, f"net {net:.4f} @ {size_usd}U"


def parse_quote(payload: object, *, swap_mint_decimals: int, token_decimals: int
                ) -> tuple[Decimal, Decimal, str, Decimal]:
    """Jupiter quote → (花的稳定币, 拿到的枚数, 路由, 价格冲击%)。"""
    try:
        spent = Decimal(str(payload["inAmount"])) / Decimal(10) ** swap_mint_decimals
        got = Decimal(str(payload["outAmount"])) / Decimal(10) ** token_decimals
        impact = Decimal(str(payload.get("priceImpactPct") or "0")) * 100
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"报价字段缺失：{exc}") from exc
    labels: list[str] = []
    for step in payload.get("routePlan") or []:
        label = str((step.get("swapInfo") or {}).get("label") or "")
        if label and label not in labels:
            labels.append(label)
    return spent, got, " → ".join(labels[:3]) or "Jupiter", impact


def quote_url(input_mint: str, output_mint: str, amount_raw: int,
              slippage_bps: int) -> str:
    query = urllib.parse.urlencode({
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": amount_raw, "slippageBps": slippage_bps,
    })
    return f"{JUP_QUOTE}?{query}"


def simulate(tx_b64: str, rpc_url: str = SOLANA_RPC, *, transport=None) -> dict:
    """链上模拟（不签名、不广播）。transport 可注入，测试不碰网络。"""
    body = {"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
            "params": [tx_b64, {"encoding": "base64", "replaceRecentBlockhash": True}]}
    if transport is None:
        import requests

        response = requests.post(rpc_url, json=body, timeout=40)
        payload = response.json()
    else:
        payload = transport(rpc_url, body)
    result = payload.get("result") or {}
    value = result.get("value") or {}
    return {
        "ok": not value.get("err"),
        "err": value.get("err"),
        "units_consumed": value.get("unitsConsumed"),
        "logs": (value.get("logs") or [])[-5:],
    }


def dumps(plan: BuyPlan) -> str:
    return json.dumps({
        "mint": plan.mint, "size_usd": str(plan.size_usd), "in_usd": str(plan.in_usd),
        "out_tokens": str(plan.out_tokens), "share": str(plan.share), "ref": str(plan.ref),
        "impact_pct": str(plan.impact_pct), "risk": plan.risk,
        "net_usdt": str(plan.net_usdt), "route": plan.route, "ok": plan.ok,
        "simulation": plan.simulation,
    }, ensure_ascii=False)
