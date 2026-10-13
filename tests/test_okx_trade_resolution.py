"""A rejected-looking write must be resolved by its client id, not assumed absent.

Measured 2026-09-16: OKX answered `code=1, "All operations failed"` for an IOC
order that had actually filled. The runner believed "not sent", skipped the spot
leg, and left a naked long. The venue's own order query is the only authority on
what happened.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.execution.okx_account import OKXReadOnlyClient
from arb_monitor.execution.okx_trade import OKXTradeClient, OKXTradeError


def read_client(rows_by_query=None, fail=False):
    def transport(url, headers):
        if fail:
            raise TimeoutError("read unavailable")
        for key, rows in (rows_by_query or {}).items():
            if key in url:
                return {"code": "0", "data": rows}
        return {"code": "0", "data": []}
    return OKXReadOnlyClient("k", "s", "p", simulated=True, transport=transport)


class ResolutionTests(unittest.TestCase):
    def client(self, post_result, read_rows=None, read_fail=False):
        def post(url, headers, body):
            if isinstance(post_result, Exception):
                raise post_result
            return post_result
        return OKXTradeClient(
            read=read_client({"clOrdId=arb1": read_rows} if read_rows is not None else {},
                             fail=read_fail),
            allow_orders=True, transport=post)

    def place(self, client):
        return client.place_ioc(inst_id="XIAOMI-USDT-SWAP", side="sell",
                                size=Decimal("3"), price=Decimal("3.35"),
                                client_order_id="arb1")

    def test_an_api_level_failure_is_resolved_to_the_real_order(self):
        client = self.client({"code": "1", "msg": "All operations failed"},
                             read_rows=[{"ordId": "3926540110082576384",
                                         "state": "filled", "accFillSz": "3"}])
        result = self.place(client)
        self.assertEqual(result["ordId"], "3926540110082576384")
        self.assertTrue(result["resolvedByClientId"])
        self.assertEqual(result["state"], "filled")

    def test_a_per_order_rejection_is_also_resolved(self):
        client = self.client({"code": "0", "data": [
            {"sCode": "51169", "sMsg": "no positions to reduce"}]},
            read_rows=[{"ordId": "1", "state": "canceled"}])
        result = self.place(client)
        self.assertEqual(result["state"], "canceled")

    def test_a_transport_failure_that_reached_the_venue_is_resolved(self):
        client = self.client(TimeoutError("socket closed"),
                             read_rows=[{"ordId": "42", "state": "filled"}])
        result = self.place(client)
        self.assertEqual(result["ordId"], "42")

    def test_when_the_venue_has_no_such_order_the_error_stands(self):
        client = self.client({"code": "1", "msg": "All operations failed"},
                             read_rows=[])
        with self.assertRaises(OKXTradeError):
            self.place(client)

    def test_an_unreadable_query_does_not_become_a_silent_success(self):
        client = self.client({"code": "1", "msg": "All operations failed"},
                             read_fail=True)
        with self.assertRaises(OKXTradeError):
            self.place(client)

    def test_the_client_id_is_never_reused(self):
        client = self.client({"code": "0", "data": [{"ordId": "1", "sCode": "0"}]})
        self.place(client)
        with self.assertRaisesRegex(OKXTradeError, "clordid_already_submitted"):
            self.place(client)


if __name__ == "__main__":
    unittest.main()
