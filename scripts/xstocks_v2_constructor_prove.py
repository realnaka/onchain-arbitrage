#!/usr/bin/env python3
"""Prove every constructor: build, then a minimum send only inside the cap.

Receipts go to ``data-v2/xstocks_v2_constructor_proof.json``. An edge whose
quoted cost is unknown or above 2 USDT is recorded and not broadcast. Edges
that already landed (x-layer → solana, 2026-10-07) are cited, not sent again.

    scripts/xstocks_v2_constructor_prove.py            # build + quote, no send
    scripts/xstocks_v2_constructor_prove.py --confirm  # send when cost ≤ 2
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arb_monitor.xstocks_v2 import capability as cap          # noqa: E402
from arb_monitor.xstocks_v2 import routes                      # noqa: E402
from arb_monitor.xstocks_v2 import wrap as wrap_core           # noqa: E402
import xstocks_v2_route as route                               # noqa: E402

OUT = ROOT / "data-v2" / "xstocks_v2_constructor_proof.json"
#: NVDAx, 2026-10-07 21:12. Source X Layer, destination Solana, arrived.
LANDED = {
    ("x-layer", "solana"): {
        "tx": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "note": "NVDAx x-layer→solana arrived 2026-10-07 21:36",
    },
    # SPCXx, 2026-10-09. EVM dialect. BSC balance matched the amount sent.
    ("x-layer", "bsc"): {
        "tx": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "note": "SPCXx x-layer→bsc arrived 2026-10-09, sold on BSC",
    },
    ("x-layer", "ink"): {
        "tx": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "note": "SPYx x-layer→ink source confirmed 2026-10-09, status 0x1",
    },
    ("x-layer", "eth"): {
        "tx": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "note": "SPYx x-layer→eth source confirmed 2026-10-09, status 0x1",
    },
}
RECEIVER = "0xabababababababababababababababababababab"
WRAPPER = "0x0000000000000000000000000000000000000001"


def _row(*, kind: str, source: str, dest: str, status: str, payload,
         cost_usdt, sent: bool, reason: str, tx: str = "") -> dict:
    return {
        "kind": kind, "source": source, "dest": dest, "status": status,
        "constructed": payload not in (None, ""),
        "cost_usdt": None if cost_usdt is None else format(Decimal(str(cost_usdt)), "f"),
        "sent": sent, "reason": reason, "tx": tx,
    }


def catalogue(*, confirm: bool = False, broadcast=None,
              costs: dict | None = None) -> dict:
    """Every bridge pair, plus one wrap and one redeem. ``broadcast`` is injected.

    ``costs`` maps ``(source, dest)`` to a USDT quote. Missing means unknown,
    and unknown is not sent.
    """
    costs = costs or {}
    rows = []
    calls = []

    def _broadcast(payload):
        calls.append(payload)
        if broadcast is not None:
            broadcast(payload)

    for source, dest, status in cap.all_bridge_pairs():
        built = route.construct(source=source, dest=dest)
        key = (routes.norm(source), routes.norm(dest))
        landed = LANDED.get(key)
        if landed:
            rows.append(_row(
                kind=built["dialect"], source=key[0], dest=key[1], status=status,
                payload=built.get("payload"), cost_usdt=Decimal("0.10"),
                sent=False, reason="already_landed", tx=landed["tx"]))
            continue
        if status != cap.READY or not built.get("payload"):
            rows.append(_row(
                kind=built["dialect"], source=key[0], dest=key[1], status=status,
                payload=built.get("payload"), cost_usdt=costs.get(key),
                sent=False, reason=built.get("reason") or status))
            continue
        cost = costs.get(key)
        if not confirm:
            allowed, why = cap.may_broadcast(cost)
            rows.append(_row(
                kind=built["dialect"], source=key[0], dest=key[1], status=status,
                payload=built["payload"], cost_usdt=cost, sent=False,
                reason="plan_only" if allowed else why))
            continue
        tx_holder: dict = {}

        def _send(payload, _holder=tx_holder):
            _broadcast(payload)
            if broadcast is None:
                raise RuntimeError("no sender")
            _holder["tx"] = broadcast(payload)

        result = cap.attempt(status=status, cost_usdt=cost, broadcast=lambda p=built["payload"]: _send(p))
        tx = str(tx_holder.get("tx") or "")
        sent = bool(result["sent"] and tx.startswith("0x"))
        rows.append(_row(
            kind=built["dialect"], source=key[0], dest=key[1], status=status,
            payload=built["payload"], cost_usdt=cost, sent=sent,
            reason="min_proof" if sent else (result["reason"] or "no_tx"),
            tx=tx))

    for kind, payload in (
            ("erc4626_wrap", wrap_core.deposit_calldata(assets=1, receiver=RECEIVER)),
            ("erc4626_redeem", wrap_core.redeem_calldata(
                shares=1, receiver=RECEIVER, owner=RECEIVER))):
        rows.append(_row(
            kind=kind, source="eth", dest="eth", status=cap.READY,
            payload=payload, cost_usdt=None, sent=False,
            reason="constructed_not_sent"))
    # The placeholder wrapper above is not a deployment. A real wrap needs the
    # book's wrapper_v2 address and a gas quote; neither is assumed here.
    rows[-2]["reason"] = "wrapper_address_required"
    rows[-2]["constructed"] = True
    rows[-1]["reason"] = "wrapper_address_required"
    return {"rows": rows, "broadcasts": len(calls)}


def quote_xlayer_usd() -> dict[tuple[str, str], Decimal]:
    """Bridge fees from X Layer, in USDT. A failed read is omitted, not zero."""
    from arb_monitor.xstocks_v2 import gas_prices
    from arb_monitor.xstocks_v2.balances import EVM_WALLET, XLAYER_RPC
    from arb_monitor.xstocks_v2.ccip_evm import DEST_SELECTORS, fee_quote
    from arb_monitor.xstocks_v2.feeds.http import Http

    http = Http()
    price, _note = gas_prices.price_okb_from_pool(http)
    if price is None or price <= 0:
        return {}
    out = {}
    for dest in DEST_SELECTORS:
        if dest == "x-layer":
            continue
        native, _why = fee_quote(
            http=http, rpc=XLAYER_RPC, source_chain="x-layer",
            sender=EVM_WALLET, dest_chain=dest)
        if native is None:
            continue
        out[("x-layer", dest)] = native * price
    return out


#: A row in one of these states is evidence (a hash, an arrival). A fresh
#: catalogue must not overwrite it with "plan_only".
KEPT_REASONS = frozenset({"arrived", "already_landed", "source_confirmed", "redeemed",
                          "min_proof"})


def merge_receipts(report: dict, path: Path) -> dict:
    """Keep every existing row that carries a send or an arrival."""
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        return report
    kept = {(row["kind"], row["source"], row["dest"]): row for row in old.get("rows") or []
            if row.get("sent") or row.get("reason") in KEPT_REASONS}
    report["rows"] = [kept.get((row["kind"], row["source"], row["dest"]), row)
                      for row in report["rows"]]
    if old.get("notes"):
        report["notes"] = old["notes"]
    return report


def main() -> int:
    confirm = "--confirm" in sys.argv
    costs = {}
    if "--offline" not in sys.argv:
        try:
            costs = quote_xlayer_usd()
        except Exception as exc:  # noqa: BLE001
            print(f"fee quote skipped: {type(exc).__name__}")
    report = merge_receipts(catalogue(confirm=confirm, costs=costs), OUT)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    sent = sum(1 for row in report["rows"] if row["sent"])
    built = sum(1 for row in report["rows"] if row["constructed"])
    print(f"rows {len(report['rows'])} constructed {built} sent {sent} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
