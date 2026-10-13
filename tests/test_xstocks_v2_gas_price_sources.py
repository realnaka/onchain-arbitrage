"""Gas prices: where they come from, and who refreshes them.

Two measured failures are pinned here:

* **2026-10-07** -- the aggregator quote is balance-gated: after a swap left
  0.0003 WOKB, every ``WOKB -> USDG`` quote answered ``no_route``, the OKB price
  went stale, and every x-layer bridge silently became ``cost_unknown``. The
  replacement sources are an ``eth_call`` to the pool (OKB) and Jupiter's public
  price API (SOL); neither can see our wallet.
* **2026-10-09** -- the refresh was a manual script nobody called on a timer, so
  both prices sat 19.7 hours old and five board rows printed
  "等：bridge_fee_usd" all night. The scan refreshes them itself now (A1), and
  a refresh that fails leaves the old row alone instead of inventing a number.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import unittest.mock
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import bridge_fees
from arb_monitor.xstocks_v2 import gas_prices


def _script():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2fees", root / "scripts" / "xstocks_v2_bridge_fees.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Quote:
    def __init__(self, price):
        self.price_usd = price


class OkbPoolPriceTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple] = []

    def _stub(self, prices_by_fee):
        """``prices_by_fee[fee][sum(usdg)] -> price`` or an error marker."""

        def fake(http, chain, pools, notional, *, recv_ms):
            pool = pools[0]
            self.calls.append((pool["fee"], str(notional)))
            table = prices_by_fee.get(pool["fee"])
            if table is None:
                return [{"errors": ["buy:execution reverted"], "buy": None}]
            price = table.get(str(notional))
            if price is None:
                return [{"errors": ["buy:execution reverted"], "buy": None}]
            return [{"errors": [], "buy": _Quote(price)}]

        return fake

    def test_two_sizes_that_agree_become_a_price(self):
        fee = D("0.0005")
        value, source = gas_prices.price_okb_from_pool(
            None, quote_fn=self._stub({fee: {"1": D("130.9"), "10": D("131.0")}}))
        self.assertEqual(value, D("131.0"))
        self.assertEqual(source, f"evm-quoter:usdg-wokb-{fee}")
        self.assertEqual([c[1] for c in self.calls], ["1", "10"])

    def test_a_reverting_pool_falls_through_to_the_next_fee_tier(self):
        deep, thin = D("0.0005"), D("0.0001")
        value, source = gas_prices.price_okb_from_pool(
            None, quote_fn=self._stub({deep: None, thin: {"1": D("131"),
                                                          "10": D("131")}}))
        self.assertEqual(value, D("131"))
        self.assertIn("0.0001", source)

    def test_sizes_that_disagree_are_not_a_price(self):
        fee = D("0.0005")
        value, reason = gas_prices.price_okb_from_pool(
            None, quote_fn=self._stub({fee: {"1": D("100"), "10": D("200")}}))
        self.assertIsNone(value)
        self.assertIn("disagree", reason)

    def test_the_pool_fee_is_a_fraction_not_the_contract_integer(self):
        """100 as a fee would encode 100_000_000 and revert (measured)."""
        for fee in gas_prices.USDG_WOKB_FEES:
            self.assertLess(fee, D("1"), fee)

    def test_the_manual_script_still_reads_the_same_sources(self):
        """The script is a viewer now: one implementation, two entry points."""
        script = _script()
        script.http_client = lambda: "fake-http"
        seen = {}

        def fake(http, *, quote_fn=None):
            seen["http"] = http
            return D("131"), "evm-quoter:usdg-wokb-0.0005"

        # ``patch.object``, not an assignment: ``script.gas_prices`` *is* the
        # module this file's other tests use, and the leak showed up as an empty
        # call list two tests later.
        with unittest.mock.patch.object(script.gas_prices,
                                        "price_okb_from_pool", fake):
            value, source = script.price_okb_from_pool()
        self.assertEqual((value, source), (D("131"), "evm-quoter:usdg-wokb-0.0005"))
        self.assertEqual(seen["http"], "fake-http")


class SolPriceTest(unittest.TestCase):
    def _http(self, status, payload):
        class _H:
            def get_json(self, url, **kw):
                self.url = url
                return status, payload, {}

        return _H()

    def test_usd_price_field_is_read(self):
        value, source = gas_prices.price_sol_from_jupiter(self._http(
            200, {gas_prices.WSOL_SOLANA: {"usdPrice": "116.4678"}}))
        self.assertEqual(value, D("116.4678"))
        self.assertEqual(source, "jupiter-price-v3")

    def test_a_non_200_is_unknown_not_zero(self):
        value, reason = gas_prices.price_sol_from_jupiter(self._http(429, None))
        self.assertIsNone(value)
        self.assertIn("429", reason)

    def test_a_200_without_a_price_is_unknown(self):
        value, reason = gas_prices.price_sol_from_jupiter(self._http(200, {}))
        self.assertIsNone(value)
        self.assertEqual(reason, "jupiter:no_price")


class RefreshRoundTest(unittest.TestCase):
    """A1: the round refreshes what is stale, and never invents a number."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / bridge_fees.PRICES_NAME
        self.now = 1_791_371_170_000

    def _write(self, prices):
        self.path.write_text(json.dumps({"prices": prices}))

    def _prices(self):
        return json.loads(self.path.read_text())["prices"]

    def test_a_stale_price_is_refreshed_and_written_with_a_new_timestamp(self):
        self._write({"OKB": {"usd": "129.4153", "source": "old",
                             "as_of_ms": self.now - 20 * 3600 * 1000}})
        calls: list[str] = []

        def refresh_fn(data_dir, *, now_ms, http=None, need=None):
            calls.append(list(need))
            return {"refreshed": {"OKB": {"usd": "131.09",
                                          "source": "evm-quoter:usdg-wokb-0.0005",
                                          "as_of_ms": now_ms}},
                    "failed": {}}

        result = gas_prices.ensure_fresh(self.dir, now_ms=self.now,
                                         refresh_fn=refresh_fn)
        # SOL was never priced at all, so it is stale too -- both are asked for.
        self.assertEqual(calls, [["OKB", "SOL"]])
        self.assertEqual(result["refreshed"], ["OKB"])
        # ``refresh_fn`` replaces the whole write, so only the report is asserted
        # here; the write itself is covered by the real ``refresh`` below.
        self.assertEqual(self._prices()["OKB"]["usd"], "129.4153")

    def test_a_fresh_price_is_not_refreshed_at_all(self):
        self._write({"OKB": {"usd": "131", "source": "x", "as_of_ms": self.now},
                     "SOL": {"usd": "116", "source": "y", "as_of_ms": self.now}})
        calls: list[str] = []

        def refresh_fn(data_dir, **kwargs):
            calls.append("called")
            return {"refreshed": {}, "failed": {}}

        result = gas_prices.ensure_fresh(self.dir, now_ms=self.now,
                                         refresh_fn=refresh_fn)
        self.assertEqual(calls, [])
        self.assertEqual(result["stale"], [])
        self.assertEqual(result["refreshed"], [])

    def test_a_token_that_was_never_priced_is_asked_for(self):
        """``missing`` only knew the bridge-fee tokens: ETH slipped through.

        2026-10-09: the scan asked for (OKB, SOL, ETH), but ``price_report``
        builds its ``missing`` list from ``bridge_fees.feeds_needed()`` -- so a
        token absent from the file was neither stale nor missing, the refresh
        never ran, and every eth row's gas line read ``gas_unknown``.
        """
        self._write({"OKB": {"usd": "131", "source": "x", "as_of_ms": self.now},
                     "SOL": {"usd": "116", "source": "y", "as_of_ms": self.now}})
        calls: list[list[str]] = []

        def refresh_fn(data_dir, *, now_ms, http=None, need=None):
            calls.append(sorted(need))
            return {"refreshed": {}, "failed": {}}

        result = gas_prices.ensure_fresh(self.dir, now_ms=self.now,
                                         need=("OKB", "SOL", "ETH"),
                                         refresh_fn=refresh_fn)
        self.assertEqual(calls, [["ETH"]])
        self.assertEqual(result["stale"], ["ETH"])

    def test_a_failed_refresh_leaves_the_old_row_alone(self):
        """The old row keeps its old timestamp, so it keeps failing its age."""
        self._write({"OKB": {"usd": "129.4153", "source": "old",
                             "as_of_ms": self.now - 20 * 3600 * 1000}})

        class _Boom:
            def get_json(self, url, **kw):
                raise RuntimeError("no route")

        result = gas_prices.refresh(self.dir, now_ms=self.now, http=_Boom(),
                                    need=["OKB", "SOL"],
                                    sources={"OKB": lambda http, **kw: (
                                        None, "quoter:no_pool")})
        self.assertEqual(result["refreshed"], {})
        self.assertEqual(sorted(result["failed"]), ["OKB", "SOL"])
        self.assertEqual(self._prices()["OKB"]["usd"], "129.4153")
        self.assertEqual(self._prices()["OKB"]["as_of_ms"],
                         self.now - 20 * 3600 * 1000)
        report = bridge_fees.price_report(data_dir=self.dir, now_ms=self.now)
        self.assertIn("OKB", report["stale"])

    def test_a_successful_refresh_adds_the_missing_asset_without_losing_the_other(self):
        self._write({"SOL": {"usd": "116.47", "source": "jupiter-price-v3",
                             "as_of_ms": self.now - 20 * 3600 * 1000}})

        result = gas_prices.refresh(self.dir, now_ms=self.now, http=object(),
                                    need=["OKB"],
                                    sources={"OKB": lambda http, **kw: (
                                        D("131.0918"),
                                        "evm-quoter:usdg-wokb-0.0005")})
        self.assertEqual(sorted(result["refreshed"]), ["OKB"])
        self.assertEqual(self._prices()["OKB"]["usd"], "131.0918")
        self.assertEqual(self._prices()["OKB"]["as_of_ms"], self.now)
        # The asset we did not ask about is untouched, timestamp included.
        self.assertEqual(self._prices()["SOL"]["as_of_ms"],
                         self.now - 20 * 3600 * 1000)

    def test_an_unpriceable_asset_is_reported_not_invented(self):
        result = gas_prices.refresh(self.dir, now_ms=self.now, http=object(),
                                    need=["XYZ"])
        self.assertEqual(result["failed"], {"XYZ": "no_source"})
        self.assertFalse(self.path.exists())


