"""Hermetic tests for the balance snapshot the board renders.

No network: every venue answer is injected. The invariant that matters is
「读不到 ≠ 0」 -- a failed read must never look like an empty wallet.
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import balances


class _Http:
    """Answers by RPC method; a method listed in ``fail`` raises."""

    def __init__(self, answers, *, fail=()):
        self.answers = answers
        self.fail = set(fail)
        self.calls: list[tuple[str, str]] = []

    def post_json(self, url, payload, **kwargs):
        if isinstance(payload, list):
            answers = []
            for item in payload:
                try:
                    _status, body = self.post_json(url, item)
                except RuntimeError as exc:
                    answers.append({"jsonrpc": "2.0", "id": item.get("id"),
                                    "error": {"message": str(exc)}})
                    continue
                answers.append(dict(body, id=item.get("id")))
            return 200, answers
        method = payload["method"]
        self.calls.append((url, method))
        if method in self.fail:
            raise RuntimeError("boom")
        return 200, self.answers.get(method, {})


class RowTest(unittest.TestCase):
    def test_an_unreadable_balance_is_not_a_zero(self):
        item = balances.row("USDG", None, error="timeout")
        self.assertIsNone(item["amount"])
        self.assertFalse(item["readable"])
        self.assertEqual(item["error"], "timeout")

    def test_a_read_balance_keeps_its_exact_text(self):
        item = balances.row("USDG", D("53.471333"), raw=53471333)
        self.assertEqual(item["amount"], "53.471333")
        self.assertTrue(item["readable"])
        self.assertEqual(item["raw"], "53471333")


class EvmTest(unittest.TestCase):
    def test_erc20_amount_is_scaled_by_its_own_decimals(self):
        http = _Http({
            "eth_getBalance": {"result": hex(2_188_106_860_838_530)},
            "eth_call": {"result": "0x" + hex(53_471_333)[2:].rjust(64, "0")},
        })
        group = balances.collect_evm(http, assets=(("USDG", "0xabc", 6),))
        rows = {item["asset"]: item for item in group["rows"]}
        self.assertEqual(rows["USDG"]["amount"], "53.471333")
        self.assertEqual(rows["OKB"]["amount"], "0.00218810686083853")

    def test_a_failed_evm_read_is_a_null_not_a_zero(self):
        http = _Http({}, fail=("eth_call",))
        group = balances.collect_evm(http, assets=(("USDG", "0xabc", 6),))
        usdg = [item for item in group["rows"] if item["asset"] == "USDG"][0]
        self.assertIsNone(usdg["amount"])
        self.assertIn("boom", usdg["error"])

    def test_an_empty_eth_call_result_is_unreadable_too(self):
        http = _Http({"eth_call": {"result": "0x"}})
        group = balances.collect_evm(http, assets=(("USDG", "0xabc", 6),))
        usdg = [item for item in group["rows"] if item["asset"] == "USDG"][0]
        self.assertIsNone(usdg["amount"])
        self.assertEqual(usdg["error"], "empty eth_call result")


class SolanaTest(unittest.TestCase):
    def test_sol_and_both_stables_are_read(self):
        http = _Http({
            "getBalance": {"result": {"value": 11_789_064}},
            "getTokenAccountsByOwner": {"result": {"value": [
                {"account": {"data": {"parsed": {"info": {
                    "mint": balances.SOLANA_QUOTES["USDT"],
                    "tokenAmount": {"amount": "44540500",
                                    "uiAmountString": "44.5405"}}}}}}]}},
        })
        group = balances.collect_solana(http, symbols={})
        rows = {item["asset"]: item for item in group["rows"]}
        self.assertEqual(rows["SOL"]["amount"], "0.011789064")
        self.assertEqual(rows["USDT"]["amount"], "44.5405")

    def test_an_unwrapped_answer_is_flagged_not_read_as_zero(self):
        http = _Http({"getBalance": {"result": {"value": 0}},
                      "getTokenAccountsByOwner": {"value": []}})
        group = balances.collect_solana(http, symbols={})
        usdc = [item for item in group["rows"] if item["asset"] == "USDC"][0]
        self.assertIsNone(usdc["amount"])
        self.assertEqual(usdc["error"], "unwrapped rpc answer")

    def test_a_twenty_two_decimal_wrapped_token_keeps_its_raw(self):
        """Token-2022 scaled-UI: raw != UI x 1e8, so both are kept."""
        http = _Http({
            "getBalance": {"result": {"value": 11_789_064}},
            "getTokenAccountsByOwner": {"result": {"value": [
                {"account": {"data": {"parsed": {"info": {
                    "mint": "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W",
                    "tokenAmount": {"amount": "650392",
                                    "uiAmountString": "0.00650392"}}}}}}]}},
        })
        group = balances.collect_solana(http, symbols={"XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W": "SPYx"})
        spyx = [item for item in group["rows"] if item["asset"] == "SPYx"][0]
        self.assertEqual(spyx["amount"], "0.00650392")
        self.assertEqual(spyx["raw"], "650392")


class KrakenTest(unittest.TestCase):
    class _Client:
        def __init__(self, answer=None, *, blow=False):
            self.answer = answer or {}
            self.blow = blow

        def balance(self):
            if self.blow:
                raise RuntimeError("EAPI:Invalid key")
            return self.answer

    def test_cash_and_tokenized_rows(self):
        client = self._Client({"ZUSD": D("0.0018"), "USDG": D("39.72613"),
                               "SPYx.T": D("0.011")})
        group = balances.collect_kraken(client)
        rows = {item["asset"]: item for item in group["rows"]}
        self.assertEqual(rows["USDG"]["amount"], "39.72613")
        self.assertEqual(rows["SPYx.T"]["amount"], "0.011")
        self.assertEqual(rows["USDC"]["amount"], "0")

    def test_a_failed_kraken_read_marks_every_asset_unreadable(self):
        group = balances.collect_kraken(self._Client(blow=True))
        for item in group["rows"]:
            self.assertIsNone(item["amount"], item)
            self.assertIn("RuntimeError", item["error"])


class RefreshTest(unittest.TestCase):
    def test_refresh_writes_the_snapshot_and_never_raises(self):
        http = _Http({}, fail=("eth_call", "getBalance", "getTokenAccountsByOwner"))
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = balances.refresh(
                data_dir=tmp, http=http,
                kraken_client=KrakenTest._Client(blow=True),
                # Hermetic: the wallet service is a subprocess with a network
                # call behind it. Off here, on in the resident round.
                use_indexer=False,
                book=Path(tmp) / "missing.sqlite")
            self.assertTrue((Path(tmp) / "xstocks_v2_balances.json").exists())
            self.assertTrue(snapshot["groups"])
            unreadable = [item for group in snapshot["groups"]
                          for item in group["rows"] if item["amount"] is None]
            self.assertTrue(unreadable, "a failed read must stay unreadable")

    def test_missing_book_files_do_not_break_the_mapping(self):
        self.assertEqual(balances.solana_symbols("/nonexistent.sqlite"), {})
        self.assertEqual(balances.xlayer_forms("/nonexistent.sqlite"), ())


class WalletServiceTest(unittest.TestCase):
    """The second source: the wallet service, which *discovers* what we hold.

    2026-10-09, operator: 「账户余额（按链 / 账户）这里面没有 eth 和其他链」.
    The on-chain readers can only answer about addresses somebody wrote down, so
    the SPCXx that arrived on BSC and the MARSCAT on X Layer were invisible.
    """

    class _Runner:
        """``onchainos wallet balance --chain X`` without a subprocess."""

        def __init__(self, answers):
            self.answers = answers
            self.asked: list[str] = []

        def __call__(self, argv):
            slug = list(argv)[list(argv).index("--chain") + 1]
            self.asked.append(slug)
            answer = self.answers.get(slug)
            if isinstance(answer, Exception):
                raise answer
            if answer is None:
                raise AssertionError(f"unexpected chain: {slug}")
            return {"ok": True, "data": {"details": [{"tokenAssets": answer}],
                                         "totalValueUsd": "0"}}

    @staticmethod
    def _asset(symbol, balance, *, raw, decimals="18", address="", usd="0"):
        return {"symbol": symbol, "balance": balance, "rawBalance": raw,
                "decimal": decimals, "tokenAddress": address,
                "usdValue": usd}

    def test_the_default_chain_list_covers_eth_and_the_other_landing_chains(self):
        keys = [chain for _slug, chain, _label, _account in balances.CLI_CHAINS]
        for wanted in ("x-layer", "solana", "eth", "bsc", "arbitrum",
                       "optimism", "base", "ink", "hyperevm", "mantle", "monad"):
            self.assertIn(wanted, keys)
        # The card order is the list order: the chains we trade hardest first.
        self.assertEqual(keys[:2], ["x-layer", "solana"])

    def test_a_chain_reports_amount_raw_and_the_services_own_usd(self):
        runner = self._Runner({"eth": [
            self._asset("USDC", "109.6774", raw="109677400", decimals="6",
                        address="0xA0b8", usd="109.6774"),
            self._asset("ETH", "0.001310896406233694",
                        raw="1310896406233694", usd="3.28272056")]})
        groups = balances.collect_chains(runner, (("eth", "eth", "Ethereum", "0xabc"),))
        self.assertEqual(len(groups), 1)
        card = groups[0]
        self.assertEqual(card["label"], "Ethereum")
        self.assertEqual(card["usd_total"], "112.96012056")
        rows = {item["asset"]: item for item in card["rows"]}
        self.assertEqual(rows["USDC"]["amount"], "109.6774")
        self.assertEqual(rows["USDC"]["raw"], "109677400")
        self.assertEqual(rows["USDC"]["address"], "0xa0b8")
        self.assertEqual(rows["ETH"]["usd"], "3.28272056")

    def test_a_chain_the_service_cannot_reach_is_unreadable_not_zero(self):
        runner = self._Runner({"bnb": RuntimeError("all_routes_failed:timeout")})
        groups = balances.collect_chains(
            runner, (("bnb", "bsc", "BNB Chain", "0xabc"),))
        card = groups[0]
        self.assertIsNone(card["usd_total"])
        self.assertTrue(all(item["amount"] is None for item in card["rows"]))
        self.assertIn("RuntimeError", card["rows"][0]["error"])
        self.assertIn("读不到", card["note"])

    def test_an_empty_answer_is_a_labelled_zero_not_a_missing_card(self):
        runner = self._Runner({"arb_eth": []})
        card = balances.collect_chains(
            runner, (("arb_eth", "arbitrum", "Arbitrum", "0xabc"),))[0]
        self.assertEqual(card["rows"], [])
        self.assertEqual(card["usd_total"], "0")
        self.assertIn("没有余额", card["note"])

    def test_the_chain_read_keeps_the_amount_and_the_service_adds_the_rest(self):
        base = balances.group("eth", "0xabc", [
            balances.row("USDC", "109.6774", raw=109677400, address="0xA0b8",
                         note="现金")], label="Ethereum")
        extra = balances.group("eth", "0xabc", [
            balances.row("USDC", "109.677400000000000001", raw=109677400,
                         address="0xa0b8", note="indexer", usd="109.6774"),
            balances.row("STRCx", "1", raw=10 ** 18, address="0x9C5C",
                         note="indexer", usd="101.5")],
            label="Ethereum", usd_total="211.1774", source="indexer")
        merged = balances.merge_group(base, extra)
        rows = {item["asset"]: item for item in merged["rows"]}
        self.assertEqual(len(merged["rows"]), 2)
        self.assertEqual(rows["USDC"]["amount"], "109.6774")   # chain wins
        self.assertEqual(rows["USDC"]["note"], "现金")          # no disagreement
        self.assertEqual(rows["USDC"]["usd"], "109.6774")      # price added
        self.assertEqual(rows["STRCx"]["amount"], "1")         # discovery kept
        self.assertEqual(merged["usd_total"], "211.1774")

    def test_a_disagreement_is_printed_not_averaged(self):
        base = balances.group("bsc", "0xabc", [
            balances.row("USDT", "2.469576542914729327", address="0x55d3")])
        extra = balances.group("bsc", "0xabc", [
            balances.row("USDT", "9.99", address="0x55d3", note="indexer",
                         usd="9.98")], usd_total="9.98", source="indexer")
        merged = balances.merge_group(base, extra)
        row = merged["rows"][0]
        self.assertEqual(row["amount"], "2.469576542914729327")
        self.assertIn("⚠ indexer 读 9.99", row["note"])

    def test_rounding_and_dust_are_not_disagreements(self):
        """Solana's RPC rounds its UI string; 1 wei of dust is not money.

        Measured 2026-10-09: QQQx read 0.00571966 (chain) against
        0.005719669528929697 (service), and three X Layer rows held exactly
        1e-18 while the service printed 1.0017e-18.
        """
        base = balances.group("solana", "0xabc", [
            balances.row("QQQx", "0.00571966", address="Xs8S1"),
            balances.row("NVDAx 原生", "0.000000000000000001", address="0xaaa"),
        ])
        extra = balances.group("solana", "0xabc", [
            balances.row("QQQx", "0.005719669528929697", address="Xs8S1",
                         note="indexer", usd="4.31"),
            balances.row("NVDAx", "0.000000000000000001001701196801074",
                         address="0xaaa", note="indexer", usd="0"),
        ], usd_total="4.31", source="indexer")
        merged = balances.merge_group(base, extra)
        for row in merged["rows"]:
            self.assertNotIn("⚠", str(row.get("note")), row)
        # The service's price is still attached to the chain's row.
        self.assertEqual(merged["rows"][0]["usd"], "4.31")

    def test_collect_adds_a_card_per_chain_and_keeps_the_order(self):
        http = _Http({
            "eth_getBalance": {"result": hex(2_188_106_860_838_530)},
            "eth_call": {"result": "0x" + hex(53_471_333)[2:].rjust(64, "0")},
            "getBalance": {"result": {"value": 11_789_064}},
            "getTokenAccountsByOwner": {"result": {"value": []}},
        })
        runner = self._Runner({
            "okb": [self._asset("USDG", "43.797561", raw="43797561",
                                decimals="6",
                                address="0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",
                                usd="43.797561"),
                    self._asset("MARSCAT", "10", raw=10 ** 19, usd="0.00002")],
            "sol": [self._asset("SOL", "0.011789064", raw=11789064,
                                usd="1.28")],
            "eth": [self._asset("USDC", "109.6774", raw="109677400",
                                decimals="6", address="0xa0b8",
                                usd="109.6774")],
        })
        chains = (("okb", "x-layer", "X Layer", "0xabc"),
                  ("sol", "solana", "Solana", "0xabc"),
                  ("eth", "eth", "Ethereum", "0xabc"))
        snapshot = balances.collect(http=http, wallet_runner=runner,
                                    cli_chains=chains,
                                    book="/nonexistent.sqlite")
        self.assertEqual([g["chain"] for g in snapshot["groups"]],
                         ["x-layer", "solana", "eth"])
        xlayer = snapshot["groups"][0]
        rows = {item["asset"]: item for item in xlayer["rows"]}
        # The chain read stays; the token nobody wrote down shows up too.
        self.assertEqual(rows["USDG"]["amount"], "53.471333")
        self.assertEqual(rows["MARSCAT"]["amount"], "10")
        self.assertEqual(xlayer["usd_total"], "43.797581")
        self.assertIn("eth", snapshot["scope"])

    def test_without_the_service_the_two_on_chain_cards_are_unchanged(self):
        http = _Http({
            "eth_getBalance": {"result": hex(2_188_106_860_838_530)},
            "getBalance": {"result": {"value": 11_789_064}},
            "getTokenAccountsByOwner": {"result": {"value": []}},
        })
        snapshot = balances.collect(http=http, use_indexer=False,
                                    book="/nonexistent.sqlite")
        self.assertEqual([g["chain"] for g in snapshot["groups"]],
                         ["x-layer", "solana"])
        for card in snapshot["groups"]:
            self.assertIsNone(card["usd_total"])
            self.assertNotEqual(card.get("source"), "indexer")


if __name__ == "__main__":
    unittest.main()
