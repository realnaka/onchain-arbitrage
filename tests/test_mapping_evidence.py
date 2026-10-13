from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from arb_monitor.apps.hk_monitor import load_config, mapping_from_token, mapping_status
from arb_monitor.core.evm import decode_uint_fixed, encode_call, keccak256, selector
from arb_monitor.core.hk import Contract
from arb_monitor.core.mapping_evidence import (summarize, verify_token_mapping,
                                               verify_venue_contract)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"
WRAPPER = "0x076cf393e701839fc7a5832d2c68aafa235682ae"
NATIVE = "0xfb4f81f511b40b80996062032260a539e60adfc0"

SPEC = {
    "shares_per_token": "1", "shares_per_perp_unit": "1", "quote_to_usdt": "1",
    "token_symbol": "XIAOx", "token_isin": "CH1580497480",
    "underlying_isin": "KYG9830T1067",
    "native_address": NATIVE, "wrapper_address": WRAPPER,
    "perp_value_currency": "XIAOMI", "verified": True,
}
ISSUER = {
    "symbol": "XIAOx", "isin": "CH1580497480", "underlyingSymbol": "1810",
    "underlying": {"symbol": "1810", "isin": "KYG9830T1067", "listingCountry": "HK"},
}
INSTRUMENT = {"instId": "XIAOMI-USDT-SWAP", "ctVal": "1", "ctValCcy": "XIAOMI",
              "ctMult": "1", "ctType": "linear"}
SUPPLY = "49469801027291989008932"


def word(value: int) -> str:
    return "0x" + f"{value:064x}"


class FakeRPC:
    """Answers exactly the calls the verifier makes, with injectable drift."""

    def __init__(self, *, supply=SUPPLY, reserve=None, asset=NATIVE,
                 multiplier=10 ** 18, rate=None, total_assets=None, missing=()):
        self.supply = int(supply)
        self.reserve = int(reserve if reserve is not None else supply)
        self.asset = asset
        self.multiplier = multiplier
        # A wrapped xStock is an ERC-4626 vault whose rate is the issuer's
        # corporate-action factor; both default to matching unless a test
        # injects drift between them.
        self.rate = int(rate if rate is not None else multiplier)
        self.total_assets = int(total_assets if total_assets is not None else self.reserve)
        self.missing = set(missing)

    def call(self, to: str, data: str, block: str = "latest") -> str:
        sig = data[:10]
        if to.lower() == WRAPPER:
            if sig == selector("totalSupply()") and "supply" not in self.missing:
                return word(self.supply)
            if sig == selector("asset()") and "asset" not in self.missing:
                return "0x" + "0" * 24 + self.asset[2:]
            if sig == selector("totalAssets()") and "total_assets" not in self.missing:
                return word(self.total_assets)
            if (sig == selector("convertToAssets(uint256)") and "rate" not in self.missing):
                return word(self.rate)
        if to.lower() == NATIVE:
            if sig == selector("balanceOf(address)") and "reserve" not in self.missing:
                return word(self.reserve)
            if sig == selector("multiplier()") and "multiplier" not in self.missing:
                return word(self.multiplier)
        raise RuntimeError("execution reverted")


