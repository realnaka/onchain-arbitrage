"""Bridge fees read from the source chain, once per edge per round (A3).

The measured table (``xstocks_v2_bridge_fees.json``) is a receipt: it proves a
fee that was really paid, and it is what the fee was on that day. It is not a
price. Measured 2026-10-09: the chain asked for ``0.00080244199238106 OKB`` on
x-layer->solana while the file still said ``0.000791242725185203`` -- 1.4% low,
because the lane's fee moves with the destination gas price.

So the scan asks the chain every round (``ccip_evm.fee_quote``: one ``eth_call``
at ``value = 0``, and the bridge's own ``InsufficientFee(provided, required)``
revert carries the number). The file keeps two jobs: it is the answer when the
chain cannot be asked, and it is the thing we cross-check the live number
against -- a disagreement is printed, never averaged away.
"""
from __future__ import annotations

from decimal import Decimal

from arb_monitor.xstocks_v2 import bridge_fees as BF
from arb_monitor.xstocks_v2 import ccip_evm
from arb_monitor.xstocks_v2 import routes
from arb_monitor.xstocks_v2.feeds import evm as evm_feed

#: Edges we can quote from a chain right now: the source chain has an EVM RPC,
#: the destination selector is known, and the fee is paid in a gas token we can
#: price. Widen this when another source chain's gas token gets a USD price
#: (today only OKB and SOL have one).
#: Edges whose fee can be read off the source chain this way. ``x-layer->bsc``
#: joined on 2026-10-09, when the EVM→EVM dialect got its first real message:
#: the lane asked **0.001114908880089956 OKB**, 40% more than the Solana lane,
#: and it is the same ``InsufficientFee`` revert that carries the number.
#: The x-layer->eth/arbitrum/ink and eth->* edges joined on 2026-10-09 21:1x,
#: after each one carried a real minimum send that arrived
#: (``data-v2/xstocks_v2_constructor_proof.json``). The X Layer sends paid the
#: fee in WOKB, not ``msg.value``, so the tx value cannot be the receipt; the
#: per-round read is.
def _quotable_edges() -> tuple[tuple[str, str], ...]:
    """Every bridge whose source fee we can price. Optimism → Solana is out:
    its slot 0 has not been read, so the edge cannot be sent."""
    edges = []
    for source in ccip_evm.FEE_SOURCE_ASSET:
        for dest in ccip_evm.DEST_SELECTORS:
            if source == dest or not routes.has_bridge(source, dest):
                continue
            if dest == "solana" and source not in ccip_evm.SOURCE_SLOT0:
                continue
            edges.append((source, dest))
    return tuple(edges)


QUOTABLE_EDGES = _quotable_edges()
#: Which gas token pays the fee on each source chain.
NATIVE_ASSET = dict(ccip_evm.FEE_SOURCE_ASSET)
#: Our X Layer wallet. Only a ``from`` for the read; the call carries no value.
DEFAULT_SENDER = "0xabababababababababababababababababababab"


def quote_edges(*, http, edges=None, sender: str = DEFAULT_SENDER,
                quote_fn=None, now_ms: int | None = None) -> tuple[dict, dict]:
    """``(live_fees, notes)`` for every quotable edge.

    ``live_fees`` has the same shape as ``bridge_fees.load_fees`` so the two can
    be merged. ``notes`` is keyed by ``"source->dest"`` and says, per edge,
    whether the chain answered and with what. An edge the chain did not answer is
    **absent** from ``live_fees`` -- never present with a stand-in number.
    """
    quote = quote_fn or ccip_evm.fee_quote
    rows: dict[tuple[str, str], dict] = {}
    notes: dict[str, dict] = {}
    for source, dest in (edges or QUOTABLE_EDGES):
        key = f"{source}->{dest}"
        asset = NATIVE_ASSET.get(source)
        chain = evm_feed.CHAINS.get(source)
        if asset is None or chain is None:
            notes[key] = {"ok": False, "reason": f"no_asset_or_rpc:{source}"}
            continue
        try:
            fee, tag = quote(http=http, rpc=chain.rpc, source_chain=source,
                             sender=sender, dest_chain=dest)
        except Exception as exc:  # noqa: BLE001 - a read that does not answer
            notes[key] = {"ok": False, "reason": f"quote:{type(exc).__name__}"}
            continue
        if fee is None or Decimal(str(fee)) <= 0:
            notes[key] = {"ok": False, "reason": str(tag)}
            continue
        rows[(source, dest)] = BF.live_row(fee_amount=fee, fee_asset=asset,
                                           source=tag, now_ms=now_ms)
        notes[key] = {"ok": True, "source": tag,
                      "fee_amount": format(Decimal(str(fee)), "f"),
                      "fee_asset": asset}
    return rows, notes


def compare_with_evidence(live: dict, measured: dict) -> dict[str, dict]:
    """``edge -> {live, measured, mismatch_bps}`` where both exist.

    A live fee that disagrees with the receipt is not an error by itself (the
    fee moves), but a *large* disagreement is worth a human's eye, so it is
    printed rather than swallowed.
    """
    out: dict[str, dict] = {}
    for edge, row in live.items():
        evidence = measured.get(edge)
        if evidence is None:
            continue
        live_amount = Decimal(str(row["fee_amount"]))
        measured_amount = Decimal(str(evidence["fee_amount"]))
        if measured_amount == 0:
            continue
        mismatch = (live_amount - measured_amount) / measured_amount * Decimal(10000)
        out[f"{edge[0]}->{edge[1]}"] = {
            "live": format(live_amount, "f"),
            "measured": format(measured_amount, "f"),
            "mismatch_bps": format(mismatch, "f"),
        }
    return out
