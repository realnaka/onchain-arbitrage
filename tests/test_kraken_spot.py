"""Hermetic tests for the Kraken spot client. No network, no credentials."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import unittest
import urllib.parse
from decimal import Decimal

from arb_monitor.venues import kraken_spot as ks


class FakeTransport:
    """Records the request and replays a canned Kraken body."""

    def __init__(self, body):
        self.body = body
        self.requests: list[dict] = []

    def __call__(self, path, data, headers, timeout):
        self.requests.append({"path": path, "data": data, "headers": headers})
        return self.body


class SignatureTests(unittest.TestCase):
    def test_message_is_path_plus_sha256_of_nonce_then_postdata(self):
        path = "/0/private/Balance"
        nonce = "1791172056434414080"
        postdata = urllib.parse.urlencode({"nonce": nonce, "asset": "MRNAx"})
        expected = path.encode() + hashlib.sha256((nonce + postdata).encode()).digest()
        self.assertEqual(ks.signature_message(path, nonce, postdata), expected)

    def test_sign_matches_manual_hmac_sha512(self):
        secret = base64.b64encode(b"secret-bytes-for-tests").decode()
        path, nonce, postdata = "/0/private/Balance", "1700000000000000001", "nonce=x"
        message = path.encode() + hashlib.sha256((nonce + postdata).encode()).digest()
        expected = base64.b64encode(
            hmac.new(base64.b64decode(secret), message, hashlib.sha512).digest()).decode()
        self.assertEqual(ks.sign(secret, path, nonce, postdata), expected)

    def test_message_changes_with_nonce(self):
        a = ks.signature_message("/0/private/Balance", "1", "nonce=1")
        b = ks.signature_message("/0/private/Balance", "2", "nonce=2")
        self.assertNotEqual(a, b)


class NonceTests(unittest.TestCase):
    def test_nanosecond_clock_is_used(self):
        fixed = 1_791_172_056_434_414_080
        nonce = ks.Nonce(clock=lambda: fixed)
        value = nonce.next()
        self.assertEqual(value, str(fixed))
        self.assertGreater(len(value), 15)  # nanoseconds, not ms/us

    def test_values_are_strictly_increasing_even_when_clock_stalls(self):
        clock = iter([5, 5, 4, 9]).__next__
        nonce = ks.Nonce(clock=clock)
        values = [int(nonce.next()) for _ in range(4)]
        self.assertEqual(values, [5, 6, 7, 9])
        self.assertTrue(all(b > a for a, b in zip(values, values[1:])))

    def test_current_time_is_nanosecond_scale(self):
        value = int(ks.Nonce().next())
        self.assertGreater(value, time.time_ns() - 60 * 10**9)


class PairSpellingTests(unittest.TestCase):
    def test_tokenized_pair_keeps_the_lowercase_x(self):
        self.assertEqual(ks.tokenized_pair("MRNAx"), "MRNAx/USD")
        self.assertEqual(ks.tokenized_pair("SPYx", "USD"), "SPYx/USD")

    def test_private_call_never_sends_the_bare_uppercase_pair(self):
        transport = FakeTransport({"error": [], "result": {"descr": {"order": "ok"}}})
        client = ks.KrakenSpotClient("key", base64.b64encode(b"secret").decode(),
                                     transport=transport)
        client.validate_order("MRNAx", "0.08", price="195")
        sent = transport.requests[0]["data"]
        self.assertIn("pair=MRNAx%2FUSD", sent)
        self.assertNotIn("MRNAXUSD", sent)
        self.assertIn("asset_class=tokenized_asset", sent)


class PrivateCallTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport({"error": [], "result": {}})
        self.client = ks.KrakenSpotClient(
            "the-key", base64.b64encode(b"the-secret").decode(), transport=self.transport)

    def test_request_carries_nonce_first_and_a_signature(self):
        self.client.balance()
        request = self.transport.requests[0]
        self.assertEqual(request["path"], "/0/private/Balance")
        self.assertTrue(request["data"].startswith("nonce="))
        self.assertEqual(request["headers"]["API-Key"], "the-key")
        self.assertTrue(request["headers"]["API-Sign"])
        nonce = request["data"].split("&", 1)[0].split("=", 1)[1]
        expected = ks.sign(base64.b64encode(b"the-secret").decode(),
                           "/0/private/Balance", nonce, request["data"])
        self.assertEqual(request["headers"]["API-Sign"], expected)

    def test_kraken_error_list_raises(self):
        failing = ks.KrakenSpotClient(
            "k", base64.b64encode(b"s").decode(),
            transport=lambda *a: {"error": ["EAPI:Invalid nonce"], "result": None})
        with self.assertRaises(ks.KrakenError) as ctx:
            failing.balance()
        self.assertIn("EAPI:Invalid nonce", ctx.exception.errors)

    def test_missing_credentials_never_hit_the_wire(self):
        client = ks.KrakenSpotClient(transport=self.transport)
        with self.assertRaises(ks.KrakenError):
            client.balance()
        self.assertEqual(self.transport.requests, [])


class BalanceTests(unittest.TestCase):
    def test_balance_is_decimal(self):
        body = {"error": [], "result": {"ZUSD": "0.0090", "MRNAx.T": "0.540000",
                                        "SOL": "0.0000000000"}}
        client = ks.KrakenSpotClient("k", base64.b64encode(b"s").decode(),
                                     transport=FakeTransport(body))
        balance = client.balance()
        self.assertEqual(balance["MRNAx.T"], Decimal("0.540000"))
        self.assertEqual(balance["ZUSD"], Decimal("0.0090"))
        self.assertIsInstance(balance["ZUSD"], Decimal)


class OrderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport({"error": [], "result": {"txid": ["OID1"]}})
        self.client = ks.KrakenSpotClient("k", base64.b64encode(b"s").decode(),
                                          transport=self.transport)

    def test_order_refuses_without_live_confirmation(self):
        with self.assertRaises(ks.KrakenError) as ctx:
            self.client.order("MRNAx", "0.08", price="195")
        self.assertIn("missing live confirmation", "; ".join(ctx.exception.errors))
        self.assertEqual(self.transport.requests, [])

    def test_order_sends_when_confirmed(self):
        self.client.order("MRNAx", "0.08", price="195", confirm="live")
        sent = self.transport.requests[0]["data"]
        self.assertIn("pair=MRNAx%2FUSD", sent)
        self.assertNotIn("validate=", sent)

    def test_validate_order_never_places(self):
        self.client.validate_order("MRNAx", "0.08", price="195")
        sent = self.transport.requests[0]["data"]
        self.assertIn("validate=true", sent)

    def test_validate_order_can_request_post_only(self):
        self.client.validate_order("MRNAx", "0.08", price="195", oflags="post_only")
        self.assertIn("oflags=post_only", self.transport.requests[0]["data"])

    def test_plain_pair_omits_asset_class(self):
        self.client.order("USDGUSD", "102.7", price="1.0000", tokenized=False,
                          confirm="live")
        sent = self.transport.requests[0]["data"]
        self.assertIn("pair=USDGUSD", sent)
        self.assertNotIn("asset_class", sent)


if __name__ == "__main__":
    unittest.main()