class PriceReportTest(unittest.TestCase):
    """A stale gas price has to be visible, not silently `cost_unknown`."""

    def test_a_stale_price_is_named_with_its_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, bridge_fees.PRICES_NAME).write_text(json.dumps(
                {"prices": {"OKB": {"usd": "131", "source": "evm-quoter",
                                    "as_of_ms": 1_000_000}}}))
            now = 1_000_000 + 4 * 60 * 60 * 1000      # 4 h later, limit is 3 h
            report = bridge_fees.price_report(data_dir=tmp, now_ms=now)
        self.assertEqual(report["stale"], ["OKB"])
        self.assertTrue(report["prices"]["OKB"]["stale"])
        self.assertEqual(report["prices"]["OKB"]["source"], "evm-quoter")
        self.assertIn("SOL", report["missing"])       # never priced at all

    def test_a_fresh_price_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, bridge_fees.PRICES_NAME).write_text(json.dumps(
                {"prices": {"OKB": {"usd": "131", "source": "evm-quoter",
                                    "as_of_ms": 1_000_000},
                            "SOL": {"usd": "116", "source": "jupiter",
                                    "as_of_ms": 1_000_000}}}))
            report = bridge_fees.price_report(data_dir=tmp, now_ms=1_000_000)
        self.assertEqual(report["stale"], [])
        self.assertEqual(report["missing"], [])


if __name__ == "__main__":
    unittest.main()
