from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arb_monitor.apps.hk_monitor import load_config
from arb_monitor.core.evm import selector
from arb_monitor.data.xlayer_onchain import (
    TRANSFER_TOPIC, XLayerOnchainAdapter, best_pool_reference, counterparty_flows,
    decode_transfers, quote_single, sqrt_price_to_price, trade_print_from_receipt, vwap,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"

XIAOMI = "0x076cf393e701839fc7a5832d2c68aafa235682ae"
USDC = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
TSLA = "0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0"
TSLA_POOL = "0x02425e1d17d8f1dc95b861d5b1a1a11db8969604"
BUYER = "0x14e83b1116e8afe0434ace75b3a62a34b4fe5001"
ROUTER = "0x7c5bee2a8091c3ef39072f64f18fac913060aeaf"


def word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def transfer(token: str, sender: str, recipient: str, raw_value: int) -> dict:
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, "0x" + word(sender), "0x" + word(recipient)],
        "data": hex(raw_value),
        "blockNumber": "0x64",
    }


class SqrtPriceTests(unittest.TestCase):
    def test_known_xiaomi_usdc_pool_price(self):
        # slot0 value read from 0xdc7f...58417 (wXIAOx token0 / USDC token1).
        price = sqrt_price_to_price(145817780343793685746091, 18, 6, base_is_token0=True)
        self.assertAlmostEqual(float(price), 3.38736, places=4)

    def test_token_ordering_is_applied_not_guessed(self):
        forward = sqrt_price_to_price(145817780343793685746091, 18, 6, True)
        reverse = sqrt_price_to_price(145817780343793685746091, 6, 18, False)
        # Same raw slot0 value read as (token0 base) and as (token1 base): the
        # two prices are reciprocals, so a caller cannot read a pool backwards.
        self.assertAlmostEqual(float(forward * reverse), 1.0, places=10)
        self.assertAlmostEqual(float(reverse), 0.29521, places=5)

    def test_meituan_pool_is_read_the_right_way_up(self):
        # slot0 from 0x54e89e...0c7 (USDG token0 / wMEITx token1). Public
        # pool data quotes wMEITx at ~9.70 USDG; an inverted read gives ~1e-23.
        price = sqrt_price_to_price(
            int("000000000000000000000000000000000004e6d2a8fb6ea98fa7002b6850b140", 16),
            18, 6, base_is_token0=False)
        self.assertAlmostEqual(float(price), 9.69, places=1)

    def test_zero_sqrt_price_is_rejected(self):
        with self.assertRaises(ValueError):
            sqrt_price_to_price(0, 18, 6, True)


