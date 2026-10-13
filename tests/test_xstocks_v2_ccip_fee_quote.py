"""The bridge fee read off the chain, per edge per round (A3).

Measured on X Layer 2026-10-09 with a real ``send`` calldata, a stubbed token
and ``value = 0``: the bridge answers

    InsufficientFee(uint256 provided, uint256 required)   # 0x785fa129
    provided = 0
    required = 802441992381060                            # wei of OKB

and the same call at exactly ``required`` returns the message id. Two facts came
out of the same probe: the fee does not depend on the token (SPYx and NVDAx) or
on the amount (0.001 / 0.01 / 1.0 tokens) -- it is per message, per lane.

The file ``xstocks_v2_bridge_fees.json`` still holds the receipts, and it is
still the answer when the chain cannot be asked. It is no longer the only
source: it is 1.4% behind the chain today.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import bridge_fees as BF
from arb_monitor.xstocks_v2 import ccip_evm
from arb_monitor.xstocks_v2 import live_fees

RPC = "https://rpc.xlayer.tech"
SENDER = "0xabababababababababababababababababababab"
REQUIRED = 802441992381060
PROVIDED = 0


def _word(value: int) -> str:
    return format(value, "064x")


def fee_revert(provided: int = PROVIDED, required: int = REQUIRED) -> str:
    return "0x" + ccip_evm.FEE_REVERT_SELECTOR + _word(provided) + _word(required)


class _Http:
    """One canned JSON-RPC answer, and the payload it was asked with."""

    def __init__(self, body: dict, status: int = 200):
        self.status = status
        self.body = body
        self.payload = None

    def request(self, url, *, data=None, headers=None):
        self.payload = json.loads((data or b"[]").decode())
        return self.status, json.dumps([self.body]).encode()


class FeeRevertDecodeTest(unittest.TestCase):
    def test_the_required_value_is_read_out_of_the_revert(self):
        self.assertEqual(ccip_evm.decode_fee_revert(fee_revert()),
                         (PROVIDED, REQUIRED))

    def test_a_partial_value_is_kept(self):
        self.assertEqual(ccip_evm.decode_fee_revert(fee_revert(provided=791)),
                         (791, REQUIRED))

    def test_another_error_is_not_a_fee(self):
        """An ``Error(string)`` revert is a different problem, not a number."""
        self.assertIsNone(ccip_evm.decode_fee_revert(
            "0x08c379a0" + _word(0) + _word(32) + _word(0)))
        self.assertIsNone(ccip_evm.decode_fee_revert("0x"))
        self.assertIsNone(ccip_evm.decode_fee_revert(None))

    def test_a_truncated_payload_is_not_a_fee(self):
        self.assertIsNone(ccip_evm.decode_fee_revert(
            "0x" + ccip_evm.FEE_REVERT_SELECTOR + _word(REQUIRED)))


class EVMEdgeIsQuotableTest(unittest.TestCase):
    """The EVM→EVM lane can be priced every round now, not left ``cost_unknown``.

    Measured 2026-10-09 on X Layer: the BSC lane asked **0.001114908880089956 OKB**,
    40% more than the Solana lane's 0.000797, and it is the same
    ``InsufficientFee`` revert that carries the number. Two things had to exist
    first -- a destination selector for BSC and the EVM dialect -- and both do.
    """

    def test_the_bsc_lane_is_quoted_like_the_solana_one(self):
        from arb_monitor.xstocks_v2 import live_fees
        self.assertIn(("x-layer", "solana"), live_fees.QUOTABLE_EDGES)
        self.assertIn(("x-layer", "bsc"), live_fees.QUOTABLE_EDGES)

    def test_every_quotable_lane_has_a_destination_selector(self):
        """A lane with no selector cannot be priced -- that is what BSC was."""
        from arb_monitor.xstocks_v2 import ccip_evm, live_fees
        for _source, dest in live_fees.QUOTABLE_EDGES:
            with self.subTest(dest=dest):
                self.assertIn(dest, ccip_evm.DEST_SELECTORS)


class FeeQuoteTest(unittest.TestCase):
    def test_the_chain_answer_becomes_the_fee_in_native_units(self):
        http = _Http({"jsonrpc": "2.0", "id": 1,
                      "error": {"code": 3, "message": "execution reverted",
                                "data": fee_revert()}})
        fee, source = ccip_evm.fee_quote(http=http, rpc=RPC, source_chain="x-layer",
                                         sender=SENDER)
        self.assertEqual(fee, D(REQUIRED) / D(10 ** 18))
        self.assertEqual(source, "ccip-dryrun:x-layer->solana")

    def test_the_quote_never_carries_value_and_stubs_the_token(self):
        """The read must not be able to spend, and must not need an allowance."""
        http = _Http({"error": {"data": fee_revert()}})
        ccip_evm.fee_quote(http=http, rpc=RPC, source_chain="x-layer",
                           sender=SENDER)
        call = http.payload[0]["params"]
        self.assertEqual(call[0]["value"], "0x0")
        self.assertEqual(call[0]["to"], ccip_evm.BRIDGE_CONTRACT)
        overrides = call[2]
        self.assertEqual(overrides[SENDER]["balance"], hex(10 ** 18))
        self.assertEqual(overrides[ccip_evm.FEE_QUOTE_TOKEN]["code"],
                         ccip_evm.FEE_QUOTE_STUB)

    def test_a_different_revert_is_unknown_not_zero(self):
        http = _Http({"error": {"message": "execution reverted: ERC20: nope",
                                "data": "0xdeadbeef"}})
        fee, reason = ccip_evm.fee_quote(http=http, rpc=RPC,
                                         source_chain="x-layer", sender=SENDER)
        self.assertIsNone(fee)
        self.assertIn("dry_run:", reason)

    def test_a_call_that_succeeds_at_zero_value_is_unknown(self):
        """If value 0 goes through we did not read the fee we measured."""
        http = _Http({"result": "0x" + "11" * 32})
        fee, reason = ccip_evm.fee_quote(http=http, rpc=RPC,
                                         source_chain="x-layer", sender=SENDER)
        self.assertIsNone(fee)
        self.assertEqual(reason, "dry_run_ok_at_zero_value")

    def test_an_unquotable_edge_says_which_part_is_missing(self):
        http = _Http({"error": {"data": fee_revert()}})
        fee, reason = ccip_evm.fee_quote(http=http, rpc=RPC, source_chain="mantle",
                                         sender=SENDER)
        self.assertIsNone(fee)
        self.assertEqual(reason, "no_fee_asset:mantle")
        fee, reason = ccip_evm.fee_quote(http=http, rpc=RPC, source_chain="x-layer",
                                         sender=SENDER, dest_chain="ton")
        self.assertIsNone(fee)
        self.assertEqual(reason, "no_dest_selector:ton")

    def test_a_transport_failure_is_unknown(self):
        class _Boom:
            def request(self, url, **kwargs):
                raise RuntimeError("no route")

        fee, reason = ccip_evm.fee_quote(http=_Boom(), rpc=RPC,
                                         source_chain="x-layer", sender=SENDER)
        self.assertIsNone(fee)
        self.assertEqual(reason, "rpc:RuntimeError")

    def test_a_non_200_is_unknown(self):
        http = _Http({"error": {"data": fee_revert()}}, status=502)
        fee, reason = ccip_evm.fee_quote(http=http, rpc=RPC,
                                         source_chain="x-layer", sender=SENDER)
        self.assertIsNone(fee)
        self.assertEqual(reason, "rpc:http_502")


class LiveFeeTableTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = 1_791_371_170_000
        (self.dir / BF.FEES_NAME).write_text(json.dumps({"edges": {
            "x-layer->solana": {"fee_asset": "OKB",
                                "fee_amount": "0.000791242725185203",
                                "samples": 2, "evidence": ["0xaa"],
                                "measured_ms": self.now}}}))
        (self.dir / BF.PRICES_NAME).write_text(json.dumps({"prices": {
            "OKB": {"usd": "129.4153", "source": "test",
                    "as_of_ms": self.now}}}))

    def test_the_live_fee_replaces_the_receipt(self):
        live = {("x-layer", "solana"): BF.live_row(
            fee_amount=D("0.00080244199238106"), fee_asset="OKB",
            source="ccip-dryrun:x-layer->solana", now_ms=self.now)}
        table, unknown = BF.load_table(data_dir=self.dir, now_ms=self.now,
                                       live_fees=live)
        self.assertEqual(unknown, {})
        self.assertAlmostEqual(float(table[("x-layer", "solana")]),
                               float(D("0.00080244199238106") * D("129.4153")),
                               places=8)

    def test_without_the_live_fee_the_receipt_still_answers(self):
        table, unknown = BF.load_table(data_dir=self.dir, now_ms=self.now)
        self.assertEqual(unknown, {})
        self.assertAlmostEqual(float(table[("x-layer", "solana")]),
                               float(D("0.000791242725185203") * D("129.4153")),
                               places=8)

    def test_a_stale_price_refuses_both_of_them(self):
        (self.dir / BF.PRICES_NAME).write_text(json.dumps({"prices": {
            "OKB": {"usd": "129.4153", "source": "test",
                    "as_of_ms": self.now - 1000}}}))
        live = {("x-layer", "solana"): BF.live_row(
            fee_amount=D("0.00080244199238106"), fee_asset="OKB",
            source="ccip-dryrun", now_ms=self.now)}
        table, unknown = BF.load_table(data_dir=self.dir, now_ms=self.now,
                                       live_fees=live,
                                       max_price_age_ms=0)
        self.assertEqual(table, {})
        self.assertEqual(unknown, {"x-layer->solana": "gas_price_stale"})

    def test_a_quote_is_not_an_arrival_sample(self):
        row = BF.live_row(fee_amount=D("0.1"), fee_asset="okb",
                          source="ccip-dryrun", now_ms=self.now)
        self.assertEqual(row["samples"], 0)
        self.assertEqual(row["fee_asset"], "OKB")
        self.assertEqual(row["evidence"], ["ccip-dryrun"])

    def test_quote_edges_records_what_the_chain_answered(self):
        seen = {}

        def fake(*, http, rpc, source_chain, sender, dest_chain="solana"):
            seen["rpc"] = rpc
            seen["chain"] = source_chain
            return D("0.00080244199238106"), "ccip-dryrun:x-layer->solana"

        rows, _ = live_fees.quote_edges(http=object(), quote_fn=fake, now_ms=self.now)
        # The fake answers for every edge it is asked about, so every quotable
        # edge comes back (eth-source edges joined 2026-10-09 21:1x).
        self.assertEqual(list(rows), list(live_fees.QUOTABLE_EDGES))
        rows, notes = live_fees.quote_edges(
            http=object(), quote_fn=fake, now_ms=self.now,
            edges=(("x-layer", "solana"), ("x-layer", "bsc")))
        self.assertEqual(list(rows), [("x-layer", "solana"), ("x-layer", "bsc")])
        self.assertEqual(notes["x-layer->solana"]["ok"], True)
        self.assertEqual(notes["x-layer->solana"]["fee_amount"],
                         "0.00080244199238106")
        self.assertIn("xlayer", seen["rpc"])
        self.assertEqual(seen["chain"], "x-layer")

    def test_an_edge_the_chain_did_not_answer_is_simply_absent(self):
        def fake(**_kwargs):
            return None, "rpc:http_502"

        rows, notes = live_fees.quote_edges(http=object(), quote_fn=fake,
                                            now_ms=self.now)
        self.assertEqual(rows, {})
        self.assertEqual(notes["x-layer->solana"],
                         {"ok": False, "reason": "rpc:http_502"})

    def test_the_receipt_is_the_cross_check(self):
        live = {("x-layer", "solana"): BF.live_row(
            fee_amount=D("0.00080244199238106"), fee_asset="OKB",
            source="ccip-dryrun", now_ms=self.now)}
        measured = {("x-layer", "solana"): {"fee_asset": "OKB",
                                            "fee_amount": D("0.000791242725185203")}}
        out = live_fees.compare_with_evidence(live, measured)
        self.assertIn("x-layer->solana", out)
        self.assertAlmostEqual(float(out["x-layer->solana"]["mismatch_bps"]),
                               141.5, places=1)


if __name__ == "__main__":
    unittest.main()
