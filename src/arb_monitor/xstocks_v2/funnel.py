"""Two-stage scan. Stage 1 is bilateral. Stage 2 is capped."""
from __future__ import annotations

from decimal import Decimal


def stage1(*, kind: str, kraken_bid, kraken_ask, chain_buy, chain_sell,
           same_pool: bool) -> dict:
    if same_pool:
        return {"ok": False, "reason": "same_pool"}
    if kind == "chain_to_kraken":
        if chain_buy is None or kraken_bid is None:
            return {"ok": False, "reason": "missing_side"}
        return {"ok": True, "reason": ""}
    if kind == "kraken_to_chain":
        if kraken_ask is None or chain_sell is None:
            return {"ok": False, "reason": "missing_side"}
        return {"ok": True, "reason": ""}
    if kind == "dex_to_dex":
        if chain_buy is None or chain_sell is None:
            return {"ok": False, "reason": "missing_side"}
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": "unknown_kind"}


def stage2(rows: list[dict], *, threshold_bps: Decimal, buffer_bps: Decimal,
           limit: int) -> list[dict]:
    """Keep names within ``buffer`` of the threshold, highest edge first."""
    floor = threshold_bps - buffer_bps
    picked = [
        row for row in rows
        if row.get("reason") != "same_pool" and Decimal(str(row["edge_bps"])) >= floor
    ]
    picked.sort(key=lambda row: Decimal(str(row["edge_bps"])), reverse=True)
    return picked[:limit]