class TradePrintTests(unittest.TestCase):
    def test_buy_is_priced_from_the_real_counterparty_not_a_dust_hop(self):
        # Real shape: buyer pays 59.866055 USDG, receives 0.163741046122029721
        # TSLAx, through a router and a fee collector.
        receipt = {
            "from": BUYER, "to": ROUTER, "blockNumber": "0x64",
            "transactionHash": "0xabc",
            "logs": [
                transfer(USDG, BUYER, ROUTER, 59866055),
                transfer(USDG, ROUTER, TSLA_POOL, 59866055),
                transfer(TSLA, TSLA_POOL, ROUTER, 163741046122029721),
                transfer(TSLA, ROUTER, BUYER, 163741046122029721),
                transfer(USDG, BUYER, "0xff3af7532535b05005b436b8a2f68c34f3c019d8", 45000),
            ],
        }
        print_row = trade_print_from_receipt(receipt, TSLA, 18)
        self.assertEqual(print_row["side"], "buy")
        # 59.866055 pool leg + 0.045 fee leg paid by the same buyer.
        self.assertAlmostEqual(float(print_row["price_usd"]), 365.89, places=1)
        self.assertEqual(print_row["quote_symbols"], ["USDG"])
        prints = counterparty_flows(receipt, TSLA, 18)
        # The router nets to zero and the fee collector has no token leg, so
        # neither can be mistaken for the trade.
        self.assertNotIn(ROUTER, {p["counterparty"] for p in prints})
        self.assertEqual({p["counterparty"] for p in prints}, {BUYER, TSLA_POOL})

    def test_mint_or_burn_legs_produce_no_fake_price(self):
        zero = "0x" + "0" * 40
        receipt = {
            "from": ROUTER, "to": ROUTER, "blockNumber": "0x64", "transactionHash": "0xdef",
            "logs": [
                transfer(TSLA, zero, ROUTER, 10**18),
                transfer(TSLA, ROUTER, zero, 10**18),
            ],
        }
        self.assertIsNone(trade_print_from_receipt(receipt, TSLA, 18))

    def test_two_sided_prints_give_bid_ask_and_spread(self):
        prints = [
            {"side": "sell", "price_usd": D("364.53"), "quote_amount_usd": D("60"),
             "token_quantity": D("0.1646"), "quote_symbols": ["USDG"]},
            {"side": "buy", "price_usd": D("366.43"), "quote_amount_usd": D("60"),
             "token_quantity": D("0.1637"), "quote_symbols": ["USDG"]},
        ]
        result = vwap(prints)
        self.assertEqual(result["onchain_bid_usd"], D("364.53"))
        self.assertEqual(result["onchain_ask_usd"], D("366.43"))
        self.assertAlmostEqual(float(result["onchain_spread_bps"]), 51.86, places=1)

    def test_one_sided_window_is_not_reported_as_a_two_way_market(self):
        result = vwap([{"side": "buy", "price_usd": D("366.43"),
                        "quote_amount_usd": D("60"), "token_quantity": D("0.1637")}])
        self.assertNotIn("onchain_bid_usd", result)
        self.assertNotIn("onchain_spread_bps", result)

    def test_malformed_logs_are_dropped_not_guessed(self):
        rows = decode_transfers([
            {"topics": [TRANSFER_TOPIC], "data": "0x1", "address": USDG},
            {"topics": ["0xdead"], "data": "0x1", "address": USDG},
        ])
        self.assertEqual(rows, [])


class DepthFloorTests(unittest.TestCase):
    def test_empty_pool_is_never_a_price(self):
        empty = {"address": TSLA_POOL, "price_usd": D("344.36"), "depth_usd": D("0.15"),
                 "liquidity": 0, "fee": 500}
        self.assertIsNone(best_pool_reference([empty], D("1000")))

    def test_deepest_pool_above_floor_wins(self):
        pools = [
            {"address": "0x1", "price_usd": D("3.30"), "depth_usd": D("1200"), "liquidity": 5},
            {"address": "0x2", "price_usd": D("3.39"), "depth_usd": D("307000"), "liquidity": 9},
        ]
        self.assertEqual(best_pool_reference(pools, D("1000"))["address"], "0x2")

    def test_config_rejects_pool_for_a_different_token(self):
        broken = CONFIG.read_text().replace(
            f'base_address = "{XIAOMI}"', f'base_address = "{TSLA}"', 1)
        self.assertNotIn(f'base_address = "{XIAOMI}"', broken)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hk_monitor.toml"
            path.write_text(broken)
            with self.assertRaises(ValueError):
                load_config(str(path))

    def test_shipped_config_loads_with_every_defined_token(self):
        config = load_config(str(CONFIG))
        self.assertTrue(config["onchain"]["enabled"])
        self.assertTrue(all(token["pools"] for token in config["tokens"]))
        # Hong Kong is still the *adopted* scope, so it is the monitor's asset
        # list; auto-discovered HK tokens may add more.
        assets = config["monitor"]["assets"]
        self.assertEqual(assets[:6],
                         ["XIAOMI", "TENCENT", "POPMART", "MEITUAN", "KUAISHOU", "SHEIN"])
        defined = {token["asset"] for token in config["tokens"]}
        for us_name in ("AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL"):
            self.assertIn(us_name, defined)
            # 2026-09-22 (operator: "现在开始对这114个公司也进行监控"): the seven
            # reviewed US blocks are in the grid. They are hand-written, so they
            # are quoted every cycle; the other 107 US candidates arrive through
            # discovery and rotate (asserted in test_us_token_extension).
            self.assertIn(us_name, assets)
        # US is scanned *and* adopted, and adopt can never name a market that is
        # not scanned.
        self.assertEqual(config["onchain"]["token_scan_countries"], ["HK", "US"])
        self.assertEqual(config["onchain"]["token_adopt_markets"], ["HK", "US"])
        self.assertTrue(set(config["onchain"]["token_adopt_markets"])
                        <= set(config["onchain"]["token_scan_countries"]))

    def test_onchain_chain_must_match_token_chain(self):
        broken = CONFIG.read_text().replace('chain_id = "196"', 'chain_id = "1"', 1)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hk_monitor.toml"
            path.write_text(broken)
            with self.assertRaises(ValueError):
                load_config(str(path))