class KeccakTests(unittest.TestCase):
    def test_matches_vectors_already_verified_against_live_contracts(self):
        self.assertEqual(keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        for signature, expected in (
                ("transfer(address,uint256)", "0xa9059cbb"),
                ("balanceOf(address)", "0x70a08231"),
                ("totalSupply()", "0x18160ddd"),
                ("approve(address,uint256)", "0x095ea7b3"),
                ("token0()", "0x0dfe1681"),
                ("slot0()", "0x3850c7bd")):
            self.assertEqual(selector(signature), expected, signature)

    def test_call_encoding_pads_arguments(self):
        encoded = encode_call("balanceOf(address)", WRAPPER)
        self.assertTrue(encoded.startswith("0x70a08231"))
        self.assertEqual(len(encoded), 10 + 64)
        self.assertEqual(encoded[10:], WRAPPER[2:].rjust(64, "0"))
        self.assertEqual(encode_call("multiplier()", )[10:], "")

    def test_fixed_point_decoding(self):
        self.assertEqual(decode_uint_fixed(word(10 ** 18)), Decimal("1"))
        self.assertEqual(decode_uint_fixed(word(1_008 * 10 ** 15)), Decimal("1.008"))


class VerificationTests(unittest.TestCase):
    def verify(self, **kwargs):
        spec = {**SPEC, **kwargs.pop("spec", {})}
        issuer = kwargs.pop("issuer", ISSUER)
        instrument = {**INSTRUMENT, **kwargs.pop("instrument", {})}
        return summarize(verify_token_mapping(spec=spec, issuer_asset=issuer,
                                              instrument=instrument, rpc=FakeRPC(**kwargs)))

    def test_a_fully_backed_one_to_one_mapping_passes(self):
        result = self.verify()
        self.assertTrue(result["ok"], result["failed"])
        self.assertEqual(result["passed"], 6)

    def test_the_vault_must_hold_the_assets_it_minted_shares_against(self):
        # A materially short vault: it claims 99.9% of what it holds.
        result = self.verify(total_assets=int(SUPPLY) * 999 // 1000)
        self.assertFalse(result["ok"])
        self.assertIn("wrapper_fully_backed", result["failed"])

    def test_wei_level_rounding_is_not_under_collateralisation(self):
        # Measured 2026-09-22: a healthy AAPLx vault reported
        # totalAssets() 54,959 wei below its native balanceOf() (6.4e-17
        # relative, two different code paths inside one vault). That is
        # rounding, and refusing it blocked the whole US mapping.
        result = self.verify(total_assets=int(SUPPLY) - 54959)
        self.assertTrue(result["ok"], result["failed"])

    def test_a_vault_whose_rate_ignores_the_multiplier_is_refused(self):
        # The rate and the corporate-action factor have to be the same number:
        # that equality is what makes shares_per_token = multiplier() measured.
        result = self.verify(rate=1_007 * 10 ** 15)
        self.assertFalse(result["ok"])
        self.assertIn("vault_conversion_rate", result["failed"])

    def test_a_split_or_dividend_invalidates_the_configured_ratio(self):
        # A 1.008 factor on a token configured as 1 share per token: the rate
        # moved, so the configured ratio is stale and must fail closed.
        result = self.verify(multiplier=1_008 * 10 ** 15)
        self.assertFalse(result["ok"])
        self.assertIn("share_conversion", result["failed"])

    def test_a_wrapper_worth_more_than_one_share_is_a_pass_not_a_failure(self):
        # Measured 2026-09-22: wAAPLx redeems for 1.003269 native tokens, and a
        # native token is one share. A config that says 1 share per wrapper is
        # wrong; a config that says 1.003269 is right.
        spec = {"shares_per_token": "1.0032690125398187",
                "native_shares_per_unit": "1"}
        result = self.verify(spec=spec, multiplier=1_003_269_012_539_818_700)
        self.assertTrue(result["ok"], result["failed"])
        stale = self.verify(spec={**spec, "shares_per_token": "1"},
                            multiplier=1_003_269_012_539_818_700)
        self.assertIn("share_conversion", stale["failed"])

    def test_a_wrapper_pointing_at_another_token_is_refused(self):
        result = self.verify(asset="0x" + "11" * 20)
        self.assertFalse(result["ok"])
        self.assertIn("wrapper_declared_asset", result["failed"])

    def test_a_contract_worth_a_fraction_of_a_share_is_refused(self):
        result = self.verify(instrument={"ctVal": "0.1"})
        self.assertFalse(result["ok"])
        self.assertIn("perp_contract_value", result["failed"])

    def test_a_mismatched_underlying_isin_is_refused(self):
        issuer = {**ISSUER, "underlying": {"symbol": "1810", "isin": "KY0000000000"}}
        result = self.verify(issuer=issuer)
        self.assertFalse(result["ok"])
        self.assertIn("issuer_registry", result["failed"])

    def test_a_missing_issuer_record_is_a_failure_not_a_pass(self):
        result = self.verify(issuer=None)
        self.assertFalse(result["ok"])
        self.assertIn("issuer_registry", result["failed"])

    def test_unreadable_contract_state_never_counts_as_verified(self):
        result = self.verify(missing=("supply", "reserve", "multiplier", "rate"))
        self.assertFalse(result["ok"])
        self.assertIn("wrapper_fully_backed", result["failed"])
        self.assertIn("vault_conversion_rate", result["failed"])
        self.assertIn("share_conversion", result["failed"])

    def test_an_unreadable_multiplier_still_fails_the_run(self):
        # The rate alone would still produce a share count, but the equality
        # between the vault rate and the issuer's factor is part of the claim.
        result = self.verify(missing=("multiplier",))
        self.assertFalse(result["ok"])
        self.assertIn("vault_conversion_rate", result["failed"])
        self.assertNotIn("share_conversion", result["failed"])


class ConfiguredMappingTests(unittest.TestCase):
    def contract(self, asset="XIAOMI", instrument="XIAOMI-USDT-SWAP"):
        return Contract("okx", instrument, asset, "1", "usd_linear", Decimal("1"),
                        Decimal("1"), Decimal("1"), Decimal("5"), Decimal("0.001"),
                        True, "okx", 1_789_485_625_069)

    def token(self, asset):
        config = load_config(str(CONFIG))
        return next(t for t in config["tokens"] if t["asset"] == asset)

    def test_xiaomi_mapping_is_verified_positive_and_time_bounded(self):
        mapping = mapping_from_token(self.token("XIAOMI"), self.contract())
        self.assertEqual(mapping.shares_per_token, Decimal("1"))
        self.assertEqual(mapping.shares_per_perp_unit, Decimal("1"))
        self.assertTrue(mapping.valid(1_789_485_625_069))
        # An expiry exists precisely so a stale claim stops counting.
        self.assertTrue(mapping.evidence)
        self.assertGreater(mapping.valid_until_ms, 1_789_485_625_069)

    def test_the_claim_expires(self):
        mapping = mapping_from_token(self.token("XIAOMI"), self.contract())
        self.assertFalse(mapping.valid(mapping.valid_until_ms + 1))

    def test_other_assets_are_not_silently_promoted(self):
        # By 2026-09-17 all six HK names are verified (TENCENT and MEITUAN were
        # the last two, see docs/research/mapping-hk-venues-2026-09-17.md).
        # What must never happen is an asset riding along *without its own*
        # evidence, so every mapping has to carry evidence, an expiry and — for
        # the venues that do not publish ctVal — the contract size it was
        # verified against. A US listing stays the control that still has none.
        for asset in ("XIAOMI", "TENCENT", "POPMART", "MEITUAN",
                      "KUAISHOU", "SHEIN"):
            token = self.token(asset)
            self.assertTrue(token.get("mapping_verified"), asset)
            self.assertTrue(token.get("mappings"), asset)
            for key, spec in (token.get("mappings") or {}).items():
                self.assertTrue(spec.get("evidence"), f"{asset} {key}")
                self.assertGreater(int(spec.get("valid_until_ms") or 0), 0,
                                   f"{asset} {key}")
                venue = key.split(":", 1)[0]
                if venue != "okx":
                    # OKX publishes ctVal/ctValCcy/ctMult; the others do not, so
                    # the size we rely on has to be written down to be checked.
                    self.assertIn("perp_contract_multiplier", spec, f"{asset} {key}")
        aapl = next(t for t in load_config(str(CONFIG))["tokens"]
                    if t["asset"] == "AAPL")
        # Operator signed the US depth-list mappings on 2026-09-23.
        self.assertTrue(aapl.get("mapping_verified"))
        spec = aapl["mappings"]["okx:AAPL-USDT-SWAP"]
        self.assertTrue(spec.get("verified"))
        self.assertGreater(int(spec.get("valid_until_ms") or 0), 0)

    def test_the_verified_assets_are_exactly_the_ones_with_a_reviewed_venue(self):
        verified = {a for a in ("XIAOMI", "TENCENT", "POPMART", "MEITUAN",
                                "KUAISHOU", "SHEIN")
                    if self.token(a).get("mapping_verified")}
        # "Reviewed venue" is the old name for this set; KUAISHOU is verified
        # against three venues (gate/bybit/bitget) even though none of them has an
        # execution adapter yet — the mapping answers "is 1 token 1 share", not
        # "can we trade here".
        self.assertEqual(verified, {"XIAOMI", "TENCENT", "POPMART", "MEITUAN",
                                    "KUAISHOU", "SHEIN"})

    def test_status_is_reported_per_contract_and_expires_on_its_own(self):
        token = self.token("XIAOMI")
        status = mapping_status(token, 1_789_485_625_069)
        self.assertTrue(status["verified"])
        entry = status["entries"][0]
        self.assertEqual(entry["contract"], "okx:XIAOMI-USDT-SWAP")
        self.assertFalse(entry["expired"])
        # Expiry is per contract. XIAOMI is now verified against four venues, so
        # the OKX row ageing out must not withdraw the gate/bybit/bitget rows
        # that were checked later — and the asset only stops being verified once
        # every row has aged out.
        after = mapping_status(token, entry["valid_until_ms"] + 1)
        self.assertTrue(after["entries"][0]["expired"])
        self.assertTrue(after["verified"])
        self.assertTrue(any(not e["expired"] for e in after["entries"]))
        last = max(int(e["valid_until_ms"]) for e in after["entries"])
        everything_expired = mapping_status(token, last + 1)
        self.assertFalse(everything_expired["verified"])
        self.assertTrue(all(e["expired"] for e in everything_expired["entries"]))

    def test_an_asset_without_mappings_reports_unverified(self):
        # A listing with no reviewed mapping block at all. This asserts the
        # contract of ``mapping_status`` rather than the state of one asset: a
        # token without a mapping block reports no entries and no verification.
        token = {**self.token("AAPL"), "mappings": {}}
        status = mapping_status(token, 1_789_485_625_069)
        self.assertFalse(status["verified"])
        self.assertEqual(status["entries"], [])


class VenueContractTests(unittest.TestCase):
    """Layer 4 for gate / bybit / bitget, which do not publish ctVal.

    These venues describe a contract differently, and the differences are not
    cosmetic: measured 2026-09-17 one contract is 0.1 share on gate for
    TENCENT/POPMART/MEITUAN but 1 share for XIAOMI/SHEIN/KUAISHOU, 1 share on
    bybit, and 0.01 share on bitget. Assuming 1:1 anywhere would mis-size the
    hedge, so the venue's own field is compared against the config and a change
    on either side fails the check.
    """

    def spec(self, **over):
        base = {"token_symbol": "TCENTx", "perp_value_currency": "TENCENT",
                "perp_contract_multiplier": "0.1", "shares_per_token": "1",
                "shares_per_perp_unit": "1",
                "issuer_underlying_symbol": "700"}
        base.update(over)
        return base

    def gate(self, **over):
        row = {"venue": "gate", "instrument_id": "TENCENT_USDT",
               "multiplier": "0.1", "active": True,
               "raw": {"name": "TENCENT_USDT", "type": "direct"}}
        row.update(over)
        return row

    def bybit(self, ticker="00700", **over):
        row = {"venue": "bybit", "instrument_id": "TENCENTUSDT",
               "multiplier": "1", "active": True,
               "raw": {"symbolType": "stock", "marketRegion": "HK",
                       "underlyingTicker": ticker, "fullName": "Tencent Holdings Ltd"}}
        row.update(over)
        return row

    def bitget(self, **over):
        row = {"venue": "bitget", "instrument_id": "TENCENTUSDT",
               "multiplier": "0.01", "active": True,
               "raw": {"baseCoin": "TENCENT", "quoteCoin": "USDT", "isRwa": "YES"}}
        row.update(over)
        return row

    def names(self, checks):
        return {check.name: check.ok for check in checks}

    def test_gate_fractional_contract_size_passes_when_the_config_agrees(self):
        checks = verify_venue_contract(spec=self.spec(), venue_contract=self.gate())
        self.assertTrue(all(check.ok for check in checks), self.names(checks))

    def test_a_venue_changing_the_contract_size_fails_the_check(self):
        # Gate turning 0.1 into 1 would make the hedge 10x too small.
        checks = verify_venue_contract(spec=self.spec(),
                                       venue_contract=self.gate(multiplier="1"))
        self.assertFalse(self.names(checks)["perp_contract_size"])

    def test_a_config_that_forgets_the_size_is_not_a_silent_pass(self):
        spec = self.spec(perp_contract_multiplier="1")
        checks = verify_venue_contract(spec=spec, venue_contract=self.gate())
        self.assertFalse(self.names(checks)["perp_contract_size"])

    def test_bybit_accepts_a_zero_padded_hk_code(self):
        checks = verify_venue_contract(spec=self.spec(), venue_contract=self.bybit())
        self.assertTrue(self.names(checks)["perp_venue_identity"])

    def test_bybit_accepts_a_ticker_string_where_shein_has_one(self):
        # Bybit writes underlyingTicker "SHEIN" for SHEIN and "01024" for
        # KUAISHOU; both spellings must be accepted, and only those.
        spec = self.spec(perp_value_currency="SHEIN", token_symbol="SHEINx",
                         perp_contract_multiplier="1", issuer_underlying_symbol="625")
        checks = verify_venue_contract(spec=spec, venue_contract=self.bybit("SHEIN"))
        self.assertTrue(self.names(checks)["perp_venue_identity"])

    def test_bybit_pointing_at_another_stock_is_refused(self):
        checks = verify_venue_contract(spec=self.spec(),
                                       venue_contract=self.bybit(ticker="09988"))
        self.assertFalse(self.names(checks)["perp_venue_identity"])

    def test_a_venue_no_longer_calling_it_a_hk_stock_is_refused(self):
        contract = self.bybit()
        contract["raw"] = {**contract["raw"], "symbolType": "crypto"}
        checks = verify_venue_contract(spec=self.spec(), venue_contract=contract)
        self.assertFalse(self.names(checks)["perp_venue_identity"])

    def test_bitget_needs_both_the_coin_and_the_rwa_flag(self):
        good = verify_venue_contract(spec=self.spec(), venue_contract=self.bitget())
        self.assertTrue(self.names(good)["perp_venue_identity"])
        wrong_coin = self.bitget()
        wrong_coin["raw"] = {**wrong_coin["raw"], "baseCoin": "MEITUAN"}
        self.assertFalse(self.names(verify_venue_contract(
            spec=self.spec(), venue_contract=wrong_coin))["perp_venue_identity"])
        not_rwa = self.bitget()
        not_rwa["raw"] = {**not_rwa["raw"], "isRwa": "NO"}
        self.assertFalse(self.names(verify_venue_contract(
            spec=self.spec(), venue_contract=not_rwa))["perp_venue_identity"])

    def binance(self, base="TENCENT", **over):
        row = {"venue": "binance", "instrument_id": f"{base}USDT",
               "multiplier": "1", "active": True,
               "raw": {"underlyingType": "HK_EQUITY", "baseAsset": base,
                       "quoteAsset": "USDT", "marginAsset": "USDT"}}
        row.update(over)
        return row

    def test_binance_name_ticker_is_a_usd_contract(self):
        checks = verify_venue_contract(spec=self.spec(perp_contract_multiplier="1"),
                                       venue_contract=self.binance())
        self.assertTrue(self.names(checks)["perp_venue_identity"])

    def test_binance_hk_code_ticker_is_refused_as_a_different_ruler(self):
        # HK0700USDT trades at 428.09 against TENCENTUSDT's 54.61: the ratio is
        # USD/HKD, so it is the same stock on a different ruler. Mapping it to a
        # USD-priced token would manufacture a ~684% "basis" out of the FX rate.
        checks = verify_venue_contract(spec=self.spec(perp_contract_multiplier="1"),
                                       venue_contract=self.binance(base="HK0700"))
        self.assertFalse(self.names(checks)["perp_venue_identity"])

    def test_an_inactive_contract_is_never_verified(self):
        checks = verify_venue_contract(spec=self.spec(),
                                       venue_contract=self.gate(active=False))
        self.assertFalse(self.names(checks)["perp_contract_active"])

    def test_okx_shaped_checks_do_not_apply_to_a_venue_contract(self):
        # Passing a venue row must replace the OKX ctVal check, not double it:
        # otherwise every gate/bybit/bitget mapping would fail on an empty spec.
        checks = verify_token_mapping(spec=self.spec(), issuer_asset=None,
                                      instrument={}, rpc=None,
                                      venue_contract=self.gate())
        self.assertNotIn("perp_contract_value", self.names(checks))
        self.assertIn("perp_contract_size", self.names(checks))


if __name__ == "__main__":
    unittest.main()