class FakeRPC:
    """Stands in for XLayerRPC so the scan logic is testable without network."""

    def __init__(self, receipts):
        self.receipts = receipts

    def block_number(self):
        return 1000

    def block(self, number):
        return {"number": hex(number), "timestamp": hex(1789218227 - (1000 - number))}

    def transfer_logs(self, token, from_block, to_block):
        return [{"transactionHash": h, "blockNumber": hex(999),
                 "topics": [TRANSFER_TOPIC], "data": "0x0", "address": token}
                for h in self.receipts]

    def batch(self, calls):
        return [self.receipts[call[1][0]] if call[0] == "eth_getTransactionReceipt" else None
                for call in calls]


class ScanFilterTests(unittest.TestCase):
    def test_dust_legs_cannot_become_the_onchain_ask(self):
        def swap(tx_hash, stable_raw, token_raw):
            return {
                "from": BUYER, "to": ROUTER, "blockNumber": "0x3e7",
                "transactionHash": tx_hash,
                "logs": [
                    transfer(USDG, BUYER, ROUTER, stable_raw),
                    transfer(TSLA, ROUTER, BUYER, token_raw),
                ],
            }

        dust = swap("0xdust", 1, 10**18)                     # 1 wei for 1 token
        real = swap("0xreal", 59866055, 163741046122029721)   # ~$59.87
        adapter = XLayerOnchainAdapter(
            {"min_pool_depth_usd": "1000", "min_trade_notional_usd": "10"},
            [{"asset": "TSLA", "symbol": "TSLAx", "address": TSLA, "decimals": 18,
              "pools": [], "trade_scan": True}],
            FakeRPC({"0xdust": dust, "0xreal": real}),
        )
        report = adapter.trade_prints(lookback_blocks=300, max_prints=8)
        self.assertEqual(report["TSLA"]["dust_prints_skipped"], 1)
        self.assertEqual(report["TSLA"]["print_count"], 1)
        self.assertAlmostEqual(float(report["TSLA"]["onchain_ask_usd"]), 365.61, places=1)
        self.assertGreater(float(report["TSLA"]["onchain_ask_usd"]), 1.0)


class QuoterCallTests(unittest.TestCase):
    def test_exact_output_reads_the_first_returned_word(self):
        class RPC:
            def call(self, to, data, block="latest"):
                self.to, self.data = to, data
                return "0x" + f"{1_500_000:064x}" + ("0" * 64) * 3

        rpc = RPC()
        amount = quote_single(
            rpc, "0x" + "ab" * 20, token_in="0x" + "11" * 20, token_out="0x" + "22" * 20,
            amount_raw=10 ** 18, fee=500, exact_out=True)
        self.assertEqual(amount, 1_500_000)
        self.assertTrue(rpc.data.startswith(selector(
            "quoteExactOutputSingle((address,address,uint256,uint24,uint160))")))

    def test_a_revert_is_no_quote(self):
        class RPC:
            def call(self, to, data, block="latest"):
                raise RuntimeError("execution reverted")

        self.assertIsNone(quote_single(
            RPC(), "0x" + "ab" * 20, token_in="0x" + "11" * 20, token_out="0x" + "22" * 20,
            amount_raw=10 ** 18, fee=500, exact_out=False))


if __name__ == "__main__":
    unittest.main()
